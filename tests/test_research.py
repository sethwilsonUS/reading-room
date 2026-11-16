"""Agent search returns the interface's results, plus bounded source context and citations."""
from __future__ import annotations

import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from book_library.research import search_with_context
from book_library import public_config, semantic_library
from book_library.web import create_app
from test_service import docx, seed

ORIGIN = 'http://127.0.0.1:18765'
# Search fields that must be identical between the interface and the CLI.
SHARED_FIELDS = ('id', 'work_id', 'section_id', 'title', 'attribution', 'heading', 'text', 'reason', 'score', 'limitations')


@pytest.fixture
def library(tmp_path):
    root = tmp_path / 'books'
    rivers = docx(root / 'A - Rivers' / 'rivers.docx', [
        ('Heading1', 'Rivers'), 'The lantern river runs through the valley at dusk.',
        ('Heading1', 'Bridges'), 'A lantern hangs on the bridge over the river.',
        ('Heading1', 'Harbors'), 'Boats wait in the harbor for the morning tide.'])
    lamps = docx(root / 'B - Lamps' / 'lamps.docx', [('Heading1', 'Lamps'), 'Every lantern needs oil and a steady hand.'])
    return seed(root, [{'title': 'Rivers', 'author': 'River Author', 'paths': [rivers]},
                       {'title': 'Lamps', 'author': 'Lamp Author', 'paths': [lamps]}])


def interface_search(library, query):
    with TestClient(create_app(library, enable_intake_worker=False), base_url=ORIGIN) as client:
        csrf = client.get('/api/ui/bootstrap').json()['csrf']
        response = client.post('/api/ui/search', json={'fields': {'q': query}},
                               headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.text
    return response.json()['data']['result']


def cli(library, *arguments):
    completed = subprocess.run([sys.executable, '-m', 'book_library.cli', '--root', str(library.root), *arguments],
                               capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_cli_agent_search_returns_exactly_the_interface_results(library):
    interface = interface_search(library, 'lantern river')
    agent = cli(library, 'search', 'lantern river', '--with-context', '--json')
    for key in ('direct', 'connections'):
        assert [h['id'] for h in agent[key]] == [h['id'] for h in interface[key]]
        for ours, theirs in zip(agent[key], interface[key]):
            assert {f: ours[f] for f in SHARED_FIELDS} == {f: theirs[f] for f in SHARED_FIELDS}
    assert len(agent['direct']) == 3
    assert agent['warnings'] == interface['warnings']


def test_context_separates_the_match_from_its_neighbors(library):
    result = search_with_context(library, '"bridge over the river"', context=1)
    hit = result['direct'][0]
    assert hit['heading'] == 'Bridges'
    context = hit['context']
    assert 'bridge over the river' in context['passage']
    assert 'lantern river runs through the valley' in context['before']
    assert 'Boats wait in the harbor' in context['after']
    assert context['section'] == 'Bridges'
    assert context['chars'] == len(context['before']) + len(context['passage']) + len(context['after'])
    assert context['previous_passage'] and context['next_passage']


def test_each_hit_carries_a_reviewable_citation_draft(library):
    hit = search_with_context(library, 'oil')['direct'][0]
    citation = hit['citation']
    assert 'Lamp Author' in citation['full_note']
    assert {'short_note', 'bibliography', 'ready', 'needs_review', 'missing_fields', 'warnings', 'links'} <= set(citation)
    assert citation == {k: v for k, v in library.cite(hit['id']).items() if k in citation}


def test_total_budget_bounds_context_and_says_what_was_omitted(library):
    result = search_with_context(library, 'lantern', context=8, max_chars=500, total_chars=600)
    hits = result['direct']
    assert len(hits) >= 2
    assert hits[0]['context'] is not None
    omitted = [h for h in hits if h['context'] is None]
    assert omitted and all('library read' in h['context_omitted'] for h in omitted)
    assert all(h['citation'] for h in hits)  # citations are small and always attached
    assert result['context_budget']['used_chars'] <= 600


def test_unreadable_source_is_reported_per_hit_without_failing_the_search(library, monkeypatch):
    # Search already skips sources changed before it ran; this covers a source
    # that changes between the search and the read of its context.
    first = library.search('lantern')['direct'][0]['id']
    real_read = library.read

    def read(passage_id, **kwargs):
        if passage_id == first:
            raise ValueError('Source file changed or disappeared; synchronize before reading this passage.')
        return real_read(passage_id, **kwargs)

    monkeypatch.setattr(library, 'read', read)
    hits = search_with_context(library, 'lantern')['direct']
    assert hits[0]['id'] == first and hits[0]['context'] is None
    assert 'synchronize' in hits[0]['context_error']
    # Citation drafting reads the same source, so it reports the same problem.
    assert hits[0]['citation'] is None and 'synchronize' in hits[0]['citation_error']
    assert all(h['context'] and h['citation'] for h in hits[1:])


def test_plain_search_output_is_unchanged_without_the_flag(library):
    plain = cli(library, 'search', 'lantern river', '--json')
    assert 'context_budget' not in plain
    assert all('context' not in h and 'citation' not in h for h in plain['direct'])


@pytest.mark.parametrize('max_chars,closing', [(6000, 'Quiet closing prose. ' * 400), (500, '')])
def test_late_exact_match_stays_in_context_with_working_continuations(tmp_path, max_chars, closing):
    root = tmp_path / 'books'
    text = 'Ordinary introductory prose. ' * 350 + 'A goldfinch rests beneath the oak. ' + closing
    path = docx(root / 'Long' / 'source.docx', [('Heading1', 'Synthetic chapter'), text])
    lib = seed(root, [{'title': 'Synthetic long chapter', 'paths': [path]}])
    plain = lib.search('goldfinch', engine='exact')['direct'][0]
    hit = search_with_context(lib, 'goldfinch', engine='exact', max_chars=max_chars, total_chars=max_chars)['direct'][0]
    assert {key: hit[key] for key in SHARED_FIELDS} == {key: plain[key] for key in SHARED_FIELDS}
    assert hit['excerpt_start'] > 6000
    context = hit['context']
    assert 'goldfinch rests beneath the oak' in context['passage']
    assert context['chars'] <= max_chars and context['truncated']
    assert context['offset'] > 0
    earlier = lib.read(hit['id'], context=0, offset=context['previous_offset'], max_chars=max_chars)
    assert earlier['next_offset'] == context['offset']
    assert 'Ordinary introductory prose' in earlier['passage']['text']
    if closing:
        later = lib.read(hit['id'], context=0, offset=context['next_offset'], max_chars=max_chars)
        assert later['passage']['text'] and 'Quiet closing prose' in later['passage']['text']
    else:
        assert context['next_offset'] is None


@pytest.mark.parametrize('max_chars', [6000, 500])
def test_semantic_context_uses_the_retrieved_window_without_changing_search_results(tmp_path, monkeypatch, max_chars):
    root = tmp_path / 'books'
    text = 'Ordinary introductory prose. ' * 1200 + 'A goldfinch rests beneath the oak. ' + 'Quiet closing prose. ' * 400
    path = docx(root / 'Long' / 'source.docx', [('Heading1', 'Synthetic chapter'), text])
    lib = seed(root, [{'title': 'Synthetic long chapter', 'paths': [path]}])
    passage_id = lib.search('goldfinch', engine='exact')['direct'][0]['id']
    source = lib.read(passage_id, context=0, max_chars=45000)['passage']['text']
    start = source.index('A goldfinch')
    public_config.accept_budget(lib, initial_usd='10', monthly_usd='10', timezone='UTC')
    semantic_library.configure_engine(lib, 'openai', approval_evidence='Synthetic fixture uses only a fake semantic provider.')
    calls = []

    def fake_search(library, query, limit):
        calls.append(query)
        return [{'passage_id': passage_id, 'window_id': 'synthetic-window', 'char_start': start, 'char_end': start + 300}]

    monkeypatch.setattr(semantic_library, 'search', fake_search)
    plain = lib.search('avian shelter')['connections'][0]
    result = search_with_context(lib, 'avian shelter', max_chars=max_chars, total_chars=max_chars)
    hit = result['connections'][0]
    assert calls == ['avian shelter', 'avian shelter']
    assert {key: hit[key] for key in SHARED_FIELDS} == {key: plain[key] for key in SHARED_FIELDS}
    assert hit['read_offset'] > 30000
    assert 'goldfinch rests beneath the oak' in hit['context']['passage']
    assert hit['context']['offset'] >= hit['read_offset']
    assert hit['context']['chars'] <= max_chars
    assert hit['context']['previous_offset'] is not None and hit['context']['next_offset'] is not None
    assert result['context_budget']['used_chars'] == hit['context']['chars']
