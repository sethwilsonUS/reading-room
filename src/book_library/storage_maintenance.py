"""Inspect local storage and maintain only recognized, disposable derived state."""
from __future__ import annotations

from contextlib import nullcontext
import sqlite3
import stat

from .storage import writer


def _usage(path):
    """Count ordinary files without following links or opening book content."""
    result = {'files': 0, 'logical_bytes': 0, 'allocated_bytes': 0,
              'skipped_paths': []}
    seen = set()

    def visit(item):
        try:
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                result['skipped_paths'].append(str(item))
            elif stat.S_ISDIR(info.st_mode):
                for child in item.iterdir():
                    visit(child)
            elif stat.S_ISREG(info.st_mode):
                identity = (info.st_dev, info.st_ino)
                if identity not in seen:
                    seen.add(identity)
                    result['files'] += 1
                    result['logical_bytes'] += info.st_size
                    result['allocated_bytes'] += info.st_blocks * 512
            else:
                result['skipped_paths'].append(str(item))
        except OSError:
            result['skipped_paths'].append(str(item))

    if path.exists() or path.is_symlink():
        visit(path)
    return result


def maintain_structure_caches(lib):
    """Best-effort post-sync maintenance; caller already holds library writer."""
    from .structure_cache import maintain_structure_cache
    try:
        return maintain_structure_cache(lib, apply=True)
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Index publication remains successful; explicit storage inspection
        # reports maintenance trouble without exposing any source contents.
        return {'applied': False, 'deferred': f'Structure cache maintenance failed ({type(exc).__name__}).'}


def _semantic_plan(lib, *, apply=False):
    from .semantic_retention import _plan, prune_locked
    root = lib.state / 'semantic'
    if not root.exists() and not root.is_symlink():
        return {'applied': False, 'state': 'not_initialized', 'reclaimable_bytes': 0}
    try:
        return prune_locked(lib, apply=True) if apply else {**_plan(lib), 'applied': False}
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        return {'applied': False, 'deferred': f'Semantic maintenance could not safely complete ({type(exc).__name__}).'}


def _check_state(lib):
    if lib.state.is_symlink() or lib.state.exists() and not lib.state.is_dir():
        raise ValueError('Storage maintenance requires an ordinary .library directory.')
    semantic = lib.state / 'semantic'
    if semantic.is_symlink() or semantic.exists() and not semantic.is_dir():
        raise ValueError('Storage maintenance requires an ordinary semantic directory.')


def storage(lib, *, compact=False):
    """A report is read-only; compaction uses publication locks, never providers.

    Reports are a point-in-time estimate: another process may publish while
    they are being read. Compaction always plans again after taking the locks.
    """
    from .structure_cache import maintain_structure_cache
    _check_state(lib)
    if compact and lib.state.exists():
        semantic = lib.state / 'semantic'
        # Match semantic publication's lock order, including the reader check
        # inside prune_locked. Do not create a semantic store for exact users.
        semantic_locked = semantic.exists()
        with writer(semantic) if semantic_locked else nullcontext():
            with writer(lib.state):
                _check_state(lib)
                structure = maintain_structure_caches(lib)
                # A first build may create semantic/ while we wait for the
                # library writer. Never prune that builder's journaled files
                # unless we actually acquired its writer before this one.
                semantic_result = _semantic_plan(lib, apply=semantic_locked)
                if not semantic_locked and semantic.exists():
                    semantic_result['deferred'] = 'Semantic storage was initialized concurrently; retry compaction.'
    else:
        try:
            structure = maintain_structure_cache(lib)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            structure = {'applied': False, 'deferred': f'Structure cache inspection could not safely complete ({type(exc).__name__}).'}
        semantic_result = _semantic_plan(lib)

    parts = ('generations', 'structure-analysis', 'extractions', 'semantic/generations',
             'semantic/snapshots', 'semantic/cache', 'semantic/queries', 'pilot')
    return {'root': str(lib.root), 'state': str(lib.state),
            'mode': 'compact' if compact else 'report',
            'usage': _usage(lib.state),
            'components': {name: _usage(lib.state / name) for name in parts},
            'structure': structure, 'semantic': semantic_result,
            'note': 'Usage is a point-in-time measurement. Paid caches, original books, and durable records are preserved. No indexing or paid requests are performed.'}
