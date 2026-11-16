"""Durable, explicitly authorized intake; reading progress never starts work.

One worker claim covers one bounded stage or target. Provider calls run outside
the shared metadata writer lock. Results are saved before application so a
restart can finish a correction without repeating a paid request.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
from pathlib import Path

from .storage import atomic_json, digest, now, read_json, stable, uid, writer
from .intake_scope import capture_scope, scope_current

POLICY_VERSION = 'intake-1'
REFRESH_BATCH_SIZE = 25
TERMINAL = {'applied', 'no_change', 'needs_evidence', 'incomplete', 'non_substantive', 'failed', 'stale'}
_SOURCE_CHECKS = {}


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value):
        raise ValueError('Choose a valid intake identifier.')
    return value


def _path(lib, intake_id):
    return lib.state / 'intakes' / (_identifier(intake_id) + '.json')


def _load(lib, intake_id):
    job = read_json(_path(lib, intake_id))
    if not isinstance(job, dict):
        raise ValueError('That intake is not available.')
    return job


def _save(lib, job):
    job['version'] = job.get('version', 0) + 1
    job['updated_at'] = now()
    atomic_json(_path(lib, job['id']), job)
    return job


def _check_version(job, expected_version):
    if expected_version is not None and (type(expected_version) is not int or expected_version != job['version']):
        raise ValueError('Intake progress changed. Reload before making this choice.')


def _summary(job, *, detail=False):
    result = copy.deepcopy(job)
    targets = result.get('targets', [])
    result['counts'] = {
        'total': len(targets), 'reviewed': sum(t['status'] in TERMINAL for t in targets),
        'queued': sum(t['status'] in {'queued', 'running', 'generated'} for t in targets),
        'needs_attention': sum(t['status'] in {'needs_evidence', 'incomplete', 'failed', 'stale'} for t in targets),
        'automatic_changes': sum(len(t.get('application', {}).get('fields', [])) for t in targets),
        'attempted': sum(t.get('attempts', 0) > 0 for t in targets),
        'fully_inspected': sum(t.get('coverage', {}).get('complete') is True and t['status'] in {'applied', 'no_change', 'needs_evidence'} for t in targets),
    }
    result['url'] = '/intake/' + result['id']
    result['status_label'] = {'queued': 'Waiting to start', 'running': 'In progress', 'paused': 'Paused',
                              'complete': 'Finished', 'needs_attention': 'Needs attention'}.get(result['status'], result['status'])
    result['review_urls'] = ['/review/' + work_id for work_id in result.get('work_ids', [])]
    if not detail:
        result.pop('targets', None)
        result.pop('files', None)
        result.pop('history', None)
    return result


def _paid_blocker(jobs):
    return next((job for job in jobs if job.get('status') == 'paused' and job.get('reason_code') in
                 {'budget', 'provider', 'credentials', 'uncertain'}), None)


def _with_blocker(result, blocker):
    if (blocker and result.get('id') != blocker['id'] and result.get('status') in {'queued', 'running'}
            and (_review_authorized(result) or _semantic_authorized(result))):
        result['paid_blocked_by'] = {'id': blocker['id'], 'url': '/intake/' + blocker['id'],
                                     'reason': blocker['reason'], 'reason_code': blocker['reason_code']}
    return result


def intake_status(lib, intake_id=None):
    """A read-only snapshot. Never recover, enqueue, reserve, or run on GET."""
    jobs = [read_json(path) for path in sorted((lib.state / 'intakes').glob('*.json'))]
    jobs = sorted((job for job in jobs if isinstance(job, dict)), key=lambda j: j['created_at'], reverse=True)
    blocker = _paid_blocker(jobs)
    if intake_id:
        return _with_blocker(_summary(_load(lib, intake_id), detail=True), blocker)
    return {'intakes': [_with_blocker(_summary(job), blocker) for job in jobs],
            **({'paid_blocked_by': {'id': blocker['id'], 'url': '/intake/' + blocker['id'], 'reason': blocker['reason']}} if blocker else {})}


def _review_authorized(job):
    return job.get('authorization', {}).get('review') == 'every_indexed_heading'


def _semantic_authorized(job):
    # Missing field belongs to legacy jobs, whose semantic work followed the
    # library's existing explicit engine approval. New jobs always snapshot it.
    return job.get('authorization', {}).get('semantic', True) is True


def enqueue_intake(lib, path, context=None, *, review_metadata=None, local_only=False):
    from .public_config import initialize, read_settings, require_budget
    if review_metadata is not None and type(review_metadata) is not bool:
        raise ValueError('Choose whether metadata review is requested.')
    if type(local_only) is not bool or (local_only and review_metadata is not None):
        raise ValueError('Use --local-only by itself, without metadata-review overrides.')
    initialize(lib)
    config = read_settings(lib.state)
    review_requested = False if local_only else (review_metadata if review_metadata is not None else config.get('metadata_review', {}).get('enabled') is True)
    semantic_settings = config.get('semantic', {})
    semantic_requested = not local_only and bool(semantic_settings.get('enabled') and semantic_settings.get('full_build_approved'))
    if review_requested:
        require_budget(lib.state)
    from .intake_registration import register_intake
    acquisition = register_intake(lib, path, context)
    identity = acquisition.get('identity') or acquisition['id']
    intake_id = stable('intake', identity, POLICY_VERSION)
    with writer(lib.state):
        existing = read_json(_path(lib, intake_id))
        if existing:
            if not _sources_current(existing, lib):
                _pause(existing, 'source_changed', 'The source changed since this intake. Resume explicitly to review the updated book.')
                _save(lib, existing)
            return _summary(existing, detail=True)
        existing_work_ids = set(acquisition.get('existing_work_ids', []))
        monitored_files = copy.deepcopy(acquisition['files'])
        if existing_work_ids:
            catalog_files = [file for file in lib._catalog()['files'].values() if file['work_id'] in existing_work_ids]
            requested_hashes = {(file['path'], digest(file['path']) if Path(file['path']).is_file() else None) for file in catalog_files}
            monitored_files = [{'path': file['path'], 'sha256': digest(file['path']), 'sequence': file.get('sequence', 0)} for file in catalog_files]
            for prior_path in (lib.state / 'intakes').glob('*.json'):
                prior = read_json(prior_path, {})
                if (set(prior.get('work_ids', [])) == existing_work_ids and
                        {(file['path'], file.get('sha256')) for file in prior.get('files', [])} == requested_hashes):
                    return _summary(prior, detail=True)
        job = {'id': intake_id, 'version': 0, 'policy_version': POLICY_VERSION,
               'authorization': {'kind': 'explicit_intake', 'review': 'every_indexed_heading' if review_requested else None,
                                 'semantic': semantic_requested, 'local_only': local_only,
                                 'review_source': 'local_only' if local_only else 'explicit_override' if review_metadata is not None else 'library_default',
                                 'review_approval_evidence': (config.get('metadata_review', {}).get('approval_evidence') if review_metadata is None else 'Explicit per-intake review choice') if review_requested else None,
                                 'automatic_changes': 'supported_provisional' if review_requested else None, 'created_at': now()},
               'status': 'queued', 'stage': 'registered', 'reason': '', 'reason_code': None,
               'created_at': now(), 'updated_at': now(), 'acquisition_id': acquisition['id'],
               'source_label': acquisition.get('source_label', acquisition.get('provider', 'Local copy')),
               'duplicate_candidates': copy.deepcopy(acquisition.get('duplicate_candidates', [])),
               'discrepancies': copy.deepcopy(acquisition.get('discrepancies', [])),
               'path': acquisition.get('path', str(path)), 'files': monitored_files,
               'source_scope': capture_scope(lib, monitored_files, requested_path=path),
               'requested_files': copy.deepcopy(acquisition.get('requested_files', acquisition['files'])),
               'work_ids': [], 'work_titles': [], 'targets': [], 'history': [],
               'readiness': {'text': 'pending', 'semantic': 'pending' if semantic_requested else 'not_requested',
                             'metadata_review': 'pending' if review_requested else 'not_requested'},
               'refresh_pending': False, 'reviewed_since_refresh': 0, 'active_operation': None}
        _save(lib, job)
    return _summary(job, detail=True)


def _sources_current(job, lib=None):
    if lib is not None and job.get('work_ids'):
        current_files = {file['path'] for file in lib._catalog()['files'].values() if file['work_id'] in job['work_ids']}
        if current_files != {source['path'] for source in job['files']}:
            return False
    for source in job['files']:
        try:
            path = Path(source['path'])
            if path.is_symlink() or not path.is_file():
                return False
            stat = path.stat()
            identity = (str(path), source['sha256'])
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if _SOURCE_CHECKS.get(identity) != signature:
                if digest(path) != source['sha256']:
                    return False
                if len(_SOURCE_CHECKS) >= 2048:
                    _SOURCE_CHECKS.clear()
                _SOURCE_CHECKS[identity] = signature
        except OSError:
            return False
    return True


def _pause(job, code, message):
    job.update(status='paused', reason_code=code, reason=message)


def pause_intake(lib, intake_id, expected_version=None):
    with writer(lib.state):
        job = _load(lib, intake_id)
        _check_version(job, expected_version)
        if job['status'] != 'complete':
            _pause(job, 'user', 'Paused at your request. Any request already in progress may finish; no next paid request will start.')
            _save(lib, job)
    return _summary(job, detail=True)


def resume_intake(lib, intake_id, expected_version=None):
    with writer(lib.state):
        job = _load(lib, intake_id)
        _check_version(job, expected_version)
        if not scope_current(lib, job):
            raise ValueError('Unselected book files appeared beside this intake. Move them out of this book’s folder or submit a new intake with an explicit complete selection before resuming.')
        if job.get('active_operation') and _worker_busy(lib):
            raise ValueError('The current intake request is still finishing. Resume after it completes.')
        sources_current = _sources_current(job, lib)
        if job['status'] == 'complete' and sources_current:
            if job.get('readiness', {}).get('semantic') == 'pending':
                job['stage'] = 'semantic'
            else:
                return _summary(job, detail=True)
        if not sources_current:
            # This explicit resume authorizes examining the new bytes, without
            # transferring old target identities or results onto new content.
            updated = []
            sources = {source['path']: source for source in job['files']}
            for source in sources.values():
                path = Path(source['path'])
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Restore the missing source files before resuming intake.')
                updated.append({**source, 'sha256': digest(path)})
            job['history'].append({'finished_at': now(), 'reason': 'source_changed', 'targets': job['targets'], 'files': job['files']})
            job.update(files=updated, targets=[], stage='registered', work_ids=[], work_titles=[])
            job['readiness'] = {'text': 'pending', 'semantic': 'pending' if _semantic_authorized(job) else 'not_requested',
                                'metadata_review': 'pending' if _review_authorized(job) else 'not_requested'}
        elif job.get('reason_code') == 'source_changed':
            # A timestamp-only change still needs the exact index's source
            # stat refreshed, but must not repeat completed paid reviews.
            job.update(stage='indexing', preserve_targets_on_index=True)
        for target in job['targets']:
            if target['status'] in {'interrupted', 'retry_pending'}:
                # Every retry has a fresh durable request identity. Previous
                # uncertain reservations remain in the shared provider ledger.
                target['status'] = 'queued'
                target['continuation_pending'] = False
        if job.get('stage') == 'semantic_running':
            job['stage'] = 'semantic'
        if job.get('semantic_refresh_pending') and job['stage'] not in ('registered', 'indexing'):
            job['stage'] = 'semantic'
        job.pop('semantic_retry_required', None)
        job.pop('refresh_error', None)
        job.update(status='queued', reason='', reason_code=None, active_operation=None)
        _save(lib, job)
    return _summary(job, detail=True)



def authorize_review(lib, intake_id, expected_version=None):
    """Explicitly add review to an existing intake without changing its identity."""
    from .public_config import require_budget
    require_budget(lib.state)
    with writer(lib.state):
        job = _load(lib, intake_id)
        _check_version(job, expected_version)
        if _review_authorized(job):
            return _summary(job, detail=True)
        if job.get('active_operation'):
            raise ValueError('Wait for the current intake operation to finish before requesting metadata review.')
        if not scope_current(lib, job) or not _sources_current(job, lib):
            raise ValueError('The selected source files changed. Resolve and resume that intake before authorizing review.')
        job['authorization'].update(review='every_indexed_heading', automatic_changes='supported_provisional',
                                    review_source='explicit_later_request', review_authorized_at=now(),
                                    review_approval_evidence='Explicit request to review this existing intake')
        for target in job['targets']:
            if target['status'] == 'not_requested':
                target['status'] = 'queued'
        job['readiness']['metadata_review'] = 'pending'
        if job['status'] == 'complete':
            job.update(status='queued', stage='reviewing', reason='', reason_code=None)
            job.pop('finished_at', None)
        _save(lib, job)
    return _summary(job, detail=True)


def undo_intake(lib, intake_id, event_id=None, expected_version=None):
    from .intake_curation import undo_automatic
    if expected_version is None:
        expected_version = _load(lib, intake_id)['version']
    return undo_automatic(lib, intake_id, event_id, expected_version=expected_version)


@contextlib.contextmanager
def _worker_claim(lib):
    lib.state.mkdir(parents=True, exist_ok=True)
    with (lib.state / 'intake-worker.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _worker_busy(lib):
    with _worker_claim(lib) as claimed:
        return not claimed


def _result_path(lib, job, target):
    return lib.state / 'intake-results' / job['id'] / (_identifier(target['attempt_id']) + '.json')


def _checkpoint_path(lib, job, target):
    return _result_path(lib, job, target).with_suffix('.checkpoint.json')


def _checkpoint(lib, job, target):
    try:
        value = read_json(_checkpoint_path(lib, job, target))
    except (ValueError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _persist_checkpoint(lib, intake_id, target_id, value):
    with writer(lib.state):
        job = _load(lib, intake_id)
        target = _target(job, target_id)
        atomic_json(_checkpoint_path(lib, job, target), value)


def _targets(lib, job):
    catalog = lib._catalog()
    requested = {source['path'] for source in job['files']}
    files = [file for file in catalog['files'].values() if file['path'] in requested]
    work_ids = list(dict.fromkeys(file['work_id'] for file in files))
    if not work_ids:
        raise ValueError('The selected files did not produce an indexed book.')
    job['work_ids'] = work_ids
    # A path can name one part of an already indexed split book. Reviewing the
    # whole work requires monitoring every part, not merely the selected path.
    files = [file for file in catalog['files'].values() if file['work_id'] in work_ids]
    allowed = set(job.get('source_scope', {}).get('allowed_paths', requested))
    if any(file['path'] not in allowed for file in files):
        raise ValueError('The indexed work includes files outside this intake’s explicit selection.')
    job['files'] = [{'path': file['path'], 'sha256': file.get('sha256'), 'sequence': file.get('sequence', 0)} for file in files]
    job['work_titles'] = [catalog['works'][work_id]['title'] for work_id in work_ids]
    targets = []
    with lib._db() as db:
        for work_id in work_ids:
            title = catalog['works'][work_id]['title']
            targets.append({'id': work_id, 'work_id': work_id, 'scope': 'work', 'title': title,
                            'status': 'queued', 'attempts': 0, 'review_url': '/review/' + work_id})
            for section in db.execute('SELECT id,title,ordinal FROM sections WHERE work_id=? ORDER BY ordinal', (work_id,)):
                targets.append({'id': section['id'], 'work_id': work_id, 'scope': 'section', 'title': section['title'],
                                'ordinal': section['ordinal'], 'status': 'queued', 'attempts': 0,
                                'review_url': '/review/' + section['id']})
    if job.pop('preserve_targets_on_index', False):
        previous = {target['id']: target for target in job['targets']}
        targets = [copy.deepcopy(previous.get(target['id'], target)) for target in targets]
    if not _review_authorized(job):
        targets = [{**target, 'status': 'not_requested'} for target in targets]
    job['targets'] = targets
    ready = [f for f in files if f.get('available') and f.get('state') == 'ready']
    job['readiness']['text'] = 'ready' if len(ready) == len(files) else 'partial' if ready else 'unavailable'
    job['extraction_errors'] = [{'path': f['path'], 'error': f.get('error', 'Source text is unavailable.')} for f in files if f not in ready]


def _recover(lib, job):
    """Called only while owning the cross-process worker claim."""
    changed = False
    for target in job['targets']:
        if target['status'] != 'running':
            continue
        checkpoint = _checkpoint(lib, job, target) if target.get('attempt_id') else None
        if target.get('attempt_id') and _result_path(lib, job, target).is_file() and not (checkpoint and checkpoint.get('in_flight')):
            target['status'] = 'generated'
            try:
                saved_result = read_json(_result_path(lib, job, target), {})
                target['continuation_pending'] = isinstance(saved_result, dict) and saved_result.get('failure_reason') == 'paused'
            except (ValueError, OSError):
                pass  # _review reports an unreadable saved result without retry.
        elif checkpoint and not checkpoint.get('in_flight'):
            # Completed provider responses are durable before the next stage.
            # Reconstruct them and continue only genuinely unstarted work.
            target.update(status='queued', continuation_pending=True)
        else:
            target['status'] = 'interrupted'
            _pause(job, 'uncertain', 'A review was interrupted before its result was saved. Its possible cost remains reserved. Resume explicitly to try again.')
        # No provider remains active under this process-wide worker claim,
        # including after a crash between saving a partial result and closing
        # its foreground generation segment.
        journal_path = lib.state / 'review-jobs' / (target['id'] + '.json')
        journal = read_json(journal_path, {})
        previous = journal.get('requests', {}).get(target.get('generation_id', target.get('attempt_id')))
        if previous and previous.get('status') == 'running':
            previous.update(status='interrupted', finished_at=now())
            atomic_json(journal_path, journal)
        changed = True
    if job['stage'] == 'semantic_running':
        _pause(job, 'uncertain', 'Semantic indexing was interrupted. Existing text remains available; resume explicitly to continue metered work.')
        changed = True
    if changed:
        job['active_operation'] = None
        _save(lib, job)
    return changed


def _refresh(lib, intake_id):
    with writer(lib.state):
        job = _load(lib, intake_id)
        if not job.get('refresh_pending'):
            return
        if not scope_current(lib, job) or not _sources_current(job, lib):
            changed_scope = not scope_current(lib, job)
            _pause(job, 'scope_changed' if changed_scope else 'source_changed',
                   'The selected source files changed before search refresh. Saved corrections are retained; resolve the source selection before resuming.')
            _save(lib, job)
            return
        job['active_operation'] = {'kind': 'refresh', 'pid': os.getpid()}
        _save(lib, job)
    try:
        lib.sync(semantic=False)
    except Exception:
        with writer(lib.state):
            job = _load(lib, intake_id)
            job['refresh_error'] = 'Saved changes are retained, but the search refresh did not finish.'
            job['active_operation'] = None
            _save(lib, job)
        return
    with writer(lib.state):
        job = _load(lib, intake_id)
        job.update(refresh_pending=False, reviewed_since_refresh=0, active_operation=None)
        job.pop('refresh_error', None)
        settings = read_json(lib.state / 'config.json', {}).get('semantic', {})
        if _semantic_authorized(job) and settings.get('enabled') and settings.get('full_build_approved'):
            job['readiness']['semantic'] = 'pending'
            job['semantic_refresh_pending'] = True
            if job['status'] in {'queued', 'running'} and not job.get('semantic_retry_required'):
                job['stage'] = 'semantic'
        _save(lib, job)


def _index(lib, intake_id):
    with writer(lib.state):
        job = _load(lib, intake_id)
        if job['status'] == 'paused':
            return
        if not scope_current(lib, job):
            _pause(job, 'scope_changed', 'Unselected book files appeared in this book’s folder. Move them out or submit an explicit complete selection; intake has not expanded to include them.')
            _save(lib, job)
            return
        job.update(stage='indexing', status='running', active_operation={'kind': 'indexing', 'pid': os.getpid()})
        _save(lib, job)
    failure_stage = 'exact_sync'
    try:
        lib.sync(job['path'], semantic=False)
        failure_stage = 'review_targets'
        with writer(lib.state):
            job = _load(lib, intake_id)
            _targets(lib, job)
            job.pop('indexing_error', None)
            job.update(stage='semantic', active_operation=None)
            _save(lib, job)
    except Exception as exc:
        with writer(lib.state):
            job = _load(lib, intake_id)
            # Sync also builds and publishes the index; a failure here does not
            # establish an extraction defect. Keep a bounded diagnostic without
            # exception messages, tracebacks, book text, or credential values.
            job['indexing_error'] = {'stage': failure_stage, 'error_type': type(exc).__name__[:120],
                                     'occurred_at': now()}
            _pause(job, 'indexing', 'Text indexing did not finish. Verified files are retained; resume to retry indexing without downloading again.')
            job.update(stage='registered', active_operation=None)
            _save(lib, job)


def _semantic(lib, intake_id):
    settings = read_json(lib.state / 'config.json', {}).get('semantic', {})
    with writer(lib.state):
        job = _load(lib, intake_id)
        if job['status'] == 'paused':
            return
        if not _semantic_authorized(job) or not settings.get('enabled') or not settings.get('full_build_approved'):
            job['readiness']['semantic'] = 'not_requested' if not _semantic_authorized(job) else 'disabled'
            job['stage'] = 'reviewing'
            _save(lib, job)
            return
        job.update(stage='semantic_running', active_operation={'kind': 'semantic', 'pid': os.getpid()})
        job['status'] = 'running'
        _save(lib, job)
    try:
        from .semantic_library import build_index, intake_source_bindings
        bindings = intake_source_bindings(lib, job['files'])
        build_index(lib, should_continue=lambda: _may_continue(lib, intake_id), budget_scope='monthly',
                    allowed_file_ids=sorted(bindings), source_bindings=bindings)
    except Exception as exc:
        from .budget import BudgetExceeded
        from .semantic_library import SemanticPaused
        with writer(lib.state):
            job = _load(lib, intake_id)
            job['readiness']['semantic'] = 'pending'
            job.update(stage='semantic', active_operation=None)
            if isinstance(exc, SemanticPaused):
                if job['status'] != 'paused':
                    _pause(job, 'user', 'Semantic indexing paused. Completed indexing work is retained.')
                _save(lib, job)
                return
            job['semantic_retry_required'] = True
            job['semantic_reason'] = 'Semantic indexing did not finish. Existing text remains available; resume indexing explicitly to retry.'
            _pause(job, 'budget' if isinstance(exc, BudgetExceeded) else 'provider',
                   'Semantic indexing is pending. Text remains available. Resume explicitly when the budget or provider issue is resolved; uncertain provider charges remain reserved.')
            _save(lib, job)
        return
    with writer(lib.state):
        job = _load(lib, intake_id)
        job['readiness']['semantic'] = 'ready'
        job['semantic_refresh_pending'] = False
        job.pop('semantic_retry_required', None)
        job.pop('semantic_reason', None)
        job.update(stage='reviewing', active_operation=None)
        _save(lib, job)


def _target(job, target_id):
    return next(target for target in job['targets'] if target['id'] == target_id)


def _finish_target(lib, intake_id, target_id, **updates):
    with writer(lib.state):
        job = _load(lib, intake_id)
        target = _target(job, target_id)
        was_terminal = target['status'] in TERMINAL
        target.update(**updates, finished_at=now())
        if not was_terminal and target['status'] in TERMINAL:
            job['reviewed_since_refresh'] += 1
        job['active_operation'] = None
        _save(lib, job)


def _may_continue(lib, intake_id):
    """Recheck authorization between provider stages, never during a request."""
    with writer(lib.state):
        job = _load(lib, intake_id)
        if job['status'] == 'paused':
            return False
        if _paid_blocker([read_json(path, {}) for path in (lib.state / 'intakes').glob('*.json')]):
            return False
        if not scope_current(lib, job):
            _pause(job, 'scope_changed', 'Unselected files appeared beside this book. No further paid request will start until its scope is resolved.')
            _save(lib, job)
            return False
        if not _sources_current(job, lib):
            _pause(job, 'source_changed', 'The source changed during this review. Its completed results are retained; resume against the updated source.')
            _save(lib, job)
            return False
    return True


def _review(lib, intake_id, target_id):
    from .review_ai import suggest
    from .review_jobs import begin_generation, finish_generation
    with writer(lib.state):
        job = _load(lib, intake_id)
        if not _review_authorized(job):
            return
        target = _target(job, target_id)
        continuing = bool(target.get('continuation_pending'))
        checkpoint = _checkpoint(lib, job, target) if continuing else None
        if target['status'] == 'generated':
            try:
                result = read_json(_result_path(lib, job, target))
            except (ValueError, OSError):
                result = None
            if not (isinstance(result, dict) and isinstance(result.get('suggestions'), list)
                    and isinstance(result.get('unresolved'), list) and result.get('status') in {'complete', 'partial', 'unavailable', 'failed'}):
                target['status'] = 'interrupted'
                _pause(job, 'result_missing', 'A saved review result is missing or unreadable. No new paid request was made. Resume explicitly if you want to request a replacement.')
                _save(lib, job)
                return
            if result.get('failure_reason') == 'paused':
                checkpoint = _checkpoint(lib, job, target)
                if checkpoint is None:
                    target['status'] = 'interrupted'
                    _pause(job, 'result_missing', 'The paused review’s saved stages are unavailable. Resume explicitly to request a replacement.')
                    _save(lib, job)
                    return
                continuing = True
                result = None
        else:
            result = None
    if result is None:
        try:
            context = lib.review_context(target_id)
        except (ValueError, KeyError):
            _finish_target(lib, intake_id, target_id, status='stale', reason='This source target is unavailable. Review its updated structure before retrying.')
            return
        if not context.get('source_current'):
            with writer(lib.state):
                job = _load(lib, intake_id)
                _pause(job, 'source_changed', 'The source changed. Resume to re-index it before continuing reviews.')
                _save(lib, job)
            return
        if continuing and (checkpoint is None or checkpoint.get('revision') != context['revision']):
            _finish_target(lib, intake_id, target_id, status='stale', reason='The information changed while this review was paused. Saved stages were retained without sending another request; review the updated information manually.')
            return
        target_evidence = [source for source in context.get('evidence', []) if source.get('in_target')]
        coverage = copy.deepcopy(context.get('coverage', {}))
        coverage['complete'] = bool(context.get('coverage_complete'))
        if target['scope'] == 'section' and not any(source.get('kind') not in ('heading', 'pagebreak') and source.get('text', '').strip() for source in target_evidence):
            _finish_target(lib, intake_id, target_id, status='non_substantive', coverage=coverage,
                           reason='This indexed heading has no substantive target text. It was accounted for without a paid review.')
            return
        if target['scope'] == 'section':
            book_draft = read_json(lib.state / 'reviews' / (_identifier(target['work_id']) + '.json'), {})
            context['intake_book_assessments'] = [copy.deepcopy(assessment) for assessment in book_draft.get('assessments', [])
                if assessment.get('kind') == 'edition' and not assessment.get('stale')
                and assessment.get('book_revision') == context.get('book_revision')][:4]
        with writer(lib.state):
            job = _load(lib, intake_id)
            if job['status'] == 'paused':
                return
            target = _target(job, target_id)
            if not continuing:
                target.update(attempts=target['attempts'] + 1, attempt_id=uid('intake_attempt'),
                              revision=context['revision'], coverage=coverage, started_at=now())
            target.update(status='running', generation_id=uid('intake_continuation') if continuing else target['attempt_id'])
            job['active_operation'] = {'kind': 'review', 'target_id': target_id, 'pid': os.getpid()}
            job['status'] = 'running'
            _save(lib, job)
            attempt_id = target['attempt_id']
            generation_id = target['generation_id']
        try:
            claim = begin_generation(lib, target_id, context['revision'], generation_id)
            if claim.get('replay'):
                raise ValueError('This paid review attempt has already been claimed.')
            result = suggest(lib, context, allow_external=target['scope'] == 'work',
                             should_continue=lambda: _may_continue(lib, intake_id), checkpoint=checkpoint,
                             on_checkpoint=lambda value: _persist_checkpoint(lib, intake_id, target_id, value))
        except Exception:
            with writer(lib.state):
                job = _load(lib, intake_id)
                _target(job, target_id)['status'] = 'interrupted'
                job['active_operation'] = None
                _pause(job, 'uncertain', 'This review was interrupted. Possible provider costs remain reserved; resume explicitly to retry.')
                _save(lib, job)
            return
        if target['scope'] == 'section':
            # A local section pass must not replace the book's stronger shared
            # ISBN investigation with a fresh unknown/local-only fallback.
            result = copy.deepcopy(result)
            result['assessments'] = [assessment for assessment in result.get('assessments', []) if assessment.get('kind') != 'edition']
            result['unresolved'] = [finding for finding in result.get('unresolved', [])
                if finding.get('category') != 'edition_conflict' and finding.get('field') not in
                {'bibliography.publisher', 'bibliography.year', 'bibliography.edition', 'isbn'}]
        with writer(lib.state):
            job = _load(lib, intake_id)
            target = _target(job, target_id)
            # The deterministic attempt filename also recovers a crash between
            # these writes, before the compact queue record says generated.
            atomic_json(_result_path(lib, job, target), result)
            target.update(status='generated', result_file=str(_result_path(lib, job, target).relative_to(lib.state)),
                          continuation_pending=result.get('failure_reason') == 'paused')
            job['active_operation'] = None
            _save(lib, job)
        finish_generation(lib, target_id, generation_id, status=result.get('status', 'complete'))
    else:
        # Recover a crash after the result's atomic write but before closing its
        # provider journal. Closing it never releases an uncertain reservation.
        finish_generation(lib, target_id, target.get('generation_id', target['attempt_id']), status=result.get('status', 'complete'))
    # Read again: Pause or a manual field edit may have arrived during the call.
    with writer(lib.state):
        job = _load(lib, intake_id)
        target = _target(job, target_id)
        if job['status'] == 'paused' and job.get('reason_code') == 'user':
            return
        if result.get('failure_reason') == 'paused':
            if job['status'] != 'paused':
                job.update(status='queued', reason_code='paid_blocked',
                           reason='Saved review stages are waiting for the paused paid-work issue to be resolved.')
                _save(lib, job)
            return
    revision = target['revision']
    try:
        lib.store_review_suggestions(target_id, result, revision=revision)
    except (ValueError, KeyError):
        _finish_target(lib, intake_id, target_id, status='stale', reason='These suggestions no longer match the available fields. The original result is retained in intake history.')
        return
    from .intake_curation import apply_automatic
    try:
        receipt = apply_automatic(lib, target_id, result, intake_id=intake_id,
                                  request_id=target['attempt_id'], revision=revision)
    except (ValueError, KeyError):
        _finish_target(lib, intake_id, target_id, status='stale', reason='The information changed before automatic saving. Suggestions remain available for review.')
        return
    failure = result.get('failure_reason')
    failed_request = result.get('status') in ('partial', 'unavailable', 'failed') or failure
    if receipt.get('needs_review') or result.get('unresolved'):
        outcome = 'needs_evidence'
    elif not target.get('coverage', {}).get('complete'):
        outcome = 'incomplete'
    else:
        outcome = 'applied' if receipt.get('saved') else 'no_change'
    compact_receipt = {key: copy.deepcopy(receipt.get(key)) for key in ('request_id', 'saved', 'fields', 'needs_review', 'refresh_required')}
    with writer(lib.state):
        job = _load(lib, intake_id)
        target = _target(job, target_id)
        target.update(status='retry_pending' if failed_request else outcome, application=compact_receipt,
                      reason='Partial results were preserved. Resume explicitly to try the unfinished review again.' if failed_request else '',
                      finished_at=now())
        job['reviewed_since_refresh'] += 1
        job['refresh_pending'] = job['refresh_pending'] or bool(receipt.get('refresh_required'))
        job['active_operation'] = None
        if failed_request:
            code = failure if failure in {'budget', 'credentials', 'provider', 'uncertain', 'source_changed'} else 'provider'
            _pause(job, code, 'The review stopped before completing. Saved results and changes are retained. Resume explicitly when the issue is resolved.')
        _save(lib, job)


def run_next(lib):
    """Perform one stage/target from an authorized queue; return whether busy."""
    with _worker_claim(lib) as claimed:
        if not claimed:
            return False
        jobs = [read_json(path) for path in (lib.state / 'intakes').glob('*.json')]
        recovered = []
        for candidate in jobs:
            if not isinstance(candidate, dict) or candidate.get('authorization', {}).get('kind') != 'explicit_intake':
                continue
            with writer(lib.state):
                job = _load(lib, candidate['id'])
                _recover(lib, job)
                recovered.append(job)
        jobs = recovered
        def priority(job):
            pending = next((target for target in job.get('targets', []) if target['status'] in {'queued', 'generated', 'running'}), {})
            stage = 0 if job['stage'] in {'registered', 'indexing', 'semantic', 'semantic_running'} else 1 if pending.get('scope') == 'work' else 2
            # New books become searchable and get their book details checked
            # before a long book's thousands of headings monopolize the queue.
            return stage, job.get('updated_at', job['created_at']), job['created_at']
        jobs = sorted((job for job in jobs if isinstance(job, dict) and job.get('authorization', {}).get('kind') == 'explicit_intake'), key=priority)
        blocker = _paid_blocker(jobs)
        for candidate in jobs:
            with writer(lib.state):
                job = _load(lib, candidate['id'])
                _recover(lib, job)
            if job['status'] == 'paused' and job.get('reason_code') in {'budget', 'provider', 'credentials', 'uncertain'}:
                blocker = job
            if job['status'] in {'paused', 'complete'}:
                if job.get('refresh_pending') and not job.get('refresh_error') and job.get('reason_code') not in {'source_changed', 'scope_changed'}:
                    _refresh(lib, job['id'])
                    return True
                continue
            if job['status'] not in {'queued', 'running'}:
                continue
            if not scope_current(lib, job):
                with writer(lib.state):
                    job = _load(lib, job['id'])
                    _pause(job, 'scope_changed', 'Unselected book files appeared in this book’s folder. Move them out or submit a new explicit complete selection before resuming.')
                    _save(lib, job)
                return True
            if not _sources_current(job, lib):
                with writer(lib.state):
                    job = _load(lib, job['id'])
                    _pause(job, 'source_changed', 'The source files changed. Resume explicitly to re-index and review their current contents.')
                    _save(lib, job)
                return True
            if job.get('refresh_pending') and not job.get('refresh_error') and job.get('reviewed_since_refresh', 0) >= REFRESH_BATCH_SIZE:
                _refresh(lib, job['id'])
                return True
            if job['stage'] in ('registered', 'indexing'):
                _index(lib, job['id'])
                return True
            if job['stage'] == 'semantic':
                configured = read_json(lib.state / 'config.json', {}).get('semantic', {})
                if blocker and _semantic_authorized(job) and configured.get('enabled') and configured.get('full_build_approved'):
                    continue
                _semantic(lib, job['id'])
                return True
            pending = next((target for target in job['targets'] if target['status'] in ('queued', 'generated')), None)
            if pending:
                if blocker and (pending['status'] != 'generated' or pending.get('continuation_pending')):
                    continue
                _review(lib, job['id'], pending['id'])
                return True
            if job.get('refresh_pending') and not job.get('refresh_error'):
                _refresh(lib, job['id'])
                return True
            with writer(lib.state):
                job = _load(lib, job['id'])
                job.update(status='complete', stage='complete', active_operation=None, finished_at=now())
                job['readiness']['metadata_review'] = 'complete' if _review_authorized(job) else 'not_requested'
                _save(lib, job)
            return True
        return False


def flush_pending_refreshes(lib):
    """Best-effort local publication on graceful shutdown, never provider work."""
    with _worker_claim(lib) as claimed:
        if not claimed:
            return
        for path in (lib.state / 'intakes').glob('*.json'):
            job = read_json(path)
            if (job and job.get('authorization', {}).get('kind') == 'explicit_intake'
                    and job.get('refresh_pending') and job.get('reason_code') not in {'source_changed', 'scope_changed'}):
                _refresh(lib, job['id'])
