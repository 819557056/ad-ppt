"""Private, derived TextLayout v1. Source text is never copied or made editable.

All offsets count Unicode code points; a hard break consumes the source LF
between two half-open ranges. Empty paragraphs (including terminal LF) have a
real line box. Coordinates are page-relative points, not DOM glyph rectangles.
"""
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal

import regex
from pydantic import BaseModel, ConfigDict, Field

from .validation import digest


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class EngineIdentity(Strict):
    chromium: str = Field(pattern=r'^\d+(\.\d+){1,3}$', max_length=64)
    node: str = Field(pattern=r'^\d+(\.\d+){1,3}$', max_length=64)
    icu: str = Field(pattern=r'^\d+(\.\d+){0,3}$', max_length=64)
    playwright: str = Field(pattern=r'^\d+(\.\d+){1,3}$', max_length=64)
    script_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    font_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class LineBox(Strict):
    x: float
    y: float
    w: float = Field(gt=0, le=2880)
    h: float = Field(gt=0, le=288)


class TextLine(Strict):
    start: int = Field(ge=0, le=10000)
    end: int = Field(ge=0, le=10000)
    break_kind: Literal['hard', 'soft', 'end']
    font_size_pt: float = Field(ge=6, le=96)
    line_box: LineBox
    baseline_pt: float


class ResolvedFont(Strict):
    family_name: str = Field(min_length=1, max_length=128)
    postscript_name: str = Field(min_length=1, max_length=128)
    is_custom_font: bool
    glyph_count: int = Field(ge=1, le=100000)


class ElementLayout(Strict):
    source_length: int = Field(ge=0, le=10000)
    font_size_pt: float = Field(ge=6, le=96)
    line_height_pt: float = Field(gt=0, le=288)
    padding_pt: float = Field(ge=0, le=48)
    lines: list[TextLine] = Field(min_length=1, max_length=10001)
    resolved_fonts: list[ResolvedFont] = Field(max_length=16)


class TextLayout(Strict):
    schema_version: Literal[1]
    engine: str = Field(max_length=80)
    engine_identity: EngineIdentity
    pages: list[dict[str, ElementLayout]] = Field(max_length=100)


def validate_identity(value):
    return EngineIdentity.model_validate(value).model_dump()


def scene_hashes(snapshot, revisions):
    result = []
    for item in snapshot.manifest_json['pages']:
        actual = digest(revisions[item['revision_id']].scene_json)
        if item.get('scene_hash') not in (None, actual):
            raise ValueError('Text layout scene hash mismatch')
        result.append(actual)
    return result


def _near(actual, expected, tolerance=.06):
    return math.isfinite(actual) and abs(actual - expected) <= tolerance


def validate_layout(value, snapshot, revisions, identity=None):
    """Independently check renderer offsets, UAX #29 boundaries, and geometry.

    regex's Unicode data may be newer than Chromium ICU: disagreement fails
    closed rather than splitting a cluster that either runtime treats as whole.
    """
    raw = {key: value[key] for key in ('schema_version', 'engine', 'engine_identity', 'pages') if key in value}
    allowed = set(raw) | {'scene_hashes', 'font_manifest_hash', 'layout_engine_version'}
    if set(value) - allowed or type(value.get('schema_version')) is not int:
        raise ValueError('Text layout envelope invalid')
    layout = TextLayout.model_validate(raw)
    measured_identity = layout.engine_identity.model_dump()
    if (identity is not None and measured_identity != identity) or layout.engine != 'chromium-' + measured_identity['chromium']:
        raise ValueError('Text layout engine identity changed')
    pages = snapshot.manifest_json['pages']
    if len(layout.pages) != len(pages):
        raise ValueError('Text layout page mapping invalid')
    hashes = scene_hashes(snapshot, revisions)
    if 'scene_hashes' in value and value['scene_hashes'] != hashes:
        raise ValueError('Text layout scene hash mismatch')
    for page, measured in zip(pages, layout.pages):
        elements = revisions[page['revision_id']].scene_json['elements']
        texts = {e['id']: e for e in elements if e['kind'] == 'text'}
        if set(measured) != set(texts):
            raise ValueError('Text layout element mapping invalid')
        for element_id, data in measured.items():
            element = texts[element_id]
            source, style, frame = element['text'], element['style'], element['frame']
            if data.source_length != len(source):
                raise ValueError('Text layout source length mismatch')
            font_size, padding = style['font_size_pt'], style['padding_pt']
            height = font_size * style['line_height']
            if not all((_near(data.font_size_pt, font_size), _near(data.line_height_pt, height),
                        _near(data.padding_pt, padding))):
                raise ValueError('Text layout style mismatch')
            if source.replace('\n', '') and not data.resolved_fonts:
                raise ValueError('Text layout actual font missing')
            for font in data.resolved_fonts:
                if (not font.is_custom_font or font.family_name != 'Noto Sans CJK SC' or
                        font.postscript_name != 'NotoSansCJKsc-Regular'):
                    raise ValueError('Text layout unexpected font fallback')
            boundaries = {0, *(part.end() for part in regex.finditer(r'\X', source))}
            cursor = 0
            # Chromium quantizes used line height to CSS subpixels. Accumulate
            # the measured height, not the unquantized Scene float.
            height = data.line_height_pt
            occupied = len(data.lines) * height
            available = frame['h'] - 2 * padding
            if occupied > available + .06:
                raise ValueError('Text layout vertical overflow')
            shift = {'top': 0, 'middle': (available - occupied) / 2, 'bottom': available - occupied}[style['vertical_align']]
            top = frame['y'] + padding + shift
            baseline_offset = data.lines[0].baseline_pt - data.lines[0].line_box.y
            if not 0 < baseline_offset < height + font_size:
                raise ValueError('Text layout baseline invalid')
            for index, line in enumerate(data.lines):
                if (line.start != cursor or not line.start <= line.end <= len(source) or
                    line.start not in boundaries or line.end not in boundaries or
                    '\n' in source[line.start:line.end]):
                    raise ValueError('Text layout source range/grapheme boundary invalid')
                if line.break_kind == 'hard':
                    if line.end >= len(source) or source[line.end] != '\n':
                        raise ValueError('Text layout hard break invalid')
                    cursor = line.end + 1
                elif line.break_kind == 'soft':
                    if (line.end == line.start or line.end >= len(source) or source[line.end] == '\n'):
                        raise ValueError('Text layout soft break invalid')
                    cursor = line.end
                else:
                    if index != len(data.lines) - 1 or line.end != len(source):
                        raise ValueError('Text layout final line invalid')
                    cursor = line.end
                if (not _near(line.font_size_pt, font_size) or not _near(line.line_box.x, frame['x'] + padding) or
                    not _near(line.line_box.y, top + index * height, .15) or
                    not _near(line.line_box.w, frame['w'] - 2 * padding) or
                    not _near(line.line_box.h, height) or
                    not _near(line.baseline_pt - line.line_box.y, baseline_offset, .06)):
                    raise ValueError('Text layout line geometry invalid')
            if cursor != len(source) or data.lines[-1].break_kind != 'end':
                raise ValueError('Text layout source coverage invalid')
    return {**value, **layout.model_dump()}


def soft_breaks(element_layout):
    return [line['end'] for line in element_layout['lines'] if line['break_kind'] == 'soft']


def font_fingerprint(font):
    manifest = json.loads(font.with_name('manifest.json').read_text(encoding='utf-8'))
    actual = hashlib.sha256(font.read_bytes()).hexdigest()
    if manifest['sha256'] != actual or manifest['font_file'] != font.name or manifest['font_manifest_id'] != 'fonts-v1':
        raise ValueError('Text layout font manifest mismatch')
    return digest(manifest), actual


def engine_fingerprint(identity, compiler):
    # Changes to HTML/CSS, validation, segmentation, or actual browser/font
    # identity invalidate cache entries even if their human version is unchanged.
    sources = {path.name: hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
               for path in (Path(__file__), Path(compiler))}
    return digest({'contract': 'text-layout-v1', 'runtime': identity,
                   'sources': sources, 'regex': regex.__version__})


class LayoutCache:
    """Bounded, private process LRU; no extra disk objects or retention bypass.

    Serialized values prevent mutation by a caller. Eviction/expiry only affect
    performance. Shared-worker keys must include owner, project and app/store.
    """
    def __init__(self, max_bytes=32 * 1024 * 1024, max_entries=64, ttl=300):
        self.max_bytes, self.max_entries, self.ttl = max_bytes, max_entries, ttl
        self._entries, self._bytes, self._lock = OrderedDict(), 0, threading.Lock()

    def _prune(self, now):
        for key, (expires, value) in list(self._entries.items()):
            if expires <= now:
                self._bytes -= len(value)
                del self._entries[key]

    def get(self, key):
        with self._lock:
            self._prune(time.monotonic())
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return json.loads(entry[1])

    def put(self, key, value):
        payload = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        with self._lock:
            self._prune(time.monotonic())
            if key in self._entries:
                self._bytes -= len(self._entries.pop(key)[1])
            if len(payload) > self.max_bytes or self.max_entries <= 0:
                return
            while self._entries and (self._bytes + len(payload) > self.max_bytes or len(self._entries) >= self.max_entries):
                self._bytes -= len(self._entries.popitem(last=False)[1][1])
            self._entries[key] = (time.monotonic() + self.ttl, payload)
            self._bytes += len(payload)


LAYOUT_CACHE = LayoutCache()
