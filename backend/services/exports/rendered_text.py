"""Non-waivable checks of the *rendered* native-text PPTX, not OOXML alone.

MuPDF's drawing trace preserves real spaces (get_text() can invent spaces from
positioning). PDFium supplies tight painted glyph bounds; it can coalesce spaces
and return UTF-16 surrogate pairs. The primary trace must match ALL source code
points, including spaces. Only the secondary ink-bound mapping excludes whitespace.
No Unicode normalization, OCR, source-text substitution, or similarity threshold.
"""
import ctypes
import hashlib
import math

import fitz
import pypdfium2 as pdfium

from services.scene.errors import SceneError
from services.scene.layout_contract import validate_layout
from services.scene.telemetry import timed_stage

# Coordinate serialization epsilon, not a calibrated visual acceptance threshold.
# OOXML uses 1/100 pt spacing; LibreOffice rounds positions during PDF serialization.
COORDINATE_EPSILON_PT = 0.25


class TargetRenderError(SceneError):
    """Must never be translated into an ordinary waivable visual warning."""


def _fail(code, message, page_id=None, element_id=None):
    raise TargetRenderError(code, message, 422, {
        key: value for key, value in (("page_id", page_id), ("element_id", element_id)) if value})


def _finite(values):
    return all(isinstance(v, (float, int)) and math.isfinite(v) for v in values)


def _object_address(value):
    return ctypes.cast(value, ctypes.c_void_p).value


def _clip_rectangles(obj):
    clip = pdfium.raw.FPDFPageObj_GetClipPath(obj)
    count = pdfium.raw.FPDFClipPath_CountPaths(clip) if clip else -1
    # PDFium returns -1 for an object with no clip path (not just a null pointer).
    if count in (-1, 0):
        return []
    if not 0 < count <= 32:
        _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text clip is unsupported')
    result = []
    for index in range(count):
        length = pdfium.raw.FPDFClipPath_CountPathSegments(clip, index)
        if length not in (4, 5):
            _fail('PPTX_RENDER_UNVERIFIED', 'Non-rectangular rendered text clip')
        points = []
        for segment_index in range(length):
            segment = pdfium.raw.FPDFClipPath_GetPathSegment(clip, index, segment_index)
            expected_type = pdfium.raw.FPDF_SEGMENT_MOVETO if segment_index == 0 else pdfium.raw.FPDF_SEGMENT_LINETO
            x, y = ctypes.c_float(), ctypes.c_float()
            if (not segment or pdfium.raw.FPDFPathSegment_GetType(segment) != expected_type or
                    not pdfium.raw.FPDFPathSegment_GetPoint(segment, x, y) or not _finite((x.value, y.value))):
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text clip geometry unavailable')
            points.append((x.value, y.value))
        if not pdfium.raw.FPDFPathSegment_GetClose(segment):
            _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text clip is not closed')
        if len(points) == 5:
            if points[0] != points[-1]:
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text clip is not a rectangle')
            points.pop()
        xs, ys = {p[0] for p in points}, {p[1] for p in points}
        if (len(xs) != 2 or len(ys) != 2 or len(set(points)) != 4 or
                any(a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1]))):
            _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text clip is not axis-aligned')
        result.append((min(xs), min(ys), max(xs), max(ys)))
    return result


def _ink_characters(text_page, page):
    height = page.get_height()
    result, clips = [], {}
    # Locked LibreOffice emits Scene text as page-level objects. Do not silently
    # ignore inherited clip paths for an unexpected nested Form text object.
    for i in range(pdfium.raw.FPDFPage_CountObjects(page)):
        obj = pdfium.raw.FPDFPage_GetObject(page, i)
        if pdfium.raw.FPDFPageObj_GetType(obj) == pdfium.raw.FPDF_PAGEOBJ_TEXT:
            clips[_object_address(obj)] = _clip_rectangles(obj)
    index, count = 0, text_page.count_chars()
    while index < count:
        value = pdfium.raw.FPDFText_GetUnicode(text_page, index)
        generated = pdfium.raw.FPDFText_IsGenerated(text_page, index)
        if generated < 0:
            _fail('PPTX_RENDER_UNVERIFIED', 'Rendered glyph mapping unavailable')
        if generated:
            index += 1
            continue
        bounds = text_page.get_charbox(index)
        if 0xD800 <= value <= 0xDBFF:
            index += 1
            if index >= count or pdfium.raw.FPDFText_IsGenerated(text_page, index) != 0:
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered Unicode mapping invalid')
            low = pdfium.raw.FPDFText_GetUnicode(text_page, index)
            if not 0xDC00 <= low <= 0xDFFF:
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered Unicode mapping invalid')
            other = text_page.get_charbox(index)
            bounds = (min(bounds[0], other[0]), min(bounds[1], other[1]),
                      max(bounds[2], other[2]), max(bounds[3], other[3]))
            value = 0x10000 + ((value - 0xD800) << 10) + low - 0xDC00
        if value == 0 or value == 0xFFFD or 0xDC00 <= value <= 0xDFFF or value > 0x10FFFF:
            _fail('PPTX_RENDER_UNVERIFIED', 'Rendered Unicode mapping invalid')
        character = chr(value)
        if not character.isspace():
            obj = pdfium.raw.FPDFText_GetTextObject(text_page, index)
            if _object_address(obj) not in clips:
                _fail('PPTX_RENDER_UNVERIFIED', 'Nested or unknown rendered text object')
            left, bottom, right, top = bounds
            epsilon = COORDINATE_EPSILON_PT
            for cl, cb, cr, ct in clips[_object_address(obj)]:
                if left < cl - epsilon or bottom < cb - epsilon or right > cr + epsilon or top > ct + epsilon:
                    _fail('PPTX_RENDER_CLIPPED', 'Actual PDF clipping path cuts rendered text')
            rect = (left, height - top, right, height - bottom)
            if not _finite(rect) or right < left or top < bottom:
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered glyph bounds invalid')
            result.append((character, rect))
        index += 1
    return result


def _page_trace(page, expected_length):
    result = []
    for span in page.get_texttrace():
        # A hidden text layer must not masquerade as a visibly rendered title.
        if (span['type'] not in (0, 1) or span.get('opacity', 0) <= 0 or
                tuple(span['dir']) != (1.0, 0.0)):
            _fail('PPTX_RENDER_UNVERIFIED', 'Unsupported or invisible rendered text')
        if span['font'] not in ('NotoSansCJKsc-Regular', 'NotoSansCJKsc-Bold'):
            _fail('PPTX_RENDER_FONT_MISMATCH', 'Actual PPTX renderer substituted the fixed font')
        for value, _glyph, origin, _bbox in span['chars']:
            if not 0 < value <= 0x10FFFF or value == 0xFFFD or 0xD800 <= value <= 0xDFFF or not _finite(origin):
                _fail('PPTX_RENDER_UNVERIFIED', 'Rendered text trace invalid')
            result.append((chr(value), tuple(origin), span['size']))
            if len(result) > expected_length:
                _fail('PPTX_RENDER_TEXT_MISMATCH', 'Rendered text differs from frozen Scene')
    return result


def _verify_page(page, unicode_page, text_page, item, scene, layouts):
    elements = [e for e in scene['elements'] if e['kind'] == 'text']
    expected = ''.join(e['text'].replace('\n', '') for e in elements)
    trace = _page_trace(page, len(expected))
    if ''.join(char for char, _, _ in trace) != expected:
        _fail('PPTX_RENDER_TEXT_MISMATCH', 'Rendered text differs from frozen Scene', item['page_id'])
    # PDFium bounds are independently mapped to the exact visible code-point
    # sequence. Spaces are checked above, not silently removed from source text.
    if text_page.count_chars() > len(expected) * 3 + 2048:
        _fail('PPTX_RENDER_UNVERIFIED', 'Rendered glyph mapping exceeds source bounds', item['page_id'])
    ink = _ink_characters(text_page, unicode_page)
    if ''.join(char for char, _ in ink) != ''.join(char for char in expected if not char.isspace()):
        _fail('PPTX_RENDER_UNVERIFIED', 'Independent rendered glyph mapping differs', item['page_id'])
    offset, ink_offset, reports = 0, 0, []
    epsilon = COORDINATE_EPSILON_PT
    for element in elements:
        source, frame = element['text'], element['frame']
        measured = layouts[element['id']]
        previous = None
        painted_lines, rendered_lines = 0, []
        for line_index, line in enumerate(measured['lines']):
            chars = source[line['start']:line['end']]
            positions = trace[offset:offset + len(chars)]
            offset += len(chars)
            if any(not _finite((size,)) or abs(size - element['style']['font_size_pt']) > epsilon
                   for _, _, size in positions):
                _fail('PPTX_RENDER_FONT_MISMATCH', 'Actual PPTX renderer changed the font size', item['page_id'], element['id'])
            visible_origins = [origin for char, origin, _ in positions if not char.isspace()]
            if not visible_origins:
                continue  # Empty/space-only lines have no ink; OOXML was checked separately.
            baselines = [origin[1] for origin in visible_origins]
            baseline = sum(baselines) / len(baselines)
            if max(baselines) - min(baselines) > epsilon:
                _fail('PPTX_RENDER_REFLOW', 'Rendered text reflowed within a frozen line', item['page_id'], element['id'])
            if previous:
                old_index, old_baseline = previous
                expected_delta = (line_index - old_index) * round(measured['line_height_pt'], 2)
                if abs(baseline - old_baseline - expected_delta) > epsilon:
                    _fail('PPTX_RENDER_REFLOW', 'Rendered line spacing or blank paragraphs changed', item['page_id'], element['id'])
            previous = line_index, baseline
            painted_lines += 1
            rendered_lines.append({'source_line_index': line_index, 'baseline_pt': round(baseline, 4)})
            for character in chars:
                if character.isspace():
                    continue
                _, (left, top, right, bottom) = ink[ink_offset]
                ink_offset += 1
                if (left < frame['x'] - epsilon or top < frame['y'] - epsilon or
                        right > frame['x'] + frame['w'] + epsilon or bottom > frame['y'] + frame['h'] + epsilon):
                    _fail('PPTX_RENDER_CLIPPED', 'Rendered glyph exceeds its frozen text frame', item['page_id'], element['id'])
                if right <= left or bottom <= top:
                    _fail('PPTX_RENDER_UNVERIFIED', 'Rendered visible glyph has no painted bounds', item['page_id'], element['id'])
        reports.append({'element_id': element['id'], 'codepoints_checked': len(source.replace('\n', '')),
                        'layout_lines': len(measured['lines']), 'painted_lines_checked': painted_lines,
                        'rendered_lines': rendered_lines})
    return {'page_id': item['page_id'], 'elements': reports}


@timed_stage('pptx_render_verify')
def verify_rendered_pptx(payload, snapshot, revisions, text_layout):
    """Accept only an independently parsed actual Office-rendered PDF.

    The PDF is a private verification artifact; the delivered PDF is still
    Chromium's same-Scene output. Native OOXML verification must also pass.
    """
    try:
        layout = validate_layout(text_layout, snapshot, revisions)
        pages = snapshot.manifest_json['pages']
        canvas = snapshot.manifest_json['canvas']
        reports = []
        with fitz.open(stream=payload, filetype='pdf') as document, pdfium.PdfDocument(payload) as unicode_document:
            if len(document) != len(pages) or len(unicode_document) != len(pages):
                _fail('PPTX_RENDER_PAGE_MISMATCH', 'Rendered PPTX page count differs')
            for index, item in enumerate(pages):
                page = document[index]
                if (page.rotation != 0 or abs(page.rect.width - canvas['width_pt']) > .2 or
                        abs(page.rect.height - canvas['height_pt']) > .2):
                    _fail('PPTX_RENDER_PAGE_MISMATCH', 'Rendered PPTX canvas differs', item['page_id'])
                unicode_page = unicode_document[index]
                text_page = unicode_page.get_textpage()
                try:
                    reports.append(_verify_page(page, unicode_page, text_page, item,
                        revisions[item['revision_id']].scene_json, layout['pages'][index]))
                finally:
                    text_page.close()
                    unicode_page.close()
        return {'version': 1, 'status': 'passed', 'rendered_pdf_sha256': hashlib.sha256(payload).hexdigest(),
                'primary_extractor': 'mupdf_drawing_trace', 'ink_extractor': 'pdfium',
                'mupdf_version': fitz.VersionBind, 'coordinate_epsilon_pt': COORDINATE_EPSILON_PT,
                'pages': reports}
    except TargetRenderError:
        raise
    except Exception as exc:
        raise TargetRenderError('PPTX_RENDER_UNVERIFIED', 'Actual PPTX text layout could not be verified', 422) from exc
