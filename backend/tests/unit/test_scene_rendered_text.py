"""Real PDF parser checks; synthetic drawing instructions are not Office QA."""
from types import SimpleNamespace
from pathlib import Path
import hashlib
import json

import fitz
import pytest

from backend.tests.scene_layout_fixtures import single_line_layout
from services.exports.quality_gate import FONT
from services.exports.rendered_text import TargetRenderError, verify_rendered_pptx
from services.scene.validation import blank_scene, digest


def fixture(source='Hello world\nAgain 123', frame=None):
    scene = blank_scene()
    element = {'id': 'text', 'kind': 'text', 'role': 'body', 'text': source,
        'frame': frame or {'x': 20, 'y': 20, 'w': 400, 'h': 200, 'rotation_deg': 0},
        'style': {'font_size_pt': 20, 'font_weight': 400, 'line_height': 1.2,
            'padding_pt': 4, 'align': 'left', 'vertical_align': 'top', 'color': '#17324D'}}
    scene['elements'] = [element]
    snapshot = SimpleNamespace(manifest_json={'canvas': scene['canvas'], 'pages': [
        {'page_id': 'page', 'revision_id': 'revision', 'scene_hash': digest(scene)}]})
    revisions = {'revision': SimpleNamespace(scene_json=scene)}
    return snapshot, revisions, single_line_layout(snapshot, revisions)


def draw(lines, *, position=(24, 50), step=24, size=20, font=True, render_mode=0,
         clip=None, pages=1, canvas=(960, 540)):
    with fitz.open() as doc:
        page = doc.new_page(width=canvas[0], height=canvas[1])
        if font:
            page.insert_font(fontname='Scene', fontfile=str(FONT))
        for index, line in enumerate(lines):
            if line:
                page.insert_text((position[0], position[1] + index * step), line,
                    fontname='Scene' if font else 'helv', fontsize=size, render_mode=render_mode)
        if clip:
            for xref in page.get_contents():
                doc.update_stream(xref, b'q ' + clip + b' re W n ' + doc.xref_stream(xref) + b' Q')
        # MuPDF's synthetic Noto cmap chooses NBSP for the shared space glyph.
        # Make the fixture's ToUnicode describe the requested U+0020, rather
        # than weakening the production exact-space check.
        for entry in page.get_fonts():
            kind, reference = doc.xref_get_key(entry[0], 'ToUnicode')
            if kind == 'xref':
                xref = int(reference.split()[0])
                mapping = doc.xref_stream(xref)
                doc.update_stream(xref, mapping.replace(b'<00a0>', b'<0020>'))
        for _ in range(1, pages):
            doc.new_page(width=canvas[0], height=canvas[1])
        return doc.tobytes()


def test_actual_trace_preserves_spaces_and_checks_blank_paragraph_geometry():
    args = fixture('A B\n\nC D\n')
    result = verify_rendered_pptx(draw(['A B', '', 'C D', '']), *args)
    assert result['status'] == 'passed' and len(result['rendered_pdf_sha256']) == 64
    element = result['pages'][0]['elements'][0]
    assert element['codepoints_checked'] == 6 and element['layout_lines'] == 4
    assert [p['source_line_index'] for p in element['rendered_lines']] == [0, 2]


@pytest.mark.parametrize(('source', 'actual'), [('A B', 'AB'), ('AB', 'A B'), ('Hello', 'Hallo'),
    ('Á', 'Á'), ('Hello', ''), ('Hello', 'HelloHello')])
def test_rendered_text_loss_extra_spaces_and_normalization_are_not_waivable(source, actual):
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw([actual]), *fixture(source))
    assert caught.value.code == 'PPTX_RENDER_TEXT_MISMATCH'


@pytest.mark.parametrize(('source', 'actual', 'step'), [
    ('ABCD', ['AB', 'CD'], 24), ('AB\nCD', ['ABCD'], 24),
    ('AB\n\nCD', ['AB', 'CD'], 24), ('AB\nCD', ['AB', 'CD'], 40)])
def test_changed_wrap_and_blank_line_or_spacing_rejected(source, actual, step):
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(actual, step=step), *fixture(source))
    assert caught.value.code == 'PPTX_RENDER_REFLOW'


@pytest.mark.parametrize('position', [(10, 50), (410, 50), (24, 25), (24, 230)])
def test_tight_glyph_bounds_cannot_escape_frozen_frame(position):
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(['Hello'], position=position), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_CLIPPED'


def test_actual_pdf_clip_cannot_hide_text_while_extraction_still_contains_it():
    # Only the left part of the H is painted. Trace and char boxes alone still
    # contain the entire original text and would incorrectly accept this PDF.
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(['Hello'], clip=b'0 450 30 90'), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_CLIPPED'


@pytest.mark.parametrize('options', [{'font': False}, {'size': 18}])
def test_font_substitution_or_implicit_shrinking_blocked(options):
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(['Hello'], **options), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_FONT_MISMATCH'


def test_hidden_text_and_unreadable_pdf_fail_closed():
    for payload in (draw(['Hello'], render_mode=3), b'not a PDF'):
        with pytest.raises(TargetRenderError) as caught:
            verify_rendered_pptx(payload, *fixture('Hello'))
        assert caught.value.code == 'PPTX_RENDER_UNVERIFIED'


@pytest.mark.parametrize('options', [{'pages': 2}, {'canvas': (800, 450)}])
def test_target_page_count_and_size_checked(options):
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(['Hello'], **options), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_PAGE_MISMATCH'


def test_secondary_ink_mapping_must_match_without_normalization(monkeypatch):
    monkeypatch.setattr('services.exports.rendered_text._ink_characters', lambda *_: [('X', (24, 24, 34, 44))])
    with pytest.raises(TargetRenderError) as caught:
        verify_rendered_pptx(draw(['Hello']), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_UNVERIFIED'


def test_empty_text_and_blank_paragraphs_keep_native_contract_without_fabricated_ink():
    for source in ('', '\n', '\n\n'):
        result = verify_rendered_pptx(draw([]), *fixture(source))
        assert result['pages'][0]['elements'][0]['painted_lines_checked'] == 0


def test_unknown_nested_text_form_does_not_bypass_inherited_clipping():
    payload = draw(['Hello'])
    with fitz.open(stream=payload, filetype='pdf') as source, fitz.open() as target:
        page = target.new_page(width=960, height=540)
        page.show_pdf_page(page.rect, source, 0)
        with pytest.raises(TargetRenderError) as caught:
            verify_rendered_pptx(target.tobytes(), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_UNVERIFIED'


def test_nonrectangular_text_clip_is_not_assumed_to_be_its_bounding_box():
    with fitz.open(stream=draw(['Hello']), filetype='pdf') as doc:
        for xref in doc[0].get_contents():
            doc.update_stream(xref, b'q 0 0 m 200 0 l 50 540 l h W n ' + doc.xref_stream(xref) + b' Q')
        with pytest.raises(TargetRenderError) as caught:
            verify_rendered_pptx(doc.tobytes(), *fixture('Hello'))
    assert caught.value.code == 'PPTX_RENDER_UNVERIFIED'


def test_real_libreoffice_regression_fixture_preserves_unicode_and_all_six_text_frames():
    root = Path(__file__).resolve().parents[3] / 'docs/fixtures'
    source = json.loads((root / 'scene_pptx_libreoffice_text.json').read_text(encoding='utf-8'))
    payload = (root / 'scene_pptx_libreoffice_text.pdf').read_bytes()
    assert hashlib.sha256(payload).hexdigest() == source['provenance']['rendered_pdf_sha256']
    result = verify_rendered_pptx(payload, SimpleNamespace(manifest_json=source['manifest']),
        {'revision': SimpleNamespace(scene_json=source['scene'])}, source['layout'])
    assert result['status'] == 'passed'
    assert [e['codepoints_checked'] for e in result['pages'][0]['elements']] == [21, 0, 3, 0, 2, 17]
