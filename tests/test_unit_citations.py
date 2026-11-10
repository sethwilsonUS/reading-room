"""Exact quotations preserve scope, Unicode offsets, and source identity."""
import json
import sqlite3

import pytest

from book_library.unit_citations import (create_selection, selection, cite_selection,
                                         cite_unit, record_target_usage)
from book_library.storage import jsonl, open_sqlite
from test_service import docx, seed
from test_citations import bib, page, verified_book
from test_scripture_citations import segmented_source


def install_unit(lib, work_id, section_ids, unit_id='unit_test'):
    """Exercise the citation contract independently of normalization heuristics."""
    with open_sqlite(lib.state / 'generations' / lib._active()['database']) as db:
        db.row_factory = sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS reading_units
            (id TEXT PRIMARY KEY,work_id TEXT,node_id TEXT,title TEXT,ordinal INTEGER,data TEXT);
            CREATE TABLE IF NOT EXISTS unit_blocks
            (unit_id TEXT,block_id TEXT,ordinal INTEGER,role TEXT,PRIMARY KEY(unit_id,block_id));''')
        root = json.loads(db.execute('SELECT data FROM sections WHERE id=?', (section_ids[0],)).fetchone()[0])
        data = {'root_section_id': section_ids[0], 'section_ids': section_ids,
                'attribution': root['attribution'], 'kind': 'contribution', 'structure_status': 'supported'}
        db.execute('INSERT OR REPLACE INTO reading_units VALUES(?,?,?,?,?,?)',
                   (unit_id, work_id, section_ids[0], root['title'], 0, json.dumps(data)))
        db.execute('DELETE FROM unit_blocks WHERE unit_id=?', (unit_id,))
        placeholders = ','.join('?' for _ in section_ids)
        rows = db.execute(f'SELECT id,ordinal,data FROM blocks WHERE section_id IN ({placeholders}) ORDER BY ordinal', section_ids).fetchall()
        for row in rows:
            block = json.loads(row['data'])
            db.execute('INSERT INTO unit_blocks VALUES(?,?,?,?)',
                       (unit_id, row['id'], row['ordinal'], 'note' if block['kind'] == 'note' else 'body'))
    return unit_id, [block for row in rows if (block := json.loads(row['data']))['text'].strip()
                     and block['kind'] != 'pagebreak' and not block.get('is_boilerplate')]


def fixture(tmp_path):
    path = docx(tmp_path / 'books' / 'Book' / 'book.docx', [
        ('Heading1', 'A complete essay'),
        'Café 🌿 e\u0301 — first paragraph.',
        'Second line\nThird line.',
        'Repeated words. Repeated words.',
        ('Heading1', 'A neighboring essay'),
        'This neighboring text is not in the reading unit.',
    ])
    lib = seed(tmp_path / 'books', [{'title': 'The Shared Classroom: Learning Together', 'author': 'Jane Doe', 'paths': [path]}])
    hit = lib.search('first paragraph', engine='exact')['direct'][0]
    unit_id, blocks = install_unit(lib, hit['work_id'], [hit['section_id']])
    return lib, hit, unit_id, blocks, path


def source_range(block, start=0, end=None):
    return {'block_id': block['id'], 'start': start, 'end': len(block['text']) if end is None else end}


def test_unicode_reversed_selection_retains_exact_lines_and_no_implicit_use(tmp_path):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    first, second = blocks[1:3]
    ranges = [source_range(first, 5), source_range(second, end=11)]
    saved = create_selection(lib, unit, list(reversed(ranges)))
    assert saved['source_ranges'] == ranges
    assert saved['quotation'] == first['text'][5:] + '\n\n' + second['text'][:11]
    assert saved['quotation'].startswith('🌿 e\u0301')
    assert saved['offset_unit'] == 'unicode_code_points'
    assert not lib.usage()
    assert selection(lib, saved['id'])['available']
    again = create_selection(lib, unit, ranges)
    assert again['id'] == saved['id']
    assert len(jsonl(lib.state / 'selections.jsonl')) == 1


@pytest.mark.parametrize('bad', ['skip', 'interior_partial', 'bool', 'outside', 'duplicate', 'negative', 'foreign', 'invalid_id'])
def test_selection_rejects_gaps_invalid_offsets_and_other_unit_text(tmp_path, bad):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    ranges = [source_range(b) for b in blocks[1:]]
    if bad == 'skip':
        ranges.pop(1)
    elif bad == 'interior_partial':
        ranges[1]['start'] = 1
    elif bad == 'bool':
        ranges[0]['start'] = False
    elif bad == 'outside':
        ranges[-1]['end'] = 100000
    elif bad == 'duplicate':
        ranges.append(ranges[-1])
    elif bad == 'negative':
        ranges[0]['start'] = -1
    elif bad == 'invalid_id':
        ranges[0]['block_id'] = []
    elif bad == 'foreign':
        neighbor = lib.search('neighboring text', engine='exact')['direct'][0]
        ranges = [source_range(lib.read(neighbor['id'], context=0)['blocks'][-1])]
    with pytest.raises(ValueError):
        create_selection(lib, unit, ranges)
    assert not (lib.state / 'selections.jsonl').exists()


def test_repeated_quotation_uses_source_coordinates_not_first_string_match(tmp_path):
    lib, _, unit, blocks, _ = fixture(tmp_path)
    block = blocks[-1]
    first = create_selection(lib, unit, [source_range(block, 0, 15)])
    second = create_selection(lib, unit, [source_range(block, 16, 31)])
    assert first['quotation'] == second['quotation'] == 'Repeated words.'
    assert first['id'] != second['id']


def test_changed_extraction_cannot_replace_existing_selection_with_same_coordinates(tmp_path):
    lib, _, unit, blocks, _ = fixture(tmp_path)
    chosen = blocks[1]
    ranges = [source_range(chosen, 0, 4)]
    saved = create_selection(lib, unit, ranges)
    original = (lib.state / 'selections.jsonl').read_bytes()
    changed = {**chosen, 'text': chosen['text'].replace('Café', 'Cafe')}
    with open_sqlite(lib.state / 'generations' / lib._active()['database']) as db:
        db.execute('UPDATE blocks SET text=?,data=? WHERE id=?', (changed['text'], json.dumps(changed), chosen['id']))
    assert not selection(lib, saved['id'])['available']
    with pytest.raises(ValueError, match='anchors changed'):
        create_selection(lib, unit, ranges)
    assert (lib.state / 'selections.jsonl').read_bytes() == original


def test_saved_selection_survives_rebuild_and_rename_but_does_not_reattach_changed_source(tmp_path):
    lib, hit, unit, blocks, source = fixture(tmp_path)
    saved = create_selection(lib, unit, [source_range(blocks[1], 0, 4)])
    before = (lib.state / 'selections.jsonl').read_bytes()
    renamed = source.with_name('Renamed.docx')
    source.rename(renamed)
    lib.sync(rebuild=True, semantic=False)
    install_unit(lib, hit['work_id'], [hit['section_id']])
    assert selection(lib, saved['id'])['available']
    assert (lib.state / 'selections.jsonl').read_bytes() == before
    docx(renamed, [('Heading1', 'A complete essay'), 'Café replacement text.'])
    current = selection(lib, saved['id'])
    assert not current['available'] and current['quotation'] == 'Café'
    with pytest.raises(ValueError, match='changed'):
        cite_selection(lib, saved['id'])
    assert (lib.state / 'selections.jsonl').read_bytes() == before


def test_unit_citation_does_not_inherit_first_passage_correction(tmp_path):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    verified_book(lib, hit, authors=[], editors=[{'given': 'Volume', 'family': 'Editor'}])
    bib(lib, hit['section_id'], {'type': 'chapter', 'title': 'The whole essay',
                                'authors': [{'given': 'Whole', 'family': 'Author'}]})
    bib(lib, hit['id'], {'type': 'poem', 'title': 'Narrower passage credit',
                        'authors': [{'given': 'Passage', 'family': 'Writer'}], 'locator': page('999')})
    result = cite_unit(lib, unit, page('10–12'))
    assert result['full_note'].startswith('Whole Author, “The whole essay,”')
    assert 'Passage Writer' not in result['full_note'] and '999' not in result['full_note']
    assert result['target_scope'] == 'unit' and result['target_id'] == unit
    assert 'passage_id' not in result['source_identity']
    assert result['metadata_scope'] == 'section'
    exact = create_selection(lib, unit, [source_range(blocks[1], 0, 4)])
    cited = cite_selection(lib, exact['id'])
    assert cited['full_note'].startswith('Passage Writer, “Narrower passage credit,”')
    assert cited['metadata_scope'] == 'passage' and cited['target_scope'] == 'selection'


def test_unknown_unit_contributor_does_not_borrow_a_passage_author(tmp_path):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    verified_book(lib, hit, authors=[], editors=[{'given': 'Volume', 'family': 'Editor'}])
    # Source's catalog calls it an authored book; make its edited nature explicit.
    lib.annotate(hit['work_id'], {'field': 'contributors', 'value': [{'name': 'Volume Editor', 'role': 'editor'}],
                                 'status': 'confirmed', 'origin': 'user', 'evidence': 'Edited anthology fixture.'})
    lib.sync(semantic=False)
    install_unit(lib, hit['work_id'], [hit['section_id']])
    bib(lib, hit['id'], {'type': 'chapter', 'title': 'A narrow excerpt', 'authors': [{'literal': 'Only This Excerpt'}]})
    result = cite_unit(lib, unit, page())
    assert 'Only This Excerpt' not in result['full_note']
    assert result['citation_extent'] == 'containing_work'
    assert 'unit.bibliography' in result['review_fields']


def test_selected_mixed_source_keeps_scripture_and_commentary_credits_separate(tmp_path):
    lib, hit, segments = segmented_source(tmp_path)
    unit, blocks = install_unit(lib, hit['work_id'], [hit['section_id']])
    saved = create_selection(lib, unit, [source_range(blocks[1], 8), source_range(blocks[2], end=25)])
    result = cite_selection(lib, saved['id'])
    assert result['metadata_scope'] == 'mixed' and not result['full_note']
    assert len(result['component_citations']) == 2
    assert 'segment_id' not in result and 'segment_id' not in result['source_identity']
    assert 'passage_id' not in result['source_identity']
    scripture, commentary = result['component_citations']
    assert scripture['metadata']['type'] == 'scripture'
    assert scripture['metadata']['authors'] == []
    assert commentary['metadata']['authors'] == [{'given': 'Aaron M.', 'family': 'Gale'}]
    assert commentary['excerpt'] == blocks[2]['text'][:25]
    assert commentary['source_ranges'] == [source_range(blocks[2], end=25)]


def test_explicit_usage_preserves_unit_or_selection_scope_and_ignores_client_identity(tmp_path):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    saved = create_selection(lib, unit, [source_range(blocks[1], 0, 4)])
    event = {'kind': 'used', 'purpose': 'class-prep', 'project': 'EfM seminar', 'notes': 'Quoted for class.',
             'work_id': 'wrong', 'attribution': 'Invented author', 'passage_id': 'wrong'}
    chosen = record_target_usage(lib, 'selection', saved['id'], event)
    assert chosen['work_id'] == hit['work_id']
    assert chosen['quotation'] == 'Café' and chosen['selection_id'] == saved['id']
    assert chosen['project'] == 'EfM seminar'
    assert chosen['target_scope'] == 'selection' and 'passage_id' not in chosen
    complete = record_target_usage(lib, 'unit', unit, {'kind': 'selected', 'purpose': 'research'})
    assert complete['target_scope'] == 'unit' and 'passage_id' not in complete
    assert complete['source_identity']['source_block_ids'] == [b['id'] for b in blocks]
    assert len(lib.usage()) == 2


def test_epub_unit_locator_covers_all_pages_but_selection_is_narrow(tmp_path):
    from test_extract import ExtractTests
    from book_library.service import Library
    root = tmp_path / 'books'; root.mkdir()
    package = ExtractTests(); package.root = root
    package.epub({'chapter.xhtml': '<h1>Water and Place</h1>'
                  '<span epub:type="pagebreak" id="p42" title="42"/><p>Copper birds gather by water.</p>'
                  '<span epub:type="pagebreak" id="p43" title="43"/><p>The community assembles.</p>'
                  '<span epub:type="pagebreak" id="p44" title="44"/>'}, ['chapter.xhtml'])
    lib = Library(root); lib.sync(semantic=False)
    hit = lib.search('copper birds', engine='exact')['direct'][0]
    verified_book(lib, hit)
    unit, blocks = install_unit(lib, hit['work_id'], [hit['section_id']])
    complete = cite_unit(lib, unit)
    assert complete['locator']['value'] == '42–43'
    block = next(b for b in blocks if b['text'] == 'The community assembles.')
    saved = create_selection(lib, unit, [source_range(block, 4, 13)])
    narrow = cite_selection(lib, saved['id'])
    assert narrow['locator']['value'] == '43'
    assert narrow['quotation'] == 'community'
    assert narrow['locator']['origin'] == 'source_page_markers'


def test_owned_note_is_selected_by_membership_not_source_envelope(tmp_path):
    lib, hit, unit, blocks, _ = fixture(tmp_path)
    neighbor = lib.search('neighboring text', engine='exact')['direct'][0]
    foreign = lib.read(neighbor['id'], context=0)['blocks'][-1]
    # Place an owned note physically after a neighboring contribution. Its
    # membership is explicit; the intervening contribution must remain absent.
    note = {**blocks[-1], 'id': 'block_owned_note', 'text': 'An owned endnote.',
            'kind': 'note', 'global_ordinal': foreign['global_ordinal'] + 1}
    with open_sqlite(lib.state / 'generations' / lib._active()['database']) as db:
        db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?)',
                   (note['id'], hit['work_id'], hit['section_id'], note['global_ordinal'], note['file_id'], note['text'], json.dumps(note)))
        db.execute('INSERT INTO unit_blocks VALUES(?,?,?,?)', (unit, note['id'], note['global_ordinal'], 'note'))
    saved = create_selection(lib, unit, [source_range(blocks[-1], 16), source_range(note, end=8)])
    assert saved['quotation'] == 'Repeated words.\n\nAn owned'
    assert foreign['id'] not in [r['block_id'] for r in saved['source_identity']['source_ranges']]
    assert 'neighboring' not in saved['quotation']
    complete = cite_unit(lib, unit)
    assert complete['source_identity']['source_block_ids'] == [b['id'] for b in blocks] + [note['id']]
    assert foreign['id'] not in complete['source_identity']['source_block_ids']


def test_whole_unit_citation_includes_verified_note_pages_without_intervening_essay(tmp_path):
    from test_extract import ExtractTests
    from book_library.service import Library
    root = tmp_path / 'books'; root.mkdir()
    package = ExtractTests(); package.root = root
    package.epub({'chapter.xhtml': '<h1>Water and Place</h1>'
                  '<span epub:type="pagebreak" id="p42" title="42"/><p>Copper birds gather by water.</p>'
                  '<span epub:type="pagebreak" id="p43" title="43"/><p>The community assembles.</p>'
                  '<h1>Other essay</h1><span epub:type="pagebreak" id="p44" title="44"/><p>Unrelated prose.</p>'
                  '<h1>Source notes</h1><span epub:type="pagebreak" id="p100" title="100"/><p>Verified owned note.</p>'}, ['chapter.xhtml'])
    lib = Library(root); lib.sync(semantic=False)
    hit = lib.search('copper birds', engine='exact')['direct'][0]
    verified_book(lib, hit)
    unit, blocks = install_unit(lib, hit['work_id'], [hit['section_id']])
    with open_sqlite(lib.state / 'generations' / lib._active()['database']) as db:
        note = db.execute('SELECT id,ordinal FROM blocks WHERE text=?', ('Verified owned note.',)).fetchone()
        db.execute('INSERT INTO unit_blocks VALUES(?,?,?,?)', (unit, note[0], note[1], 'note'))
    citation = cite_unit(lib, unit)
    assert citation['locator']['source_labels'] == ['42', '43', '100']
    assert citation['locator']['value'] == '42–43, 100'
    assert citation['source_identity']['source_block_ids'] == [b['id'] for b in blocks] + [note[0]]


def test_exact_selection_respects_user_correction_that_withholds_automatic_components(tmp_path):
    lib, hit, _ = segmented_source(tmp_path)
    unit, blocks = install_unit(lib, hit['work_id'], [hit['section_id']])
    lib.annotate(hit['id'], {'field': 'attribution', 'value': 'User checked credit', 'origin': 'user',
                            'status': 'confirmed', 'evidence': 'User reviewed this passage.'})
    saved = create_selection(lib, unit, [source_range(blocks[1])])
    result = cite_selection(lib, saved['id'])
    assert result['attribution'] == 'User checked credit'
    assert 'component.user_correction' in result['review_fields']
    assert not result.get('component_citations')
