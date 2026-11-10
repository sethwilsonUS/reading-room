"""Approved, incremental semantic search over the shared local library.

The frozen experiment remains untouched. Full-library vectors live in a separate
content-addressed SQLite cache; USearch and its window map publish together.
Original books are never opened by this module. All API requests go through the
same persistent budget ledger as the pilot.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

import numpy as np

from . import semantic as provider
from . import openai_windowing
from .budget import Budget
from .semantic_retention import prune_locked, semantic_reader
from .storage import atomic_json, now, open_sqlite, read_json, stable, uid, writer

SCHEMA_VERSION = 1


class SemanticPaused(RuntimeError):
    """A caller stopped between requests; completed vectors remain reusable."""


def _continue_build(should_continue):
    if should_continue is not None and not should_continue():
        raise SemanticPaused('Semantic indexing paused before another request. Completed vectors are retained.')


def _settings(lib):
    return read_json(lib.state / 'config.json', {}).get('semantic', {})


def configure_engine(lib, engine, *, approval_evidence):
    """Record the user's explicit choice. This does not itself send source text."""
    if engine not in ('local', 'openai'):
        raise ValueError('Choose local or openai semantic search.')
    if not isinstance(approval_evidence, str) or not approval_evidence.strip():
        raise ValueError('Engine selection needs the user’s explicit authorization evidence.')
    if len(approval_evidence) > 4000:
        raise ValueError('Keep engine authorization evidence under 4000 characters.')
    if engine == 'openai':
        from .public_config import require_budget
        require_budget(lib.state)
    with writer(lib.state):
        config = read_json(lib.state / 'config.json', {})
        previous = config.get('semantic', {})
        selected = {**previous, 'engine': engine, 'enabled': True,
                    'full_build_approved': True, 'approval_evidence': approval_evidence.strip(),
                    'approved_at': now()}
        config['semantic'] = selected
        atomic_json(lib.state / 'config.json', config)
        cat = lib._catalog()
        if refresh_acquisition_statuses(lib, cat):
            atomic_json(lib.state / 'catalog.json', cat)
    return status(lib)


def disable_engine(lib):
    """Stop future requests without erasing approvals, generations, or costs."""
    with writer(lib.state):
        config = read_json(lib.state / 'config.json', {})
        config['semantic'] = {**config.get('semantic', {}), 'enabled': False, 'disabled_at': now()}
        atomic_json(lib.state / 'config.json', config)
    return status(lib)


def _approved(lib, engine=None):
    settings = _settings(lib)
    if not settings.get('enabled') or not settings.get('full_build_approved'):
        raise ValueError('Full-library semantic indexing awaits an explicit engine choice and authorization.')
    selected = settings.get('engine')
    if selected not in ('local', 'openai') or (engine is not None and engine != selected):
        raise ValueError('The requested engine does not match the approved library engine.')
    return selected, settings


def _root(lib):
    return lib.state / 'semantic'


def _safe_path(root, relative):
    if not isinstance(relative, str) or not relative:
        raise ValueError('Semantic generation path is missing.')
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise ValueError('Semantic generation path is outside its private state directory.')
    return target


def _current(lib):
    try:
        value = read_json(_root(lib) / 'current.json', {})
        return value if isinstance(value, dict) else {}
    except (ValueError, OSError):
        return {}


def refresh_acquisition_statuses(lib, cat, *, published=None):
    """Refresh acquisition bookkeeping while the caller holds the library writer.

    Only acquisition records change: source signatures and source/curation
    identities must remain independent of this resumable processing state.
    A published generation can cover all readable text even when an export has
    empty or boilerplate-only files; keep that limitation explicit.
    """
    settings = _settings(lib)
    approved = bool(settings.get('enabled') and settings.get('full_build_approved'))
    active = _current(lib) if published is None else published
    current = lib._active()
    ready = bool(approved and active.get('generation') and
                 active.get('engine') == settings.get('engine') and
                 active.get('exact_generation') == current.get('generation'))
    if ready:
        try:
            ready = all(_safe_path(_root(lib), active.get(key)).is_file()
                        for key in ('index_file', 'windows_file'))
        except (ValueError, OSError):
            ready = False
    covered = active.get('files', {}) if ready else {}
    grouped = {}
    for file in cat.get('files', {}).values():
        grouped.setdefault(file.get('acquisition_id'), []).append(file)
    empty = set()
    if ready:
        candidates = {file['id'] for files in grouped.values() for file in files
                      if file.get('available') and file.get('state') == 'ready' and file['id'] not in covered}
        if candidates:
            # Read the exact index, never the original archives. An absent vector
            # is excused only when that file has no searchable passage content.
            with lib._db() as db:
                readable = {row[0] for row in db.execute("""
                    SELECT DISTINCT source.value FROM passages p,
                    json_each(p.data, '$.file_ids') AS source
                    WHERE p.boilerplate=0 AND length(trim(p.text))>0
                """)}
            empty = candidates - readable
            if active.get('allowed_file_ids') is not None:
                empty &= set(active['allowed_file_ids'])
    changed = False
    for acquisition in cat.get('acquisitions', {}).values():
        before = dict(acquisition)
        files = grouped.get(acquisition['id'], [])
        text_ready = bool(files) and all(file.get('available') and file.get('state') == 'ready' for file in files)
        acquisition['stage'] = 'text_ready' if text_ready else 'downloaded'
        acquisition['semantic_status'] = 'pending' if approved else 'pending_engine_review'
        for key in ('semantic_engine', 'semantic_generation', 'semantic_exact_generation',
                    'semantic_empty_file_ids', 'semantic_limitations'):
            acquisition.pop(key, None)
        if approved:
            acquisition['semantic_engine'] = settings.get('engine')
        if ready and text_ready:
            current_files = True
            for file in files:
                binding = covered.get(file['id'])
                if binding is not None:
                    if binding.get('sha256') != file.get('sha256'):
                        current_files = False
                        break
                elif file['id'] not in empty:
                    current_files = False
                    break
                try:
                    stat = Path(file['path']).stat()
                    if (stat.st_size, stat.st_mtime_ns) != (file.get('size'), file.get('mtime_ns')):
                        current_files = False
                        break
                except OSError:
                    current_files = False
                    break
            if current_files:
                acquisition.update(semantic_status='ready', semantic_generation=active['generation'],
                                   semantic_exact_generation=active['exact_generation'])
                empty_files = sorted(file['id'] for file in files if file['id'] in empty)
                if empty_files:
                    acquisition['semantic_empty_file_ids'] = empty_files
                    acquisition['semantic_limitations'] = ['Some exported files contain no indexable text; readiness covers the available extracted text.']
        changed = changed or acquisition != before
    return changed


def _build_settings(lib, engine, current):
    """Refresh after taking the semantic writer, repairing a published-build flag.

    Publishing current.json and recording the convenience flag are separate
    atomic writes. The published generation is the durable evidence if a crash
    occurred between them; later additions must use the monthly ceiling.
    """
    _, settings = _approved(lib, engine)
    completed_initial = (current.get('initial_build_completed') is True or
                         (current.get('budget_scope') == 'initial' and current.get('source_scope', 'whole_library') == 'whole_library'))
    if (engine != 'openai' or settings.get('openai_initial_complete')
            or current.get('engine') != 'openai' or not completed_initial):
        return settings
    try:
        published = bool(current.get('generation')) and all(
            _safe_path(_root(lib), current.get(key)).is_file()
            for key in ('index_file', 'windows_file'))
    except (ValueError, OSError):
        published = False
    if published:
        with writer(lib.state):
            _approved(lib, engine)
            config = read_json(lib.state / 'config.json', {})
            config['semantic']['openai_initial_complete'] = True
            atomic_json(lib.state / 'config.json', config)
            settings = config['semantic']
    return settings


def status(lib):
    """A cheap coverage check; no source reads, tokenizer loads, or API calls."""
    settings = _settings(lib)
    if not settings.get('enabled') or not settings.get('full_build_approved'):
        retained = read_json(lib.state / 'semantic.json', {'coverage_books': 0, 'engine': None})
        return {**retained, 'state': 'disabled' if settings.get('disabled_at') else 'awaiting_engine_selection',
                'enabled': False, 'engine': settings.get('engine'), 'pending': False}
    active = _current(lib)
    current_generation = lib._active().get('generation')
    engine = settings.get('engine')
    matching = active.get('engine') == engine
    try:
        available = matching and all(_safe_path(_root(lib), active.get(k)).is_file() for k in ('index_file', 'windows_file'))
    except (ValueError, OSError):
        available = False
    catalog = lib._catalog()
    eligible = {fid: record for fid, record in catalog.get('files', {}).items()
                if record.get('available') and record.get('state') == 'ready'}
    bindings = active.get('authorized_files', active.get('files', {}))
    excluded = sorted(fid for fid, record in eligible.items()
                      if bindings.get(fid, {}).get('sha256') != record.get('sha256')) if active.get('source_scope') == 'intake' else []
    pending = not available or active.get('exact_generation') != current_generation or bool(excluded)
    progress = read_json(_root(lib) / 'progress.json', {})
    result = {'state': 'pending_update' if pending else 'ready', 'engine': engine,
              'scope': active.get('scope', 'whole library') if available else 'whole library',
              'source_scope': active.get('source_scope', 'whole_library') if available else None,
              'excluded_source_files': len(excluded), 'full_build_approved': True,
              'coverage_books': active.get('coverage_books', 0) if available else 0,
              'total_books': len(catalog.get('works', {})),
              'windows': active.get('windows', 0) if available else 0,
              'exact_generation': current_generation,
              'indexed_generation': active.get('exact_generation') if matching else None,
              'built_at': active.get('built_at') if matching else None,
              'pending': pending}
    if progress and progress.get('engine') == engine and progress.get('state') != 'complete':
        result['last_attempt'] = {k: progress[k] for k in ('state', 'updated_at', 'completed_windows', 'total_windows', 'error') if k in progress}
    return result


def _cache(root, config):
    folder = root / 'cache' / provider.canonical_hash(config)
    folder.mkdir(parents=True, exist_ok=True)
    db = open_sqlite(folder / 'content.sqlite3')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS vectors(text_hash TEXT PRIMARY KEY, dimensions INTEGER NOT NULL, vector BLOB NOT NULL, origin TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS segmentation(signature TEXT PRIMARY KEY, segments TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return db


def _hash_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _segments(cache, tokenizer, title, heading, text):
    signature = provider.canonical_hash({'title': title, 'heading': heading, 'text': text, 'windowing': 1})
    saved = cache.execute('SELECT segments FROM segmentation WHERE signature=?', (signature,)).fetchone()
    if saved:
        return json.loads(saved[0])
    # This is the same windowing contract as the frozen pilot. Identity is added
    # later, so renamed files and unchanged text can reuse segmentation/vectors.
    header = title + ' · ' + heading
    if len(tokenizer('passage: ' + header)['input_ids']) > 96:
        header = tokenizer.decode(tokenizer(header, add_special_tokens=False)['input_ids'][:90])
    offsets = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)['offset_mapping']
    room = 512 - len(tokenizer('passage: ' + header + '\n')['input_ids']) - 12
    start = 0
    result = []
    while start < len(offsets):
        end = min(start + room, len(offsets))
        a, b = offsets[start][0], offsets[end - 1][1]
        value = header + '\n' + text[a:b]
        while len(tokenizer('passage: ' + value)['input_ids']) > 512:
            end -= 1
            if end <= start:
                raise ValueError('A source segment cannot fit the shared tokenizer limit.')
            b = offsets[end - 1][1]
            value = header + '\n' + text[a:b]
        result.append({'text': value, 'char_start': a, 'char_end': b,
                       'token_count': len(tokenizer('passage: ' + value)['input_ids'])})
        if end == len(offsets):
            break
        start = max(start + 1, end - 40)
    cache.execute('INSERT OR REPLACE INTO segmentation VALUES(?,?)', (signature, json.dumps(result, ensure_ascii=False)))
    return result


def _validate_files(lib, snapshot, file_ids=None):
    latest = lib._catalog()
    ids = snapshot['files'] if file_ids is None else file_ids
    for fid in ids:
        expected = snapshot['files'][fid]
        record = latest.get('files', {}).get(fid)
        if not record or not record.get('available') or record.get('state') != 'ready' or record.get('sha256') != expected['sha256']:
            raise ValueError('A semantic source changed or disappeared; synchronize exact search and resume.')
        try:
            current = Path(record['path']).stat()
        except OSError:
            raise ValueError('A semantic source is unavailable; synchronize exact search and resume.') from None
        if (current.st_size, current.st_mtime_ns) != (record.get('size'), record.get('mtime_ns')):
            raise ValueError('A semantic source changed after extraction; synchronize exact search and resume.')


def _snapshot(lib, config, cache, *, allowed_file_ids=None, source_bindings=None):
    generation = lib._active().get('generation')
    if not generation:
        raise ValueError('Build the exact-text index before semantic indexing.')
    cat = lib._catalog()
    identity = {'exact_generation': generation, 'config': config,
                'windowing': 2 if config.get('window_tokenizer') == openai_windowing.TOKENIZER else 1}
    if allowed_file_ids is not None:
        allowed_file_ids = sorted(set(allowed_file_ids))
        source_bindings = source_bindings if source_bindings is not None else {fid: {'sha256': cat['files'].get(fid, {}).get('sha256')} for fid in allowed_file_ids}
        if set(source_bindings) != set(allowed_file_ids):
            raise ValueError('Every authorized semantic source needs its original hash binding.')
        for fid, binding in source_bindings.items():
            record = cat['files'].get(fid, {})
            if (record.get('sha256') != binding.get('sha256') or not record.get('available')
                    or record.get('state') != 'ready'):
                raise ValueError('An authorized semantic source changed after selection; no expanded source bytes were submitted.')
        identity['allowed_file_ids'] = allowed_file_ids
        identity['source_bindings'] = source_bindings
    snapshot_id = provider.canonical_hash(identity)
    folder = _root(lib) / 'snapshots'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (snapshot_id + '.sqlite3')
    meta_path = folder / (snapshot_id + '.json')
    existing = read_json(meta_path)
    if existing and target.exists():
        with open_sqlite(target) as check:
            if check.execute('PRAGMA quick_check').fetchone()[0] == 'ok':
                _validate_files(lib, existing)
                return existing, target
    temporary = folder / (snapshot_id + '.pending.sqlite3')
    temporary.unlink(missing_ok=True)
    db = sqlite3.connect(temporary)
    db.executescript('''
        CREATE TABLE windows(ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE, work_id TEXT, passage_id TEXT, section_id TEXT, text_hash TEXT, text TEXT, file_ids TEXT, char_start INTEGER, char_end INTEGER);
        CREATE INDEX window_hash ON windows(text_hash);
    ''')
    native_openai = config.get('window_tokenizer') == openai_windowing.TOKENIZER
    if native_openai:
        if config.get('window_tokenizer_revision') != openai_windowing.REVISION:
            raise ValueError('This OpenAI windowing version is unsupported; existing indexes and caches are retained.')
        tokenizer = openai_windowing.encoding()
    elif config.get('engine') == 'openai':
        tokenizer = openai_windowing.legacy_tokenizer(config)
    else:
        tokenizer = provider.local_model().tokenizer
    allowed = set(allowed_file_ids) if allowed_file_ids is not None else None
    authorized_files = {fid: {'sha256': record['sha256']} for fid, record in cat['files'].items()
                        if record.get('available') and record.get('state') == 'ready'
                        and (allowed is None or fid in allowed)}
    works = set()
    files = set()
    counter = 0
    try:
        with lib._db() as source:
            for p in source.execute('SELECT p.*, w.title FROM passages p JOIN works w ON w.id=p.work_id WHERE p.boilerplate=0 ORDER BY p.work_id,p.ordinal'):
                data = json.loads(p['data'])
                file_ids = data['file_ids']
                if allowed is not None and not set(file_ids).issubset(allowed):
                    continue
                if any(not cat['files'].get(fid, {}).get('available') or cat['files'][fid].get('state') != 'ready' for fid in file_ids):
                    continue
                segmenter = openai_windowing.segments if native_openai else _segments
                segments = segmenter(cache, tokenizer, p['title'], p['heading'], p['text'])
                for part in segments:
                    db.execute('INSERT INTO windows VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (counter, stable('window', p['id'], part['char_start'], part['char_end']), p['work_id'], p['id'], p['section_id'], _hash_text(part['text']), part['text'], json.dumps(file_ids), part['char_start'], part['char_end']))
                    counter += 1
                if segments:
                    works.add(p['work_id'])
                    files.update(file_ids)
        db.commit()
        cache.commit()
    finally:
        db.close()
    metadata = {'schema_version': SCHEMA_VERSION, 'snapshot_id': snapshot_id,
                'exact_generation': generation, 'config': config, 'windows': counter,
                'work_ids': sorted(works), 'files': {fid: {'sha256': cat['files'][fid]['sha256']} for fid in sorted(files)},
                'allowed_file_ids': allowed_file_ids, 'authorized_files': authorized_files,
                'source_scope': 'whole_library' if allowed_file_ids is None else 'intake',
                'created_at': now()}
    _validate_files(lib, metadata)
    os.replace(temporary, target)
    atomic_json(meta_path, metadata)
    return metadata, target


def _put_vectors(cache, hashes, vectors, dimensions, origin):
    matrix = provider._validate_vectors(vectors, len(hashes), dimensions)
    for key, vector in zip(hashes, matrix):
        cache.execute('INSERT OR IGNORE INTO vectors VALUES(?,?,?,?)', (key, dimensions, vector.tobytes(), origin))
    cache.commit()


def _get_vector(cache, key, dimensions):
    row = cache.execute('SELECT dimensions,vector FROM vectors WHERE text_hash=?', (key,)).fetchone()
    if not row:
        return None
    if row[0] != dimensions:
        raise ValueError('Cached semantic vector dimensions changed; paid processing stopped.')
    vector = np.frombuffer(row[1], dtype=np.float32)
    return provider._validate_vectors(vector.reshape(1, -1), 1, dimensions)[0]


def _import_pilot(lib, cache, config):
    """Import only fully bound, identical pilot text/config without changing it."""
    engine = config['engine']
    pilot = lib.state / 'pilot'
    info = read_json(pilot / engine / 'engine.json')
    if not info or any(info.get(key) != value for key, value in config.items()):
        return 0
    try:
        manifest, windows = provider._validate_manifest(lib)
    except ValueError:
        return 0
    if info.get('manifest_hash') != provider.canonical_hash(manifest) or info.get('windows_sha256') != manifest.get('windows_sha256'):
        return 0
    marker = 'pilot:' + str(info['cache_fingerprint'])
    if cache.execute('SELECT 1 FROM metadata WHERE key=?', (marker,)).fetchone():
        return 0
    batch_size = 64 if engine == 'openai' else 128
    folder = pilot / engine / 'builds' / info['cache_fingerprint']
    imported = 0
    for offset in range(0, len(windows), batch_size):
        part = windows[offset:offset + batch_size]
        key = provider.canonical_hash({'binding': info['cache_fingerprint'], 'offset': offset, 'windows': part})
        path = folder / ('batch-' + key + '.npy')
        if not path.exists():
            continue
        vectors = provider._load_vectors(path, len(part), config['dimensions'])
        _put_vectors(cache, [_hash_text(w['text']) for w in part], vectors, config['dimensions'], 'frozen pilot')
        imported += len(part)
    cache.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (marker, str(imported)))
    cache.commit()
    return imported


def _progress(lib, engine, state, **details):
    record = {'engine': engine, 'state': state, 'updated_at': now(), **details}
    atomic_json(_root(lib) / 'progress.json', record)
    return record


def _build_config(engine, lib=None):
    if engine != 'openai':
        return provider.embedding_config(engine)
    # Never change an existing paid cache's segmentation implicitly. Published
    # indexes can be queried without loading any tokenizer at all.
    selected = None
    marker = _root(lib) / 'windowing-openai.json' if lib is not None else None
    if lib is not None:
        current = _current(lib)
        if current.get('engine') == 'openai':
            selected = current.get('config')
            if not isinstance(selected, dict) or not selected:
                raise ValueError('The published OpenAI generation lost its window configuration. Restore its metadata before rebuilding; no automatic reembedding was started.')
        if selected is None:
            selected = read_json(marker)
        if selected is None:
            legacy = {}
            for path in (_root(lib) / 'snapshots').glob('*.json'):
                candidate = read_json(path, {}).get('config', {})
                if candidate.get('engine') == 'openai' and candidate.get('window_tokenizer') == provider.LOCAL_MODEL:
                    legacy[provider.canonical_hash(candidate)] = candidate
            if len(legacy) > 1:
                raise ValueError('Multiple legacy OpenAI window configurations were retained. Restore the intended published generation before rebuilding; no automatic reembedding was started.')
            selected = next(iter(legacy.values()), None)
    if selected is None and lib is not None:
        for path in (_root(lib) / 'cache').glob('*/content.sqlite3'):
            try:
                with open_sqlite(path.as_uri() + '?mode=ro', uri=True) as cache:
                    retained = cache.execute('SELECT 1 FROM vectors WHERE dimensions=? LIMIT 1', (provider.OPENAI_DIMENSIONS,)).fetchone()
            except sqlite3.Error:
                raise ValueError('A retained semantic cache is unreadable. Restore its configuration and metadata before rebuilding; no automatic reembedding was started.') from None
            if retained:
                raise ValueError('Paid OpenAI vectors remain but their window configuration is missing. Restore a published generation, snapshot, or windowing-openai.json before rebuilding; no automatic reembedding was started.')
    if selected is None:
        selected = openai_windowing.config()
    if (not isinstance(selected, dict) or selected.get('engine') != 'openai'
            or selected.get('model') != provider.OPENAI_MODEL or selected.get('dimensions') != provider.OPENAI_DIMENSIONS
            or selected.get('window_tokenizer') not in (provider.LOCAL_MODEL, openai_windowing.TOKENIZER)):
        raise ValueError('The saved OpenAI window configuration is invalid; existing caches are retained.')
    if marker is not None and not marker.exists():
        atomic_json(marker, selected)
    return selected


def intake_source_bindings(lib, files):
    """Carry the job's original source hashes all the way to the provider stage."""
    approved = {item['path']: item['sha256'] for item in files}
    result = {}
    for fid, record in lib._catalog()['files'].items():
        if record.get('path') not in approved:
            continue
        expected = approved[record['path']]
        if record.get('sha256') != expected:
            raise ValueError('An intake source changed after its authorization; resume against the intended source version.')
        if record.get('available') and record.get('state') == 'ready':
            result[fid] = {'sha256': expected}
    return result


def intake_file_ids(lib, files):
    return sorted(intake_source_bindings(lib, files))


def _merge_intake_scope(lib, file_ids, current, engine, source_bindings=None):
    if file_ids is None:
        if source_bindings is not None:
            raise ValueError('Hash-bound semantic sources require an explicit file scope.')
        return None, None
    if not isinstance(file_ids, (list, tuple, set, frozenset)) or any(not isinstance(fid, str) for fid in file_ids):
        raise ValueError('An intake semantic scope must contain explicit source file identifiers.')
    catalog = lib._catalog()['files']
    if any(fid not in catalog for fid in file_ids):
        raise ValueError('An authorized semantic source is no longer in the catalog.')
    expected = source_bindings if source_bindings is not None else {fid: {'sha256': catalog[fid].get('sha256')} for fid in file_ids}
    if not isinstance(expected, dict) or set(expected) != set(file_ids):
        raise ValueError('Every authorized semantic source needs its original hash binding.')
    bindings = {}
    for fid, binding in expected.items():
        record = catalog[fid]
        if (not isinstance(binding, dict) or record.get('sha256') != binding.get('sha256')
                or not record.get('available') or record.get('state') != 'ready'):
            raise ValueError('An authorized semantic source changed after selection; no expanded source bytes were submitted.')
        bindings[fid] = {'sha256': binding['sha256']}
    # Reuse already-published permission only for the same provider and exact
    # same source bytes. Retain these bindings through snapshot construction.
    prior_bindings = current.get('authorized_files', current.get('files', {})) if current.get('engine') == engine else {}
    for fid, binding in prior_bindings.items():
        record = catalog.get(fid, {})
        if (record.get('sha256') == binding.get('sha256') and record.get('available')
                and record.get('state') == 'ready'):
            bindings[fid] = {'sha256': binding['sha256']}
    return sorted(bindings), bindings


def estimate_index(lib, *, engine=None):
    """Count current uncached input tokens without sending source text anywhere."""
    import tiktoken
    settings = _settings(lib)
    engine = engine or settings.get('engine') or 'openai'
    if engine not in ('local', 'openai'):
        raise ValueError('Choose local or openai semantic search.')
    if not lib._active():
        raise ValueError('Create the local text index first with library sync --exact; estimation never grants paid permission.')
    with writer(_root(lib)):
        config = _build_config(engine, lib)
        cache = _cache(_root(lib), config)
        try:
            snapshot, windows_path = _snapshot(lib, config, cache)
            imported = _import_pilot(lib, cache, config)
            encoding = tiktoken.get_encoding('cl100k_base')
            count = tokens = 0
            with open_sqlite(windows_path) as windows:
                for key, text in windows.execute('SELECT text_hash,text FROM windows GROUP BY text_hash'):
                    if cache.execute('SELECT 1 FROM vectors WHERE text_hash=?', (key,)).fetchone():
                        continue
                    count += 1
                    tokens += len(encoding.encode(text, disallowed_special=()))
            current = _current(lib)
            completed = settings.get('openai_initial_complete') or current.get('initial_build_completed') or (current.get('budget_scope') == 'initial' and current.get('source_scope', 'whole_library') == 'whole_library')
            scope = 'monthly' if completed else 'initial'
            return {'engine': engine, 'scope': scope, 'windows': snapshot['windows'], 'coverage_books': len(snapshot['work_ids']),
                    'uncached_unique_windows': count, 'uncached_tokens': tokens,
                    'estimated_api_dollars': round(tokens * .13 / 1_000_000, 6) if engine == 'openai' else 0,
                    'pilot_vectors_imported': imported, 'budget': Budget(lib.state).report(),
                    'authorization_changed': False, 'local_cache_writes': True}
        finally:
            cache.close()


def _retain_generations(lib):
    try:
        return prune_locked(lib, apply=True)
    except (OSError, ValueError) as exc:
        # Cleanup failure must not invalidate a published index.
        return {'applied': False, 'error': type(exc).__name__}


def build_index(lib, *, progress=None, force=False, batch_size=None, should_continue=None, budget_scope=None, allowed_file_ids=None, source_bindings=None):
    """Resume approved full-library indexing, charging initial or monthly scope."""
    if budget_scope not in (None, 'monthly', 'initial'):
        raise ValueError('Choose a supported semantic budget scope.')
    if allowed_file_ids is not None and budget_scope == 'initial':
        raise ValueError('The initial allowance is reserved for an explicit full-library build.')
    started = time.perf_counter()
    engine, settings = _approved(lib)
    batch_size = min(256, max(1, int(batch_size or (256 if engine == 'openai' else 128))))
    root = _root(lib)
    with writer(root):
        current = _current(lib)
        allowed_file_ids, source_bindings = _merge_intake_scope(lib, allowed_file_ids, current, engine, source_bindings)
        settings = _build_settings(lib, engine, current)
        if (not force and current.get('engine') == engine
                and current.get('exact_generation') == lib._active().get('generation')
                and current.get('allowed_file_ids') == allowed_file_ids
                and (allowed_file_ids is None or current.get('authorized_files') == source_bindings)):
            if all(_safe_path(root, current.get(key)).is_file() for key in ('index_file', 'windows_file')):
                with writer(lib.state):
                    if current.get('exact_generation') == lib._active().get('generation'):
                        _validate_files(lib, current)
                        cat = lib._catalog()
                        if refresh_acquisition_statuses(lib, cat, published=current):
                            atomic_json(lib.state / 'catalog.json', cat)
                        return {**current, 'changed': False, 'retention': _retain_generations(lib)}
        config = _build_config(engine, lib)
        cache = _cache(root, config)
        before = Budget(lib.state).report()
        scope = budget_scope or ('monthly' if settings.get('openai_initial_complete') else 'initial')
        completed = embedded = cached = batches = 0
        try:
            _progress(lib, engine, 'preparing')
            snapshot, windows_path = _snapshot(lib, config, cache, allowed_file_ids=allowed_file_ids, source_bindings=source_bindings)
            imported = _import_pilot(lib, cache, config)
            with open_sqlite(windows_path) as windows:
                pending = []
                for key, text, file_ids in windows.execute('SELECT text_hash,text,file_ids FROM windows GROUP BY text_hash ORDER BY ordinal'):
                    if _get_vector(cache, key, config['dimensions']) is not None:
                        cached += 1
                        continue
                    pending.append((key, text, json.loads(file_ids)))
                    if len(pending) >= batch_size:
                        _embed_batch(lib, engine, snapshot, cache, config, pending, scope, should_continue=should_continue)
                        embedded += len(pending); batches += 1; pending = []
                        completed = embedded + cached
                        update = _progress(lib, engine, 'embedding', completed_windows=completed, total_windows=snapshot['windows'], embedded_windows=embedded, cached_windows=cached, budget_scope=scope)
                        if progress:
                            progress(update)
                if pending:
                    _embed_batch(lib, engine, snapshot, cache, config, pending, scope, should_continue=should_continue)
                    embedded += len(pending); batches += 1
                _progress(lib, engine, 'building_index', completed_windows=embedded + cached, total_windows=snapshot['windows'])
                from usearch.index import Index
                index = Index(ndim=config['dimensions'], metric='cos', dtype='f32', connectivity=16, expansion_add=128, expansion_search=128)
                vector_batch = []; keys = []
                for ordinal, key in windows.execute('SELECT ordinal,text_hash FROM windows ORDER BY ordinal'):
                    vector = _get_vector(cache, key, config['dimensions'])
                    if vector is None:
                        raise ValueError('A completed embedding cache entry is missing; resume indexing.')
                    keys.append(ordinal); vector_batch.append(vector)
                    if len(keys) >= 256:
                        index.add(np.asarray(keys, dtype=np.uint64), np.vstack(vector_batch), threads=6)
                        keys = []; vector_batch = []
                if keys:
                    index.add(np.asarray(keys, dtype=np.uint64), np.vstack(vector_batch), threads=6)
                generation = uid('sem')
                folder = root / 'generations' / generation
                folder.mkdir(parents=True, exist_ok=True)
                index.save(str(folder / 'index.usearch'))
                # Immutable map copied through SQLite backup, not by opening books.
                with open_sqlite(folder / 'windows.sqlite3') as destination:
                    windows.backup(destination)
            after = Budget(lib.state).report()
            info = {**snapshot, **config, 'generation': generation, 'engine': engine,
                    'scope': 'whole library' if allowed_file_ids is None else 'authorized intake sources', 'coverage_books': len(snapshot['work_ids']),
                    'total_books': len(lib._catalog()['works']), 'built_at': now(),
                    'seconds': time.perf_counter() - started, 'budget_scope': scope,
                    'initial_build_completed': bool(settings.get('openai_initial_complete') or (scope == 'initial' and allowed_file_ids is None)),
                    'embedded_windows': embedded, 'cached_windows': cached,
                    'pilot_vectors_imported': imported, 'embedded_batches': batches,
                    'index_file': str((folder / 'index.usearch').relative_to(root)),
                    'windows_file': str((folder / 'windows.sqlite3').relative_to(root)),
                    'previous_generation': current.get('generation'),
                    'api_usage': {'requests': after['requests'] - before['requests'],
                                  'actual_tokens': after['actual_tokens'] - before['actual_tokens'],
                                  'dollars_spent_or_reserved': after['spent_or_reserved'][scope] - before['spent_or_reserved'][scope]}}
            with writer(lib.state):
                _approved(lib, engine)
                if lib._active().get('generation') != snapshot['exact_generation']:
                    raise ValueError('The exact index changed during semantic processing. Cached vectors are safe; resume to publish a current index.')
                _validate_files(lib, snapshot)
                atomic_json(folder / 'generation.json', info)
                atomic_json(root / 'current.json', info)
                cfg = read_json(lib.state / 'config.json', {})
                if engine == 'openai' and info['initial_build_completed']:
                    cfg['semantic']['openai_initial_complete'] = True
                    atomic_json(lib.state / 'config.json', cfg)
                atomic_json(lib.state / 'semantic.json', {'state': 'ready', 'engine': engine, 'full_build_approved': True, 'scope': info['scope'], 'coverage_books': info['coverage_books'], 'total_books': info['total_books'], 'windows': info['windows'], 'generation': generation})
                cat = lib._catalog()
                if refresh_acquisition_statuses(lib, cat, published=info):
                    atomic_json(lib.state / 'catalog.json', cat)
                retention = _retain_generations(lib)
            _progress(lib, engine, 'complete', completed_windows=snapshot['windows'], total_windows=snapshot['windows'])
            return {**info, 'changed': True, 'retention': retention}
        except Exception as exc:
            # Controlled messages from the provider never contain credentials or
            # submitted text. Unexpected errors expose only their class.
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            _progress(lib, engine, 'pending', completed_windows=embedded + cached, error=safe[:800])
            raise
        finally:
            cache.close()


def _embed_batch(lib, engine, snapshot, cache, config, batch, scope, *, should_continue=None):
    _approved(lib, engine)
    _validate_files(lib, snapshot, {fid for _, _, ids in batch for fid in ids})
    if lib._active().get('generation') != snapshot['exact_generation']:
        raise ValueError('The exact index changed during semantic processing; synchronize and resume.')
    texts = [row[1] for row in batch]
    if engine == 'local':
        _continue_build(should_continue)
        vectors = provider.embed_local(texts)
        _put_vectors(cache, [row[0] for row in batch], vectors, config['dimensions'], 'full library')
        return
    # Larger requests amortize network overhead. A conservative token boundary
    # still protects unusual multilingual windows from aggregate API limits.
    import tiktoken
    encoding = tiktoken.get_encoding('cl100k_base')
    part = []; token_count = 0
    for row in batch:
        tokens = len(encoding.encode(row[1], disallowed_special=()))
        if part and token_count + tokens > 250_000:
            _continue_build(should_continue)
            _approved(lib, engine)
            vectors = provider.embed_openai(lib.state, [r[1] for r in part], scope=scope)
            _put_vectors(cache, [r[0] for r in part], vectors, config['dimensions'], 'full library')
            part = []; token_count = 0
        part.append(row); token_count += tokens
    if part:
        _continue_build(should_continue)
        _approved(lib, engine)
        vectors = provider.embed_openai(lib.state, [r[1] for r in part], scope=scope)
        _put_vectors(cache, [r[0] for r in part], vectors, config['dimensions'], 'full library')


def search(lib, query, *, limit=60):
    """Query the approved full index; callers retain exact-search fallback."""
    with semantic_reader(_root(lib)):
        return _search(lib, query, limit=limit)


def _search(lib, query, *, limit=60):
    engine, _ = _approved(lib)
    if not isinstance(query, str) or not query.strip() or len(query) > 1500:
        raise ValueError('Enter a semantic query of 1–1500 characters.')
    root = _root(lib)
    info = _current(lib)
    if info.get('engine') != engine:
        raise ValueError('The approved full-library semantic index is not ready. Exact search remains available.')
    config = info.get('config', {})
    expected_model = provider.LOCAL_MODEL if engine == 'local' else provider.OPENAI_MODEL
    expected_dimensions = provider.LOCAL_DIMENSIONS if engine == 'local' else provider.OPENAI_DIMENSIONS
    if config.get('engine') != engine or config.get('model') != expected_model or config.get('dimensions') != expected_dimensions:
        raise ValueError('Semantic embedding configuration is invalid; rebuild before querying.')
    if engine == 'local' and config.get('model_revision') != provider.local_revision():
        raise ValueError('The local model revision changed; rebuild semantic indexing.')
    if not info.get('windows'):
        return []
    index_path = _safe_path(root, info['index_file'])
    windows_path = _safe_path(root, info['windows_file'])
    if not index_path.is_file() or not windows_path.is_file():
        raise ValueError('The semantic index is unavailable; rebuild it. Exact search remains available.')
    from usearch.index import Index
    index = Index.restore(str(index_path), view=True)
    if index.ndim != expected_dimensions or len(index) != info['windows']:
        raise ValueError('The semantic index does not match its published generation; rebuild it.')
    cache = root / 'queries' / provider.canonical_hash(config)
    key = provider.canonical_hash({'query': query, 'kind': 'query', 'config': config}) + '.npy'
    with writer(cache):
        path = cache / key
        if path.exists():
            matrix = provider._load_vectors(path, 1, expected_dimensions)
        else:
            pilot_path = lib.state / 'pilot' / engine / 'queries' / provider.canonical_hash(config) / key
            if pilot_path.exists():
                matrix = provider._load_vectors(pilot_path, 1, expected_dimensions)
            else:
                _approved(lib, engine)
                matrix = provider.embed_local([query], kind='query') if engine == 'local' else provider.embed_openai(lib.state, [query], scope='monthly')
            provider._save_vectors(path, provider._validate_vectors(matrix, 1, expected_dimensions))
    limit = max(1, min(int(limit), 200))
    matches = index.search(matrix[0], min(info['windows'], max(100, limit * 5)))
    seen = set(); result = []
    with open_sqlite(windows_path) as windows:
        for match in matches:
            row = windows.execute('SELECT passage_id,work_id,id,char_start,char_end FROM windows WHERE ordinal=?', (int(match.key),)).fetchone()
            if not row or row[0] in seen:
                continue
            seen.add(row[0])
            try:
                lib.read(row[0], context=0, offset=row[3], max_chars=500)
            except ValueError:
                continue
            result.append({'passage_id': row[0], 'work_id': row[1], 'window_id': row[2],
                           'char_start': row[3], 'char_end': row[4],
                           'distance': float(match.distance), 'scope': info.get('scope', 'whole library')})
            if len(result) >= limit:
                break
    return result
