"""`library correction` and `library browse` share the interface's state and rules."""
from __future__ import annotations

import pytest

from book_library import cli, correction_cli
from book_library.review import ReviewConflict
from book_library.taxonomy import facet_books
from test_correction_review import review_book  # noqa: F401  (fixture)
from test_review_decisions import offer


def run(lib, *arguments):
    return cli.run(cli.parser().parse_args(['--root', str(lib.root), *arguments]))


def correction(lib, action, target, *arguments):
    return run(lib, 'correction', action, target, *arguments)


def test_show_matches_the_review_page_state(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    view = correction(lib, 'show', target)
    context = lib.review_context(target)
    assert view['revision'] == context['revision'] and view['version'] == 0
    assert [f['key'] for f in view['fields']] == [f['key'] for f in context['fields']]
    assert view['draft_changes'] == {} and view['source_current']


def test_set_drafts_into_the_shared_review_without_changing_the_library(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    before = list(lib._curation())
    result = correction(lib, 'set', target, '--field', 'bibliography.authors', '--value', 'Ada Writer\nBea Writer',
                        '--evidence', 'Opening byline of the section.')
    change = lib.review_draft(target)['changes']['bibliography.authors']  # what the review page reads
    assert change['value'] == [{'literal': 'Ada Writer'}, {'literal': 'Bea Writer'}]
    assert change['evidence'] == 'Opening byline of the section.' and change['verified'] is False
    assert result['review']['version'] == 1
    assert lib._curation() == before


def test_changing_a_verified_value_clears_verification(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Introduction', '--verified', 'yes')
    assert lib.review_draft(target)['changes']['bibliography.title']['verified'] is True
    correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Introduction to the collection', '--verified', 'yes')
    assert lib.review_draft(target)['changes']['bibliography.title']['verified'] is False


def test_evidence_unset_and_discard_edit_only_the_draft(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Introduction')
    correction(lib, 'set', target, '--field', 'profile.themes', '--value', 'lanterns, light')
    assert lib.review_draft(target)['changes']['profile.themes']['value'] == ['lanterns', 'light']
    correction(lib, 'evidence', target, '--field', 'bibliography.title', '--evidence', 'Heading inspected.')
    assert lib.review_draft(target)['changes']['bibliography.title']['evidence'] == 'Heading inspected.'
    correction(lib, 'unset', target, '--field', 'profile.themes')
    assert set(lib.review_draft(target)['changes']) == {'bibliography.title'}
    correction(lib, 'discard', target)
    assert lib.review_draft(target)['changes'] == {}


def test_inherited_fields_are_not_editable_on_a_section(review_book):
    lib, _, hit, _ = review_book
    with pytest.raises(ValueError, match='editable field'):
        correction(lib, 'set', hit['section_id'], '--field', 'bibliography.publisher', '--value', 'Other Press')


def test_stale_version_from_show_is_refused(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    seen = correction(lib, 'show', target)
    correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Introduction')  # someone else edits
    with pytest.raises(ReviewConflict):
        correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Other', '--version', str(seen['version']))


def test_apply_saves_only_selected_changes_as_provisional_user_curation(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    correction(lib, 'set', target, '--field', 'bibliography.title', '--value', 'Introduction', '--evidence', 'Heading inspected.')
    correction(lib, 'set', target, '--field', 'profile.themes', '--value', 'lanterns')
    with pytest.raises(ValueError, match='--field'):
        correction(lib, 'apply', target)
    result = correction(lib, 'apply', target, '--field', 'bibliography.title', '--request-id', 'agent-1')
    assert result['receipt']['saved'] and result['receipt']['fields'] == ['bibliography.title']
    saved = lib._curation()[-1]
    assert (saved['origin'], saved['status'], saved['evidence']) == ('user', 'provisional', 'Heading inspected.')
    assert set(result['review']['draft_changes']) == {'profile.themes'}
    count = len(lib._curation())
    assert correction(lib, 'apply', target, '--all', '--request-id', 'agent-1')['receipt']['request_id'] == 'agent-1'
    assert len(lib._curation()) == count  # a repeated request ID never saves twice


def test_decide_and_recommendation_follow_the_review_rules(review_book):
    lib, _, hit, _ = review_book
    target = hit['section_id']
    offer(lib, target)  # an already-generated suggestion; no provider is called
    view = correction(lib, 'show', target)
    item = view['items'][0]
    assert item['status'] == 'pending'
    with pytest.raises(ValueError, match='--decision'):
        correction(lib, 'decide', target, '--item', item['id'], '--decision', 'maybe')
    decided = correction(lib, 'decide', target, '--item', item['id'], '--decision', 'accept')['review']
    assert decided['items'][0]['status'] == 'accepted'
    assert decided['draft_changes']['bibliography.title']['verified'] is False
    correction(lib, 'decide', target, '--item', item['id'], '--decision', 'undo')
    rec = correction(lib, 'show', target)['recommendations'][0]
    preview = correction(lib, 'preview', target, '--item', rec['id'])
    assert preview['changes'] and lib.review_draft(target)['changes'] == {}  # preview never writes
    after = correction(lib, 'recommendation', target, '--item', rec['id'], '--decision', 'accept')['review']
    assert 'bibliography.title' in after['draft_changes']


def test_paid_suggestions_are_not_a_cli_action():
    assert 'suggest' not in correction_cli.ACTIONS


def test_browse_matches_the_shelves_and_classification_views(review_book):
    lib, *_ = review_book
    assert run(lib, 'browse', '--sort', 'creator') == lib.browse_books('', sort='creator')
    assert run(lib, 'browse', 'collection', '--filter', 'author=Jane') == lib.browse_books('collection', facet={'author': 'Jane'})
    lib.annotate(lib.books()[0]['id'], {'field': 'profile', 'value': {'themes': ['lanterns']}, 'origin': 'user',
                                        'status': 'confirmed', 'evidence': 'Synthetic fixture contents.'})
    lib.sync()
    by_theme = run(lib, 'browse', '--classification', 'themes=lanterns')
    assert by_theme == facet_books(lib, 'themes', 'lanterns') and by_theme['total'] == 1
    with pytest.raises(ValueError, match='alone'):
        run(lib, 'browse', 'x', '--classification', 'themes=lanterns')
    with pytest.raises(ValueError, match='filter syntax'):
        run(lib, 'browse', '--filter', 'colour=blue')
