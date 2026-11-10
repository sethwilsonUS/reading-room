"""SQLite connections opened through storage helpers never outlive their block."""
from __future__ import annotations

import sqlite3

import pytest

from book_library.storage import connect, open_sqlite


def closed(db):
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute('SELECT 1')
    return True


def test_with_block_commits_then_closes(tmp_path):
    path = tmp_path / 'data.sqlite3'
    with open_sqlite(path) as db:
        db.execute('CREATE TABLE t(x)')
        db.execute('INSERT INTO t VALUES (1)')
    assert closed(db)
    with open_sqlite(path) as check:
        assert check.execute('SELECT x FROM t').fetchone() == (1,)


def test_with_block_rolls_back_then_closes_on_error(tmp_path):
    path = tmp_path / 'data.sqlite3'
    with open_sqlite(path) as db:
        db.execute('CREATE TABLE t(x)')
    with pytest.raises(RuntimeError):
        with open_sqlite(path) as db:
            db.execute('INSERT INTO t VALUES (1)')
            raise RuntimeError('abort')
    assert closed(db)
    with open_sqlite(path) as check:
        assert check.execute('SELECT count(*) FROM t').fetchone() == (0,)


def test_read_only_connect_closes_and_keeps_row_factory(tmp_path):
    path = tmp_path / 'index.sqlite3'
    with open_sqlite(path) as db:
        db.execute('CREATE TABLE t(x)')
    with connect(path) as db:
        assert isinstance(db.execute('SELECT 1 AS one').fetchone(), sqlite3.Row)
        with pytest.raises(sqlite3.OperationalError):
            db.execute('INSERT INTO t VALUES (1)')
    assert closed(db)
