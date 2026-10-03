"""Content-free, per-object audit metadata shared by both export verifiers."""
from collections import Counter

from services.scene.validation import digest

ROLES = {'text': {'title', 'body', 'annotation'},
         'image': {'illustration', 'chart', 'icon', 'photo', 'decoration'}}


def page_inventory(page, scene):
    """Record source order/role, not a second editable copy of slide text."""
    elements = []
    for index, element in enumerate(scene['elements']):
        kind, role = element['kind'], element['role']
        if kind not in ROLES or role not in ROLES[kind]:
            raise ValueError('Export report element kind or role invalid')
        item = {'element_id': element['id'], 'kind': kind, 'role': role, 'z_index': index,
                'editable_content': 'native_text' if kind == 'text' else 'whole_image'}
        if kind == 'image':
            item['asset_id'] = element['asset_id']
        elements.append(item)
    counts = {kind: dict(sorted(Counter(e['role'] for e in elements if e['kind'] == kind).items()))
              for kind in ROLES}
    result = {'page_id': page['page_id'], 'revision_id': page['revision_id'],
              'scene_hash': digest(scene), 'text_count': sum(counts['text'].values()),
              'image_count': sum(counts['image'].values()), 'role_counts': counts, 'elements': elements}
    if page.get('scene_hash') not in (None, result['scene_hash']):
        raise ValueError('Export report Scene hash mismatch')
    # A page background is not included in the count of movable image objects.
    background = scene.get('background')
    if background:
        result['background'] = {'kind': background['kind']}
        if background['kind'] == 'image':
            result['background']['asset_id'] = background['asset_id']
    return result


def font_notices(fmt, visual):
    """Differentiate recipient requirements from observed renderer warnings."""
    notices = []
    if fmt == 'pptx':
        notices.append({'code': 'PPTX_FONT_NOT_EMBEDDED', 'scope': 'recipient',
                        'family': 'Noto Sans CJK SC'})
    allowed = {'FONT_FAMILY_UNAVAILABLE', 'FONT_AVAILABILITY_UNVERIFIED'}
    for code in sorted(set(visual.get('target_engine', {}).get('warnings', [])) & allowed):
        notices.append({'code': code, 'scope': 'verification_renderer'})
    return notices
