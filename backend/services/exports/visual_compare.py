"""Raster evidence for human export review; metrics are not an uncalibrated pass gate."""
from io import BytesIO

from services.scene.telemetry import timed_stage

import fitz
from PIL import Image, ImageChops, ImageDraw, ImageStat
from flask import current_app

from services.scene.render_jobs import run_render_job
from .scene_pdf import scene_html
from .rendered_text import verify_rendered_pptx


def _pdf_pages(payload):
    with fitz.open(stream=payload, filetype='pdf') as document:
        return [page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False).tobytes('png')
                for page in document]


@timed_stage('visual_compare')
def compare_export(payload, fmt, snapshot, revisions, assets, text_layout=None):
    """Return per-page numeric evidence and bounded contact sheets.

    No automatic visual acceptance threshold exists until a real sample set is
    calibrated. Every compared export therefore requires explicit review.
    """
    if not current_app.config.get('SCENE_RENDER_JOB_ROOT'):
        return {'status': 'not_run', 'reason': 'isolated_renderer_not_configured', 'pages': []}, []
    count = len(snapshot.manifest_json['pages'])
    expected, source_engine = run_render_job('html_preview',
        scene_html(snapshot, revisions, assets).encode('utf-8'), expected_count=count)
    rendered_text_layout = None
    if fmt == 'pdf':
        actual = _pdf_pages(payload)
        target_engine = {'kind': 'pymupdf', 'engine_version': fitz.VersionBind}
    else:
        target_pdf, target_engine = run_render_job('pptx_pdf', payload)
        rendered_text_layout = verify_rendered_pptx(target_pdf, snapshot, revisions, text_layout)
        actual = _pdf_pages(target_pdf)
    if len(expected) != count or len(actual) != count:
        raise ValueError('visual comparison page count mismatch')
    reports, contact_sheets = [], []
    for index, (source_bytes, output_bytes) in enumerate(zip(expected, actual)):
        with Image.open(BytesIO(source_bytes)) as source_image, Image.open(BytesIO(output_bytes)) as output_image:
            source = source_image.convert('RGB')
            output = output_image.convert('RGB')
        size_mismatch = source.size != output.size
        aligned = output.resize(source.size, Image.Resampling.LANCZOS) if size_mismatch else output
        diff = ImageChops.difference(source, aligned)
        stat = ImageStat.Stat(diff)
        changed = diff.convert('L').point(lambda value: 255 if value > 32 else 0)
        changed_fraction = changed.histogram()[255] / (source.width * source.height)
        # Readable visual evidence is kept separately from the immutable export.
        width = min(640, source.width)
        height = max(1, round(source.height * width / source.width))
        sheet = Image.new('RGB', (width * 3, height + 28), 'white')
        draw = ImageDraw.Draw(sheet)
        for offset, label, picture in ((0, 'Scene preview', source),
                                       (width, 'Export render', output),
                                       (2 * width, 'Absolute difference', diff)):
            draw.text((offset + 6, 7), label, fill='black')
            sheet.paste(picture.resize((width, height), Image.Resampling.LANCZOS), (offset, 28))
        stream = BytesIO()
        sheet.save(stream, format='PNG', optimize=True)
        contact_sheets.append(stream.getvalue())
        reports.append({'page_id': snapshot.manifest_json['pages'][index]['page_id'],
                        'source_px': list(source.size), 'export_px': list(output.size),
                        'size_mismatch': size_mismatch,
                        'mean_abs_rgb': round(sum(stat.mean) / 3, 3),
                        'changed_fraction_luma_gt_32': round(changed_fraction, 6)})
    return {'status': 'needs_review', 'comparison': 'scene_browser_vs_' +
            ('pdf_raster' if fmt == 'pdf' else 'libreoffice_pptx'),
            'source_engine': source_engine, 'target_engine': target_engine,
            'rendered_text_layout': rendered_text_layout,
            'raster_scale_px_per_pt': 1.5, 'pages': reports,
            'note': 'Metrics are descriptive; no calibrated automatic pass threshold.'}, contact_sheets
