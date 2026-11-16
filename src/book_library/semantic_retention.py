"""Bound disposable semantic state, preserving paid vectors and unknown files."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import re
from pathlib import Path

from .storage import atomic_json, read_json, writer

GENERATION = re.compile(r'sem_[0-9a-f]{20}')
SNAPSHOT = re.compile(r'[0-9a-f]{64}')
GENERATION_FILES = {'generation.json', 'index.usearch', 'windows.sqlite3'}


@contextlib.contextmanager
def semantic_reader(root):
    """Protect path opening as well as reads while old generations are pruned."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'readers.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _record(path):
    value = read_json(path, {})
    if not isinstance(value, dict):
        raise ValueError('Invalid semantic retention metadata.')
    return value


def _timestamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('Semantic generation timestamp needs a timezone.')
    return parsed


def _plain(path, *, directory=False):
    return not path.is_symlink() and (path.is_dir() if directory else path.is_file())


def _operation_path(root, kind, identity):
    return root / 'operations' / f'{kind}-{identity}.json'


def begin_operation(root, kind, identity, *, snapshot_id=None, action='build'):
    """Record ownership before creating files; the semantic writer is held.

    The journal lives outside the generated directory and is removed last, so
    even an interrupted rmdir or a half-written SQLite snapshot is recoverable.
    """
    pattern = GENERATION if kind == 'generation' else SNAPSHOT if kind == 'snapshot' else None
    if pattern is None or not pattern.fullmatch(identity) or action not in {'build', 'delete'}:
        raise ValueError('Invalid semantic operation identity.')
    if kind == 'generation' and (not isinstance(snapshot_id, str) or not SNAPSHOT.fullmatch(snapshot_id)):
        raise ValueError('Invalid semantic operation snapshot.')
    operation = {'version': 1, 'kind': kind, 'identity': identity, 'action': action}
    if kind == 'generation':
        operation['snapshot_id'] = snapshot_id
    folder = root / 'operations'
    if folder.exists() and not _plain(folder, directory=True):
        raise ValueError('Semantic operations require an ordinary directory.')
    path = _operation_path(root, kind, identity)
    if path.is_symlink():
        raise ValueError('Semantic operation journal must not be a symlink.')
    atomic_json(path, operation)
    return operation


def finish_operation(root, kind, identity):
    _operation_path(root, kind, identity).unlink(missing_ok=True)


def _operations(root):
    folder = root / 'operations'
    if not folder.exists() and not folder.is_symlink():
        return {}, []
    if not _plain(folder, directory=True):
        raise ValueError('Semantic operations require an ordinary directory.')
    operations, skipped = {}, []
    for path in folder.iterdir():
        try:
            if not _plain(path):
                raise ValueError('Not an ordinary operation journal.')
            info = _record(path)
            kind, identity = info['kind'], info['identity']
            pattern = GENERATION if kind == 'generation' else SNAPSHOT if kind == 'snapshot' else None
            if (info.get('version') != 1 or pattern is None or not isinstance(identity, str)
                    or not pattern.fullmatch(identity) or info.get('action') not in {'build', 'delete'}
                    or path != _operation_path(root, kind, identity)
                    or kind == 'generation' and (not isinstance(info.get('snapshot_id'), str)
                                                or not SNAPSHOT.fullmatch(info['snapshot_id']))):
                raise ValueError('Invalid semantic operation journal.')
            operations[kind, identity] = info
        except (ValueError, KeyError, TypeError, OSError):
            skipped.append(str(path))
    return operations, skipped


def _generation_info(root, folder):
    info = _record(folder / 'generation.json')
    sid = info.get('snapshot_id')
    if (info.get('generation') != folder.name or not isinstance(sid, str) or not SNAPSHOT.fullmatch(sid)
            or info.get('index_file') != f'generations/{folder.name}/index.usearch'):
        raise ValueError('Invalid generated file binding.')
    local_map = f'generations/{folder.name}/windows.sqlite3'
    shared_map = f'snapshots/{sid}.sqlite3'
    if info.get('windows_file') == local_map:
        expected = GENERATION_FILES
    elif info.get('windows_file') == shared_map and _plain(root / shared_map):
        expected = {'generation.json', 'index.usearch'}
    else:
        raise ValueError('Invalid generated window map binding.')
    if {p.name for p in folder.iterdir()} != expected or any(not _plain(folder / name) for name in expected):
        raise ValueError('Incomplete semantic generation.')
    return info, _timestamp(info['built_at'])


def _snapshot_files(root, sid):
    # Only these application-owned files can be cleaned after a failed build.
    folder = root / 'snapshots'
    return [folder / f'{sid}{suffix}' for suffix in
            ('.pending.sqlite3-journal', '.pending.sqlite3', '.sqlite3', '.json')]


def _plan(lib):
    """Read-only, advisory plan. Applying it always rereads under writer locks."""
    root = lib.state / 'semantic'
    if not _plain(root, directory=True):
        raise ValueError('Semantic retention requires ordinary state directories.')
    for folder in (root / 'generations', root / 'snapshots'):
        if (folder.exists() or folder.is_symlink()) and not _plain(folder, directory=True):
            raise ValueError('Semantic retention requires ordinary state directories.')
    pointer = root / 'current.json'
    if pointer.exists() or pointer.is_symlink():
        if not _plain(pointer):
            raise ValueError('Semantic retention requires an ordinary current pointer.')
        current = _record(pointer)
        active = current.get('generation', '')
        if not isinstance(active, str) or not GENERATION.fullmatch(active):
            raise ValueError('Invalid current semantic generation identity.')
        previous = current.get('previous_generation')
        if previous is not None and (not isinstance(previous, str) or not GENERATION.fullmatch(previous)):
            raise ValueError('Invalid previous semantic generation identity.')
    else:
        current, active = {}, None
    operations, skipped = _operations(root)
    records, cleanup = {}, {}
    uncertain_generation = bool(skipped)
    protected = {active, current.get('previous_generation')}
    for folder in (root / 'generations').glob('*'):
        op = operations.get(('generation', folder.name))
        owned = (op and _plain(folder, directory=True)
                 and {p.name for p in folder.iterdir()}.issubset(GENERATION_FILES)
                 and all(_plain(p) for p in folder.iterdir()))
        if owned and folder.name not in protected:
            cleanup['generation', folder.name] = op
            continue
        try:
            if not GENERATION.fullmatch(folder.name) or not _plain(folder, directory=True):
                raise ValueError('Unrecognized semantic generation.')
            records[folder.name] = _generation_info(root, folder)
        except (ValueError, KeyError, TypeError, OSError):
            skipped.append(str(folder))
            uncertain_generation |= folder.is_dir() or folder.is_symlink()
    if active and (active not in records or any(current.get(k) != records[active][0].get(k)
                                              for k in ('snapshot_id', 'index_file', 'windows_file', 'built_at'))):
        raise ValueError('Current semantic generation is incomplete or inconsistent; nothing pruned.')
    active_time = records[active][1] if active else None
    older = sorted((key for key, (_, stamp) in records.items()
                    if active and key != active and stamp <= active_time),
                   key=lambda key: (records[key][1], key), reverse=True)
    previous = current.get('previous_generation')
    if previous not in older:
        previous = older[0] if older else None
    keep = {active} if active else set(records)
    if previous:
        keep.add(previous)
    # Legacy complete uncommitted generations have no ownership journal.
    keep.update(key for key, (_, stamp) in records.items() if active and stamp > active_time)
    for key in older:
        if key not in keep:
            cleanup['generation', key] = {'kind': 'generation', 'identity': key, 'snapshot_id': records[key][0]['snapshot_id']}
    snapshots = {info['snapshot_id'] for key, (info, _) in records.items() if key in keep}
    discard_journals = []
    for key, op in operations.items():
        if op['kind'] != 'generation':
            continue
        folder = root / 'generations' / op['identity']
        if op['identity'] in keep and op['action'] == 'build':
            discard_journals.append(_operation_path(root, *key))
        elif not folder.exists() and not folder.is_symlink() and op['identity'] not in protected:
            cleanup[key] = op
    exact = lib._active().get('generation')
    if not uncertain_generation:
        for metadata in (root / 'snapshots').glob('*.json'):
            sid = metadata.stem
            database = metadata.with_suffix('.sqlite3')
            if not SNAPSHOT.fullmatch(sid) or not _plain(metadata) or not _plain(database):
                continue
            if any((p.exists() or p.is_symlink()) and not _plain(p) for p in _snapshot_files(root, sid)):
                skipped.append(str(metadata))
                continue
            try:
                info = _record(metadata)
                if (info.get('snapshot_id') != sid or sid in snapshots or info.get('exact_generation') == exact
                        or active_time is None or _timestamp(info['created_at']) > active_time):
                    continue
            except (ValueError, KeyError, TypeError, OSError):
                continue
            cleanup['snapshot', sid] = {'kind': 'snapshot', 'identity': sid}
        for key, op in operations.items():
            if op['kind'] != 'snapshot':
                continue
            sid = op['identity']
            paths = _snapshot_files(root, sid)
            if any((p.exists() or p.is_symlink()) and not _plain(p) for p in paths):
                skipped.append(str(_operation_path(root, *key)))
                continue
            complete = False
            try:
                info = _record(root / 'snapshots' / f'{sid}.json')
                complete = (info.get('snapshot_id') == sid and _plain(root / 'snapshots' / f'{sid}.sqlite3'))
            except (ValueError, OSError):
                pass
            if sid in snapshots or complete and info.get('exact_generation') == exact:
                if complete and op['action'] == 'build':
                    discard_journals.append(_operation_path(root, *key))
                continue
            cleanup[key] = op
    generation_paths, snapshot_paths = [], []
    for (kind, identity), op in cleanup.items():
        if kind == 'generation':
            folder = root / 'generations' / identity
            if folder.exists():
                generation_paths.append(folder)
        else:
            snapshot_paths.extend(p for p in _snapshot_files(root, identity) if p.exists())
    paths = generation_paths + snapshot_paths
    size = sum(child.stat().st_blocks * 512 for path in paths
               for child in (list(path.iterdir()) if path.is_dir() else [path]))
    return {'current_generation': active, 'retained_generations': sorted(keep),
            'retained_snapshot_ids': sorted(snapshots), 'skipped_paths': sorted(set(skipped)),
            'generation_paths': [str(p) for p in sorted(generation_paths)],
            'snapshot_paths': [str(p) for p in sorted(snapshot_paths)],
            'operation_paths': [str(_operation_path(root, *key)) for key in sorted(cleanup)],
            'completed_operation_paths': [str(p) for p in discard_journals],
            'operations': [cleanup[key] for key in sorted(cleanup)], 'reclaimable_bytes': size}


def prune_locked(lib, *, apply=False):
    """Caller holds semantic writer then library writer; active readers defer GC."""
    root = lib.state / 'semantic'
    with (root / 'readers.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'applied': False, 'deferred': 'Semantic readers are active.'}
        try:
            plan = _plan(lib)
            removed = []
            if apply:
                for operation in plan['operations']:
                    kind, identity = operation['kind'], operation['identity']
                    begin_operation(root, kind, identity, snapshot_id=operation.get('snapshot_id'), action='delete')
                    if kind == 'generation':
                        folder = root / 'generations' / identity
                        # Journal persists outside the folder until rmdir succeeds.
                        if folder.exists():
                            for filename in sorted(GENERATION_FILES):
                                (folder / filename).unlink(missing_ok=True)
                            folder.rmdir()
                            removed.append(str(folder))
                    else:
                        for path in _snapshot_files(root, identity):
                            if path.exists():
                                path.unlink()
                                removed.append(str(path))
                    finish_operation(root, kind, identity)
                for path in plan['completed_operation_paths']:
                    Path(path).unlink(missing_ok=True)
            return {**plan, 'applied': apply, 'removed_paths': removed}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prune_semantic_state(lib, *, apply=False):
    """Dry run by default; take publication locks and reread all live pointers."""
    with writer(lib.state / 'semantic'):
        with writer(lib.state):
            return prune_locked(lib, apply=apply)
