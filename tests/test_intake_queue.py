"""Intake mutations use isolated, real source packages and fake provider results."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from book_library import intake
from book_library.service import Library
from book_library.storage import atomic_json, read_json, writer
from test_service import docx


def fixture(tmp_path, paragraphs=None, *, semantic=False):
    root = tmp_path / 'books'
    source = docx(root / 'Example' / 'book.docx', paragraphs or [
        ('Heading1', 'Introduction'), 'This introduction explains the arrangement of the book.',
        ('Heading1', 'The argument'), 'Copper birds remember the patient gardener.',
        ('Heading1', 'An empty heading')])
    lib = Library(root)
    from book_library.public_config import accept_budget, set_review_default
    accept_budget(lib, initial_usd='50', monthly_usd='25', timezone='America/Chicago', approval_evidence='Synthetic fixture explicitly approves paid processing.')
    set_review_default(lib, True)
    if semantic:
        from book_library.semantic_library import configure_engine
        configure_engine(lib, 'openai', approval_evidence='Synthetic fixture explicitly approves semantic indexing.')
    job = intake.enqueue_intake(lib, source.parent, {'provider': 'local', 'metadata': {'title': 'A fixture book'}})
    return lib, source, job


def result(**extra):
    return {'status': 'complete', 'suggestions': [], 'unresolved': [], 'assessments': [], 'warnings': [],
            'model': 'fixture', 'cost': None, **extra}


def fake_provider(monkeypatch, responses=None):
    calls = []
    def suggest(lib, context, *, allow_external=True, should_continue=None, **kwargs):
        calls.append((context['target']['id'], allow_external, context.get('coverage_complete')))
        return responses.pop(0) if responses else result()
    monkeypatch.setattr('book_library.review_ai.suggest', suggest)
    return calls


def drain(lib, count=100):
    for _ in range(count):
        if not intake.run_next(lib):
            return
    raise AssertionError('The fixture queue did not become idle.')


def test_explicit_intake_is_idempotent_and_gets_do_not_start_work(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, source, job = fixture(tmp_path)
    before = intake._path(lib, job['id']).read_bytes()
    repeated = intake.enqueue_intake(lib, source.parent, {'provider': 'local', 'metadata': {'title': 'A fixture book'}})
    assert repeated['id'] == job['id']
    assert intake.intake_status(lib, job['id'])['status'] == 'queued'
    assert len(intake.intake_status(lib)['intakes']) == 1
    assert before == intake._path(lib, job['id']).read_bytes()
    assert not calls
    assert not lib._active()


def test_book_first_every_heading_accounted_and_only_selected_book_reviewed(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, source, job = fixture(tmp_path)
    unrelated = docx(lib.root / 'Unrequested' / 'other.docx', [('Heading1', 'Unrelated'), 'Silver bells ring elsewhere.'])
    drain(lib)
    done = intake.intake_status(lib, job['id'])
    assert done['status'] == 'complete'
    assert done['readiness']['text'] == 'ready'
    assert done['targets'][0]['scope'] == 'work'
    with lib._db() as db:
        sections = {row[0] for row in db.execute('SELECT id FROM sections WHERE work_id=?', (done['work_ids'][0],))}
    assert {t['id'] for t in done['targets'][1:]} == sections
    assert len(sections) == 4  # Includes the extraction's empty front-matter section.
    assert done['targets'][-1]['status'] == 'non_substantive'
    assert calls[0][1] is True
    assert all(not call[1] for call in calls[1:])
    assert {call[0] for call in calls}.issubset({t['id'] for t in done['targets']})
    assert lib.search('silver bells', engine='exact')['direct']
    assert not (lib.state / 'curation.jsonl').exists()


def test_sync_alone_never_creates_review_authorization(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    source = docx(tmp_path / 'books' / 'Plain' / 'book.docx', ['An ordinary source.'])
    lib = Library(tmp_path / 'books')
    lib.sync(source.parent, semantic=False)
    assert not intake.run_next(lib)
    assert intake.intake_status(lib) == {'intakes': []}
    assert not calls


@pytest.mark.parametrize('operation,stage,error_type', [
    ('sync', 'exact_sync', RuntimeError),
    ('targets', 'review_targets', ValueError),
])
def test_index_failure_keeps_private_diagnostic_and_requires_resume(tmp_path, monkeypatch, operation, stage, error_type):
    calls = fake_provider(monkeypatch)
    lib, source, job = fixture(tmp_path)
    source_bytes = source.read_bytes()
    owner, attribute = (lib, 'sync') if operation == 'sync' else (intake, '_targets')
    original = getattr(owner, attribute)

    def fail(*args, **kwargs):
        raise error_type('PRIVATE_BOOK_TEXT_AND_CREDENTIAL_SENTINEL')

    monkeypatch.setattr(owner, attribute, fail)
    assert intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['status'] == 'paused' and paused['stage'] == 'registered'
    assert paused['reason_code'] == 'indexing'
    assert paused['indexing_error']['stage'] == stage
    assert paused['indexing_error']['error_type'] == error_type.__name__
    assert paused['indexing_error']['occurred_at']
    assert set(paused['indexing_error']) == {'stage', 'error_type', 'occurred_at'}
    assert 'PRIVATE_BOOK_TEXT_AND_CREDENTIAL_SENTINEL' not in intake._path(lib, job['id']).read_text()
    assert paused['authorization'] == job['authorization']
    assert source.read_bytes() == source_bytes
    assert paused['active_operation'] is None
    assert not intake.run_next(lib)
    assert not calls

    monkeypatch.setattr(owner, attribute, original)
    intake.resume_intake(lib, job['id'], paused['version'])
    assert intake.run_next(lib)
    resumed = intake.intake_status(lib, job['id'])
    assert resumed['readiness']['text'] == 'ready'
    assert resumed['stage'] == 'semantic'
    assert 'indexing_error' not in resumed
    assert not calls


def test_budget_pause_requires_explicit_resume_and_new_attempt_identity(tmp_path, monkeypatch):
    responses = [result(status='unavailable', failure_reason='budget')]
    calls = fake_provider(monkeypatch, responses)
    lib, _, job = fixture(tmp_path)
    drain(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['status'] == 'paused' and paused['reason_code'] == 'budget'
    assert paused['readiness']['text'] == 'ready'
    attempt = paused['targets'][0]['attempt_id']
    for _ in range(3):
        assert not intake.run_next(lib)
    assert len(calls) == 1
    intake.resume_intake(lib, job['id'], paused['version'])
    drain(lib)
    done = intake.intake_status(lib, job['id'])
    assert done['status'] == 'complete'
    assert done['targets'][0]['attempt_id'] != attempt
    assert done['targets'][0]['attempts'] == 2


def test_pause_during_request_keeps_result_and_resumes_without_regeneration(tmp_path, monkeypatch):
    lib, _, job = fixture(tmp_path)
    calls = []
    def suggest(lib, context, **kwargs):
        calls.append(context['target']['id'])
        current = intake.intake_status(lib, job['id'])
        intake.pause_intake(lib, job['id'], current['version'])
        return result()
    monkeypatch.setattr('book_library.review_ai.suggest', suggest)
    assert intake.run_next(lib)  # exact
    assert intake.run_next(lib)  # semantic setting
    assert intake.run_next(lib)  # book result
    paused = intake.intake_status(lib, job['id'])
    assert paused['status'] == 'paused'
    assert paused['targets'][0]['status'] == 'generated'
    assert (lib.state / paused['targets'][0]['result_file']).is_file()
    intake.resume_intake(lib, job['id'], paused['version'])
    assert intake.run_next(lib)
    assert len(calls) == 1
    assert intake.intake_status(lib, job['id'])['targets'][0]['status'] == 'no_change'


def test_crash_after_saved_generation_recovers_without_another_model_request(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path)
    from book_library.intake_curation import apply_automatic as real_apply
    def crash(*args, **kwargs):
        saved = intake.intake_status(lib, job['id'])['targets'][0]
        assert (lib.state / saved['result_file']).is_file()
        raise SystemExit('simulated process interruption')
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', crash)
    intake.run_next(lib)
    intake.run_next(lib)
    with pytest.raises(SystemExit):
        intake.run_next(lib)
    assert len(calls) == 1
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', real_apply)
    assert intake.run_next(lib)
    assert len(calls) == 1
    assert intake.intake_status(lib, job['id'])['targets'][0]['status'] == 'no_change'


def test_interrupted_paid_attempt_does_not_restart_until_explicit_resume(tmp_path, monkeypatch):
    lib, _, job = fixture(tmp_path)
    def crash(*args, **kwargs):
        raise SystemExit('simulated lost provider response')
    monkeypatch.setattr('book_library.review_ai.suggest', crash)
    intake.run_next(lib)
    intake.run_next(lib)
    with pytest.raises(SystemExit):
        intake.run_next(lib)
    assert not intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['reason_code'] == 'uncertain'
    assert paused['targets'][0]['status'] == 'interrupted'
    fake_provider(monkeypatch)
    intake.resume_intake(lib, job['id'], paused['version'])
    drain(lib)
    assert intake.intake_status(lib, job['id'])['status'] == 'complete'


def test_changed_source_pauses_then_explicit_resume_replans_and_retains_history(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, source, job = fixture(tmp_path)
    intake.run_next(lib)
    intake.run_next(lib)
    intake.run_next(lib)
    before = intake.intake_status(lib, job['id'])
    docx(source, [('Heading1', 'Replacement'), 'A different argument from a new source version.'])
    assert intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['reason_code'] == 'source_changed'
    assert len(calls) == 1
    resumed = intake.resume_intake(lib, job['id'], paused['version'])
    assert resumed['stage'] == 'registered'
    assert resumed['history'][0]['targets'] == before['targets']
    drain(lib)
    done = intake.intake_status(lib, job['id'])
    assert done['status'] == 'complete'
    assert done['targets'][-1]['title'] == 'Replacement'


def test_concurrent_version_checks_and_worker_claim(tmp_path):
    lib, _, job = fixture(tmp_path)
    paused = intake.pause_intake(lib, job['id'], job['version'])
    with pytest.raises(ValueError, match='progress changed'):
        intake.resume_intake(lib, job['id'], job['version'])
    with intake._worker_claim(lib) as claimed:
        assert claimed
        assert not intake.run_next(lib)
    assert intake.intake_status(lib, job['id'])['version'] == paused['version']


def test_incomplete_coverage_is_not_reported_as_fully_inspected(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    paragraphs = [('Heading1', 'Long section')] + [f'Body paragraph {i} with a separate argument.' for i in range(80)]
    lib, _, job = fixture(tmp_path, paragraphs)
    drain(lib)
    done = intake.intake_status(lib, job['id'])
    section = next(target for target in done['targets'] if target['title'] == 'Long section')
    assert section['status'] == 'incomplete'
    assert section['coverage']['target_blocks_supplied'] < section['coverage']['target_blocks_total']
    assert not section['coverage']['complete']


def test_refresh_is_coalesced_and_citation_only_changes_do_not_rebuild(tmp_path, monkeypatch):
    fake_provider(monkeypatch)
    paragraphs = []
    for i in range(29):
        paragraphs.extend([('Heading1', f'Part {i}'), f'The argument in part {i}.'])
    lib, _, job = fixture(tmp_path, paragraphs)
    calls = []
    original = lib.sync
    def sync(*args, **kwargs):
        calls.append(kwargs.get('semantic'))
        return original(*args, **kwargs)
    monkeypatch.setattr(lib, 'sync', sync)
    def apply(lib, target, result, **kwargs):
        return {'request_id': kwargs['request_id'], 'saved': True, 'fields': ['title'],
                'refresh_required': True, 'needs_review': False}
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', apply)
    drain(lib)
    assert len(calls) == 3  # initial exact + 25-target batch + completion
    assert all(value is False for value in calls)
    assert not intake.intake_status(lib, job['id'])['refresh_pending']


def test_semantic_provider_failure_preserves_exact_search(tmp_path, monkeypatch):
    fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path, semantic=True)
    atomic_json(lib.state / 'config.json', {'semantic': {'enabled': True, 'full_build_approved': True, 'engine': 'openai'}})
    def fail(lib, **kwargs):
        raise RuntimeError('synthetic provider failure')
    monkeypatch.setattr('book_library.semantic_library.build_index', fail)
    drain(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['status'] == 'paused'
    assert paused['readiness'] == {'text': 'ready', 'semantic': 'pending', 'metadata_review': 'pending'}
    assert lib.search('copper birds', engine='exact')['direct']


def test_source_hash_cache_avoids_rereading_unchanged_books(tmp_path, monkeypatch):
    lib, source, job = fixture(tmp_path)
    intake._SOURCE_CHECKS.clear()
    calls = []
    real_digest = intake.digest
    def digest(path):
        calls.append(path)
        return real_digest(path)
    monkeypatch.setattr(intake, 'digest', digest)
    assert intake._sources_current(job)
    assert intake._sources_current(job)
    assert len(calls) == 1
    docx(source, ['Changed bytes.'])
    assert not intake._sources_current(job)
    assert len(calls) == 2


def test_refresh_updates_semantics_through_existing_metered_stage(tmp_path, monkeypatch):
    fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path, semantic=True)
    atomic_json(lib.state / 'config.json', {'semantic': {'enabled': True, 'full_build_approved': True, 'engine': 'openai'}})
    semantic_calls = []
    monkeypatch.setattr('book_library.semantic_library.build_index', lambda lib, **kwargs: semantic_calls.append(True))
    def apply(lib, target, result, **kwargs):
        return {'request_id': kwargs['request_id'], 'saved': True, 'fields': ['title'],
                'refresh_required': True, 'needs_review': False}
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', apply)
    drain(lib)
    assert len(semantic_calls) == 2  # Initial text, then coalesced metadata refresh.
    assert intake.intake_status(lib, job['id'])['readiness']['semantic'] == 'ready'


def test_retry_pending_semantics_never_repeats_completed_ai_reviews(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path, semantic=True)
    atomic_json(lib.state / 'config.json', {'semantic': {'enabled': True, 'full_build_approved': True, 'engine': 'openai'}})
    monkeypatch.setattr('book_library.semantic_library.build_index', lambda lib, **kwargs: None)
    drain(lib)
    with writer(lib.state):
        done = intake._load(lib, job['id'])
        done['readiness']['semantic'] = 'pending'
        intake._save(lib, done)
    before = len(calls)
    assert done['readiness']['semantic'] == 'pending'
    monkeypatch.setattr('book_library.semantic_library.build_index', lambda lib, **kwargs: None)
    intake.resume_intake(lib, job['id'], done['version'])
    drain(lib)
    assert len(calls) == before
    assert intake.intake_status(lib, job['id'])['readiness']['semantic'] == 'ready'


def test_timestamp_only_change_reindexes_without_repeating_completed_reviews(tmp_path, monkeypatch):
    import os
    calls = fake_provider(monkeypatch)
    lib, source, job = fixture(tmp_path)
    intake.run_next(lib)
    intake.run_next(lib)
    intake.run_next(lib)
    completed_work = calls[0][0]
    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    intake.run_next(lib)  # Non-substantive front matter also notices stale context.
    paused = intake.intake_status(lib, job['id'])
    assert paused['reason_code'] == 'source_changed'
    intake.resume_intake(lib, job['id'], paused['version'])
    drain(lib)
    assert [call[0] for call in calls].count(completed_work) == 1
    assert intake.intake_status(lib, job['id'])['status'] == 'complete'


def test_saved_result_closes_interrupted_journal_without_regeneration(tmp_path, monkeypatch):
    from book_library import review_jobs
    calls = fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path)
    original = review_jobs.finish_generation
    def crash(*args, **kwargs):
        raise SystemExit('crash after saved response')
    monkeypatch.setattr(review_jobs, 'finish_generation', crash)
    intake.run_next(lib)
    intake.run_next(lib)
    with pytest.raises(SystemExit):
        intake.run_next(lib)
    saved = intake.intake_status(lib, job['id'])['targets'][0]
    assert saved['status'] == 'generated'
    monkeypatch.setattr(review_jobs, 'finish_generation', original)
    intake.run_next(lib)
    assert len(calls) == 1
    assert review_jobs.generation_status(lib, saved['id'])['status'] == 'complete'


@pytest.mark.parametrize('corrupt', [False, True])
def test_missing_saved_result_never_silently_repeats_a_paid_request(tmp_path, monkeypatch, corrupt):
    calls = fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path)
    from book_library.intake_curation import apply_automatic
    def crash(*args, **kwargs):
        raise SystemExit('interrupt before application')
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', crash)
    intake.run_next(lib)
    intake.run_next(lib)
    with pytest.raises(SystemExit):
        intake.run_next(lib)
    saved = intake.intake_status(lib, job['id'])['targets'][0]
    path = lib.state / saved['result_file']
    if corrupt:
        path.write_text('{invalid')
    else:
        path.unlink()
    monkeypatch.setattr('book_library.intake_curation.apply_automatic', apply_automatic)
    intake.run_next(lib)
    assert len(calls) == 1
    assert intake.intake_status(lib, job['id'])['reason_code'] == 'result_missing'
    assert not intake.run_next(lib)


def test_split_book_intake_monitors_all_parts_and_detects_changed_unselected_part(tmp_path, monkeypatch):
    from test_service import seed
    calls = fake_provider(monkeypatch)
    root = tmp_path / 'books'
    first = docx(root / 'Split' / '001.docx', [('Heading1', 'First'), 'Copper birds open the book.'])
    second = docx(root / 'Split' / '002.docx', [('Heading1', 'Second'), 'Silver bells close the book.'])
    lib = seed(root, [{'title': 'Split book', 'paths': [first, second]}])
    job = intake.enqueue_intake(lib, first)
    intake.run_next(lib)
    indexed = intake.intake_status(lib, job['id'])
    assert {file['path'] for file in indexed['files']} == {str(first), str(second)}
    assert len(indexed['requested_files']) == 1
    docx(second, [('Heading1', 'Changed second part'), 'The second part changed.'])
    assert not intake._sources_current(indexed, lib)
    intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['reason_code'] == 'source_changed'
    assert not calls


def test_same_existing_book_selected_by_other_part_reuses_intake(tmp_path, monkeypatch):
    from test_service import seed
    root = tmp_path / 'books'
    first = docx(root / 'Split' / '001.docx', [('Heading1', 'First'), 'The first part.'])
    second = docx(root / 'Split' / '002.docx', [('Heading1', 'Second'), 'The second part.'])
    lib = seed(root, [{'title': 'Split book', 'paths': [first, second]}])
    job = intake.enqueue_intake(lib, first)
    intake.run_next(lib)
    repeated = intake.enqueue_intake(lib, second)
    assert repeated['id'] == job['id']
    assert len(intake.intake_status(lib)['intakes']) == 1


def test_provider_failure_blocks_other_paid_jobs_but_not_exact_indexing(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch, [result(status='unavailable', failure_reason='provider')])
    lib, _, job = fixture(tmp_path)
    drain(lib)
    assert intake.intake_status(lib, job['id'])['reason_code'] == 'provider'
    other = docx(lib.root / 'Other' / 'book.docx', [('Heading1', 'Other'), 'This other book is still searchable.'])
    second = intake.enqueue_intake(lib, other, {'metadata': {'title': 'Other book'}})
    drain(lib)
    blocked = intake.intake_status(lib, second['id'])
    assert blocked['readiness']['text'] == 'ready'
    assert blocked['paid_blocked_by']['id'] == job['id']
    assert len(calls) == 1
    assert lib.search('still searchable', engine='exact')['direct']
    first = intake.intake_status(lib, job['id'])
    intake.resume_intake(lib, job['id'], first['version'])
    drain(lib)
    assert intake.intake_status(lib, second['id'])['status'] == 'complete'


def test_all_book_reviews_precede_long_section_queues(tmp_path, monkeypatch):
    calls = fake_provider(monkeypatch)
    lib, _, first = fixture(tmp_path)
    other = docx(lib.root / 'Other' / 'book.docx', [('Heading1', 'Other'), 'A second book.'])
    second = intake.enqueue_intake(lib, other, {'metadata': {'title': 'Other book'}})
    drain(lib)
    assert calls[0][1] is True and calls[1][1] is True
    assert set(call[0] for call in calls[:2]) == {intake.intake_status(lib, first['id'])['work_ids'][0], intake.intake_status(lib, second['id'])['work_ids'][0]}


def test_section_local_review_does_not_replace_book_isbn_assessment(tmp_path, monkeypatch):
    lib, _, job = fixture(tmp_path)
    def suggest(lib, context, *, allow_external=True, should_continue=None, **kwargs):
        relation = 'related_format' if allow_external else 'unknown'
        return result(assessments=[{'id': 'edition-'+relation, 'kind': 'edition', 'fields': [], 'outcome': 'keep',
            'recommendation': 'Keep the current ISBN.', 'rationale': 'Synthetic fixture comparison.',
            'strength': 'likely', 'edition_relationship': relation, 'evidence': [],
            'comparison': {'relationship': relation, 'records': []}}], book_revision=context['book_revision'])
    monkeypatch.setattr('book_library.review_ai.suggest', suggest)
    drain(lib)
    done = intake.intake_status(lib, job['id'])
    book = lib.review_draft(done['work_ids'][0])
    assert book['assessments'][0]['edition_relationship'] == 'related_format'
    for section in done['targets'][1:]:
        if section['attempts']:
            assert not any(a['kind'] == 'edition' for a in lib.review_draft(section['id'])['assessments'])


def test_clean_pause_resumes_web_stages_once_without_repeating_local_review(tmp_path, monkeypatch):
    from book_library import review_ai as ai
    from test_review_ai import response, result as model_result, web_response
    lib, _, job = fixture(tmp_path)
    calls = []
    items = iter([
        response(model_result(unresolved=[{'field': 'bibliography.publisher', 'reason': 'Not shown in the copy.'}])),
        web_response('https://www.worldcat.org/title/fixture', 'The source is inconclusive.'),
        response(model_result()),
    ])
    class Client:
        responses = None
        def __init__(self):
            self.responses = self
        async def create(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                intake.pause_intake(lib, job['id'])
            return next(items)
        async def close(self):
            pass
    monkeypatch.setattr(ai, 'api_key', lambda state: 'fixture-key')
    async def factory(key):
        return Client()
    monkeypatch.setattr(ai, '_make_client', factory)
    intake.run_next(lib)
    intake.run_next(lib)
    intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['status'] == 'paused' and paused['reason_code'] == 'user'
    assert len(calls) == 1 and paused['targets'][0]['continuation_pending']
    original_attempt = paused['targets'][0]['attempt_id']
    checkpoint = intake._checkpoint(lib, paused, paused['targets'][0])
    assert set(checkpoint['responses']) == {'local'} and checkpoint['in_flight'] is None
    intake.resume_intake(lib, job['id'], paused['version'])
    intake.run_next(lib)
    resumed = intake.intake_status(lib, job['id'])
    assert resumed['status'] != 'paused'
    assert len(calls) == 3 and resumed['targets'][0]['attempts'] == 1
    assert resumed['targets'][0]['attempt_id'] == original_attempt
    assert resumed['targets'][0]['status'] == 'needs_evidence'


def test_bookkeeping_delay_queues_authorized_cached_continuation(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from book_library import review_ai as ai
    from book_library.budget import Budget
    from book_library.storage import jsonl
    from test_review_ai import use_client, response, result as model_result, web_response
    lib, _, job = fixture(tmp_path)
    authorization = copy.deepcopy(job['authorization'])
    clock = [0.0]
    monkeypatch.setattr(ai, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    client = use_client(monkeypatch, [
        response(model_result(unresolved=[{'field': 'bibliography.publisher', 'reason': 'Not shown in the copy.'}])),
        web_response('https://www.worldcat.org/title/fixture', 'The source is inconclusive.'),
        response(model_result()),
    ])
    settle = Budget.settle_review_stage
    delayed = False
    def delayed_settle(self, reservation, stage, usage, **kwargs):
        nonlocal delayed
        if stage == 'local' and not delayed:
            clock[0] += 103
            delayed = True
        return settle(self, reservation, stage, usage, **kwargs)
    monkeypatch.setattr(Budget, 'settle_review_stage', delayed_settle)
    for _ in range(3):
        assert intake.run_next(lib)
    queued = intake.intake_status(lib, job['id'])
    assert queued['status'] == 'queued' and queued['reason_code'] == 'continuation'
    assert queued['targets'][0]['continuation_pending'] and len(client.calls) == 1
    attempt = queued['targets'][0]['attempt_id']
    checkpoint = intake._checkpoint(lib, queued, queued['targets'][0])
    assert checkpoint['in_flight'] is None and set(checkpoint['responses']) == {'local'}
    assert Budget(lib.state).report()['uncertain_reservations'] == 0

    # The original intake authorization permits this unstarted work; no resume
    # or fresh authorization is needed because no provider request failed.
    assert intake.run_next(lib)
    done = intake.intake_status(lib, job['id'])
    assert done['status'] != 'paused' and len(client.calls) == 3
    assert done['reason_code'] is None and done['reason'] == ''
    assert done['authorization'] == authorization
    assert done['targets'][0]['attempt_id'] == attempt
    assert done['targets'][0]['attempts'] == 1
    assert not done['targets'][0]['continuation_pending']
    assert [e['stage'] for e in jsonl(Budget(lib.state).path) if e['kind'] == 'review_stage_started'] == ['local', 'web', 'normalize']


def test_semantic_pause_is_monthly_and_requires_only_one_resume(tmp_path, monkeypatch):
    from book_library.semantic_library import SemanticPaused
    fake_provider(monkeypatch)
    lib, _, job = fixture(tmp_path, semantic=True)
    atomic_json(lib.state / 'config.json', {'semantic': {'enabled': True, 'full_build_approved': True, 'engine': 'openai'}})
    calls = []
    def build(lib, *, should_continue, budget_scope, allowed_file_ids, source_bindings):
        calls.append(budget_scope)
        if len(calls) == 1:
            intake.pause_intake(lib, job['id'])
            assert not should_continue()
            raise SemanticPaused()
        assert should_continue()
    monkeypatch.setattr('book_library.semantic_library.build_index', build)
    intake.run_next(lib)
    intake.run_next(lib)
    paused = intake.intake_status(lib, job['id'])
    assert paused['reason_code'] == 'user' and paused['readiness']['text'] == 'ready'
    intake.resume_intake(lib, job['id'], paused['version'])
    drain(lib)
    assert intake.intake_status(lib, job['id'])['status'] == 'complete'
    assert calls == ['monthly', 'monthly']


def test_stage_checkpoint_survives_crash_before_final_result_without_paid_replay(tmp_path, monkeypatch):
    from book_library import review_ai as ai
    from test_review_ai import use_client, response, result as model_result
    lib, _, job = fixture(tmp_path)
    client = use_client(monkeypatch, [response(model_result())])
    persisted = intake._persist_checkpoint
    crashed = []
    def checkpoint(lib, intake_id, target_id, value):
        persisted(lib, intake_id, target_id, value)
        if 'local' in value['responses'] and not crashed:
            crashed.append(True)
            raise SystemExit('process stopped after durable local response')
    monkeypatch.setattr(intake, '_persist_checkpoint', checkpoint)
    intake.run_next(lib)
    intake.run_next(lib)
    with pytest.raises(SystemExit):
        intake.run_next(lib)
    assert client.closed and len(client.calls) == 1
    state = intake.intake_status(lib, job['id'])
    assert state['targets'][0]['status'] == 'running'
    assert not intake._result_path(lib, state, state['targets'][0]).exists()
    monkeypatch.setattr(ai, 'api_key', lambda state: pytest.fail('A cached response needs no credential or provider call'))
    assert intake.run_next(lib)
    resumed = intake.intake_status(lib, job['id'])
    assert resumed['targets'][0]['status'] == 'no_change'
    assert resumed['targets'][0]['attempts'] == 1 and len(client.calls) == 1


def test_generated_continuation_obeys_another_intakes_paid_barrier(tmp_path, monkeypatch):
    fake_provider(monkeypatch)
    lib, _, first = fixture(tmp_path)
    intake.run_next(lib)
    intake.run_next(lib)
    intake.run_next(lib)
    second_source = docx(lib.root / 'Second' / 'book.docx', ['A different source.'])
    second = intake.enqueue_intake(lib, second_source)
    with writer(lib.state):
        blocker = intake._load(lib, second['id'])
        intake._pause(blocker, 'budget', 'Monthly budget needs explicit resumption.')
        intake._save(lib, blocker)
        job = intake._load(lib, first['id'])
        job['targets'][0].update(status='generated', continuation_pending=True)
        intake._save(lib, job)
    assert not intake._may_continue(lib, first['id'])
    monkeypatch.setattr('book_library.review_ai.suggest', lambda *args, **kwargs: pytest.fail('Paid continuation must wait'))
    assert not intake.run_next(lib)
    assert intake.intake_status(lib, first['id'])['paid_blocked_by']['id'] == second['id']
