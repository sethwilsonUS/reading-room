"""Lossless, disposable structure caches with conservative generation retention."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import zlib

from .storage import connect, read_json

SIGNATURE = re.compile(r'[0-9a-f]{64}')
GENERATION = re.compile(r'gen_[0-9a-f]{20}')
CACHE_NAME = re.compile(r'([0-9a-f]{64})\.json(?:\.gz)?')
CACHE_ERRORS = (OSError, EOFError, ValueError, TypeError, zlib.error)


def _ordinary(path, *, directory=False):
    return not path.is_symlink() and (path.is_dir() if directory else path.is_file())


def _valid(record, signature):
    from .structure import ANALYZER_VERSION, _FIELDS
    if not isinstance(record, dict) or set(record) != {'version', 'report', 'patches'}:
        return False
    version = record['version']
    report = record['report']
    patches = record['patches']
    if (not isinstance(version, str) or not version.isdecimal()
            or not 0 < int(version) <= int(ANALYZER_VERSION)
            or not isinstance(report, dict) or report.get('source_signature') != signature
            or report.get('analyzer_version') != version or not isinstance(patches, dict)):
        return False
    allowed_fields = set(_FIELDS)
    return all(isinstance(fid, str) and fid and isinstance(values, list)
               and all(isinstance(patch, list) and len(patch) == 2
                       and type(patch[0]) is int and patch[0] >= 0
                       and isinstance(patch[1], dict) and set(patch[1]) <= allowed_fields
                       for patch in values)
               for fid, values in patches.items())


def _read(path, signature):
    if not _ordinary(path):
        raise ValueError('Structure cache is not an ordinary file.')
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        record = json.load(stream)
    if not _valid(record, signature):
        raise ValueError('Unrecognized structure cache metadata.')
    return record


def load_structure_cache(state, signature):
    """Read either format without modifying a cache, falling back after corruption."""
    folder = state / 'structure-analysis'
    if not _ordinary(folder, directory=True) or not SIGNATURE.fullmatch(signature):
        return None
    for suffix in ('.json.gz', '.json'):
        try:
            return _read(folder / (signature + suffix), signature)
        except CACHE_ERRORS:
            continue
    return None


def _atomic_gzip(path, *, record=None, source=None):
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', compresslevel=6,
                               mtime=0, fileobj=raw) as encoded:
                if source is not None:
                    with source.open('rb') as stream:
                        shutil.copyfileobj(stream, encoded)
                else:
                    text = io.TextIOWrapper(encoded, encoding='utf-8')
                    try:
                        json.dump(record, text, ensure_ascii=False, separators=(',', ':'))
                        text.flush()
                    finally:
                        text.detach()
            raw.flush()
            os.fsync(raw.fileno())
        # Migration preserves even JSON formatting bytes. Verify the completed
        # compressed file before publishing it or removing its legacy input.
        if source is not None:
            with source.open('rb') as stream, gzip.open(temporary, 'rb') as restored:
                if hashlib.file_digest(stream, 'sha256').digest() != hashlib.file_digest(restored, 'sha256').digest():
                    raise ValueError('Compressed structure cache did not preserve its input.')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_structure_cache(state, signature, record):
    """Write new cache entries atomically; unfamiliar existing paths stay intact."""
    folder = state / 'structure-analysis'
    if folder.is_symlink() or not SIGNATURE.fullmatch(signature) or not _valid(record, signature):
        return
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (signature + '.json.gz')
    if target.exists() or target.is_symlink():
        # A corrupt or unrecognized cache is expendable for reading, but is not
        # sufficient evidence for overwriting an unknown file during indexing.
        try:
            _read(target, signature)
        except CACHE_ERRORS:
            return
    _atomic_gzip(target, record=record)


def _references(lib):
    """Protect references from every remaining exact generation, including rollback."""
    generations = lib.state / 'generations'
    current_path = lib.state / 'current.json'
    if not _ordinary(generations, directory=True) or not _ordinary(current_path):
        raise ValueError('Published exact generations are unavailable; structure retention deferred.')
    current = read_json(current_path)
    active = current.get('generation') if isinstance(current, dict) else None
    if (not isinstance(active, str) or not GENERATION.fullmatch(active)
            or current.get('database') != active + '.sqlite3'):
        raise ValueError('Exact generation identity is inconsistent; structure retention deferred.')
    references = set()
    seen = set()
    for path in generations.iterdir():
        if path.name == '.DS_Store' and _ordinary(path):
            continue
        if (not _ordinary(path) or path.suffix != '.sqlite3'
                or not GENERATION.fullmatch(path.stem)):
            raise ValueError('Unrecognized exact generation state; structure retention deferred.')
        with connect(path) as db:
            identity = db.execute("SELECT value FROM metadata WHERE key='generation'").fetchone()
            if not identity or identity[0] != path.stem:
                raise ValueError('Exact generation metadata is incomplete; structure retention deferred.')
            for row in db.execute('SELECT data FROM works'):
                work = json.loads(row[0])
                report = work.get('structure', {}) if isinstance(work, dict) else {}
                signature = report.get('source_signature') if isinstance(report, dict) else None
                if not isinstance(signature, str) or not SIGNATURE.fullmatch(signature):
                    raise ValueError('Exact generation structure references are incomplete; retention deferred.')
                references.add(signature)
        seen.add(path.stem)
    if active not in seen:
        raise ValueError('Current exact generation is missing; structure retention deferred.')
    return references


def _allocated(path):
    return path.stat().st_blocks * 512


def maintain_structure_cache(lib, *, apply=False):
    """Plan or apply maintenance. Caller holds the library writer lock when applying.

    ``compressible_bytes`` counts inputs, not promised savings. Reclaimable bytes
    include only obsolete/duplicate files; actual compression savings are reported
    after application. Read-only inspection never creates folders or lock files.
    """
    result = {'applied': False, 'retained_signatures': [], 'compressible_paths': [],
              'obsolete_paths': [], 'skipped_paths': [], 'compressible_bytes': 0,
              'reclaimable_bytes': 0, 'compressed_paths': [], 'removed_paths': [],
              'reclaimed_bytes': 0}
    folder = lib.state / 'structure-analysis'
    if not folder.exists() and not folder.is_symlink():
        return result
    if not _ordinary(folder, directory=True):
        return {**result, 'deferred': 'Structure cache directory is not ordinary.'}
    try:
        references = _references(lib)
    except (OSError, ValueError, TypeError, sqlite3.Error) as exc:
        return {**result, 'deferred': str(exc)}
    result['retained_signatures'] = sorted(references)
    for path in sorted(folder.iterdir()):
        match = CACHE_NAME.fullmatch(path.name)
        if not match or not _ordinary(path):
            result['skipped_paths'].append(str(path))
            continue
        signature = match[1]
        if signature in references and path.suffix == '.gz':
            # Common path after the first migration: retained immutable gzip
            # caches need no full decompression on every no-op synchronization.
            continue
        try:
            record = _read(path, signature)
        except CACHE_ERRORS:
            result['skipped_paths'].append(str(path))
            continue
        size = _allocated(path)
        if signature not in references:
            result['obsolete_paths'].append(str(path))
            result['reclaimable_bytes'] += size
            if apply:
                path.unlink()
                result['removed_paths'].append(str(path))
                result['reclaimed_bytes'] += size
            continue
        target = path.with_suffix('.json.gz')
        if target.exists() or target.is_symlink():
            try:
                if _read(target, signature) != record:
                    raise ValueError('Conflicting structure cache representations.')
            except CACHE_ERRORS:
                result['skipped_paths'].append(str(path))
                continue
            # Both representations are known and equivalent; the gzip copy was
            # fully read and verified before the legacy copy can be removed.
            result['obsolete_paths'].append(str(path))
            result['reclaimable_bytes'] += size
            if apply:
                path.unlink()
                result['removed_paths'].append(str(path))
                result['reclaimed_bytes'] += size
        else:
            result['compressible_paths'].append(str(path))
            result['compressible_bytes'] += size
            if apply:
                _atomic_gzip(target, source=path)
                path.unlink()
                result['compressed_paths'].append(str(target))
                result['removed_paths'].append(str(path))
                result['reclaimed_bytes'] += size - _allocated(target)
    result['applied'] = apply
    return result
