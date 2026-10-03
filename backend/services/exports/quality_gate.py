"""Preflight deterministic content before either native-text exporter runs."""
from services.scene.telemetry import timed_stage

from pathlib import Path
from functools import lru_cache

from PIL import ImageFont
from fontTools.ttLib import TTFont, TTLibError

from services.scene.versioning import SceneError

FONT = Path(__file__).resolve().parents[2] / 'fonts' / 'NotoSansSC-Regular.ttf'


@lru_cache(maxsize=4)
def _font_codepoints(path, mtime_ns, byte_size):
    """Cache by actual file identity; snapshot checks the manifest SHA separately."""
    font = TTFont(path, lazy=True)
    try:
        cmap = font.getBestCmap()
        if not cmap:
            raise ValueError('Font has no Unicode character map')
        return frozenset(cmap)
    finally:
        font.close()


def validate_font_coverage(scene):
    try:
        info = FONT.stat()
        supported = _font_codepoints(str(FONT), info.st_mtime_ns, info.st_size)
    except (OSError, ValueError, TTLibError) as exc:
        raise SceneError('FONT_UNAVAILABLE', 'Fixed Scene font is missing or invalid', 503) from exc
    for element in scene['elements']:
        if element['kind'] != 'text':
            continue
        missing = sorted({ord(character) for character in element['text']
                          if character != '\n' and ord(character) not in supported})
        if missing:
            preview = ', '.join(f'U+{point:04X}' for point in missing[:8])
            raise SceneError('FONT_GLYPH_UNAVAILABLE',
                f'Fixed server font cannot render {preview}; replace these characters before export',
                422, {'element_id': element['id'], 'codepoints': [f'U+{point:04X}' for point in missing[:8]]})


def text_lines(element):
    style, frame = element['style'], element['frame']
    if not element['text']:
        return []
    width_px = (frame['w'] - 2 * style['padding_pt']) * 96 / 72
    if width_px <= 0:
        raise ValueError(f"Text overflow in element {element['id']}: no inner width")
    font = ImageFont.truetype(str(FONT), round(style['font_size_pt'] * 96 / 72))
    lines = []
    for hard_line in element['text'].split('\n'):
        current = ''
        for character in hard_line:
            if font.getlength(character) > width_px:
                raise ValueError(f"Text overflow in element {element['id']}: glyph wider than frame")
            if current and font.getlength(current + character) > width_px:
                lines.append(current)
                current = character
            else:
                current += character
        lines.append(current)
    return lines


def validate_text_fit(scene):
    validate_font_coverage(scene)
    for element in scene['elements']:
        if element['kind'] != 'text' or not element['text']:
            continue
        available = element['frame']['h'] - 2 * element['style']['padding_pt']
        if element['text'] and available <= 0:
            raise ValueError(f"Text overflow in element {element['id']}: no inner height")
        occupied = len(text_lines(element)) * element['style']['font_size_pt'] * element['style']['line_height']
        if occupied > available + 2:
            raise ValueError(f"Text overflow in element {element['id']}")


@timed_stage('snapshot_validate')
def validate_snapshot_content(snapshot, revisions):
    for item in snapshot.manifest_json['pages']:
        validate_text_fit(revisions[item['revision_id']].scene_json)
