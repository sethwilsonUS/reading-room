"""Full-library semantic lifecycle, with real SQLite/USearch and fake embeddings."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest

from book_library import semantic as provider
from book_library import semantic_library as sem
from book_library.budget import Budget, BudgetExceeded
from book_library.storage import atomic_json, open_sqlite, read_json
from test_service import docx, seed
from test_semantic import fake_openai

REV = 'a' * 40


class Tokenizer:
    def __init__(self):
        self.words = {}
        self.by_id = {}
        self.calls = 0

    def __call__(self, text, add_special_tokens=True, return_offsets_mapping=False):
        self.calls += 1
        offsets = [m.span() for m in re.finditer(r'\S+', text)]
        tokens = []
        for a, b in offsets:
            word = text[a:b]
            if word not in self.words:
                n = len(self.words) + 1
                self.words[word] = n
                self.by_id[n] = word
            tokens.append(self.words[word])
        result = {'input_ids': ([0] if add_special_tokens else []) + tokens + ([0] if add_special_tokens else [])}
        if return_offsets_mapping:
            result['offset_mapping'] = offsets
        return result

    def decode(self, tokens):
        return ' '.join(self.by_id.get(t, '') for t in tokens)


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / 'books'
    source = docx(root / 'Alpha' / 'Alpha.docx', [('Heading1', 'Water and memory'), 'River voices carry the memory of a community.'])
    second = docx(root / 'Beta' / 'Beta.docx', [('Heading1', 'Teaching practice'), 'Group learning invites patient questioning and mutual attention.'])
    lib = seed(root, [{'title': 'Alpha', 'paths': [source]}, {'title': 'Beta', 'paths': [second]}])
    tokenizer = Tokenizer()
    model = type('Model', (), {'tokenizer': tokenizer})()
    monkeypatch.setattr(provider, 'local_model', lambda: model)
    monkeypatch.setattr(provider, 'local_revision', lambda: REV)
    calls = []

    def embed(texts, kind='passage'):
        calls.append((kind, list(texts)))
        result = np.zeros((len(texts), 384), dtype=np.float32)
        for i, text in enumerate(texts):
            result[i, int(hashlib.sha256(text.encode()).hexdigest(), 16) % 384] = 1.
        return result
    monkeypatch.setattr(provider, 'embed_local', embed)
    return lib, [source, second], calls, tokenizer


def approve(lib, engine='local'):
    if engine == 'openai':
        from book_library.public_config import accept_budget
        accept_budget(lib, initial_usd='50', monthly_usd='25', timezone='America/Chicago', approval_evidence='Synthetic fixture accepts historical limits.')
    return sem.configure_engine(lib, engine, approval_evidence='The fixture explicitly authorizes this engine.')


def test_explicit_choice_required_preserves_other_config_and_does_not_embed(library):
    lib, _, calls, _ = library
    atomic_json(lib.state / 'config.json', {'openai_env_file': '/safe/existing/path'})
    with pytest.raises(ValueError, match='authorization'):
        sem.build_index(lib)
    with pytest.raises(ValueError, match='authorization'):
        sem.configure_engine(lib, 'openai', approval_evidence='')
    with pytest.raises(ValueError, match='Choose'):
        approve(lib, '../unknown')
    result = approve(lib)
    assert result['state'] == 'pending_update'
    assert result['engine'] == 'local'
    assert not calls
    assert read_json(lib.state / 'config.json')['openai_env_file'] == '/safe/existing/path'
    assert {acq['semantic_status'] for acq in lib._catalog()['acquisitions'].values()} == {'pending'}


def test_full_local_build_and_incremental_metadata_rebuild_reuse_content(library):
    lib, paths, calls, tokenizer = library
    approve(lib)
    result = sem.build_index(lib)
    assert result['scope'] == 'whole library'
    assert result['coverage_books'] == 2
    assert result['embedded_windows'] == 2
    assert sem.status(lib)['state'] == 'ready'
    before_calls = len(calls); before_tokens = tokenizer.calls
    assert sem.build_index(lib)['changed'] is False
    assert len(calls) == before_calls
    assert tokenizer.calls == before_tokens

    # A curation-only exact generation must republish its semantic map, but not
    # tokenize or pay again for unchanged header+passage text.
    wid = lib.books()[0]['id']
    lib.annotate(wid, {'field': 'notes', 'value': 'A useful workshop source.', 'evidence': 'Test review', 'origin': 'user', 'status': 'confirmed'})
    lib.sync(semantic=False)
    assert sem.status(lib)['pending'] is True
    second = sem.build_index(lib)
    assert second['embedded_windows'] == 0
    assert second['cached_windows'] == 2
    assert len(calls) == before_calls
    assert tokenizer.calls == before_tokens
    assert sem.status(lib)['state'] == 'ready'


def test_addition_edit_removal_and_verified_rename_keep_healthy_retrieval(library):
    lib, paths, calls, _ = library
    approve(lib); sem.build_index(lib)
    before = len(calls)
    renamed = paths[0].with_name('Alpha - descriptive name.docx')
    paths[0].rename(renamed); lib.sync(semantic=False)
    result = sem.build_index(lib)
    assert result['embedded_windows'] == 0
    assert len(calls) == before
    extra = docx(renamed.parent / 'New companion.docx', [('Heading1', 'New section'), 'Copper images participate in collective ritual.'])
    lib.sync(semantic=False); result = sem.build_index(lib)
    assert result['embedded_windows'] == 1
    paths[1].unlink()
    # A stale semantic generation does not expose a removed original as evidence.
    results = sem.search(lib, 'group learning', limit=20)
    assert all(lib.read(hit['passage_id'])['work']['title'] != 'Beta' for hit in results)
    lib.sync(semantic=False); result = sem.build_index(lib)
    assert result['coverage_books'] == 1
    docx(renamed, [('Heading1', 'Water and memory'), 'A revised river passage about community.'])
    lib.sync(semantic=False); result = sem.build_index(lib)
    assert result['embedded_windows'] == 1
    assert sem.status(lib)['pending'] is False


def test_queries_cached_and_source_archives_never_opened(library, monkeypatch):
    import zipfile
    lib, _, calls, _ = library
    approve(lib); sem.build_index(lib)
    monkeypatch.setattr(zipfile, 'ZipFile', lambda *a, **k: pytest.fail('Semantic search opened an original book'))
    first = sem.search(lib, 'river memory')
    assert first and first[0]['scope'] == 'whole library'
    total = len(calls)
    assert sem.search(lib, 'river memory') == first
    assert len(calls) == total
    assert sem.status(lib)['state'] == 'ready'


def test_paid_initial_build_then_monthly_updates_and_queries(library, monkeypatch):
    lib, paths, _, _ = library
    calls = fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai')
    result = sem.build_index(lib)
    assert result['budget_scope'] == 'initial'
    assert result['api_usage']['requests'] == 1
    ledger = Budget(lib.state).report()
    assert ledger['spent_or_reserved']['initial'] > 0
    assert ledger['spent_or_reserved']['monthly'] == 0
    assert read_json(lib.state / 'config.json')['semantic']['openai_initial_complete'] is True
    docx(paths[0], [('Heading1', 'Water and memory'), 'Changed river argument.'])
    lib.sync(semantic=False); second = sem.build_index(lib)
    assert second['budget_scope'] == 'monthly'
    assert second['embedded_windows'] == 1
    count = len(calls)
    sem.search(lib, 'fresh uncached research question')
    assert len(calls) == count + 1
    sem.search(lib, 'fresh uncached research question')
    assert len(calls) == count + 1


def test_explicit_monthly_scope_cannot_use_initial_allowance(library, monkeypatch):
    lib, *_ = library
    fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai')
    assert not read_json(lib.state / 'config.json')['semantic'].get('openai_initial_complete')
    built = sem.build_index(lib, budget_scope='monthly')
    assert built['budget_scope'] == 'monthly'
    ledger = Budget(lib.state).report()
    assert ledger['spent_or_reserved']['monthly'] > 0
    assert ledger['spent_or_reserved']['initial'] == 0


def test_pause_stops_next_embedding_request_and_resume_reuses_completed_vectors(library, monkeypatch):
    lib, *_ = library
    calls = fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai')
    with pytest.raises(sem.SemanticPaused, match='retained'):
        sem.build_index(lib, batch_size=1, budget_scope='monthly', should_continue=lambda: not calls)
    assert len(calls) == 1
    assert not (lib.state / 'semantic/current.json').exists()
    built = sem.build_index(lib, batch_size=1, budget_scope='monthly', should_continue=lambda: True)
    assert len(calls) == 2
    assert built['cached_windows'] == 1 and built['embedded_windows'] == 1
    assert Budget(lib.state).report()['uncertain_reservations'] == 0


def test_pause_is_checked_for_each_token_split_request(library, monkeypatch):
    import tiktoken
    lib, *_ = library
    approve(lib, 'openai')
    calls, cached = [], []
    monkeypatch.setattr(sem, '_validate_files', lambda *_args: None)
    monkeypatch.setattr(tiktoken, 'get_encoding', lambda _: type('LargeTokens', (), {'encode': lambda self, text, **kwargs: range(130_000)})())
    monkeypatch.setattr(provider, 'embed_openai', lambda state, texts, scope: calls.append(texts) or np.ones((len(texts), 3072)))
    monkeypatch.setattr(sem, '_put_vectors', lambda cache, keys, *_args: cached.extend(keys))
    snapshot = {'exact_generation': lib._active()['generation']}
    with pytest.raises(sem.SemanticPaused):
        sem._embed_batch(lib, 'openai', snapshot, None, {'dimensions': 3072},
                         [('first', 'First text', []), ('second', 'Second text', [])],
                         'monthly', should_continue=lambda: not calls)
    assert calls == [['First text']] and cached == ['first']


def test_paid_scope_requires_durable_openai_authorization(library, monkeypatch):
    lib, *_ = library
    calls = fake_openai(monkeypatch, lib.state)
    with pytest.raises(ValueError, match='authorization'):
        provider.embed_openai(lib.state, ['source text'], scope='initial')
    approve(lib, 'local')
    with pytest.raises(ValueError, match='authorization'):
        provider.embed_openai(lib.state, ['source text'], scope='initial')
    assert calls == [] and Budget(lib.state).report()['requests'] == 0


def test_interrupted_paid_build_resumes_content_cache_without_repaying(library, monkeypatch):
    lib, paths, _, _ = library
    docx(paths[0], [piece for n in range(70) for piece in [('Heading1', f'Chapter {n}'), f'Source passage distinct number {n}.']])
    lib.sync(semantic=False); approve(lib, 'openai')
    calls = []
    def fake(state, texts, scope):
        calls.append((len(texts), scope))
        if len(calls) == 2:
            raise RuntimeError('Simulated network interruption; reservation retained.')
        return np.ones((len(texts), 3072), dtype=np.float32)
    monkeypatch.setattr(provider, 'embed_openai', fake)
    with pytest.raises(RuntimeError, match='interruption'):
        sem.build_index(lib, batch_size=64)
    assert not (lib.state / 'semantic/current.json').exists()
    assert sem.status(lib)['state'] == 'pending_update'
    second = sem.build_index(lib, batch_size=64)
    assert calls == [(64, 'initial'), (7, 'initial'), (7, 'initial')]
    assert second['embedded_windows'] == 7 and second['cached_windows'] == 64


def test_exact_generation_change_cannot_publish_stale_semantic_index(library):
    lib, paths, calls, _ = library
    docx(paths[0], [piece for n in range(130) for piece in [('Heading1', f'Chapter {n}'), f'Source passage distinct number {n}.']])
    lib.sync(semantic=False); approve(lib)
    triggered = []
    def interrupt(record):
        if not triggered:
            triggered.append(True)
            docx(paths[1], [('Heading1', 'Changed chapter'), 'Replacement source wording.'])
            lib.sync(semantic=False)
    with pytest.raises(ValueError, match='changed'):
        sem.build_index(lib, progress=interrupt)
    assert not (lib.state / 'semantic/current.json').exists()
    done = sem.build_index(lib)
    assert done['coverage_books'] == 2
    assert done['cached_windows'] >= 127  # one prior batch item may be the edited work


def test_budget_stop_preserves_exact_search_and_completed_generation(library, monkeypatch):
    lib, paths, _, _ = library
    fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai'); sem.build_index(lib)
    original = read_json(lib.state / 'semantic/current.json')['generation']
    reservation = Budget(lib.state).reserve(192_307_690, 'monthly')
    docx(paths[0], [('Heading1', 'Water and memory'), 'Violet sentinel changed text ' * 1200])
    lib.sync(semantic=False)
    with pytest.raises(BudgetExceeded):
        sem.build_index(lib)
    assert read_json(lib.state / 'semantic/current.json')['generation'] == original
    assert lib.search('violet sentinel')['direct']
    assert sem.status(lib)['pending'] is True


def test_pilot_identical_vectors_reused_without_modifying_frozen_records(library, monkeypatch):
    lib, _, calls, _ = library
    approve(lib)
    config = provider.embedding_config('local')
    with sem._cache(sem._root(lib), config) as cache:
        snapshot, path = sem._snapshot(lib, config, cache)
    with open_sqlite(path) as db:
        windows = [{'id': row[1], 'text': row[2], 'work_id': f'work{i%12}', 'passage_id': f'passage{i}'} for i,row in enumerate(db.execute('SELECT ordinal,id,text FROM windows'))]
    windows = [{**windows[i % 2], 'id': f'w{i}', 'work_id': f'work{i}'} for i in range(12)]
    queries = [{'query': f'question{i}'} for i in range(24)]
    manifest = {'frozen': True, 'query_count': 24, 'work_ids': [f'work{i}' for i in range(12)], 'queries_sha256': provider.canonical_hash(queries), 'windows_sha256': provider.canonical_hash(windows), 'tokenizer_revision': REV}
    fingerprint = provider.canonical_hash({'config': config, 'manifest': manifest})
    info = {**config, 'manifest_hash': provider.canonical_hash(manifest), 'windows_sha256': manifest['windows_sha256'], 'cache_fingerprint': fingerprint}
    atomic_json(lib.state / 'pilot/manifest.json', manifest)
    atomic_json(lib.state / 'pilot/queries.json', queries)
    atomic_json(lib.state / 'pilot/windows.json', windows)
    atomic_json(lib.state / 'pilot/local/engine.json', info)
    batch_hash = provider.canonical_hash({'binding': fingerprint, 'offset': 0, 'windows': windows})
    provider._save_vectors(lib.state / f'pilot/local/builds/{fingerprint}/batch-{batch_hash}.npy', np.ones((12, 384), dtype=np.float32))
    frozen = (lib.state / 'pilot/manifest.json').read_bytes()
    result = sem.build_index(lib)
    assert result['pilot_vectors_imported'] == 12
    assert result['embedded_windows'] == 0
    assert calls == []
    assert (lib.state / 'pilot/manifest.json').read_bytes() == frozen


def test_offline_estimate_has_no_paid_calls_and_reuses_tokenization(library, monkeypatch):
    lib, _, _, tokenizer = library
    calls = fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai')
    estimate = sem.estimate_index(lib)
    assert estimate['uncached_unique_windows'] == 2
    assert estimate['uncached_tokens'] > 0 and estimate['estimated_api_dollars'] > 0
    assert calls == [] and Budget(lib.state).report()['requests'] == 0
    before = tokenizer.calls
    sem.estimate_index(lib)
    assert tokenizer.calls == before


def test_empty_collection_and_corrupt_vector_cache(library):
    lib, paths, _, _ = library
    approve(lib); info = sem.build_index(lib)
    with sem._cache(sem._root(lib), info['config']) as cache:
        cache.execute('UPDATE vectors SET vector=?', (np.full((384,), np.nan, dtype=np.float32).tobytes(),))
    with pytest.raises(ValueError, match='finite'):
        sem.build_index(lib, force=True)
    for path in paths: path.unlink()
    lib.sync(semantic=False)
    empty = sem.build_index(lib)
    assert empty['windows'] == empty['coverage_books'] == 0
    assert sem.search(lib, 'anything') == []


def test_missing_full_index_does_not_charge_for_an_unusable_query(library, monkeypatch):
    lib, *_ = library
    calls = fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai'); info = sem.build_index(lib)
    count = len(calls)
    (sem._root(lib) / info['index_file']).unlink()
    assert sem.status(lib)['pending'] is True
    with pytest.raises(ValueError, match='unavailable'):
        sem.search(lib, 'a new research question')
    assert len(calls) == count


def test_acquisition_sync_automatically_prepares_semantics_and_retries_without_download(library, monkeypatch):
    lib, paths, _, _ = library
    approve(lib, 'openai')
    calls = fake_openai(monkeypatch, lib.state)
    initial = lib.sync()
    assert initial["semantic"]["coverage_books"] == 2
    assert len(calls) == 1
    assert {a['semantic_status'] for a in lib._catalog()['acquisitions'].values()} == {'ready'}
    added = docx(lib.root / "Gamma" / "A newly acquired book.docx", [("Heading1", "Fresh resource"), "Violet rivers connect ritual memory and workshop teaching."])
    manifest_path = lib.root / '.catalog/library.json'
    manifest = read_json(manifest_path)
    manifest['books'].append({'title': 'Gamma', 'author': 'New Author', 'bookshare_id': '12345',
                              'folder': str(added.parent), 'files': [{'path': str(added), 'format': 'docx'}]})
    atomic_json(manifest_path, manifest)
    original = added.read_bytes()
    def unavailable(*args, **kwargs):
        raise RuntimeError("Temporary provider failure; reservation retained.")
    monkeypatch.setattr(provider, "embed_openai", unavailable)
    failed = lib.sync(path=added)
    assert failed["semantic"]["state"] == "pending"
    assert lib.search("violet rivers", engine="exact")["direct"]
    acquisition = next(a for a in lib._catalog()['acquisitions'].values() if a.get('provider_id') == '12345')
    acquisition_id = acquisition['id']
    assert acquisition['stage'] == 'text_ready'
    assert acquisition['semantic_status'] == 'pending'
    assert 'semantic_generation' not in acquisition
    files_before = lib._catalog()['files']
    signature_before = lib._active()['source_signature']
    # Restoring the provider resumes indexing; sync does not redownload or rewrite.
    monkeypatch.undo()
    # Reinstall the deterministic local dependencies and API factory after undo.
    tokenizer = Tokenizer()
    monkeypatch.setattr(provider, "local_model", lambda: type("Model", (), {"tokenizer": tokenizer})())
    monkeypatch.setattr(provider, "local_revision", lambda: REV)
    resumed_calls = fake_openai(monkeypatch, lib.state)
    resumed = lib.sync(path=added)
    assert resumed["changed"] is False
    assert resumed["semantic"]["embedded_windows"] == 1
    assert len(resumed_calls) == 1
    assert added.read_bytes() == original
    assert sem.status(lib)["state"] == "ready"
    catalog = lib._catalog()
    acquisition = catalog['acquisitions'][acquisition_id]
    assert acquisition['semantic_status'] == 'ready'
    assert acquisition['semantic_generation'] == resumed['semantic']['generation']
    assert acquisition['semantic_exact_generation'] == resumed['generation']
    assert len(catalog['acquisitions']) == 3
    assert catalog['files'] == files_before
    assert lib._active()['source_signature'] == signature_before
    from book_library.service import catalog_signature
    assert catalog_signature(catalog) == signature_before
    assert lib.sync()['changed'] is False


def test_openai_query_does_not_load_local_weights_and_invalid_pointer_keeps_exact(library, monkeypatch):
    lib, *_ = library
    fake_openai(monkeypatch, lib.state)
    approve(lib, "openai"); sem.build_index(lib)
    monkeypatch.setattr(provider, "local_model", lambda: pytest.fail("OpenAI query loaded local model"))
    assert sem.search(lib, "river memory")
    (lib.state / "semantic/current.json").write_text("truncated{")
    assert sem.status(lib)["state"] == "pending_update"
    result = lib.search("river voices")
    assert result["direct"]
    assert any("unavailable" in warning.lower() or "not ready" in warning.lower() for warning in result["warnings"])


def test_semantic_window_offsets_preserve_late_reading_context(library, monkeypatch):
    lib, paths, _, _ = library
    body = ('Introduction ' * 5000) + 'Zirconia motifs encode communal recognition.'
    docx(paths[0], [('Heading1', 'Long essay'), body])
    lib.sync(semantic=False)
    def embed(texts, kind='passage'):
        vectors = np.zeros((len(texts), 384), dtype=np.float32)
        for i, text in enumerate(texts):
            vectors[i, 0 if kind == 'query' or 'Zirconia motifs' in text else 1] = 1.
        return vectors
    monkeypatch.setattr(provider, 'embed_local', embed)
    approve(lib); sem.build_index(lib)
    match = sem.search(lib, 'crystalline belonging')[0]
    assert match['char_start'] > 20000
    hit = next(h for h in lib.search('crystalline belonging')['connections'] if h['id'] == match['passage_id'])
    assert hit['read_offset'] > 20000
    assert hit['semantic_window']['char_start'] == match['char_start']
    assert not hit['text'].startswith('Long essay')
    assert len(hit['text']) <= 702
    assert 'Zirconia motifs' in lib.read(hit['id'], offset=hit['read_offset'])['passage']['text']


def test_published_initial_build_repairs_interrupted_completion_flag(library, monkeypatch):
    lib, paths, _, _ = library
    fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai'); sem.build_index(lib)
    # Simulate interruption after current.json publication but before the next
    # atomic config write. The unchanged fast path must still repair the flag.
    config = read_json(lib.state / 'config.json')
    config['semantic'].pop('openai_initial_complete')
    atomic_json(lib.state / 'config.json', config)
    # Publication can also precede the atomic acquisition bookkeeping update.
    cat = lib._catalog()
    for acquisition in cat['acquisitions'].values():
        acquisition['semantic_status'] = 'pending'
        acquisition.pop('semantic_generation')
    atomic_json(lib.state / 'catalog.json', cat)
    repaired = sem.build_index(lib)
    assert repaired['changed'] is False
    assert all(a['semantic_status'] == 'ready' and a['semantic_generation'] == repaired['generation']
               for a in lib._catalog()['acquisitions'].values())
    assert read_json(lib.state / 'config.json')['semantic']['openai_initial_complete'] is True
    docx(paths[0], [('Heading1', 'New edition'), 'A changed argument after initial publication.'])
    lib.sync(semantic=False)
    assert sem.build_index(lib)['budget_scope'] == 'monthly'


def test_acquisition_empty_exports_are_limits_not_unfinished_processing(library):
    lib, paths, calls, _ = library
    blank = docx(paths[0].parent / 'Empty export.docx', [])
    manifest_path = lib.root / '.catalog/library.json'
    manifest = read_json(manifest_path)
    manifest['books'][0]['files'].append({'path': str(blank), 'format': 'docx'})
    atomic_json(manifest_path, manifest)
    lib.sync(semantic=False)
    cat = lib._catalog()
    acquisition_id = next(a['id'] for a in cat['acquisitions'].values() if a['record']['title'] == 'Alpha')
    cat['acquisitions'][acquisition_id]['extraction_warnings'] = ['Source export is incomplete.']
    atomic_json(lib.state / 'catalog.json', cat)
    approve(lib)
    result = sem.build_index(lib)
    cat = lib._catalog()
    acquisition = cat['acquisitions'][acquisition_id]
    blank_id = next(f['id'] for f in cat['files'].values() if f['path'] == str(blank))
    assert acquisition['semantic_status'] == 'ready'
    assert acquisition['semantic_generation'] == result['generation']
    assert acquisition['semantic_empty_file_ids'] == [blank_id]
    assert acquisition['semantic_limitations']
    assert acquisition['extraction_warnings'] == ['Source export is incomplete.']
    assert result['embedded_windows'] == 2
    before = len(calls)
    # Unchanged sync preserves readiness without re-embedding or re-extraction.
    assert lib.sync()['changed'] is False
    assert len(calls) == before
    # Missing files are an extraction/acquisition problem, even when they were empty.
    blank.unlink()
    lib.sync()
    acquisition = lib._catalog()['acquisitions'][acquisition_id]
    assert acquisition['stage'] == 'downloaded'
    assert acquisition['semantic_status'] == 'pending'
    assert 'semantic_generation' not in acquisition
    assert acquisition['extraction_warnings'] == ['Source export is incomplete.']


def test_acquisition_readiness_requires_current_sources_and_full_file_coverage(library):
    from book_library.service import catalog_signature
    from book_library.storage import writer
    lib, paths, _, _ = library
    approve(lib); published = sem.build_index(lib)
    cat = lib._catalog()
    signature = catalog_signature(cat)
    file = next(f for f in cat['files'].values() if f['path'] == str(paths[0]))
    acquisition_id = file['acquisition_id']
    # A map with no coverage for a readable file must not be called ready.
    uncovered = {**published, 'files': {k: v for k, v in published['files'].items() if k != file['id']}}
    with writer(lib.state):
        sem.refresh_acquisition_statuses(lib, cat, published=uncovered)
    assert cat['acquisitions'][acquisition_id]['semantic_status'] == 'pending'
    assert catalog_signature(cat) == signature
    # Even a matching stored hash cannot hide an unsynchronized source edit.
    docx(paths[0], [('Heading1', 'Changed source'), 'New material requires extraction and indexing.'])
    with writer(lib.state):
        sem.refresh_acquisition_statuses(lib, cat, published=published)
    assert cat['acquisitions'][acquisition_id]['semantic_status'] == 'pending'
    assert catalog_signature(cat) == signature
    assert 'semantic_generation' not in cat['acquisitions'][acquisition_id]


def test_queued_build_refreshes_budget_scope_after_acquiring_writer(library, monkeypatch):
    from contextlib import contextmanager
    lib, _, _, _ = library
    fake_openai(monkeypatch, lib.state)
    approve(lib, 'openai')
    original_writer = sem.writer
    @contextmanager
    def intervening_completion(path):
        with original_writer(path):
            if path == sem._root(lib):
                # A queued call already read settings; another build completed
                # before this writer became available (its pointer may be gone).
                config = read_json(lib.state / 'config.json')
                config['semantic']['openai_initial_complete'] = True
                atomic_json(lib.state / 'config.json', config)
            yield
    monkeypatch.setattr(sem, 'writer', intervening_completion)
    assert sem.build_index(lib)['budget_scope'] == 'monthly'
