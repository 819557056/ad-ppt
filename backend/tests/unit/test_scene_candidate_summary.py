"""Offline review-diff tests; no renderer or provider required."""
from copy import deepcopy

from services.scene.candidate_summary import summarize_candidate


def element(element_id, **changes):
    return {'id': element_id, 'kind': 'text', 'role': 'title', 'text': '原文',
            'frame': {'x': 0, 'y': 0, 'w': 100, 'h': 30}, **changes}


def scene(*elements, color='#FFFFFF'):
    return {'elements': list(elements), 'background': {'kind': 'solid', 'color': color}}


def test_lists_added_removed_modified_by_identity_without_mutation():
    original = scene(element('kept'), element('removed', text='旧标题'), element('changed'))
    proposal = scene(element('new', text='新标题'), element('kept'),
                     element('changed', text='新正文', role='body'), color='#000000')
    copies = deepcopy((original, proposal))
    result = summarize_candidate(original, proposal, 'frozen-revision')
    assert result['base_revision_id'] == 'frozen-revision'
    assert [item['element_id'] for item in result['added']] == ['new']
    assert result['removed'][0]['label'] == '旧标题'
    assert result['modified'] == [{
        'element_id': 'changed', 'kind': 'text', 'role': 'body', 'label': '新正文',
        'changed_fields': ['role', 'text']}]
    assert result['background_changed'] is True
    assert result['layer_order_changed'] is False
    assert (original, proposal) == copies
    assert summarize_candidate(original, proposal, 'frozen-revision') == result


def test_reorder_is_distinct_from_addition_or_removal_and_noop():
    original = scene(element('one'), element('two'))
    same = summarize_candidate(original, deepcopy(original), 'base')
    assert not any(same[key] for key in ('added', 'removed', 'modified',
                                       'background_changed', 'layer_order_changed'))
    reordered = summarize_candidate(original, scene(element('two'), element('one')), 'base')
    assert reordered['layer_order_changed'] is True
    assert reordered['modified'] == []


def test_empty_base_and_bounded_labels_and_generated_image_warning():
    result = summarize_candidate(None, scene(element('text', text='行\n' + '字' * 100),
        element('image', kind='image', role='illustration', text=None, alt_text='插画')),
        None, has_generated_image=True)
    assert (result['text_count'], result['image_count']) == (1, 1)
    assert len(result['added'][0]['label']) == 81
    assert '\n' not in result['added'][0]['label']
    assert result['added'][1]['label'] == '插画'
    assert result['warnings'] == ['IMAGE_TEXT_UNVERIFIED']
    assert result['removed'] == [] and result['modified'] == []


def test_image_replacement_and_kind_change_are_not_lost():
    before = scene(element('image', kind='image', role='photo', asset_id='old', alt_text='旧图'))
    after = deepcopy(before)
    after['elements'][0].update(asset_id='new', opacity=0.5)
    result = summarize_candidate(before, after, 'base')
    assert result['modified'][0]['changed_fields'] == ['asset_id', 'opacity']
    assert summarize_candidate(before, scene(element('image')), 'base')['modified'][0]['kind'] == 'text'
