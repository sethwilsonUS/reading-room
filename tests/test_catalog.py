from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from book_library.catalog import page
from book_library.service import Library
from book_library.storage import SCHEMA, atomic_json, open_sqlite
from book_library.taxonomy import facet_books
from book_library.web import create_app

ORIGIN = 'http://127.0.0.1:8765'


def library_with(tmp_path, books, facets=()):
    library = Library(tmp_path)
    folder = library.state / 'generations'
    folder.mkdir(parents=True)
    database = folder / 'catalog.sqlite3'
    with open_sqlite(database) as db:
        db.executescript(SCHEMA)
        for book in books:
            db.execute('INSERT INTO works VALUES(?,?,?,?)',
                       (book['id'], book['title'], book.get('author', ''), json.dumps(book)))
        for target, work, facet, value in facets:
            db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)',
                       (target, work, facet, value, 'user', 'confirmed', 'Synthetic catalog fixture'))
    atomic_json(library.state / 'current.json', {'database': database.name})
    atomic_json(library.state / 'focuses' / 'fixture.json',
                {'id': 'fixture', 'name': 'Research', 'books': [], 'breadth': 'balanced'})
    return library


@pytest.fixture
def library(tmp_path):
    books = [{'id': f'book-{i:03}', 'title': f'Book {i:03}', 'author': 'Visible catalog credit',
              'bibliography': {'authors': [{'given': 'Ada', 'family': f'Family {101 - i:03}'}]},
              'files': [], 'available': True} for i in range(1, 101)]
    facets = [(book['id'], book['id'], 'subjects', 'poetry') for book in books]
    return library_with(tmp_path, books, facets)


@pytest.fixture
def client(library):
    with TestClient(create_app(library), base_url=ORIGIN) as client:
        yield client


def test_catalog_counts_before_slicing_and_stops_at_exact_boundary(library):
    first = library.browse_books()
    final = library.browse_books(offset=50)
    outside = library.browse_books(offset=100)
    assert first['total'] == final['total'] == outside['total'] == 100
    assert first['has_more'] and len(first['books']) == 50
    assert not final['has_more'] and len(final['books']) == 50
    assert not outside['has_more'] and outside['books'] == []
    assert library.browse_books(query='Book 001')['total'] == 1
    assert library.browse_books(query='missing')['total'] == 0


def test_legacy_list_contract_and_order_are_preserved(tmp_path):
    books = [{'id': 'z', 'title': 'Zebra', 'author': 'First'},
             {'id': 'a', 'title': 'apple', 'author': 'Second'}]
    lib = library_with(tmp_path, books)
    assert lib.books() == books
    assert [book['id'] for book in lib.browse_books()['books']] == ['a', 'z']
    assert isinstance(lib.books(limit=1), list)
    assert 'catalog_credit' not in lib.books()[0]


def test_creator_search_uses_recorded_book_level_names(library):
    result = library.browse_books(query='Family 001')
    assert [book['id'] for book in result['books']] == ['book-100']
    assert library.books(query='Family 001') == []  # Existing list API is unchanged.
    assert library.browse_books(facet='poetry', query='Family 001')['total'] == 1
    assert library.browse_books(facet='unknown')['total'] == 0


def test_creator_search_includes_book_translators_without_sorting_by_them(tmp_path):
    book = {'id': 'translated', 'title': 'Collected work', 'author': 'Displayed author',
            'bibliography': {'authors': [{'family': 'Zed'}], 'translators': [{'given': 'Ada', 'family': 'Baker'}]},
            'contributors': [{'name': 'Quoted Person', 'role': 'quoted'}]}
    lib = library_with(tmp_path, [book])
    assert lib.browse_books(query='Ada Baker')['total'] == 1
    assert lib.browse_books(query='Quoted Person')['total'] == 0


def test_creator_order_uses_roles_and_supplied_names_without_guessing():
    books = [
        {'id': 'author', 'title': 'Author', 'author': 'Display credit',
         'bibliography': {'authors': [{'given': 'Zoe', 'family': 'Able'}],
                          'editors': [{'family': 'Aardvark'}]}},
        {'id': 'editor', 'title': 'Editor', 'bibliography': {'editors': [{'given': 'Ada', 'family': 'Baker'}]}},
        {'id': 'translator', 'title': 'Translation', 'bibliography': {'translators': [{'family': 'Chen'}]}},
        {'id': 'literal', 'title': 'Literal', 'bibliography': {'authors': [{'literal': 'Liu Xinwu'}]}},
        {'id': 'fallback', 'title': 'Fallback', 'author': 'Royal Society'},
        {'id': 'unknown', 'title': 'Unknown', 'contributors': [{'name': 'Aardvark', 'role': 'foreword'}]},
    ]
    ordered = page(list(reversed(books)), sort='creator')['books']
    assert [book['id'] for book in ordered] == [book['id'] for book in books]
    assert ordered[1]['catalog_credit'] == 'Edited by Ada Baker'
    assert ordered[2]['catalog_credit'] == 'Translated by Chen'
    assert ordered[0]['catalog_credit'] == 'Display credit'
    assert ordered[-1]['catalog_credit'] == ''


def test_title_order_normalizes_accents_and_has_stable_ties():
    books = [{'id': 'b', 'title': 'École'}, {'id': 'a', 'title': 'ecole'}, {'id': 'c', 'title': 'The Garden'}]
    assert [book['id'] for book in page(books)['books']] == ['a', 'b', 'c']
    with pytest.raises(ValueError, match='ordering'):
        page(books, sort='random')


def test_taxonomy_sort_counts_available_books_once_and_retains_metadata(tmp_path):
    books = [{'id': 'one', 'title': 'A title', 'author': 'Zed', 'available': True,
              'bibliography': {'year': '2024', 'editors': [{'family': 'Zed'}]}},
             {'id': 'two', 'title': 'B title', 'author': 'Alpha', 'available': True},
             {'id': 'gone', 'title': 'Missing', 'author': 'A', 'available': False}]
    facets = [('one', 'one', 'themes', 'trust'), ('section', 'one', 'themes', 'trust'),
              ('two', 'two', 'themes', 'trust'), ('gone', 'gone', 'themes', 'trust')]
    lib = library_with(tmp_path, books, facets)
    result = facet_books(lib, 'themes', 'trust', sort='creator', limit=1)
    assert result['total'] == 2 and result['has_more']
    assert result['books'][0]['id'] == 'two'
    last = facet_books(lib, 'themes', 'trust', sort='creator', limit=1, offset=1)
    assert last['books'][0]['catalog_year'] == '2024'
    assert not last['has_more']


def test_shelves_keep_query_filter_sort_and_correct_final_page(client):
    response = client.get('/api/ui/books', params={'q': 'Book', 'facet': 'poetry', 'sort': 'creator'})
    assert response.status_code == 200
    document = response.json()['data']
    assert document['result']['total'] == 100 and document['result']['offset'] == 0
    assert len(document['books']) == 50 and document['books'][0]['id'] == 'book-100'
    next_url = document['next_url']
    assert parse_qs(urlsplit(next_url).query) == {'q': ['Book'], 'facet': ['poetry'], 'sort': ['creator'], 'offset': ['50']}
    clear = document['clear_filter_url']
    assert parse_qs(urlsplit(clear).query) == {'q': ['Book'], 'sort': ['creator']}
    final = client.get('/api/ui' + next_url).json()['data']
    assert final['next_url'] is None
    outside = client.get('/api/ui/books?offset=100&sort=creator').json()['data']
    assert outside['result']['total'] == 100 and outside['books'] == []
    assert outside['first_page_url'] == '/books?q=&facet=&sort=creator'


def test_shelves_deliver_raw_source_strings_as_json_not_markup(tmp_path):
    book = {'id': 'unsafe', 'title': '<script>bad()</script>' + ' a long title' * 20,
            'author': '<img src=x onerror=bad()>', 'edition': {'internal': 'record'},
            'bibliography': {'year': '2025'}, 'profile': {'description': 'Long description belongs on details'},
            'files': [{'id': 'one'}, {'id': 'two'}]}
    with TestClient(create_app(library_with(tmp_path, [book])), base_url=ORIGIN) as client:
        response = client.get('/api/ui/books')
    row = response.json()['data']['books'][0]
    assert row['title'] == book['title'] and row['author'] == book['author']
    assert response.headers['content-type'].startswith('application/json')
    assert row['catalog_edition'] == book['edition'] and row['catalog_year'] == '2025'
    # Compact, escaped markup and decorative cover behavior live in AdminPages.test.tsx.


def test_creator_sorted_preferred_books_save_only_visible_page(client, library):
    focus = library.focuses()[0]
    library.save_focus(focus['id'], {**focus, 'books': ['book-001', 'book-100']})
    document = client.get('/api/ui/focus/fixture/books?sort=creator').json()['data']
    csrf = client.get('/api/ui/bootstrap').json()['csrf']
    assert document['sort'] == 'creator'
    assert document['books'][0]['id'] == 'book-100'
    response = client.post('/api/ui/focus/fixture/books', json={'fields': {'sort': 'creator', 'offset': '0', 'q': '', 'books': 'book-099'}}, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
    assert response.status_code == 200
    assert parse_qs(urlsplit(response.json()['redirect']).query)['sort'] == ['creator']
    saved = next(item for item in library.focuses() if item['id'] == 'fixture')
    assert set(saved['books']) == {'book-001', 'book-099'}


@pytest.mark.parametrize('path', ['/books?sort=unknown', '/taxonomy/browse?facet=subjects&value=poetry&sort=unknown', '/focus/fixture/books?sort=unknown'])
def test_invalid_sort_is_rejected(client, path):
    assert client.get('/api/ui' + path).status_code == 400
