"""Deterministic review metadata from the frozen base, never model-written prose."""


def _entry(element):
    text = element.get('text') if element['kind'] == 'text' else element.get('alt_text')
    label = ' '.join((text or '').split())
    return {'element_id': element['id'], 'kind': element['kind'], 'role': element['role'],
            'label': label[:80] + ('…' if len(label) > 80 else '')}


def summarize_candidate(base_scene, scene, base_revision_id, *, has_generated_image=False):
    """Compare validated scenes; retain IDs even when every display label is identical.

    Inserting/deleting an object alone is not a layer reorder. Changed fields are
    sorted for reproducibility; lists retain the source/destination layer order.
    """
    before = {element['id']: element for element in (base_scene or {}).get('elements', [])}
    after = {element['id']: element for element in scene['elements']}
    shared_before = [element_id for element_id in before if element_id in after]
    shared_after = [element_id for element_id in after if element_id in before]
    modified = []
    for element_id, element in after.items():
        if element_id not in before:
            continue
        previous = before[element_id]
        fields = sorted(field for field in previous.keys() | element.keys()
                        if previous.get(field) != element.get(field))
        if fields:
            modified.append({**_entry(element), 'changed_fields': fields})
    return {
        'schema_version': 1, 'base_revision_id': base_revision_id,
        'text_count': sum(element['kind'] == 'text' for element in after.values()),
        'image_count': sum(element['kind'] == 'image' for element in after.values()),
        'added': [_entry(element) for key, element in after.items() if key not in before],
        'removed': [_entry(element) for key, element in before.items() if key not in after],
        'modified': modified,
        'background_changed': (base_scene or {}).get('background') != scene['background'],
        'layer_order_changed': shared_before != shared_after,
        'warnings': ['IMAGE_TEXT_UNVERIFIED'] if has_generated_image else [],
    }
