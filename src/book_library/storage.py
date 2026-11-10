"""Durable curation and atomic, single-writer index generations."""
from __future__ import annotations
import contextlib, datetime as dt, fcntl, hashlib, json, os, sqlite3, tempfile, uuid
from pathlib import Path


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex[:20]


def stable(prefix, *parts):
    return prefix + '_' + hashlib.sha256('\0'.join(map(str, parts)).encode()).hexdigest()[:24]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''): h.update(data)
    return h.hexdigest()


def read_json(path, default=None):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default


def atomic_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextlib.contextmanager
def writer(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state/'writer.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def append_jsonl(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        with path.open('rb') as existing:
            existing.seek(-1,2); terminated=existing.read(1)==b'\n'
        if not terminated:
            with path.open('a') as boundary: boundary.write('\n'); boundary.flush(); os.fsync(boundary.fileno())
    with path.open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n'); f.flush(); os.fsync(f.fileno())


def append_jsonl_batch(path, records):
    """Atomically append a validated batch; caller holds the shared writer lock.

    Replacing a complete temporary log ensures a failed write cannot leave half
    a reviewed correction applied. Existing bytes, including history, survive.
    """
    if not records:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = path.read_bytes() if path.exists() else b''
    if previous and not previous.endswith(b'\n'):
        previous += b'\n'
    appended = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records).encode('utf-8')
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(previous)
            stream.write(appended)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def jsonl(path):
    if not path.exists(): return []
    result=[]
    for line in path.read_text().splitlines():
        try: result.append(json.loads(line))
        except json.JSONDecodeError: continue  # Interrupted final log line cannot invalidate earlier events.
    return result


SCHEMA = '''
PRAGMA journal_mode=DELETE;
PRAGMA synchronous=FULL;
CREATE TABLE works(id TEXT PRIMARY KEY,title TEXT,author TEXT,data TEXT);
CREATE TABLE sections(id TEXT PRIMARY KEY,work_id TEXT,title TEXT,ordinal INTEGER,data TEXT);
CREATE INDEX sections_work ON sections(work_id,ordinal);
CREATE TABLE blocks(id TEXT PRIMARY KEY,work_id TEXT,section_id TEXT,ordinal INTEGER,file_id TEXT,text TEXT,data TEXT);
CREATE INDEX blocks_context ON blocks(work_id,ordinal);
CREATE INDEX blocks_section ON blocks(section_id,ordinal);
CREATE TABLE passages(id TEXT PRIMARY KEY,work_id TEXT,section_id TEXT,ordinal INTEGER,first_ordinal INTEGER,last_ordinal INTEGER,text TEXT,heading TEXT,attribution TEXT,boilerplate INTEGER,data TEXT);
CREATE INDEX passages_work ON passages(work_id,ordinal);
CREATE INDEX passages_section ON passages(section_id,ordinal);
CREATE VIRTUAL TABLE passage_fts USING fts5(text,heading,attribution,content='passages',content_rowid='rowid',tokenize='unicode61 remove_diacritics 2');
CREATE TABLE facets(target_id TEXT,work_id TEXT,facet TEXT,value TEXT,origin TEXT,status TEXT,evidence TEXT);
CREATE INDEX facets_lookup ON facets(facet,value,work_id);
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
'''


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose ``with`` block also closes it.

    The standard library's connection context manager only commits or rolls
    back; the file handle stays open until garbage collection, which leaks
    descriptors in the long-running server and raises ResourceWarning on
    Python 3.13+. Every connection here is scoped to one ``with`` block.
    """
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def open_sqlite(database, **kwargs):
    """Open a ClosingConnection; arguments match :func:`sqlite3.connect`."""
    return sqlite3.connect(database, factory=ClosingConnection, **kwargs)


def connect(path):
    db=open_sqlite(f'file:{path}?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    return db
