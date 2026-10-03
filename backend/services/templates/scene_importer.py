"""Static reference import; PPTX conversion requires the isolated job renderer."""
from services.scene.telemetry import timed_stage

import io
import zipfile
from pathlib import PurePosixPath
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from flask import current_app
from PIL import Image

from models import db, ProjectTemplateAsset
from models.scene_v1 import Asset, TemplateDocument, new_id
from services.scene.store import checked_bytes, put_bytes, put_image
from services.scene.versioning import SceneError


def inspect_pptx(payload):
    if len(payload) > current_app.config['MAX_UPLOAD_BYTES']:
        raise SceneError('UPLOAD_TOO_LARGE', 'Template exceeds upload limit', 413)
    warnings = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if (len(names) > 6000 or len(names) != len({name.casefold() for name in names}) or
                    '[Content_Types].xml' not in names or 'ppt/presentation.xml' not in names):
                raise ValueError('invalid PPTX package')
            total = 0
            for info in archive.infolist():
                name = PurePosixPath(info.filename)
                if (name.is_absolute() or '..' in name.parts or '\\' in info.filename or
                        any(part in ('', '.') for part in info.filename.rstrip('/').split('/')) or
                        ':' in info.filename.split('/', 1)[0]):
                    raise ValueError('unsafe package path')
                is_symlink = ((info.external_attr >> 16) & 0o170000) == 0o120000
                if info.flag_bits & 0x1 or is_symlink:
                    raise ValueError('encrypted or linked package entry')
                lowered = info.filename.lower()
                if lowered.endswith('/vbaproject.bin') or lowered.startswith('ppt/activex/'):
                    raise ValueError('macro or ActiveX content unsupported')
                total += info.file_size
                oversized_ratio = (info.file_size > 0 and info.compress_size == 0 or
                                   info.compress_size > 0 and info.file_size / info.compress_size > 1000)
                if total > current_app.config['MAX_UNPACKED_BYTES'] or oversized_ratio:
                    raise ValueError('PPTX decompression limit exceeded')
                if lowered.endswith('.rels'):
                    if info.file_size > 2 * 1024 * 1024:
                        raise ValueError('relationship part too large')
                    relationships = ElementTree.fromstring(archive.read(info))
                    if any(rel.attrib.get('TargetMode', '').lower() == 'external'
                           for rel in relationships):
                        warnings.append('EXTERNAL_RELATIONSHIP_IGNORED')
                if lowered.startswith('ppt/embeddings/'):
                    warnings.append('EMBEDDED_OBJECT_STATIC_ONLY')
            presentation_part = archive.getinfo('ppt/presentation.xml')
            if presentation_part.file_size > 4 * 1024 * 1024:
                raise ValueError('presentation part too large')
            presentation = ElementTree.fromstring(archive.read(presentation_part))
            slide_list = presentation.find('{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst')
            count = 0 if slide_list is None else len(slide_list.findall(
                '{http://schemas.openxmlformats.org/presentationml/2006/main}sldId'))
            if not 1 <= count <= current_app.config['MAX_TEMPLATE_PAGES']:
                raise ValueError('PPTX page count exceeds limit')
            return count, sorted(set(warnings))
    except (zipfile.BadZipFile, ValueError, NotImplementedError, RuntimeError,
            ElementTree.ParseError, DefusedXmlException) as exc:
        raise SceneError('TEMPLATE_INVALID', str(exc)) from exc


def inspect_reference_image(payload):
    """Sniff the actual static format before persisting an image template."""
    try:
        with Image.open(io.BytesIO(payload)) as image:
            formats = {'PNG': ('image/png', 'png'), 'JPEG': ('image/jpeg', 'jpg'),
                       'WEBP': ('image/webp', 'webp')}
            result = formats.get(image.format)
            if (result is None or image.width < 1 or image.height < 1 or
                    image.width * image.height > 40_000_000 or
                    getattr(image, 'n_frames', 1) != 1):
                raise ValueError('unsupported or animated image template')
            image.verify()
            return result
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise SceneError('TEMPLATE_INVALID', 'Invalid static image template') from exc


def _convert_pptx(payload, expected_count):
    if not current_app.config.get('SCENE_RENDER_JOB_ROOT') or not current_app.config.get('SCENE_RENDER_UNIQUE_UID'):
        raise SceneError('TEMPLATE_RENDERER_UNAVAILABLE', 'Configure a per-job isolated template renderer', 503)
    from services.scene.render_jobs import run_render_job
    pages, result = run_render_job('pptx', payload, expected_count=expected_count)
    allowed = {'FONT_FAMILY_UNAVAILABLE', 'FONT_AVAILABILITY_UNVERIFIED'}
    reported = result.get('warnings', [])
    renderer_warnings = sorted({code for code in reported if isinstance(code, str) and code in allowed}) \
        if isinstance(reported, list) else []
    return pages, {'renderer': 'isolated-job-container', 'page_count': result['page_count'],
                   'engine_version': result.get('engine_version'), 'warnings': renderer_warnings}


@timed_stage('template_import')
def import_document(document_id):
    document = db.session.get(TemplateDocument, document_id)
    if document.status == 'preview_ready':
        return {'document_id': document.id, 'page_count': document.source_page_count}
    source = db.session.get(Asset, document.source_asset_id)
    payload = checked_bytes(source)
    if document.source_type == 'pptx':
        count, warnings = inspect_pptx(payload)
        document.source_page_count = count
        document.warnings_json = warnings
        document.status = 'converting'
        db.session.commit()
        pages, renderer_manifest = _convert_pptx(payload, count)
        document.renderer_manifest_json = renderer_manifest
        document.warnings_json = sorted(set(warnings) | set(renderer_manifest['warnings']))
        kind = 'pptx_render'
    else:
        pages = [payload]
        document.source_page_count = 1
        kind = 'upload'
    for index, page_bytes in enumerate(pages, 1):
        preview = put_image(document.owner_id, document.project_id, page_bytes,
                            kind='template_preview', provenance={'template_document_id': document.id, 'page_index': index})
        with Image.open(io.BytesIO(checked_bytes(preview))) as image:
            image.thumbnail((480, 270))
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            thumb = put_bytes(document.owner_id, document.project_id, 'template_preview',
                              buffer.getvalue(), 'image/png', 'png', dimensions=image.size,
                              provenance={'source_asset_id': preview.id})
        # Asset IDs alone do not establish ORM relationship ordering. PostgreSQL
        # checks these non-deferrable FKs immediately, unlike permissive SQLite fixtures.
        db.session.flush()
        db.session.add(ProjectTemplateAsset(id=new_id(), project_id=document.project_id,
            image_path=f'private:{preview.id}', thumb_path=f'private:{thumb.id}',
            file_size=preview.byte_size, source=kind, source_page_index=index,
            template_document_id=document.id, preview_asset_id=preview.id,
            thumbnail_asset_id=thumb.id, analysis_status='pending', sort_order=index))
    document.status = 'preview_ready'
    db.session.flush()
    return {'document_id': document.id, 'page_count': len(pages)}
