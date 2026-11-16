"""Synthetic source indexes omitted from an EPUB's packaged navigation."""
from copy import deepcopy
from types import SimpleNamespace
from zipfile import ZipFile
import json

import pytest

from book_library.extract import extract_file
from book_library.structure import prepare_work
from test_service import seed


def epub(path, bodies, nav='<a href="index.xhtml#notes">Study Notes</a>'):
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf"/></rootfiles></container>')
        manifest = ''.join(f'<item id="m{i}" href="{member}" media-type="application/xhtml+xml"/>' for i, member in enumerate(bodies))
        spine = ''.join(f'<itemref idref="m{i}"/>' for i in range(len(bodies)))
        z.writestr('EPUB/package.opf', f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>{manifest}<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine>{spine}</spine></package>')
        z.writestr('EPUB/nav.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol><li>' + nav + '</li></ol></nav></body></html>')
        for member, body in bodies.items():
            z.writestr('EPUB/' + member, '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + body + '</body></html>')
    return path


def prepare(path, state):
    extraction = extract_file(path)
    original = deepcopy(extraction['blocks'])
    file = {'id': 'file_test', 'sha256': 'synthetic', 'path': str(path), 'available': True, 'state': 'ready'}
    report = prepare_work(SimpleNamespace(state=state), {'id': 'work_test', 'title': 'Synthetic Notes'},
                          [file], {file['id']: extraction})
    assert [(b['text'], b['locator'], b['ordinal']) for b in extraction['blocks']] == [
        (b['text'], b['locator'], b['ordinal']) for b in original]
    return extraction['blocks'], report


def note_bodies():
    return {
        'index.xhtml': '<div id="notes"><p>Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p><p><a href="beta.xhtml#beta">Beta Study Notes</a></p></div>',
        'alpha.xhtml': '<div id="alpha"><p>Alpha <a href="index.xhtml#notes">Study Notes</a></p><p><a href="alpha1.xhtml#a1">1</a> <a href="alpha2.xhtml#a2">2</a></p></div>',
        'alpha1.xhtml': '<div id="a1"><p><a href="verses.xhtml#v1">1:1</a> An opening explanation.</p><p>An adjacent explanation.</p></div>',
        'alpha2.xhtml': '<div id="a2"><p><a href="verses.xhtml#v2">2:1</a> A second explanation.</p></div>',
        'beta.xhtml': '<div id="beta"><p>Beta Study Notes</p><p><a href="beta1.xhtml#b1">1</a></p></div>',
        'beta1.xhtml': '<div id="b1"><p><a href="verses.xhtml#v3">1:1</a> A different book explanation.</p></div>',
    }


def test_linked_source_index_recovers_bounded_note_books_without_rewriting_prose(tmp_path):
    root = tmp_path / 'books'
    path = epub(root / 'Notes.epub', note_bodies())
    source, report = prepare(path, tmp_path / 'structure')
    headings = [b for b in source if b['kind'] == 'heading' and not b.get('is_navigation')]
    assert [b['text'] for b in headings] == ['Study Notes', 'Alpha Study Notes', 'Beta Study Notes']
    assert headings[0]['structure_role'] == 'group'
    assert headings[1]['heading_path'] == ['Study Notes', 'Alpha Study Notes']
    assert headings[2]['structure_parent_key'] == f"file_test:{headings[0]['ordinal']}"
    evidence = headings[1]['heading_evidence']
    assert evidence['origin'] == 'linked_navigation_index'
    assert evidence['contents']['locator']['member'] == 'EPUB/index.xhtml'
    assert evidence['source']['locator']['member'] == 'EPUB/alpha.xhtml'
    assert not any(i['code'] == 'unresolved_navigation_target' for i in report['issues'])
    assert all(b['kind'] == 'paragraph' for b in source if b['locator'].get('member') in {
        'EPUB/alpha1.xhtml', 'EPUB/alpha2.xhtml', 'EPUB/beta1.xhtml'})

    # Even a work without an editor credit must keep the indexed books apart.
    lib = seed(root, [{'title': 'Synthetic Notes', 'paths': [path]}])
    with lib._db() as db:
        units = {r['title']: r for r in db.execute('SELECT * FROM reading_units')}
        assert set(units) == {'Study Notes', 'Alpha Study Notes', 'Beta Study Notes'}
        alpha = units['Alpha Study Notes']
        texts = [r[0] for r in db.execute('SELECT b.text FROM unit_blocks u JOIN blocks b ON b.id=u.block_id WHERE unit_id=? ORDER BY u.ordinal', (alpha['id'],))]
        assert any('An opening explanation.' in t for t in texts)
        assert 'An adjacent explanation.' in texts
        assert any('A second explanation.' in t for t in texts)
        assert not any('A different book explanation.' in t for t in texts)
        assert json.loads(alpha['data'])['attribution_verified'] is False
        first_ids = {title: row['id'] for title, row in units.items()}
    lib.sync(rebuild=True, semantic=False)
    with lib._db() as db:
        assert {r['title']: r['id'] for r in db.execute('SELECT id,title FROM reading_units')} == first_ids


@pytest.mark.parametrize('index_body', [
    '<p id="notes">Study Notes</p><p>See <a href="alpha.xhtml#alpha">Alpha Study Notes</a> for a discussion.</p>',
    '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p><p>Substantive introduction.</p>',
    '<p>Prior substantive text.</p><p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p>',
])
def test_substantive_members_are_not_treated_as_nested_indexes(tmp_path, index_body):
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': index_body,
        'alpha.xhtml': '<p id="alpha">Alpha Study Notes</p><p>Ordinary discussion.</p>',
    })
    source, _ = prepare(path, tmp_path / 'state')
    assert next(b for b in source if b['locator'].get('member') == 'EPUB/alpha.xhtml')['kind'] == 'paragraph'


@pytest.mark.parametrize('destination', [
    '<p id="alpha">A different title</p><p>Alpha Study Notes</p>',
    '<p id="alpha">Alpha Study Notes</p><p id="alpha">Alpha Study Notes</p>',
    '<p>Alpha Study Notes</p>',
])
def test_linked_index_requires_exact_unique_anchored_title(tmp_path, destination):
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p>',
        'alpha.xhtml': destination,
    })
    source, _ = prepare(path, tmp_path / 'state')
    assert all(b['kind'] == 'paragraph' for b in source if b['locator'].get('member') == 'EPUB/alpha.xhtml')
    assert next(b for b in source if b['text'] == 'Study Notes' and not b.get('is_navigation'))['structure_role'] == 'section'


def test_nested_index_recovery_preserves_hierarchy_and_terminates_backlinks(tmp_path):
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Chapter One</a></p>',
        'alpha.xhtml': '<p id="alpha">Chapter One</p><p><a href="index.xhtml#notes">Study Notes</a></p><p><a href="opening.xhtml#opening">Opening Discussion</a></p>',
        'opening.xhtml': '<p id="opening">Opening Discussion</p><p>Substantive explanation.</p>',
    })
    source, _ = prepare(path, tmp_path / 'state')
    opening = next(b for b in source if b['locator'].get('member') == 'EPUB/opening.xhtml')
    assert opening['heading_path'] == ['Study Notes', 'Chapter One', 'Opening Discussion']
    alpha = next(b for b in source if b['locator'].get('member') == 'EPUB/alpha.xhtml')
    assert alpha['structure_role'] == 'group'


def test_numeric_index_links_do_not_promote_body_paragraphs(tmp_path):
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">1</a></p>',
        'alpha.xhtml': '<p id="alpha">1</p><p>Opening prose.</p>',
    })
    source, _ = prepare(path, tmp_path / 'state')
    assert next(b for b in source if b['locator'].get('member') == 'EPUB/alpha.xhtml')['kind'] == 'paragraph'


@pytest.mark.parametrize('target_before_index', [False, True])
def test_global_index_links_do_not_reparent_another_section(tmp_path, target_before_index):
    index = '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p>'
    target = '<p id="alpha">Alpha Study Notes</p><p>Ordinary discussion.</p>'
    bodies = ({'alpha.xhtml': target, 'index.xhtml': index} if target_before_index else {
        'index.xhtml': index, 'appendix.xhtml': '<p id="appendix">Appendix</p><p>Other content.</p>',
        'alpha.xhtml': target})
    nav = '<a href="index.xhtml#notes">Study Notes</a>'
    if not target_before_index:
        nav += '</li><li><a href="appendix.xhtml#appendix">Appendix</a>'
    path = epub(tmp_path / 'source.epub', bodies, nav)
    source, _ = prepare(path, tmp_path / 'state')
    assert next(b for b in source if b['locator'].get('member') == 'EPUB/alpha.xhtml')['kind'] == 'paragraph'


@pytest.mark.parametrize('declare_nested', [False, True])
def test_conflicting_explicit_index_parents_do_not_choose_an_ancestry(tmp_path, declare_nested):
    nav = '<a href="index.xhtml#notes">Study Notes</a>'
    if declare_nested:
        nav += '<ol><li><a href="alpha.xhtml#alpha">Alpha Study Notes</a></li></ol>'
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p><p><a href="opening.xhtml#opening">Opening Discussion</a></p>',
        'alpha.xhtml': '<p id="alpha">Alpha Study Notes</p><p><a href="opening.xhtml#opening">Opening Discussion</a></p>',
        'opening.xhtml': '<p id="opening">Opening Discussion</p><p><a href="deep.xhtml#deep">Further Discussion</a></p>',
        'deep.xhtml': '<p id="deep">Further Discussion</p><p>Substantive explanation.</p>',
    }, nav)
    source, _ = prepare(path, tmp_path / 'state')
    assert next(b for b in source if b['locator'].get('member') == 'EPUB/opening.xhtml')['kind'] == 'paragraph'
    assert next(b for b in source if b['locator'].get('member') == 'EPUB/deep.xhtml')['kind'] == 'paragraph'


def test_unresolved_boilerplate_target_does_not_abort_linked_index_recovery(tmp_path):
    path = epub(tmp_path / 'source.epub', {
        'index.xhtml': '<p id="notes">Study Notes</p><p><a href="alpha.xhtml#alpha">Alpha Study Notes</a></p>',
        'alpha.xhtml': '<p id="alpha">Alpha Study Notes</p><p>Substantive discussion.</p>',
        'wrapper.xhtml': '<p id="wrapper">Removed wrapper text.</p>',
    }, '<a href="index.xhtml#notes">Study Notes</a></li><li><a href="wrapper.xhtml#wrapper">Unavailable Label</a>')
    extraction = extract_file(path)
    wrapper = next(b for b in extraction['blocks'] if b['locator'].get('member') == 'EPUB/wrapper.xhtml')
    wrapper['is_boilerplate'] = True
    file = {'id': 'file_test', 'sha256': 'synthetic', 'path': str(path), 'available': True, 'state': 'ready'}
    report = prepare_work(SimpleNamespace(state=tmp_path / 'state'), {'id': 'work_test', 'title': 'Synthetic Notes'},
                          [file], {file['id']: extraction})
    alpha = next(b for b in extraction['blocks'] if b['locator'].get('member') == 'EPUB/alpha.xhtml')
    assert alpha['heading_path'] == ['Study Notes', 'Alpha Study Notes']
    assert wrapper['is_boilerplate'] is True
    assert any(i['code'] == 'unresolved_navigation_target' and i['navigation_label'] == 'Unavailable Label'
               for i in report['issues'])
