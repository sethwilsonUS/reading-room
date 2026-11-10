"""One-call research results for agents: search hits with source context and citations.

`search_with_context` runs the ordinary `Library.search` (the same operation
the interface calls, with the same engine defaults, ranking, filters, and
warnings), then attaches to each hit the surrounding source text from
`Library.read` and the citation draft from `Library.cite`. Hit identity and
order are therefore exactly the interface's; only the attachments are new.

Output is bounded per hit and in total so an agent's context window is not
flooded. Every hit keeps its passage ID, so an agent can continue with
`library read` or `library read-unit` for more text.
"""
from __future__ import annotations

from typing import Any

DEFAULT_CONTEXT_PASSAGES = 2
DEFAULT_HIT_CHARS = 6000
DEFAULT_TOTAL_CHARS = 60000
MIN_HIT_CHARS = 500  # Library.read's own minimum
MAX_TOTAL_CHARS = 400000
# Citation fields an agent needs to cite honestly: the drafts, what is verified
# or missing, and the warnings. Rendering-only and provenance internals stay
# available through `library cite`.
CITATION_FIELDS = ('full_note', 'short_note', 'bibliography', 'markdown', 'ready', 'needs_review',
                   'missing_fields', 'review_fields', 'warnings', 'locator', 'links', 'component_citations')


def _join(blocks: list[dict[str, Any]]) -> str:
    return '\n\n'.join(block['text'] for block in blocks if block.get('text', '').strip())


def source_context(lib, passage_id: str, *, context: int, max_chars: int) -> dict[str, Any]:
    """Matched passage plus surrounding text, split so the match stays identifiable."""
    reading = lib.read(passage_id, context=context, max_chars=max_chars)
    before, match, after = [], [], []
    for block in reading['blocks']:
        if block.get('selected'):
            match.append(block)
        else:
            (after if match else before).append(block)
    text = {'before': _join(before), 'passage': _join(match), 'after': _join(after)}
    return {**text, 'chars': sum(map(len, text.values())), 'section': reading['section'].get('title', ''),
            'location': reading['passage'].get('location', ''), 'truncated': reading['truncated'],
            'next_offset': reading.get('next_offset'), 'previous_passage': reading.get('previous'),
            'next_passage': reading.get('next'), 'warnings': reading.get('warnings', [])}


def citation_summary(lib, passage_id: str) -> dict[str, Any]:
    citation = lib.cite(passage_id)
    return {key: citation[key] for key in CITATION_FIELDS if key in citation}


def search_with_context(lib, query: str, *, context: int = DEFAULT_CONTEXT_PASSAGES,
                        max_chars: int = DEFAULT_HIT_CHARS, total_chars: int = DEFAULT_TOTAL_CHARS,
                        **search_options: Any) -> dict[str, Any]:
    """Search, then attach `context` and `citation` to every hit within the budgets.

    Direct hits are expanded before possible connections, so they get the
    budget first. A hit whose source cannot be read (for example a file that
    changed since the last sync) keeps its search fields and gets an `error`
    explaining why instead of failing the whole search.
    """
    total_chars = max(MIN_HIT_CHARS, min(int(total_chars), MAX_TOTAL_CHARS))
    max_chars = int(max_chars)
    result = lib.search(query, **search_options)
    remaining = total_chars
    for key in ('direct', 'connections'):
        for hit in result[key]:
            if remaining < MIN_HIT_CHARS:
                hit['context'] = None
                hit['context_omitted'] = 'The total context budget was used by earlier results. Use `library read PASSAGE_ID --json`.'
            else:
                try:
                    hit['context'] = source_context(lib, hit['id'], context=context, max_chars=min(max_chars, remaining))
                except ValueError as exc:
                    hit['context'] = None
                    hit['context_error'] = str(exc)
                else:
                    remaining = max(0, remaining - hit['context']['chars'])
            try:
                hit['citation'] = citation_summary(lib, hit['id'])
            except ValueError as exc:
                hit['citation'] = None
                hit['citation_error'] = str(exc)
    result['context_budget'] = {'total_chars': total_chars, 'used_chars': total_chars - remaining,
                                'per_hit_chars': max_chars, 'context_passages': context}
    return result
