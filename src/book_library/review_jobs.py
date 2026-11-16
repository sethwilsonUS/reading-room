"""Durable request identity for explicitly requested, foreground AI reviews.

The provider's cost ledger survives interrupted requests independently. This
small journal prevents retries of the same browser request from charging again.
It never runs or restarts a provider request while reading status.
"""
from __future__ import annotations

import os
import re
import time

from .storage import atomic_json, now, read_json, writer

LEASE_SECONDS = 180  # Longer than a review generation's 110-second dispatch window.


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise ValueError('The AI review request is not valid. Reload the review page.')
    return value


def _path(library, target_id):
    return library.state / 'review-jobs' / (_identifier(target_id) + '.json')


def _load(library, target_id):
    return read_json(_path(library, target_id), {'version': 1, 'requests': {}, 'active': None})


def _running(record):
    if record.get('status') != 'running' or record.get('expires_at', 0) <= time.time():
        return False
    try:
        os.kill(record['pid'], 0)
    except (ProcessLookupError, KeyError, TypeError, ValueError):
        return False
    except PermissionError:
        return True
    return True


def generation_status(library, target_id):
    journal = _load(library, target_id)
    record = journal['requests'].get(journal.get('active'))
    if not record:
        return {'status': 'idle', 'running': False}
    result = dict(record)
    if result['status'] == 'running' and not _running(result):
        result['status'] = 'interrupted'
    result['running'] = result['status'] == 'running'
    return result


def begin_generation(library, target_id, revision, request_id):
    """Claim once, before the provider can reserve money or start requests."""
    _identifier(request_id)
    path = _path(library, target_id)
    with writer(library.state):
        journal = _load(library, target_id)
        existing = journal['requests'].get(request_id)
        if existing:
            result = dict(existing)
            if result['status'] == 'running' and not _running(result):
                result['status'] = 'interrupted'
            return {**result, 'replay': True}
        active = journal['requests'].get(journal.get('active'))
        if active and _running(active):
            raise ValueError('An AI review is already running for this item. Return shortly to see its results.')
        context = library.review_context(target_id)
        if not context.get('source_current', False):
            raise ValueError('The source changed. Synchronize the library before requesting an AI review.')
        if context['revision'] != revision:
            raise ValueError('The information changed. Reload and review the current values before requesting suggestions.')
        if active and active['status'] == 'running':
            active.update(status='interrupted', finished_at=now())
        record = {'request_id': request_id, 'target_id': target_id, 'revision': revision,
                  'status': 'running', 'pid': os.getpid(), 'started_at': now(),
                  'expires_at': time.time() + LEASE_SECONDS}
        journal['requests'][request_id] = record
        journal['active'] = request_id
        atomic_json(path, journal)
        return {**record, 'replay': False}


def finish_generation(library, target_id, request_id, status='complete'):
    """Record an outcome without treating an interrupted request as refunded."""
    if status not in ('complete', 'partial', 'unavailable', 'interrupted', 'failed'):
        raise ValueError('Unknown AI review outcome.')
    _identifier(request_id)
    path = _path(library, target_id)
    with writer(library.state):
        journal = _load(library, target_id)
        record = journal['requests'].get(request_id)
        if record is None:
            raise ValueError('This AI review request was not started.')
        if record['status'] != 'running':
            return dict(record)
        record.update(status=status, finished_at=now())
        atomic_json(path, journal)
        return dict(record)
