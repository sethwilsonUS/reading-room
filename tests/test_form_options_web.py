"""HTTP boundary checks for read-only autocomplete suggestions."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient
import pytest

from book_library.storage import open_sqlite
from book_library.web import create_app
from test_form_options import library  # Reuse the real SQLite metadata fixture.

ORIGIN = 'http://127.0.0.1:18765'


@pytest.fixture
def client(library):
    with TestClient(create_app(library), base_url=ORIGIN) as client:
        yield client


def test_options_get_is_read_only_bounded_and_uses_no_source_material(client, library, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError('Suggestions must not read books, embed text, or mutate state.')
    for method in ('_inventory', 'sync', 'search', 'read', 'annotate', 'record_usage'):
        monkeypatch.setattr(library, method, forbidden)
    before = {p: p.read_bytes() for p in library.state.rglob('*') if p.is_file()}
    response = client.get('/api/form-options', params={'kind': 'authors', 'limit': 1})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload['options']) == 1 and payload['more'] and payload['total'] > 1
    assert 'PRIVATE ORIGINAL BODY' not in response.text
    assert before == {p: p.read_bytes() for p in library.state.rglob('*') if p.is_file()}
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    policy = response.headers['content-security-policy']
    assert "connect-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "'unsafe-inline'" not in policy


@pytest.mark.parametrize('params', [
    {}, {'kind': 'not-a-kind'}, {'kind': 'x' * 51},
    {'kind': 'books', 'q': 'x' * 301}, {'kind': 'books', 'q': 'new\nline'},
    {'kind': 'books', 'limit': 0}, {'kind': 'books', 'limit': 101},
    {'kind': 'books', 'limit': 'nope'}, {'kind': 'books', 'exclude': 'x' * 201},
])
def test_options_route_rejects_invalid_or_unbounded_inputs(client, params):
    response = client.get('/api/form-options', params=params)
    assert response.status_code == 400
    assert set(response.json()) == {'error'}


def test_suggestions_keep_localhost_boundary_and_have_no_mutation_method(client):
    assert client.get('/api/form-options?kind=books', headers={'Host': 'evil.example'}).status_code == 400
    assert client.post('/api/form-options?kind=books', headers={'Origin': ORIGIN}).status_code == 405
    assert client.post('/api/form-options?kind=books', headers={'Origin': 'https://evil.example'}).status_code == 403
    response = client.get('/api/form-options?kind=books', headers={'Origin': 'https://evil.example'})
    assert 'access-control-allow-origin' not in response.headers


def test_untrusted_labels_are_json_data_not_executable_markup(client, library):
    malicious = '<script>alert("book")</script><img src=x onerror=alert(1)>'
    with open_sqlite(library.state / 'generations' / 'forms.sqlite3') as db:
        db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)',
                   ('book-one', 'book-one', 'themes', malicious, 'user', 'confirmed', 'Evidence'))
    response = client.get('/api/form-options', params={'kind': 'themes', 'q': '<script>'})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.json()['options'][0] == {'value': malicious, 'label': malicious, 'detail': 'Themes'}
    assert json.loads(response.text)['options'][0]['value'] == malicious


def test_options_route_excludes_current_term_and_resolves_accented_search(client):
    result = client.get('/api/form-options', params={'kind': 'taxonomy', 'exclude': 'poetry'}).json()
    assert [o['value'] for o in result['options']] == ['Biblical studies']
    result = client.get('/api/form-options', params={'kind': 'authors', 'q': 'kruger'}).json()
    assert [o['value'] for o in result['options']] == ['René Krüger']


def test_long_catalog_option_value_roundtrips_through_catalog_route(client):
    result = client.get('/api/form-options', params={'kind': 'catalog', 'q': 'kruger'}).json()
    option = next(o for o in result['options'] if o['label'].startswith('A very long title'))
    assert len(option['value']) == 300 and len(option['label']) > 300
    response = client.get('/api/ui/books', params={'q': option['value']})
    assert response.status_code == 200
    assert option['label'] in [book['title'].strip() for book in response.json()['data']['books']]
