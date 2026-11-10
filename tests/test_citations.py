"""Source identity, attribution and honest locators matter more than cosmetic polish."""
from __future__ import annotations
import json
from pathlib import Path
import zipfile

import pytest

from book_library.citations import cite_passage, validate_bibliography
from book_library.service import Library
from book_library.storage import append_jsonl, open_sqlite
from test_service import docx, seed
import test_extract


def bib(lib, target, value, status='confirmed', origin='user'):
    # Durable edit fixtures also exercise citations before an index refresh.
    return lib.annotate(target, {'field': 'bibliography', 'value': value,
                                'status': status, 'origin': origin,
                                'evidence': 'Inspected title/copyright page and contribution byline in this fixture.'})


def fixture(tmp_path):
    root = tmp_path / 'books'
    source = docx(root / 'Book' / 'book.docx', [('Heading1', 'Learning in Community'), 'Copper birds gather near the water.'])
    lib = seed(root, [{'title': 'The Shared Classroom: Learning Together', 'author': 'Jane Doe', 'paths': [source]}])
    hit = lib.search('copper birds')['direct'][0]
    return lib, hit, source


def verified_book(lib, hit, **patch):
    value = {'title': 'The Shared Classroom: Learning Together', 'short_title': 'Shared Classroom',
             'authors': [{'given': 'Jane', 'family': 'Doe'}], 'publisher': 'Example Press', 'year': '2024',
             'format': 'EPUB', **patch}
    bib(lib, hit['work_id'], value)


def page(value='42'):
    return {'type': 'page', 'value': value, 'verified': True, 'evidence': 'Inspected printed pagination in this edition.'}


def test_source_byline_bibliography_respects_field_corrections_and_name_uncertainty(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit, authors=[], editors=[{'given': 'Volume', 'family': 'Editor'}])
    event = {'origin': 'source_byline', 'status': 'confirmed', 'evidence': 'Closing byline matches this volume’s contributor roster.',
             'value': {'type': 'chapter', 'title': 'Learning in Community', 'authors': [{'literal': 'Ronald J. Allen'}],
                       'locator': {'type': 'section', 'value': 'Learning in Community', 'verified': True, 'evidence': 'Source heading'}}}
    with open_sqlite(lib.state/'generations'/lib._active()['database']) as db:
        data = json.loads(db.execute('SELECT data FROM sections WHERE id=?', (hit['section_id'],)).fetchone()[0])
        data['source_bibliography'] = event
        db.execute('UPDATE sections SET data=? WHERE id=?', (json.dumps(data), hit['section_id']))
        db.commit()
    citation = cite_passage(lib, hit['id'])
    assert citation['full_note'].startswith('Ronald J. Allen, “Learning in Community,”')
    assert 'section.name_order' in citation['review_fields']
    assert citation['provenance']['section.authors']['origin'] == 'source_byline'
    assert lib.bibliography_record(hit['section_id'])['record']['authors'] == [{'literal': 'Ronald J. Allen'}]
    bib(lib, hit['section_id'], {'authors': [{'given': 'Ronald J.', 'family': 'Allen'}]})
    corrected = cite_passage(lib, hit['id'])
    assert corrected['ready']
    assert corrected['bibliography'].startswith('Allen, Ronald J.')
    assert corrected['provenance']['section.authors']['origin'] == 'user'
    assert corrected['provenance']['section.title']['origin'] == 'source_byline'


def test_verified_book_citation_works_before_sync_and_has_rich_formats(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit)
    c = cite_passage(lib, hit['id'], page('42–43'))
    assert c['ready'] and not c['needs_review']
    assert c['full_note'] == 'Jane Doe, The Shared Classroom: Learning Together (Example Press, 2024), 42–43, EPUB.'
    assert c['short_note'] == 'Doe, Shared Classroom, 42–43.'
    assert c['bibliography'] == 'Doe, Jane. The Shared Classroom: Learning Together. Example Press, 2024. EPUB.'
    assert '<i>The Shared Classroom: Learning Together</i>' in c['html']['full_note']
    assert '*Shared Classroom*' in c['markdown']['short_note']
    assert '42' not in c['bibliography']
    assert c['source_identity']['edition_id']
    assert c['links']['source_files'] == hit['source_paths']
    assert 'http://127.0.0.1' not in c['full_note']
    assert c['citation_id'] == cite_passage(lib, hit['id'], page('42–43'))['citation_id']


def test_chapter_author_is_distinct_from_volume_editors(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit, authors=[], editors=[{'given': 'Avery', 'family': 'Editor'}])
    bib(lib, hit['section_id'], {'type': 'chapter', 'title': 'Learning in Community', 'short_title': 'Learning',
                                'authors': [{'given': 'Ruth', 'family': 'Writer'}]})
    c = cite_passage(lib, hit['id'], page())
    assert c['ready']
    assert c['full_note'] == 'Ruth Writer, “Learning in Community,” in The Shared Classroom: Learning Together, ed. Avery Editor (Example Press, 2024), 42, EPUB.'
    assert c['bibliography'] == 'Writer, Ruth. “Learning in Community.” In The Shared Classroom: Learning Together, edited by Avery Editor. Example Press, 2024. EPUB.'
    assert c['short_note'] == 'Writer, “Learning,” 42.'
    assert 'Editor, Avery' not in c['bibliography']


@pytest.mark.parametrize('count', [2, 3, 6, 7])
@pytest.mark.parametrize('contribution', [False, True])
def test_bibliography_editor_credits_use_natural_order_and_six_name_limit(tmp_path, count, contribution):
    lib, hit, _ = fixture(tmp_path)
    editors = [{'given': f'Given{i}', 'family': f'Editor{i}'} for i in range(count)]
    verified_book(lib, hit, editors=editors)
    if contribution:
        bib(lib, hit['section_id'], {'type': 'chapter', 'title': 'Learning in Community',
                                    'authors': [{'given': 'Ruth', 'family': 'Writer'}]})
    c = cite_passage(lib, hit['id'], page())
    names = [f'Given{i} Editor{i}' for i in range(count)]
    expected = (', '.join(names[:3]) + ', et al.' if count > 6 else
                ' and '.join(names) if count == 2 else ', '.join(names[:-1]) + ', and ' + names[-1] + '.')
    if count == 2:
        expected += '.'
    credit = 'edited by ' if contribution else 'Edited by '
    assert credit + expected in c['bibliography']
    assert 'Editor0, Given0' not in c['bibliography']
    assert ', ed. ' + (names[0] + ' et al.' if count > 2 else ' and '.join(names)) + ' (' in c['full_note']
    assert credit + expected in c['html']['bibliography']
    assert credit + expected in c['markdown']['bibliography']


def test_bibliography_translator_credit_does_not_use_note_name_limit(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit, translators=[{'given': f'Given{i}', 'family': f'Translator{i}'} for i in range(3)])
    c = cite_passage(lib, hit['id'], page())
    assert 'trans. Given0 Translator0 et al.' in c['full_note']
    assert 'Translated by Given0 Translator0, Given1 Translator1, and Given2 Translator2.' in c['bibliography']


def test_unstructured_anthology_attribution_cannot_become_poem_author(tmp_path):
    root = tmp_path / 'books'
    source = docx(root / 'Poems' / 'poems.docx', [('Heading1', 'River Poem'), 'Copper birds gather near the water.'])
    lib = seed(root, [{'title': 'An Anthology', 'author': 'Volume Editor', 'paths': [source]}])
    hit = lib.search('copper birds')['direct'][0]
    bib(lib, hit['work_id'], {'title': 'An Anthology', 'authors': [], 'editors': [{'given': 'Volume', 'family': 'Editor'}], 'publisher': 'Press', 'year': 2020})
    c = cite_passage(lib, hit['id'], page())
    assert c['needs_review']
    assert c['full_note'].startswith('Volume Editor, ed., An Anthology')
    assert 'section.authorship' in c['review_fields']
    assert 'River Poem' not in c['full_note']
    assert c['bibliography'].startswith('Editor, Volume, ed. An Anthology.')
    assert 'ed..' not in c['bibliography']


def test_translated_work_preserves_eastern_name_order(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit, authors=[{'literal': 'Liu Xinwu', 'short': 'Liu'}],
                  translators=[{'given': 'Jeremy', 'family': 'Tiang'}])
    c = cite_passage(lib, hit['id'], page())
    assert c['ready']
    assert c['bibliography'].startswith('Liu Xinwu.')
    assert 'Xinwu, Liu' not in c['bibliography']
    assert c['short_note'].startswith('Liu,')
    assert 'trans. Jeremy Tiang' in c['full_note']
    assert 'Translated by Jeremy Tiang.' in c['bibliography']


def test_multi_author_rules_do_not_reorder_or_guess_names(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    authors = [{'given': f'Person{i}', 'family': f'Family{i}'} for i in range(7)]
    verified_book(lib, hit, authors=authors)
    c = cite_passage(lib, hit['id'], page())
    assert c['full_note'].startswith('Person0 Family0 et al.,')
    assert c['short_note'].startswith('Family0 et al.,')
    assert c['bibliography'].startswith('Family0, Person0, Person1 Family1, Person2 Family2, et al.')
    assert 'Family3' not in c['bibliography']


def test_unknown_metadata_is_visible_and_word_ordinals_are_not_pages(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    c = cite_passage(lib, hit['id'])
    assert c['needs_review'] and not c['ready']
    assert set(c['missing_fields']) >= {'publisher', 'year'}
    assert '[publisher unverified]' in c['full_note']
    assert '[year unverified]' in c['full_note']
    assert c['locator']['type'] == 'section'
    assert 'Learning in Community' in c['full_note']
    assert 'block ' not in c['full_note']
    assert c['locator']['verified'] is False
    unverified = cite_passage(lib, hit['id'], '42')
    assert unverified['needs_review'] and unverified['locator']['verified'] is False


def test_source_epub_page_markers_survive_without_opening_archive(tmp_path, monkeypatch):
    root = tmp_path / 'books'; root.mkdir()
    package = test_extract.ExtractTests(); package.root = root
    package.epub({'chapter.xhtml': '<h1>Water and Place</h1><span epub:type="pagebreak" id="p42" title="42"/><p>Copper birds gather by water.</p><span epub:type="pagebreak" id="p43" title="43"/><p>The community assembles.</p>'}, ['chapter.xhtml'])
    lib = Library(root); lib.sync(); hit = lib.search('copper birds')['direct'][0]
    verified_book(lib, hit)
    monkeypatch.setattr(zipfile, 'ZipFile', lambda *a, **k: pytest.fail('Citation opened an original archive'))
    c = cite_passage(lib, hit['id'])
    assert c['locator']['type'] == 'page'
    assert c['locator']['value'] == '42–43'
    assert c['locator']['origin'] == 'source_page_markers'
    assert c['ready']


def test_terminal_epub_pagebreak_does_not_extend_passage_page_range(tmp_path):
    root = tmp_path / 'books'; root.mkdir()
    package = test_extract.ExtractTests(); package.root = root
    package.epub({'chapter.xhtml': '<span epub:type="pagebreak" id="p177" title="177"/><h1>River Poem</h1><p>Copper birds gather by water.</p><span epub:type="pagebreak" id="p178" title="178"/>'}, ['chapter.xhtml'])
    lib = Library(root); lib.sync(); hit = lib.search('copper birds')['direct'][0]
    c = cite_passage(lib, hit['id'])
    assert c['locator']['value'] == '177'
    assert c['locator']['source_labels'] == ['177']


def test_citation_rejects_stale_sources_and_retains_identity_after_rename(tmp_path):
    lib, hit, source = fixture(tmp_path); verified_book(lib, hit)
    first = cite_passage(lib, hit['id'], page())
    renamed = source.with_name('Descriptive title.docx'); source.rename(renamed)
    with pytest.raises(ValueError, match='changed or disappeared'):
        cite_passage(lib, hit['id'], page())
    lib.sync()
    again = cite_passage(lib, hit['id'], page())
    assert again['citation_id'] == first['citation_id']
    assert again['links']['source_files'] == [str(renamed)]
    docx(renamed, [('Heading1', 'Changed'), 'A different text.'])
    with pytest.raises(ValueError, match='changed or disappeared'):
        cite_passage(lib, hit['id'], page())


def test_html_and_markdown_escape_source_strings_and_bad_urls_rejected(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit, title='<script>alert(1)</script> *Study*', publisher='A & B <Press>')
    c = cite_passage(lib, hit['id'], page())
    assert '<script>' not in c['html']['full_note']
    assert '&lt;script&gt;' in c['html']['full_note']
    assert '\\*Study\\*' in c['markdown']['full_note']
    for invalid in [{'url': 'javascript:alert(1)'}, {'url': 'https://secret:token@example.org'}, {'authors': [{'given': 'Only Given'}]}, {'authors': [{'literal': 'Group', 'family': 'Name'}]}, {'year': True}, {'locator': {'type': 'page', 'value': 'block 42', 'verified': True, 'evidence': 'guess'}}]:
        with pytest.raises(ValueError):
            validate_bibliography(invalid)


def test_user_bibliographic_correction_survives_later_codex_patch_and_rebuild(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    verified_book(lib, hit)
    bib(lib, hit['work_id'], {'publisher': 'Wrong Press', 'year': '1900'}, origin='codex')
    bib(lib, hit['work_id'], {'edition': '2nd ed.'}, origin='codex')
    before = cite_passage(lib, hit['id'], page())
    assert 'Example Press, 2024' in before['full_note']
    assert '2nd ed.' in before['full_note']
    lib.sync(rebuild=True)
    assert cite_passage(lib, hit['id'], page())['full_note'] == before['full_note']


def test_edition_date_discrepancy_stays_visible_until_explicitly_resolved(tmp_path):
    lib, hit, _ = fixture(tmp_path)
    cat = lib._catalog(); cat['works'][hit['work_id']]['metadata']['edition_note'] = '2026 edition preface but older copyright notice.'
    from book_library.storage import atomic_json
    atomic_json(lib.state/'catalog.json', cat)
    verified_book(lib, hit)
    c = cite_passage(lib, hit['id'], page())
    assert c['needs_review'] and 'edition_context' in c['review_fields']
    bib(lib, hit['work_id'], {'year': '2026', 'edition': 'Revised edition'})
    c = cite_passage(lib, hit['id'], page())
    assert c['ready']
    assert '2026' in c['full_note']
    assert any('older copyright' in w for w in c['warnings'])
