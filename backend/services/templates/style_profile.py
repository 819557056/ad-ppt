"""Bounded, canonical style reference; template text is never a content field."""
import math
import re

from services.scene.versioning import SceneError

ROLES = {'cover', 'agenda', 'section', 'content', 'closing', 'unknown'}
REGIONS = {'title', 'body', 'image', 'decoration'}
CORE = {'schema_version', 'role', 'palette', 'font_suggestions', 'layout_hints',
        'decorative_hints', 'content_density', 'warnings'}
FONT_SOURCES = {'visual_inference', 'user', 'unknown'}


def _strings(value, limit, max_length):
    if not isinstance(value, list) or len(value) > limit or any(
        not isinstance(item, str) or len(item) > max_length or
        any((ord(char) < 32 and char not in '\n\t') or 0xD800 <= ord(char) <= 0xDFFF
            for char in item) for item in value):
        raise SceneError('ANALYSIS_INVALID', 'Style hint list is invalid')
    return value


def _font_suggestions(value):
    if not isinstance(value, list) or len(value) > 5:
        raise SceneError('ANALYSIS_INVALID', 'Font suggestions invalid')
    result = []
    for hint in value:
        if isinstance(hint, str):
            # Keep legacy JSON/hash stable; missing evidence is not confidence=1.
            result.extend(_strings([hint], 1, 60))
            continue
        if not isinstance(hint, dict) or set(hint) != {'family', 'source', 'confidence'}:
            raise SceneError('ANALYSIS_INVALID', 'Font evidence fields invalid')
        family = _strings([hint['family']], 1, 60)[0]
        source, confidence = hint['source'], hint['confidence']
        if not family.strip() or not isinstance(source, str) or source not in FONT_SOURCES:
            raise SceneError('ANALYSIS_INVALID', 'Font source invalid')
        if confidence is not None and (isinstance(confidence, bool) or
                not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or
                not 0 <= confidence <= 1):
            raise SceneError('ANALYSIS_INVALID', 'Font confidence invalid')
        result.append({'family': family, 'source': source, 'confidence': confidence})
    return result


def normalize_style_profile(value):
    if not isinstance(value, dict) or set(value) != CORE or type(value.get('schema_version')) is not int or value['schema_version'] != 1 or not isinstance(value.get('role'), str) or value['role'] not in ROLES:
        raise SceneError('ANALYSIS_INVALID', 'Style profile v1 fields invalid')
    palette = value['palette']
    if not isinstance(palette, dict) or set(palette) != {'background', 'text', 'accent'} or any(
        not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color)
        for color in palette.values()):
        raise SceneError('ANALYSIS_INVALID', 'Style palette invalid')
    hints = value['layout_hints']
    if not isinstance(hints, list) or len(hints) > 12:
        raise SceneError('ANALYSIS_INVALID', 'Too many layout hints')
    clean_hints = []
    for hint in hints:
        required = {'region', 'x', 'y', 'w', 'h'}
        if not isinstance(hint, dict) or not required <= set(hint) or set(hint) - required - {'suggested_max_chars'} or not isinstance(hint['region'], str) or hint['region'] not in REGIONS:
            raise SceneError('ANALYSIS_INVALID', 'Layout region invalid')
        coordinates = {}
        for key in ('x', 'y', 'w', 'h'):
            number = hint[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not 0 <= number <= 1:
                raise SceneError('ANALYSIS_INVALID', 'Layout coordinate invalid')
            coordinates[key] = round(float(number), 4)
        if coordinates['w'] <= 0 or coordinates['h'] <= 0 or coordinates['x'] + coordinates['w'] > 1.0001 or coordinates['y'] + coordinates['h'] > 1.0001:
            raise SceneError('ANALYSIS_INVALID', 'Layout region outside page')
        clean = {'region': hint['region'], **coordinates}
        if 'suggested_max_chars' in hint:
            capacity = hint['suggested_max_chars']
            if capacity is not None and (type(capacity) is not int or not 1 <= capacity <= 10000):
                raise SceneError('ANALYSIS_INVALID', 'Suggested text capacity invalid')
            clean['suggested_max_chars'] = capacity
        clean_hints.append(clean)
    if not isinstance(value['content_density'], str) or value['content_density'] not in {'low', 'medium', 'high'}:
        raise SceneError('ANALYSIS_INVALID', 'Content density invalid')
    return {'schema_version': 1, 'role': value['role'],
            'palette': {key: palette[key].upper() for key in ('background', 'text', 'accent')},
            'font_suggestions': _font_suggestions(value['font_suggestions']),
            'layout_hints': clean_hints,
            'decorative_hints': _strings(value['decorative_hints'], 10, 200),
            'content_density': value['content_density'],
            'warnings': _strings(value['warnings'], 10, 200)}
