from __future__ import annotations

import json

import pytest

from book_library.form_options import KINDS, suggestions
from book_library.service import Library
from book_library.storage import SCHEMA, append_jsonl, atomic_json, open_sqlite


def edit(lib, target, field, value, *, origin='user', status='confirmed'):
    append_jsonl(lib.state / 'curation.jsonl', {
        'target_id': target, 'field': field, 'value': value,
        'origin': origin, 'status': status, 'evidence': 'Inspected fixture credit.',
    })


@pytest.fixture
def library(tmp_path):
    lib = Library(tmp_path)
    path = lib.state / 'generations' / 'forms.sqlite3'
    path.parent.mkdir(parents=True)
    with open_sqlite(path) as db:
        db.executescript(SCHEMA)
        works = [
            {'id': 'book-one', 'title': 'Poetry and Theology', 'author': 'Volume Editor',
             'available': True, 'metadata': {'publisher': 'A Press', 'format': 'epub'},
             'bibliography': {'editors': [{'given': 'Volume', 'family': 'Editor'}]}},
            {'id': 'book-two', 'title': 'A very long title ' + 'with detail ' * 30,
             'author': 'René Krüger', 'available': True, 'metadata': {}},
            {'id': 'book-missing', 'title': 'Unavailable Book', 'author': 'Missing Person',
             'available': False, 'metadata': {'publisher': 'Missing Press'}},
        ]
        for work in works:
            db.execute('INSERT INTO works VALUES(?,?,?,?)', (work['id'], work['title'], work['author'], json.dumps(work)))
        section = {'id': 'section-one', 'title': 'A contribution', 'source_bibliography': {
            'status': 'confirmed', 'value': {'authors': [{'given': 'Chapter', 'family': 'Writer'}]}}}
        db.execute('INSERT INTO sections VALUES(?,?,?,?,?)', ('section-one', 'book-one', section['title'], 0, json.dumps(section)))
        db.execute('INSERT INTO passages(id,work_id,section_id,text,attribution,data) VALUES(?,?,?,?,?,?)',
                   ('passage-one', 'book-one', 'section-one', 'PRIVATE ORIGINAL BODY', 'Chapter Writer', '{}'))
        db.execute('INSERT INTO passages(id,work_id,section_id,text,attribution,data) VALUES(?,?,?,?,?,?)',
                   ('passage-missing', 'book-missing', 'missing-section', 'MISSING BODY', 'Missing Person', '{}'))
        for target, wid, facet, value in [
            ('book-one', 'book-one', 'subjects', 'biblical-studies'),
            ('section-one', 'book-one', 'subjects', 'biblical-studies'),
            ('book-one', 'book-one', 'forms', 'poem'),
            ('book-one', 'book-one', 'themes', 'Unusual lateral idea'),
            ('book-two', 'book-two', 'people', 'Thérèse'),
            ('book-missing', 'book-missing', 'subjects', 'missing-topic'),
        ]:
            db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)', (target, wid, facet, value, 'user', 'confirmed', 'Evidence'))
    atomic_json(lib.state / 'current.json', {'generation': 'forms', 'database': path.name})
    atomic_json(lib.state / 'taxonomy.json', {'terms': [
        {'id': 'biblical-studies', 'label': 'Biblical studies', 'aliases': ['Bible', 'Scripture']},
        {'id': 'poetry', 'label': 'Poetry', 'aliases': ['verse', 'poems']},
    ]})
    edit(lib, 'passage-one', 'bibliography', {
        'authors': [{'given': 'Passage', 'family': 'Scholar'}],
        'bible_translation': 'New Revised Standard Version', 'bible_translation_short': 'NRSV',
    })
    edit(lib, 'passage-missing', 'bibliography', {'authors': [{'literal': 'Unavailable Scholar'}]})
    append_jsonl(lib.state / 'usage.jsonl', {'project': 'Workshop', 'destination': 'Proper 26'})
    return lib


def values(result):
    return [option['value'] for option in result['options']]


def test_books_submit_ids_preserve_long_titles_and_search_author(library):
    result = suggestions(library, 'books', 'kruger')
    assert values(result) == ['book-two']
    assert len(result['options'][0]['label']) > 300
    assert result['options'][0]['detail'] == 'René Krüger'
    assert values(suggestions(library, 'books', 'book-two')) == ['book-two']
    assert 'book-missing' not in values(suggestions(library, 'books'))


def test_catalog_values_match_actual_catalog_search_inputs(library):
    result = suggestions(library, 'catalog', 'rene')
    assert 'René Krüger' in values(result)
    assert any(value.startswith('A very long title') for value in values(result))
    assert suggestions(library, 'catalog', 'Passage Scholar')['options'] == []


def test_catalog_creators_preserve_roles_and_alias_book_titles(library):
    edit(library, 'book-one', 'bibliography', {
        'authors': [{'given': 'Ada', 'family': 'Writer'}],
        'editors': [{'given': 'Mina', 'family': 'Editor'}],
        'translators': [{'literal': 'Liu Xinwu'}],
    })
    for name, role in [('Ada Writer', 'Author'), ('Mina Editor', 'Editor'), ('Liu Xinwu', 'Translator')]:
        result = suggestions(library, 'catalog', name)
        assert set(values(result)) == {name, 'Poetry and Theology'}
        person = next(option for option in result['options'] if option['value'] == name)
        assert person['detail'] == f'{role} · Poetry and Theology'
        assert values(suggestions(library, 'books', name)) == ['book-one']
        for option in result['options']:
            assert library.browse_books(query=option['value'])['books'][0]['id'] == 'book-one'
    assert suggestions(library, 'catalog', 'Chapter Writer')['options'] == []
    assert suggestions(library, 'catalog', 'Passage Scholar')['options'] == []


def test_catalog_creator_corrections_invalidate_cache_and_match_browse(library):
    edit(library, 'book-one', 'bibliography', {'authors': [{'literal': 'Original Writer'}]})
    assert 'Original Writer' in values(suggestions(library, 'catalog', 'Original Writer'))
    edit(library, 'book-one', 'bibliography', {'authors': [{'literal': 'Corrected Writer'}]})
    edit(library, 'book-one', 'bibliography', {'authors': [{'literal': 'Generated Replacement'}]}, origin='codex')
    assert set(values(suggestions(library, 'catalog', 'Corrected Writer'))) == {'Corrected Writer', 'Poetry and Theology'}
    assert suggestions(library, 'catalog', 'Original Writer')['options'] == []
    assert suggestions(library, 'catalog', 'Generated Replacement')['options'] == []
    assert library.browse_books(query='Corrected Writer')['total'] == 1
    assert library.browse_books(query='Original Writer')['total'] == 0
    assert library.books(query='Corrected Writer') == []  # Preserve the older list contract.


def test_untrusted_catalog_creator_is_literal_data(library):
    name = '<img src=x onerror=alert(1)>'
    edit(library, 'book-one', 'bibliography', {'translators': [{'literal': name}]})
    result = suggestions(library, 'catalog', '<img')
    option = next(option for option in result['options'] if option['value'] == name)
    assert option['label'] == name
    assert option['detail'] == 'Translator · Poetry and Theology'
    assert set(option) == {'value', 'label', 'detail'}


def test_long_catalog_titles_show_full_label_but_submit_searchable_prefix(library):
    option = next(o for o in suggestions(library, 'catalog', 'kruger')['options']
                  if o['label'].startswith('A very long title'))
    assert len(option['label']) > 300
    assert option['value'] == option['label'][:300]
    assert library.books(query=option['value'])[0]['id'] == 'book-two'


def test_facets_keep_raw_search_values_and_aliases_find_them(library):
    result = suggestions(library, 'subjects', 'scripture')
    assert result == {'options': [{'value': 'biblical-studies', 'label': 'biblical-studies', 'detail': 'Biblical studies'}], 'total': 1, 'more': False}
    assert values(suggestions(library, 'forms')) == ['poem']
    assert values(suggestions(library, 'themes')) == ['Unusual lateral idea']
    assert values(suggestions(library, 'people', 'therese')) == ['Thérèse']
    assert 'missing-topic' not in values(suggestions(library, 'subjects'))


def test_forgiving_matching_never_collapses_distinct_facet_values(library):
    with open_sqlite(library.state / 'generations' / 'forms.sqlite3') as db:
        for value in ('art-history', 'art history', 'Art History'):
            db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)',
                       ('book-one', 'book-one', 'subjects', value, 'user', 'confirmed', 'Evidence'))
    result = suggestions(library, 'subjects', 'art history')
    assert set(values(result)) == {'art-history', 'art history', 'Art History'}
    assert result['total'] == 3
    assert all(option['value'] == option['label'] for option in result['options'])


def test_distinct_printed_names_survive_accent_insensitive_matching(library):
    edit(library, 'passage-one', 'bibliography', {
        'authors': [{'literal': 'José Reader'}, {'literal': 'Jose Reader'}],
    })
    assert set(values(suggestions(library, 'authors', 'Jose Reader'))) == {'José Reader', 'Jose Reader'}
    assert set(values(suggestions(library, 'authors', 'José Reader'))) == {'José Reader', 'Jose Reader'}


def test_excluding_a_taxonomy_term_does_not_exclude_other_accented_term(library):
    atomic_json(library.state / 'taxonomy.json', {'terms': [
        {'id': 'jose-one', 'label': 'José', 'aliases': []},
        {'id': 'jose-two', 'label': 'Jose', 'aliases': []},
    ]})
    assert set(values(suggestions(library, 'taxonomy', 'jose'))) == {'José', 'Jose'}
    assert values(suggestions(library, 'taxonomy', 'jose', exclude='jose-one')) == ['Jose']
    assert values(suggestions(library, 'taxonomy', 'jose', exclude='José')) == ['Jose']


def test_taxonomy_submits_labels_resolves_aliases_and_excludes_self(library):
    assert values(suggestions(library, 'taxonomy', 'verse')) == ['Poetry']
    assert values(suggestions(library, 'taxonomy', 'biblical-studies')) == ['Biblical studies']
    assert values(suggestions(library, 'taxonomy', exclude='poetry')) == ['Biblical studies']
    assert values(suggestions(library, 'taxonomy', exclude='Poetry')) == ['Biblical studies']


def test_structured_names_preserve_roles_and_include_section_and_passage_credits(library):
    options = suggestions(library, 'authors')['options']
    assert {'Volume Editor', 'Chapter Writer', 'Passage Scholar', 'René Krüger'} <= {o['value'] for o in options}
    assert next(o for o in options if o['value'] == 'Volume Editor')['detail'].startswith('Editor')
    assert 'Unavailable Scholar' not in {o['value'] for o in options}
    assert 'Missing Person' not in {o['value'] for o in options}


def test_names_do_not_treat_unknown_or_compound_display_messages_as_people(library):
    edit(library, 'passage-one', 'attribution', 'Scripture: Scripture — NRSV; Commentary: Somebody')
    assert not any(o['value'].startswith('Scripture') for o in suggestions(library, 'authors')['options'])
    edit(library, 'passage-one', 'attribution', 'Contributor unverified; edited by Volume Editor')
    assert not any('unverified' in o['value'] for o in suggestions(library, 'authors')['options'])


def test_metadata_changes_invalidate_cache_and_user_corrections_win(library):
    assert values(suggestions(library, 'authors', 'Passage')) == ['Passage Scholar']
    edit(library, 'passage-one', 'bibliography', {'authors': [{'literal': 'Corrected Author'}]})
    edit(library, 'passage-one', 'bibliography', {'authors': [{'literal': 'Generated Replacement'}]}, origin='codex')
    assert values(suggestions(library, 'authors', 'Corrected')) == ['Corrected Author']
    assert suggestions(library, 'authors', 'Passage Scholar')['options'] == []
    assert suggestions(library, 'authors', 'Generated Replacement')['options'] == []


def test_publishers_formats_translations_and_usage_are_existing_values(library):
    assert values(suggestions(library, 'publishers')) == ['A Press']
    assert values(suggestions(library, 'formats')) == ['epub']
    assert values(suggestions(library, 'translations')) == ['New Revised Standard Version']
    assert values(suggestions(library, 'translation_short')) == ['NRSV']
    assert values(suggestions(library, 'destinations')) == ['Proper 26', 'Workshop']
    append_jsonl(library.state / 'usage.jsonl', {'project': 'Another workshop'})
    assert 'Another workshop' in values(suggestions(library, 'destinations'))


def test_limits_report_more_and_queries_do_not_silently_cut_off(library):
    result = suggestions(library, 'authors', limit=1)
    assert len(result['options']) == 1 and result['more'] and result['total'] > 1
    assert result['options'] == suggestions(library, 'authors', limit=1)['options']
    assert not suggestions(library, 'authors', 'no such person')['more']


@pytest.mark.parametrize('kwargs', [
    {'kind': 'invalid'}, {'kind': 'books', 'query': 'x' * 301},
    {'kind': 'books', 'query': None}, {'kind': 'books', 'query': 'line\nbreak'},
    {'kind': 'books', 'limit': 0}, {'kind': 'books', 'limit': 101},
    {'kind': 'books', 'limit': True}, {'kind': 'books', 'limit': 1.5},
    {'kind': 'books', 'exclude': 'x' * 301},
])
def test_invalid_requests_are_rejected(library, kwargs):
    with pytest.raises(ValueError): suggestions(library, **kwargs)


def test_no_source_reads_or_writes_and_cache_avoids_repeated_database_scans(library, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Original book access or indexing is forbidden.')
    monkeypatch.setattr(library, '_inventory', forbidden)
    monkeypatch.setattr(library, 'sync', forbidden)
    before = {p: p.read_bytes() for p in library.state.rglob('*') if p.is_file()}
    first = suggestions(library, 'authors')
    assert 'PRIVATE ORIGINAL BODY' not in json.dumps(first)
    monkeypatch.setattr(library, '_db', forbidden)
    assert suggestions(library, 'authors', 'Scholar')['options']
    assert before == {p: p.read_bytes() for p in library.state.rglob('*') if p.is_file()}


def test_published_generation_and_taxonomy_edits_refresh_options(library):
    assert suggestions(library, 'themes', 'fresh')['options'] == []
    with open_sqlite(library.state / 'generations' / 'forms.sqlite3') as db:
        db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)', ('book-one', 'book-one', 'themes', 'Fresh field', 'user', 'confirmed', 'Evidence'))
    assert values(suggestions(library, 'themes', 'fresh')) == ['Fresh field']
    atomic_json(library.state / 'taxonomy.json', {'terms': [{'id': 'new', 'label': 'New vocabulary', 'aliases': ['fresh']} ]})
    assert values(suggestions(library, 'taxonomy', 'fresh')) == ['New vocabulary']
    assert suggestions(library, 'taxonomy', 'Poetry')['options'] == []


def test_untrusted_strings_stay_data_not_html(library):
    malicious = '<img src=x onerror=alert(1)>'
    with open_sqlite(library.state / 'generations' / 'forms.sqlite3') as db:
        db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)', ('book-one', 'book-one', 'themes', malicious, 'user', 'confirmed', 'Evidence'))
    result = suggestions(library, 'themes', '<img')
    assert result['options'][0]['value'] == malicious
    assert set(result['options'][0]) == {'value', 'label', 'detail'}


def test_empty_library_keeps_vocabulary_without_creating_state(tmp_path):
    lib = Library(tmp_path)
    assert suggestions(lib, 'books') == {'options': [], 'total': 0, 'more': False}
    assert suggestions(lib, 'taxonomy')['options']
    assert not lib.state.exists()
    for kind in KINDS:
        assert set(suggestions(lib, kind)) == {'options', 'total', 'more'}
