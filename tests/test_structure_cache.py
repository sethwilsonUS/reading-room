"""Synthetic cache lifecycle checks: lossless migration and safe retention."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from book_library import structure_cache as cache
from book_library.storage import atomic_json, open_sqlite
from book_library.structure import ANALYZER_VERSION
from test_structure_pipeline import blocks, prepare


def record(signature):
    return {'version': ANALYZER_VERSION,
            'report': {'analyzer_version': ANALYZER_VERSION, 'source_signature': signature,
                       'evidence': ['Synthetic naïve poetry\nwith line breaks.']},
            'patches': {'file_synthetic': [[0, {'heading_path': ['Synthetic heading'] * 2000}]]}}


def generation(lib, number, signatures):
    name = f'gen_{number:020x}'
    folder = lib.state / 'generations'
    folder.mkdir(parents=True, exist_ok=True)
    with open_sqlite(folder / (name + '.sqlite3')) as db:
        db.execute('CREATE TABLE metadata(key TEXT,value TEXT)')
        db.execute('INSERT INTO metadata VALUES(?,?)', ('generation', name))
        db.execute('CREATE TABLE works(data TEXT)')
        db.executemany('INSERT INTO works VALUES(?)',
                       [(json.dumps({'structure': {'source_signature': signature}}),)
                        for signature in signatures])
    atomic_json(lib.state / 'current.json', {'generation': name, 'database': name + '.sqlite3'})


@pytest.fixture
def state(tmp_path):
    lib = SimpleNamespace(state=tmp_path / '.library')
    signatures = [f'{n:064x}' for n in range(1, 4)]
    folder = lib.state / 'structure-analysis'
    for signature in signatures:
        atomic_json(folder / (signature + '.json'), record(signature))
    generation(lib, 1, [signatures[1]])
    generation(lib, 2, [signatures[2]])
    return lib, signatures


def test_new_gzip_cache_and_legacy_cache_preserve_analysis_and_source_text(tmp_path):
    original = blocks([(1, 'Synthetic chapter'), 'Poetry remembers naïve hope.\nA second line.'])
    report, first = prepare(tmp_path, [deepcopy(original)], work={'title': 'Synthetic monograph'})
    saved = next((tmp_path / 'structure-analysis').iterdir())
    assert saved.name == report['source_signature'] + '.json.gz'
    cached_report, second = prepare(tmp_path, [deepcopy(original)], work={'title': 'Synthetic monograph'})
    assert cached_report['cache_reused'] and second == first
    raw = gzip.decompress(saved.read_bytes())
    legacy = saved.with_suffix('')
    legacy.write_bytes(raw)
    saved.unlink()
    cached_report, third = prepare(tmp_path, [deepcopy(original)], work={'title': 'Synthetic monograph'})
    assert cached_report['cache_reused'] and third == first
    assert legacy.read_bytes() == raw and not saved.exists()
    assert [block['text'] for block in third['file_one']['blocks']] == [block['text'] for block in original]


def test_dry_run_and_compaction_preserve_current_rollback_and_durable_state(state):
    lib, signatures = state
    durable = ['curation.jsonl', 'costs.jsonl', 'usage.jsonl', 'section-identities.json',
               'passage-history.sqlite3', 'semantic/cache/vectors.sqlite3']
    for name in durable:
        path = lib.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic durable record')
    before = {p.name: p.read_bytes() for p in (lib.state / 'structure-analysis').iterdir()}
    plan = cache.maintain_structure_cache(lib)
    assert not plan['applied']
    assert len(plan['obsolete_paths']) == 1 and len(plan['compressible_paths']) == 2
    assert plan['retained_signatures'] == signatures[1:]
    assert {p.name: p.read_bytes() for p in (lib.state / 'structure-analysis').iterdir()} == before
    result = cache.maintain_structure_cache(lib, apply=True)
    assert result['applied'] and result['reclaimed_bytes'] > 0
    assert sorted(p.name for p in (lib.state / 'structure-analysis').iterdir()) == [s + '.json.gz' for s in signatures[1:]]
    for signature in signatures[1:]:
        compressed = lib.state / 'structure-analysis' / (signature + '.json.gz')
        assert gzip.decompress(compressed.read_bytes()) == before[signature + '.json']
    assert all((lib.state / name).read_bytes() == b'Synthetic durable record' for name in durable)
    assert cache.maintain_structure_cache(lib, apply=True)['removed_paths'] == []


def test_warm_maintenance_does_not_decompress_retained_caches(state, monkeypatch):
    lib, _ = state
    cache.maintain_structure_cache(lib, apply=True)
    monkeypatch.setattr(cache, '_read', lambda *args: pytest.fail('Warm retained cache was decompressed'))
    assert cache.maintain_structure_cache(lib, apply=True)['reclaimed_bytes'] == 0


@pytest.mark.parametrize('problem', ['missing_current', 'missing_reference', 'unreadable_rollback', 'symlink_generation'])
def test_uncertain_generation_references_defer_all_maintenance(state, problem, tmp_path):
    lib, _ = state
    folder = lib.state / 'structure-analysis'
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    if problem == 'missing_current':
        (lib.state / 'current.json').unlink()
    elif problem == 'missing_reference':
        with open_sqlite(next((lib.state / 'generations').glob('*.sqlite3'))) as db:
            db.execute('UPDATE works SET data=?', ('{}',))
    elif problem == 'unreadable_rollback':
        next((lib.state / 'generations').glob('*.sqlite3')).write_bytes(b'Interrupted database')
    else:
        external = tmp_path / 'outside.sqlite3'
        external.write_bytes(b'Synthetic external file')
        (lib.state / 'generations' / 'gen_00000000000000000005.sqlite3').symlink_to(external)
    result = cache.maintain_structure_cache(lib, apply=True)
    assert not result['applied'] and result['deferred']
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_unknown_corrupt_future_and_symlinked_cache_files_stay_intact(state, tmp_path):
    lib, _ = state
    folder = lib.state / 'structure-analysis'
    unknown = folder / ('a' * 64 + '.json')
    unknown.write_text('{"unrecognized": true}')
    corrupt = folder / ('b' * 64 + '.json.gz')
    corrupt.write_bytes(b'corrupt gzip')
    future = folder / ('c' * 64 + '.json')
    atomic_json(future, {**record('c' * 64), 'version': str(int(ANALYZER_VERSION) + 1)})
    external = tmp_path / 'external.txt'
    external.write_bytes(b'Synthetic source remains unchanged')
    link = folder / ('d' * 64 + '.json')
    link.symlink_to(external)
    original = {p: p.read_bytes() for p in [unknown, corrupt, future, external]}
    result = cache.maintain_structure_cache(lib, apply=True)
    assert set(map(str, [unknown, corrupt, future, link])) <= set(result['skipped_paths'])
    assert all(p.read_bytes() == content for p, content in original.items())
    assert link.is_symlink()


def test_failed_compression_keeps_legacy_input_and_retries(state, monkeypatch):
    lib, signatures = state
    original_replace = cache.os.replace
    target = lib.state / 'structure-analysis' / (signatures[1] + '.json.gz')
    source = target.with_suffix('')
    before = source.read_bytes()

    def fail_publish(old, new):
        if new == target:
            raise OSError('Synthetic disk full')
        return original_replace(old, new)

    monkeypatch.setattr(cache.os, 'replace', fail_publish)
    with pytest.raises(OSError, match='disk full'):
        cache.maintain_structure_cache(lib, apply=True)
    assert source.read_bytes() == before and not target.exists()
    assert not list(target.parent.glob('.*'))
    monkeypatch.setattr(cache.os, 'replace', original_replace)
    cache.maintain_structure_cache(lib, apply=True)
    assert gzip.decompress(target.read_bytes()) == before and not source.exists()


def test_interrupted_legacy_removal_resumes_without_recompressing(state, monkeypatch):
    lib, signatures = state
    source = lib.state / 'structure-analysis' / (signatures[1] + '.json')
    original_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        if path == source:
            raise OSError('Synthetic interrupted removal')
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_unlink)
    with pytest.raises(OSError, match='interrupted removal'):
        cache.maintain_structure_cache(lib, apply=True)
    target = source.with_suffix('.json.gz')
    assert gzip.decompress(target.read_bytes()) == source.read_bytes()
    monkeypatch.setattr(Path, 'unlink', original_unlink)
    result = cache.maintain_structure_cache(lib, apply=True)
    assert str(source) in result['removed_paths'] and not source.exists()


def test_conflicting_compressed_file_does_not_replace_legacy_input(state):
    lib, signatures = state
    signature = signatures[1]
    source = lib.state / 'structure-analysis' / (signature + '.json')
    target = source.with_suffix('.json.gz')
    target.write_bytes(b'Unrecognized existing file')
    before = source.read_bytes()
    result = cache.maintain_structure_cache(lib, apply=True)
    assert str(source) in result['skipped_paths']
    assert source.read_bytes() == before and target.read_bytes() == b'Unrecognized existing file'


def test_absent_cache_report_does_not_create_library_state(tmp_path):
    lib = SimpleNamespace(state=tmp_path / '.library')
    assert not cache.maintain_structure_cache(lib)['applied']
    assert not lib.state.exists()
