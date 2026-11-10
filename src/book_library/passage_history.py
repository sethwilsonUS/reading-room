"""Durable source extents for links and corrections predating a resegmentation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .storage import digest, open_sqlite


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def snapshot(lib):
    if not lib._active():
        return
    path = lib.state / 'passage-history.sqlite3'
    with open_sqlite(path) as history, lib._db() as old:
        history.execute('CREATE TABLE IF NOT EXISTS extents(id TEXT PRIMARY KEY,work_id TEXT,data TEXT)')
        known = {r[0] for r in history.execute('SELECT id FROM extents')}
        sections = {r['id']: dict(r) for r in old.execute('SELECT * FROM sections')}
        files = {}
        for r in old.execute('SELECT id,data FROM works'):
            files[r['id']] = {f['id']: f.get('sha256') for f in json.loads(r['data']).get('files', [])}
        rows = old.execute('SELECT p.*,a.id first_block_id,z.id last_block_id FROM passages p JOIN blocks a ON a.work_id=p.work_id AND a.ordinal=p.first_ordinal JOIN blocks z ON z.work_id=p.work_id AND z.ordinal=p.last_ordinal')
        for row in rows:
            if row['id'] in known:
                continue
            passage = {k: row[k] for k in row.keys() if k not in ('first_block_id', 'last_block_id')}
            # The source text already lives in the source/index. Keep only its
            # checksum here, so historical links do not duplicate whole books.
            checksum = _hash(passage.pop('text'))
            record = {'passage': passage, 'section': sections[row['section_id']],
                      'first_block_id': row['first_block_id'], 'last_block_id': row['last_block_id'],
                      'text_sha256': checksum, 'files': {fid: files[row['work_id']].get(fid) for fid in json.loads(row['data'])['file_ids']}}
            history.execute('INSERT INTO extents VALUES(?,?,?)', (row['id'], row['work_id'], json.dumps(record, ensure_ascii=False)))


def resolve(lib, passage_id, db=None, *, verify_bytes=False):
    path = lib.state / 'passage-history.sqlite3'
    if not path.exists():
        return None
    with open_sqlite(f'file:{path}?mode=ro', uri=True) as history:
        row = history.execute('SELECT data FROM extents WHERE id=?', (passage_id,)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    connection = db or lib._db()
    try:
        p = record['passage']
        first = connection.execute('SELECT ordinal FROM blocks WHERE id=? AND work_id=?', (record['first_block_id'], p['work_id'])).fetchone()
        last = connection.execute('SELECT ordinal FROM blocks WHERE id=? AND work_id=?', (record['last_block_id'], p['work_id'])).fetchone()
        if not first or not last or first[0] > last[0]:
            return None
        work_row = connection.execute('SELECT data FROM works WHERE id=?', (p['work_id'],)).fetchone()
        if not work_row:
            return None
        work = json.loads(work_row[0]); files = {f['id']: f.get('sha256') for f in work.get('files', [])}
        if any(files.get(fid) != sha for fid, sha in record['files'].items()):
            return None
        if not lib._current_files({'file_ids': list(record['files'])}, work):
            return None
        if verify_bytes:
            current_files = {f['id']: f for f in work.get('files', [])}
            try:
                if any(digest(Path(current_files[fid]['path'])) != sha for fid, sha in record['files'].items()):
                    return None
            except OSError:
                return None
        rows = connection.execute('SELECT data FROM blocks WHERE work_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal', (p['work_id'], first[0], last[0])).fetchall()
        blocks = [json.loads(r[0]) for r in rows]
        text = '\n\n'.join(b['text'] for b in blocks if b['text'].strip())
        if _hash(text) != record['text_sha256']:
            return None
        p.update(text=text, first_ordinal=first[0], last_ordinal=last[0])
        # A compatibility view retains the exact old extent. It does not turn a
        # historical excerpt correction into a correction of a new contribution.
        info = json.loads(p['data'])
        info.setdefault('limitations', []).append('This saved excerpt retains its original source extent after the book structure was updated.')
        info['historical_extent'] = True
        p['data'] = json.dumps(info, ensure_ascii=False)
        return {**record, 'blocks': blocks}
    finally:
        if db is None:
            connection.close()


class HistoricalLibrary:
    """Read-only main index with one validated historical excerpt in TEMP views."""
    def __init__(self, library, record):
        self.library = library; self.record = record

    def __getattr__(self, name):
        return getattr(self.library, name)

    def _db(self):
        db = self.library._db()
        p = {**self.record['passage'], 'ordinal': -1}; s = self.record['section']
        db.execute('CREATE TEMP TABLE historical_passage AS SELECT * FROM main.passages WHERE 0')
        columns = list(p)
        db.execute('INSERT INTO historical_passage ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', [p[k] for k in columns])
        db.execute('CREATE TEMP VIEW passages AS SELECT * FROM main.passages UNION ALL SELECT * FROM historical_passage')
        db.execute('CREATE TEMP TABLE historical_section AS SELECT * FROM main.sections WHERE 0')
        if not db.execute('SELECT 1 FROM main.sections WHERE id=?', (s['id'],)).fetchone():
            columns = list(s)
            db.execute('INSERT INTO historical_section ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', [s[k] for k in columns])
        db.execute('CREATE TEMP VIEW sections AS SELECT * FROM main.sections UNION ALL SELECT * FROM historical_section')
        return db


def historical_read(lib, passage_id, context=3, offset=0, max_chars=20000):
    record = resolve(lib, passage_id, verify_bytes=True)
    if record is None:
        raise ValueError('Saved passage is unavailable in this source version; its history is preserved and requires review.')
    from .service import Library
    result = Library.read(HistoricalLibrary(lib, record), passage_id, context=0, offset=offset, max_chars=max_chars)
    # A snapshot may predate a later correction. Resolve durable edits against
    # this exact old target, without assigning them to replacement passages.
    from .passage_curation import passage_credit, credit_limitations
    from .passage_segments import segments_for_passage, segment_summary, segment_conflict
    _, edits = lib._effective(lib._catalog())
    credit = passage_credit(passage_id, edits)
    passage = result['passage']
    passage['limitations'] = credit_limitations(passage['limitations'], credit)
    if credit:
        passage.update(attribution=credit['attribution'], attribution_scope='passage')
        for block in result['blocks']:
            block.update(attribution=credit['attribution'], attribution_scope='passage', passage_id=passage_id)
    if type_event := edits.get((passage_id, 'text_type')):
        passage.update(text_type=type_event['value'], text_type_status=type_event.get('status', 'provisional'))
        for block in result['blocks']:
            block['text_type'] = type_event['value']
    if note_event := edits.get((passage_id, 'notes')):
        passage['notes'] = note_event['value']
    segments = segments_for_passage(passage_id, edits, record['blocks'])
    if segments:
        passage.update(segment_summary(segments), attribution_scope='segments')
        source_credits = {block['id']: segment for segment in segments for block in segment['blocks']}
        for block in result['blocks']:
            if segment := source_credits.get(block['id']):
                block.update(attribution=segment['attribution'], attribution_scope='segment',
                             text_type=segment['text_type'], segment_id=segment['segment_id'], passage_id=passage_id)
    if conflict := segment_conflict(passage_id, edits):
        passage['limitations'] = list(dict.fromkeys([*passage['limitations'], conflict]))
    return result


def historical_cite(lib, passage_id, locator=None, segment_id=None):
    record = resolve(lib, passage_id, verify_bytes=True)
    if record is None:
        raise ValueError('Saved citation is unavailable in this source version; its history is preserved and requires review.')
    from .citations import cite_passage
    return cite_passage(HistoricalLibrary(lib, record), passage_id, locator=locator, segment_id=segment_id)
