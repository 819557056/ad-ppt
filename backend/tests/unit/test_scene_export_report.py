"""Every PDF/PPTX report traces each source object's role and editability."""
from copy import deepcopy

import pytest

from services.exports.report import page_inventory, font_notices
from services.scene.validation import digest


def fixture():
    scene = {'background': {'kind': 'image', 'asset_id': 'background-asset'}, 'elements': [
        {'id': 't1', 'kind': 'text', 'role': 'title', 'text': 'Private title'},
        {'id': 'i1', 'kind': 'image', 'role': 'chart', 'asset_id': 'chart-asset'},
        {'id': 't2', 'kind': 'text', 'role': 'annotation', 'text': 'Private label'},
        {'id': 'i2', 'kind': 'image', 'role': 'icon', 'asset_id': 'icon-asset'},
    ]}
    return {'page_id': 'page', 'revision_id': 'revision', 'scene_hash': digest(scene)}, scene


def test_inventory_retains_source_roles_layer_order_and_revision_without_copying_text():
    page, scene = fixture()
    before = deepcopy(scene)
    result = page_inventory(page, scene)
    assert result['scene_hash'] == page['scene_hash'] and result['revision_id'] == 'revision'
    assert result['text_count'] == result['image_count'] == 2
    assert result['role_counts'] == {'text': {'title': 1, 'annotation': 1}, 'image': {'chart': 1, 'icon': 1}}
    assert [e['element_id'] for e in result['elements']] == ['t1', 'i1', 't2', 'i2']
    assert [e['z_index'] for e in result['elements']] == [0, 1, 2, 3]
    assert result['elements'][1]['editable_content'] == 'whole_image'
    assert result['elements'][1]['asset_id'] == 'chart-asset'
    assert result['background']['asset_id'] == 'background-asset'
    assert all('text' not in element for element in result['elements']) and scene == before


def test_inventory_rejects_hash_and_unknown_role():
    page, scene = fixture()
    with pytest.raises(ValueError, match='hash mismatch'):
        page_inventory({**page, 'scene_hash': '0' * 64}, scene)
    scene['elements'][0]['role'] = 'arbitrary'
    with pytest.raises(ValueError, match='role invalid'):
        page_inventory(page, scene)


def test_font_notices_distinguish_recipient_requirements_and_observed_renderer_warnings():
    report = {'target_engine': {'warnings': ['FONT_FAMILY_UNAVAILABLE', 'untrusted warning', 'FONT_FAMILY_UNAVAILABLE']}}
    assert font_notices('pdf', {}) == []
    assert font_notices('pptx', report) == [
        {'code': 'PPTX_FONT_NOT_EMBEDDED', 'scope': 'recipient', 'family': 'Noto Sans CJK SC'},
        {'code': 'FONT_FAMILY_UNAVAILABLE', 'scope': 'verification_renderer'}]
