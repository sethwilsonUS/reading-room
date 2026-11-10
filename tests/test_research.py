"""Agent search returns the interface's results, plus bounded source context and citations."""
from __future__ import annotations

import json
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from book_library.research import search_with_context
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
