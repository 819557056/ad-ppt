"""Synthetic TextLayout fixtures, not browser measurements."""
import hashlib
from pathlib import Path


def identity():
    return {'chromium': '143.0.7499.4', 'node': '22.23.3', 'icu': '77.1', 'playwright': '1.57.0',
            'script_sha256': 'a' * 64, 'font_sha256': hashlib.sha256(
                (Path(__file__).parents[1] / 'fonts/NotoSansSC-Regular.ttf').read_bytes()).hexdigest()}


def single_line_layout(snapshot, revisions, engine=None):
    engine = engine or identity()
    pages = []
    for page in snapshot.manifest_json['pages']:
        measured = {}
        for element in revisions[page['revision_id']].scene_json['elements']:
            if element['kind'] != 'text':
                continue
            source, frame, style = element['text'], element['frame'], element['style']
            size, padding = style['font_size_pt'], style['padding_pt']
            height = size * style['line_height']
            paragraphs = source.split('\n')
            available = frame['h'] - 2 * padding
            occupied = len(paragraphs) * height
            shift = {'top': 0, 'middle': (available - occupied) / 2, 'bottom': available - occupied}[style['vertical_align']]
            lines, offset = [], 0
            for i, paragraph in enumerate(paragraphs):
                top = frame['y'] + padding + shift + i * height
                lines.append({'start': offset, 'end': offset + len(paragraph),
                    'break_kind': 'end' if i == len(paragraphs) - 1 else 'hard', 'font_size_pt': size,
                    'line_box': {'x': frame['x'] + padding, 'y': top, 'w': frame['w'] - 2 * padding, 'h': height},
                    'baseline_pt': top + size})
                offset += len(paragraph) + 1
            measured[element['id']] = {'source_length': len(source), 'font_size_pt': size, 'line_height_pt': height,
                'padding_pt': padding, 'lines': lines, 'resolved_fonts': ([{'family_name': 'Noto Sans CJK SC',
                'postscript_name': 'NotoSansCJKsc-Regular', 'is_custom_font': True, 'glyph_count': len(source.replace('\n', ''))}]
                if source.replace('\n', '') else [])}
        pages.append(measured)
    return {'schema_version': 1, 'engine': 'chromium-' + engine['chromium'], 'engine_identity': engine, 'pages': pages}
