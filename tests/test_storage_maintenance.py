"""Storage maintenance uses synthetic sources and never invokes paid work."""
import gzip
import hashlib

import pytest

from book_library.cli import parser, run
from book_library.service import Library
from book_library.storage_maintenance import _usage
from test_service import docx


def command(root, *args):
    return run(parser().parse_args(['--root', str(root), 'storage', *args, '--json']))


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def test_report_and_empty_compaction_do_not_initialize_a_library(tmp_path):
    for args in [(), ('report',), ('compact',)]:
        result = command(tmp_path, *args)
        assert result['usage']['files'] == 0
        assert result['semantic']['state'] == 'not_initialized'
        assert not (tmp_path / '.library').exists()


def test_report_is_read_only_and_compact_preserves_source_and_paid_records(tmp_path, monkeypatch):
    source = docx(tmp_path / 'Synthetic' / 'Book.docx', [
        ('Heading1', 'Invented river chapter'), 'A synthetic silver river flows through this fixture. ' * 100])
    lib = Library(tmp_path)
    lib.sync(semantic=False)
    # Exercise a legacy cache without changing its JSON contents or signature.
    caches = lib.state / 'structure-analysis'
    for path in caches.glob('*.json.gz'):
        path.with_suffix('').write_bytes(gzip.decompress(path.read_bytes()))
        path.unlink()
    legacy = list(caches.glob('*.json'))
    assert legacy
    expected = {p.with_suffix('.json.gz').name: p.read_bytes() for p in legacy}
    durable = [source, lib.state / 'config.json', lib.state / 'catalog.json']
    for name in ['usage.jsonl', 'costs.jsonl', 'semantic/cache/synthetic-paid.sqlite3']:
        path = lib.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'preserved synthetic record\n')
        durable.append(path)
    saved = {p: p.read_bytes() for p in durable}
    monkeypatch.setattr(Library, 'sync', lambda *_a, **_kw: pytest.fail('Maintenance must not index'))
    before = snapshot(tmp_path)
    report = command(tmp_path)
    assert report['mode'] == 'report'
    assert report['structure']['compressible_paths']
    assert snapshot(tmp_path) == before
    result = command(tmp_path, 'compact')
    assert result['mode'] == 'compact'
    assert result['structure']['compressed_paths']
    assert all(p.read_bytes() == data for p, data in saved.items())
    # Compression preserves the complete parsed structure, including evidence.
    import json
    for name, data in expected.items():
        assert json.loads(gzip.decompress((caches / name).read_bytes())) == json.loads(data)
    assert not list(caches.glob('*.json'))
    assert command(tmp_path, 'compact')['structure']['compressed_paths'] == []


def test_usage_never_follows_links_and_deduplicates_hardlinks(tmp_path):
    folder = tmp_path / 'state'
    folder.mkdir()
    content = folder / 'data'
    content.write_bytes(b'synthetic')
    (folder / 'same-file').hardlink_to(content)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'book').write_bytes(b'not counted')
    (folder / 'external').symlink_to(outside, target_is_directory=True)
    usage = _usage(folder)
    assert usage['files'] == 1 and usage['logical_bytes'] == len(b'synthetic')
    assert usage['skipped_paths'] == [str(folder / 'external')]


@pytest.mark.parametrize('component', ['.library', '.library/semantic'])
def test_maintenance_refuses_linked_state_directories(tmp_path, component):
    outside = tmp_path / 'outside'
    outside.mkdir()
    link = tmp_path / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    for action in ['report', 'compact']:
        with pytest.raises(ValueError, match='ordinary'):
            command(tmp_path, action)
    assert not list(outside.iterdir())


def test_sync_reports_deferred_maintenance_without_losing_publication(tmp_path, monkeypatch):
    from book_library import structure_cache
    source = docx(tmp_path / 'Synthetic.docx', ['Synthetic healthy text.'])
    lib = Library(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError('Synthetic unavailable cache')

    monkeypatch.setattr(structure_cache, 'maintain_structure_cache', fail)
    first = lib.sync(semantic=False)
    assert first['changed'] and not first['structure_retention']['applied']
    assert lib.books()[0]['available']
    unchanged = lib.sync(semantic=False)
    assert not unchanged['changed'] and unchanged['structure_retention']['deferred']
    assert source.exists()


def test_compact_does_not_prune_a_concurrently_initialized_semantic_build(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from book_library import storage_maintenance
    from book_library.semantic_retention import begin_operation
    from book_library.storage import writer
    lib = Library(tmp_path)
    lib.state.mkdir()
    semantic = lib.state / 'semantic'
    generation = 'sem_' + '1' * 20
    folder = semantic / 'generations' / generation

    @contextmanager
    def concurrent_writer(path):
        with writer(path):
            assert path == lib.state
            # Compaction already chose its lock path when a first builder
            # creates semantic/. That builder holds semantic writer throughout.
            with writer(semantic):
                begin_operation(semantic, 'generation', generation, snapshot_id='a' * 64)
                folder.mkdir(parents=True)
                (folder / 'index.usearch').write_bytes(b'in-progress synthetic index')
                yield

    monkeypatch.setattr(storage_maintenance, 'writer', concurrent_writer)
    result = command(tmp_path, 'compact')
    assert not result['semantic']['applied']
    assert 'concurrently' in result['semantic']['deferred']
    assert (folder / 'index.usearch').read_bytes() == b'in-progress synthetic index'
