"""HTML/Chromium PDF with real text, never a slide screenshot."""
from services.scene.telemetry import timed_stage

import base64
import html
import json
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path

import fitz
import pypdfium2 as pdfium

from services.exports.scene_pptx import _picture_bytes
from services.exports.report import page_inventory
from services.scene.layout_contract import (LAYOUT_CACHE, validate_identity, validate_layout,
    scene_hashes, font_fingerprint, engine_fingerprint)

ROOT = Path(__file__).resolve().parents[3]
FONT = ROOT / 'backend' / 'fonts' / 'NotoSansSC-Regular.ttf'
RENDERER = ROOT / 'renderer' / 'render_pdf.mjs'
LAYOUT_RENDERER = ROOT / 'renderer' / 'render_text_layout.mjs'


def _image(asset, element):
    payload, _ = _picture_bytes(asset, element)
    encoded = base64.b64encode(payload.getvalue()).decode('ascii')
    frame = element['frame']
    return (f'<img alt="{html.escape(element.get("alt_text", ""), quote=True)}" '
            f'src="data:image/png;base64,{encoded}" style="position:absolute;'
            f'left:{frame["x"]}pt;top:{frame["y"]}pt;width:{frame["w"]}pt;height:{frame["h"]}pt;'
            f'object-fit:{element.get("fit", "contain")};'
            f'transform:rotate({frame.get("rotation_deg", 0)}deg)" />')


def scene_html(snapshot, revisions, assets):
    manifest = snapshot.manifest_json
    width, height = manifest['canvas']['width_pt'], manifest['canvas']['height_pt']
    pages = []
    for item in manifest['pages']:
        scene = revisions[item['revision_id']].scene_json
        bg = scene['background']
        style = f'background:{bg["color"]};' if bg['kind'] == 'solid' else ''
        parts = [f'<section class="slide" style="{style}">']
        if bg['kind'] == 'image':
            parts.append(_image(assets[bg['asset_id']], {'frame': {'x': 0, 'y': 0, 'w': width, 'h': height},
                                                            'crop': bg['crop'], 'fit': 'cover'}))
        for element in scene['elements']:
            if element['kind'] == 'image':
                parts.append(_image(assets[element['asset_id']], element))
                continue
            frame, ts = element['frame'], element['style']
            justify = {'top': 'flex-start', 'middle': 'center', 'bottom': 'flex-end'}[ts['vertical_align']]
            parts.append(f'<div data-scene-id="{element["id"]}" style="position:absolute;'
                         f'left:{frame["x"]}pt;top:{frame["y"]}pt;width:{frame["w"]}pt;height:{frame["h"]}pt;'
                         f'box-sizing:border-box;padding:{ts["padding_pt"]}pt;overflow:hidden;'
                         f'font-family:NotoScene;font-size:{ts["font_size_pt"]}pt;'
                         f'font-weight:{ts["font_weight"]};line-height:{ts["line_height"]};'
                         f'color:{ts["color"]};text-align:{ts["align"]};white-space:pre-wrap;'
                         f'overflow-wrap:anywhere;display:flex;flex-direction:column;'
                         f'justify-content:{justify}">'
                         '<div data-scene-content style="flex:none;width:100%">' +
                         ''.join('<div data-scene-paragraph>' + (html.escape(line) if line else '<br>') + '</div>'
                                 for line in element['text'].split('\n')) + '</div></div>')
        parts.append('</section>')
        pages.append(''.join(parts))
    css = (f'@font-face{{font-family:NotoScene;src:url("{FONT.as_uri()}") format("opentype")}}'
           f'@page{{size:{width}pt {height}pt;margin:0}}'
           'html,body{margin:0;padding:0}.slide{position:relative;'
           f'width:{width}pt;height:{height}pt;overflow:hidden;break-after:page;page-break-after:always}}'
           '.slide:last-child{break-after:auto;page-break-after:auto}*{-webkit-print-color-adjust:exact;print-color-adjust:exact}')
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>' + css + '</style><body>' + ''.join(pages) + '</body></html>'


def _layout_identity():
    """Probe before cache lookup; stale/unhealthy renderers cannot be masked by it."""
    from flask import current_app
    root = current_app.config.get('SCENE_RENDER_JOB_ROOT')
    if root:
        from services.scene.render_jobs import _read_render_output
        status = json.loads(_read_render_output(Path(root).resolve(), '.renderer_status.json', 4096))
        age = time.time() - status['updated_at']
        if (not 0 <= age <= current_app.config.get('SCENE_READY_HEARTBEAT_SECONDS', 15) or
            status.get('chromium_ok') is not True or
            (current_app.config.get('SCENE_RENDER_UNIQUE_UID') and status.get('isolation_mode') != 'unique_uid')):
            raise ValueError('Scene text layout renderer unavailable')
        return validate_identity(status.get('text_layout_identity'))
    if not LAYOUT_RENDERER.is_file():
        raise ValueError('Scene text layout renderer unavailable')
    process = subprocess.run(['node', str(LAYOUT_RENDERER), '--identity'], capture_output=True,
        text=True, encoding='utf-8', errors='replace', timeout=45)
    if process.returncode != 0 or len(process.stdout) > 4096:
        raise ValueError('Scene text layout identity probe failed')
    return validate_identity(json.loads(process.stdout))


@timed_stage('text_layout')
def measure_text_layout(snapshot, revisions, assets):
    """Measure once per immutable input/runtime key; never cache an unchecked result."""
    if not FONT.is_file():
        raise ValueError('Scene text layout renderer/font unavailable')
    from flask import current_app
    identity = _layout_identity()
    font_hash, font_sha = font_fingerprint(FONT)
    if (identity['font_sha256'] != font_sha or
            snapshot.manifest_json.get('font_sha256', font_sha) != font_sha):
        raise ValueError('Scene text layout font identity mismatch')
    hashes = scene_hashes(snapshot, revisions)
    engine_version = engine_fingerprint(identity, __file__)
    # No owner/project means an internal standalone use: deliberately no caching.
    owner, project = getattr(snapshot, 'owner_id', None), getattr(snapshot, 'project_id', None)
    key = (id(current_app._get_current_object()), owner, project, tuple(hashes), font_hash, engine_version)
    if owner and project:
        cached = LAYOUT_CACHE.get(key)
        if cached is not None:
            return validate_layout(cached, snapshot, revisions, identity)
    source_html = scene_html(snapshot, revisions, assets)
    if current_app.config.get('SCENE_RENDER_JOB_ROOT'):
        from services.scene.render_jobs import run_render_job
        payload = run_render_job('text_layout', source_html.encode('utf-8'))[0]
    else:
        with tempfile.TemporaryDirectory(prefix='scene-layout-') as directory:
            source = Path(directory) / 'scene.html'
            target = Path(directory) / 'layout.json'
            source.write_text(source_html, encoding='utf-8')
            process = subprocess.run(['node', str(LAYOUT_RENDERER), str(source), str(target)],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
            if process.returncode != 0 or not target.is_file():
                raise ValueError('Chromium text layout failed')
            if target.stat().st_size > 8 * 1024 * 1024:
                raise ValueError('Scene text layout output too large')
            payload = target.read_bytes()
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError('Scene text layout output too large')
    layout = validate_layout(json.loads(payload), snapshot, revisions, identity)
    layout.update(scene_hashes=hashes, font_manifest_hash=font_hash, layout_engine_version=engine_version)
    if owner and project:
        LAYOUT_CACHE.put(key, layout)
    return layout


@timed_stage('pdf_compile')
def render_pdf(snapshot, revisions, assets, text_layout=None):
    if not FONT.is_file():
        raise ValueError('PDF renderer/font unavailable')
    from flask import current_app
    if text_layout is not None:
        validate_layout(text_layout, snapshot, revisions, _layout_identity())
    source_html = scene_html(snapshot, revisions, assets)
    if current_app.config.get('SCENE_RENDER_JOB_ROOT'):
        from services.scene.render_jobs import run_render_job
        payload, metadata = run_render_job('pdf', source_html.encode('utf-8'))
        if text_layout and metadata.get('engine_version') != text_layout['engine_identity']['chromium']:
            raise ValueError('PDF text layout engine changed')
        return payload
    if not RENDERER.is_file():
        raise ValueError('PDF renderer unavailable')
    with tempfile.TemporaryDirectory(prefix='scene-pdf-') as directory:
        source = Path(directory) / 'scene.html'
        target = Path(directory) / 'scene.pdf'
        source.write_text(source_html, encoding='utf-8')
        process = subprocess.run(['node', str(RENDERER), str(source), str(target)],
                                 capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        if process.returncode != 0 or not target.is_file():
            raise ValueError('Chromium PDF render failed: ' + (process.stderr or '')[-500:])
        if text_layout and process.stdout.strip() != text_layout['engine_identity']['chromium']:
            raise ValueError('PDF text layout engine changed')
        return target.read_bytes()


@timed_stage('pdf_verify')
def verify_pdf(payload, snapshot, revisions):
    # Chromium Type3/ActualText handling differs by extractor. PDFium can lose
    # actual spaces or fragment lines; MuPDF can emit U+FFFD for valid CJK.
    # Require ONE independent extractor to match the entire page AND every
    # text frame exactly (apart from line separators). Never synthesize source
    # text, strip spaces, normalize Unicode, or combine partial successes.
    without_breaks = lambda value: value.replace('\r', '').replace('\n', '')
    pages = snapshot.manifest_json['pages']
    width = snapshot.manifest_json['canvas']['width_pt']
    height = snapshot.manifest_json['canvas']['height_pt']
    page_reports = []
    with fitz.open(stream=payload, filetype='pdf') as document, pdfium.PdfDocument(payload) as unicode_document:
        if len(document) != len(pages) or len(unicode_document) != len(pages):
            raise ValueError('PDF page count mismatch')
        for index, item in enumerate(pages):
            page = document[index]
            if abs(page.rect.width - width) > .2 or abs(page.rect.height - height) > .2:
                raise ValueError('PDF page size mismatch')
            elements = revisions[item['revision_id']].scene_json['elements']
            text_elements = [e for e in elements if e['kind'] == 'text']
            expected = ''.join(without_breaks(e['text']) for e in text_elements)
            unicode_page = unicode_document[index]
            text_page = unicode_page.get_textpage()
            try:
                candidates = [
                    ('pdfium', text_page.get_text_range(), lambda f: text_page.get_text_bounded(
                        f['x'] - 3, height - f['y'] - f['h'] - 3, f['x'] + f['w'] + 3, height - f['y'] + 3)),
                    ('mupdf', page.get_text(), lambda f: page.get_textbox(fitz.Rect(
                        f['x'] - 3, f['y'] - 3, f['x'] + f['w'] + 3, f['y'] + f['h'] + 3))),
                ]
                matched = None
                content_matched = False
                for name, extracted, bounded in candidates:
                    if Counter(without_breaks(extracted)) != Counter(expected):
                        continue
                    content_matched = True
                    if all(without_breaks(e['text']) in without_breaks(bounded(e['frame'])) for e in text_elements):
                        matched = name
                        break
                if matched is None:
                    if content_matched:
                        raise ValueError('PDF text element missing or outside frame')
                    raise ValueError('PDF text layer content or character count mismatch')
                if expected and not page.get_fonts():
                    raise ValueError('PDF text font missing')
                page_reports.append({**page_inventory(item, revisions[item['revision_id']].scene_json),
                                     'text_extractor': matched})
            finally:
                text_page.close()
                unicode_page.close()
    return {'page_count': len(pages), 'searchable_text': True, 'pages': page_reports}
