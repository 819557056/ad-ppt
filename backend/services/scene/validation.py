"""Strict Scene v1 contract; no HTML, URLs, paths, or arbitrary model fields."""
import hashlib
import json
import math
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Canvas(Strict):
    width_pt: float = Field(gt=0, le=2880)
    height_pt: float = Field(gt=0, le=2880)


class Frame(Strict):
    x: float = Field(ge=-2880, le=2880)
    y: float = Field(ge=-2880, le=2880)
    w: float = Field(gt=0, le=2880)
    h: float = Field(gt=0, le=2880)
    rotation_deg: float = Field(ge=-180, le=180)


class Crop(Strict):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode='after')
    def inside(self):
        if self.x + self.w > 1.000001 or self.y + self.h > 1.000001:
            raise ValueError('crop exceeds source image')
        return self


class TextStyle(Strict):
    font_family_id: Literal['noto-sans-sc'] = 'noto-sans-sc'
    font_size_pt: float = Field(ge=6, le=96)
    font_weight: Literal[400, 700] = 400
    color: str
    align: Literal['left', 'center', 'right'] = 'left'
    vertical_align: Literal['top', 'middle', 'bottom'] = 'top'
    line_height: float = Field(ge=0.8, le=3)
    padding_pt: float = Field(ge=0, le=48)

    @field_validator('color')
    @classmethod
    def color_hex(cls, value):
        import re
        if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError('color must be #RRGGBB')
        return value.upper()


def clean_text(value):
    if len(value) > 10000 or any((ord(c) < 32 and c != '\n') or
                                 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError('text length/control character invalid')
    return value


class TextElement(Strict):
    id: str
    kind: Literal['text']
    role: Literal['title', 'body', 'annotation']
    frame: Frame
    text: str
    style: TextStyle
    locked: bool = False

    _clean_text = field_validator('text')(clean_text)


class ImageElement(Strict):
    id: str
    kind: Literal['image']
    role: Literal['illustration', 'chart', 'icon', 'photo', 'decoration']
    asset_id: str
    frame: Frame
    crop: Crop = Crop(x=0, y=0, w=1, h=1)
    fit: Literal['contain', 'cover'] = 'contain'
    opacity: float = Field(ge=0, le=1)
    locked: bool = False
    alt_text: str = Field(default='', max_length=500)


class SolidBackground(Strict):
    kind: Literal['solid']
    color: str

    _color_hex = field_validator('color')(TextStyle.color_hex.__func__)


class ImageBackground(Strict):
    kind: Literal['image']
    asset_id: str
    crop: Crop = Crop(x=0, y=0, w=1, h=1)


class SlideScene(Strict):
    schema_version: Literal[1]
    font_manifest_id: Literal['fonts-v1']
    canvas: Canvas
    background: Annotated[Union[SolidBackground, ImageBackground], Field(discriminator='kind')]
    elements: list[Annotated[Union[TextElement, ImageElement], Field(discriminator='kind')]] = Field(max_length=200)

    @model_validator(mode='after')
    def check_elements(self):
        ids = set()
        for element in self.elements:
            try:
                UUID(element.id)
            except ValueError as exc:
                raise ValueError('element id must be UUID') from exc
            if element.id in ids:
                raise ValueError('duplicate element id')
            ids.add(element.id)
            frame = element.frame
            if frame.x + frame.w <= 0 or frame.y + frame.h <= 0 or frame.x >= self.canvas.width_pt or frame.y >= self.canvas.height_pt:
                raise ValueError('element completely outside canvas')
            if element.kind == 'text' and frame.rotation_deg != 0:
                raise ValueError('text rotation unsupported in v1')
            if element.kind == 'text' and (frame.x < 0 or frame.y < 0 or frame.x + frame.w > self.canvas.width_pt or frame.y + frame.h > self.canvas.height_pt):
                raise ValueError('text frame outside canvas')
        return self


def normalize_scene(data: dict, width: float, height: float) -> dict:
    scene = SlideScene.model_validate(data).model_dump(mode='json')
    if scene['canvas'] != {'width_pt': float(width), 'height_pt': float(height)}:
        raise ValueError('scene canvas differs from project')
    for element in scene['elements']:
        for key, value in element['frame'].items():
            if not math.isfinite(value):
                raise ValueError('non-finite geometry')
            element['frame'][key] = round(value, 2)
        if element['kind'] == 'text':
            style = element['style']
            style['font_size_pt'] = round(style['font_size_pt'], 2)
            style['padding_pt'] = round(style['padding_pt'], 2)
        else:
            element['opacity'] = round(element['opacity'], 6)
            element['crop'] = {k: round(v, 6) for k, v in element['crop'].items()}
    return scene


def digest(data: dict) -> str:
    return hashlib.sha256(canonical_bytes(data)).hexdigest()


def canonical_bytes(data) -> bytes:
    """JCS-compatible encoding for the bounded v1 numeric domain (<= 6 dp).

    Keys in the v1 contract are ASCII. Non-finite and unbounded-precision numbers
    are rejected; this avoids Python/JavaScript exponent-format differences.
    """
    def encode(value):
        if value is None:
            return 'null'
        if value is True:
            return 'true'
        if value is False:
            return 'false'
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            if not math.isfinite(value) or abs(value) > 10**12:
                raise ValueError('canonical number out of range')
            decimal = format(value, '.6f').rstrip('0').rstrip('.')
            if abs(value - float(decimal or '0')) > 1e-9:
                raise ValueError('canonical number exceeds 6 decimal places')
            return decimal if decimal and decimal != '-0' else '0'
        if isinstance(value, list):
            return '[' + ','.join(encode(item) for item in value) + ']'
        if isinstance(value, dict):
            if not all(isinstance(k, str) for k in value):
                raise ValueError('canonical object keys must be strings')
            return '{' + ','.join(encode(k) + ':' + encode(value[k]) for k in sorted(value)) + '}'
        raise ValueError('unsupported canonical value')
    return encode(data).encode('utf-8')


def blank_scene(width=960, height=540):
    return {'schema_version': 1, 'font_manifest_id': 'fonts-v1',
            'canvas': {'width_pt': width, 'height_pt': height},
            'background': {'kind': 'solid', 'color': '#FFFFFF'}, 'elements': []}


def asset_refs(scene):
    refs = []
    if scene['background']['kind'] == 'image':
        refs.append((scene['background']['asset_id'], 'background'))
    refs += [(e['asset_id'], e['role']) for e in scene['elements'] if e['kind'] == 'image']
    return refs
