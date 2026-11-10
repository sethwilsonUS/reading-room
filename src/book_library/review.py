"""Source-aware review projections and durable drafts, separate from curation.

A proposal is never an annotation. Explicitly applying a reviewed value creates
ordinary field-scoped curation; verification is an independent property.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

from .citations import _annotation, _display, _name_candidates, validate_bibliography
from .passage_segments import TEXT_TYPES, validate_text_type
from .review_education import TEXT_TYPE_GUIDE, field_education
from .storage import atomic_json, append_jsonl_batch, now, read_json, stable, uid, writer

FACETS = ('subjects', 'forms', 'themes', 'biblical_references', 'people', 'places',
          'disciplines', 'motifs', 'questions', 'works', 'artworks', 'periods',
          'cultures', 'traditions', 'media', 'approaches')
FACET_HELP = {
    'subjects': ('The main topics this material studies or explains.', 'Advent, Christian worship'),
    'forms': ('The kind of writing or resource, rather than its subject.', 'commentary, prayer, poem, sermon'),
    'themes': ('The ideas or concerns developed in the material.', 'hope, justice, incarnation'),
    'biblical_references': ('Biblical passages actually discussed or interpreted, not every incidental quotation.', 'Isaiah 9:2–7, Luke 1:26–38'),
    'people': ('People discussed in the text. Use citation credits to record who wrote it.', 'Mary, John the Baptist'),
    'places': ('Geographical places or settings discussed in the text.', 'Bethlehem, Jerusalem'),
    'disciplines': ('The fields of study that inform this material.', 'biblical studies, history, theology'),
    'motifs': ('Recurring images, symbols, or patterns used by the text.', 'light and darkness, wilderness, journey'),
    'questions': ('Research questions this material can help explore.', 'How does Advent shape Christian hope?'),
    'works': ('Other named books, compositions, or works discussed here. This is not this book’s own title.', 'The Divine Comedy, Messiah'),
    'artworks': ('Individual visual artworks discussed or reproduced in the material.', 'The Annunciation'),
    'periods': ('Historical periods covered by the material, not this copy’s publication year.', 'Second Temple period, late antiquity'),
    'cultures': ('Cultural settings or communities discussed in the material.', 'ancient Judean, Greco-Roman'),
    'traditions': ('Religious or intellectual traditions engaged by the material.', 'Anglican, Reformed, Jewish'),
    'media': ('The artistic or communicative media discussed or used.', 'painting, music, film'),
    'approaches': ('Methods of interpretation or analysis used in the material.', 'historical criticism, literary analysis, pastoral reflection'),
}
GROUPS = ('Titles and credits', 'Citation and publication details', 'Research classifications')
BIB_TEXT = {
    'title': ('Citation title', 'The title used in citations; this is separate from the browsing title.'),
    'short_title': ('Short citation title', 'An optional shortened title for repeated citations.'),
    'publisher': ('Publisher', 'The publisher of the edition you actually consulted.'),
    'year': ('Publication year', 'The publication year of this edition, not its download or export date.'),
    'edition': ('Edition statement', 'The edition statement as printed in this copy.'),
    'volume': ('Volume number', 'The volume number within a multivolume work.'),
    'volume_title': ('Volume title', 'The title of this volume, where distinct from the series title.'),
    'url': ('Source URL', 'A stable publisher or institutional URL identifying this edition.'),
    'doi': ('DOI', 'The DOI assigned to this work, if one is recorded.'),
    'format': ('Format', 'The format of the copy consulted, such as EPUB.'),
    'bible_translation': ('Bible translation', 'The translation of biblical text, distinct from its commentary author.'),
    'bible_translation_short': ('Translation abbreviation', 'The supported abbreviation of the Bible translation.'),
}


class ReviewConflict(ValueError):
    """A draft must be reviewed again before it can replace current information."""


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _path(lib, target_id):
    if not isinstance(target_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', target_id):
        raise ValueError('Choose a valid review target.')
    return lib.state / 'reviews' / (target_id + '.json')


def _source_current(files):
    signatures = []
    current = True
    for file in files:
        try:
            stat = Path(file['path']).stat()
            actual = [stat.st_size, stat.st_mtime_ns]
        except OSError:
            actual = None
        expected = [file.get('size'), file.get('mtime_ns')]
        current = current and actual == expected and file.get('state') == 'ready' and file.get('available', True)
        signatures.append([file['id'], file.get('sha256'), expected, actual])
    return bool(files) and current, signatures


def _field_event(edits, target_id, key, fallback=None):
    family, _, child = key.partition('.')
    event = edits.get((target_id, family), {})
    if child:
        return event.get('field_events', {}).get(child, fallback or {}) if child in event.get('value', {}) else fallback or {}
    return event or fallback or {}


def _show(value, kind='text'):
    if value is None or value == '':
        return 'Not recorded'
    if kind == 'names':
        return '; '.join(_display(person) for person in value) if value else 'No named contributor recorded'
    if isinstance(value, list):
        return ', '.join(value) if value else 'No values recorded'
    return str(value)


def _field(key, label, value, *, group=GROUPS[0], kind='text', help='', example='', event=None,
           inherited=False, editable=True, options=None, raw_value=None, raw_supplied=False,
           inherited_value=None, related_fields=None, edit_url=None):
    event = event or {}
    empty = value is None or value == '' or value == []
    status = event.get('status', 'unknown' if empty else 'candidate')
    return {'key': key, 'label': label, 'group': group, 'kind': kind, 'value': copy.deepcopy(value),
            'raw_value': copy.deepcopy(raw_value if raw_supplied else value),
            'display_value': _show(value, kind), 'help': help, 'example': example,
            'status': status, 'origin': event.get('origin', 'not_recorded' if empty else 'source_or_catalog'),
            'evidence': event.get('evidence', ''), 'annotation_id': event.get('id'),
            'provenance_target_id': event.get('target_id'), 'inherited': bool(inherited),
            'inherited_value': copy.deepcopy(inherited_value), 'editable': editable,
            'options': options or [], 'related_fields': related_fields or [], 'edit_url': edit_url,
            **field_education(key, label, help, example)}


def _publication_evidence(db, work_id):
    """Find bounded publication candidates beyond export notices and covers.

    These are source leads, not verified publication claims. Nearby blocks keep
    split credit labels and names together; book context never becomes a section
    byline merely because it is supplied to the same review.
    """
    candidates = db.execute("""SELECT id,ordinal,file_id,text,data FROM blocks WHERE work_id=? AND
        (text LIKE '%ISBN%' OR text LIKE '%©%' OR text LIKE '%Copyright%' OR
         text LIKE '%edition%' OR text LIKE '%published by%' OR text LIKE '%edited by%' OR
         text LIKE '%compiled by%' OR text LIKE '%translated by%' OR
         text LIKE '%Cataloging-in-Publication%') ORDER BY ordinal LIMIT 96""", (work_id,)).fetchall()
    candidates = [r for r in candidates if not json.loads(r['data']).get('is_boilerplate')]
    def rank(row):
        text = row['text'].casefold()
        return (0 if re.search(r'\bisbn(?:-1[03])?\s*[: ]*\d', text) else
                1 if re.search(r'\b(?:edited|compiled|translated|published) by\b|©|cataloging-in-publication', text) else 2,
                row['ordinal'])
    selected = {}
    for row in sorted(candidates, key=rank)[:12]:
        for neighbor in db.execute('SELECT id,ordinal,file_id,text,data FROM blocks WHERE work_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',
                                   (work_id, row['ordinal'] - 2, row['ordinal'] + 2)):
            data = json.loads(neighbor['data'])
            if neighbor['text'].strip() and data.get('kind') != 'pagebreak' and not data.get('is_boilerplate'):
                selected.setdefault(neighbor['id'], neighbor)
    # A title can be split across several paragraphs without an explicit label.
    for row in db.execute('SELECT id,ordinal,file_id,text,data FROM blocks WHERE work_id=? ORDER BY ordinal LIMIT 160', (work_id,)):
        data = json.loads(row['data'])
        if row['text'].strip() and data.get('kind') != 'pagebreak' and not data.get('is_boilerplate'):
            selected.setdefault(row['id'], row)
            if sum(1 for value in selected.values() if value['ordinal'] <= row['ordinal']) >= 8:
                break
    # ISBN and credit anchors were inserted first, so the bound cannot displace
    # them with a long unrelated opening chapter.
    return sorted(list(selected.values())[:48], key=lambda row: row['ordinal'])


def review_context(lib, target_id):
    """Build a bounded, read-only view using current curation, not stale UI data."""
    _path(lib, target_id)
    catalog = lib._catalog()
    works, edits = lib._effective(catalog)
    scope = 'work' if target_id in works else None
    section_id = None
    with lib._db() as db:
        if scope == 'work':
            work_id = target_id
            target_data = copy.deepcopy(works[target_id])
            selector, params = 'work_id=?', (work_id,)
            passage_rows = db.execute('SELECT id,section_id,first_ordinal,last_ordinal FROM passages WHERE work_id=? AND boilerplate=0 ORDER BY ordinal', (work_id,)).fetchall()
        else:
            row = db.execute('SELECT * FROM sections WHERE id=?', (target_id,)).fetchone()
            if row:
                scope, work_id, section_id = 'section', row['work_id'], target_id
                target_data = json.loads(row['data'])
                selector, params = 'section_id=?', (target_id,)
                passage_rows = db.execute('SELECT id,section_id,first_ordinal,last_ordinal FROM passages WHERE section_id=? AND boilerplate=0 ORDER BY ordinal', (target_id,)).fetchall()
            else:
                row = db.execute('SELECT * FROM passages WHERE id=?', (target_id,)).fetchone()
                if not row:
                    raise ValueError('That book, section, or passage is no longer available. Its saved review remains in local history.')
                scope, work_id, section_id = 'passage', row['work_id'], row['section_id']
                target_data = json.loads(db.execute('SELECT data FROM sections WHERE id=?', (section_id,)).fetchone()[0])
                target_data.update(attribution=row['attribution'], **{k: v for k, v in json.loads(row['data']).items() if k in ('text_type', 'notes')})
                selector, params = 'work_id=? AND ordinal BETWEEN ? AND ?', (work_id, row['first_ordinal'], row['last_ordinal'])
                passage_rows = [row]
        if work_id not in works:
            raise ValueError('The containing book is unavailable.')
        work = works[work_id]
        source_section = json.loads(db.execute('SELECT data FROM sections WHERE id=?', (section_id,)).fetchone()[0]) if section_id else {}
        # SQL is assembled only from the fixed selectors above, never user text.
        rows = db.execute('SELECT id,ordinal,file_id,text,data FROM blocks WHERE ' + selector + ' ORDER BY ordinal', params).fetchall()
        content_rows = [r for r in rows if r['text'].strip() and json.loads(r['data']).get('kind') != 'pagebreak'
                        and not json.loads(r['data']).get('is_boilerplate')]
        chosen = content_rows if len(content_rows) <= 64 else content_rows[:40] + content_rows[-24:]
        # Source-supported bylines and publication matter can be outside a small target.
        supporting = _publication_evidence(db, work_id)
        publication_candidates = {row['id'] for row in supporting}
        all_rows = list(chosen)
        seen = {r['id'] for r in all_rows}
        for row in supporting:
            if row['id'] not in seen:
                all_rows.append(row); seen.add(row['id'])
        membership = [[r['id'], r['file_id'], hashlib.sha256(r['text'].encode()).hexdigest()] for r in rows]
        units = {}
        for row in all_rows:
            unit = db.execute('SELECT unit_id FROM unit_blocks WHERE block_id=? ORDER BY unit_id LIMIT 1', (row['id'],)).fetchone()
            if unit:
                units[row['id']] = unit[0]
    files = [f for f in catalog['files'].values() if f['work_id'] == work_id]
    source_current, source_signature = _source_current(files)
    file_map = {f['id']: f for f in files}
    target_blocks = {r['id'] for r in rows}
    evidence = []
    remaining = 60000
    target_truncated = False
    for row in all_rows:
        data = json.loads(row['data'])
        if not row['text'].strip() or data.get('kind') == 'pagebreak':
            continue
        limit = min(4000, remaining)
        if limit <= 0:
            break
        text = row['text'][:limit]
        target_truncated = target_truncated or (row['id'] in target_blocks and text != row['text'])
        remaining -= len(text)
        link = '/units/' + quote(units[row['id']], safe='') + '?anchor=' + quote(row['id'], safe='') + '#' + quote(row['id'], safe='') if row['id'] in units else '/read/' + quote(passage_rows[0]['id'], safe='') if passage_rows else '/books/' + quote(work_id, safe='')
        evidence.append({'id': row['id'], 'text': text, 'locator': data.get('locator', {}),
                         'content_hash': hashlib.sha256(row['text'].encode()).hexdigest(),
                         'location': data.get('location', Path(file_map.get(row['file_id'], {}).get('path', '')).name),
                         'read_url': link, 'file_id': row['file_id'], 'kind': data.get('kind', 'paragraph'),
                         'ordinal': row['ordinal'],
                         'scope': 'target' if row['id'] in target_blocks else 'book',
                         'role': 'target_text' if scope != 'work' and row['id'] in target_blocks else 'publication_candidate' if row['id'] in publication_candidates else 'target_text' if row['id'] in target_blocks else 'book_context',
                         'in_target': row['id'] in target_blocks, 'fragment': text != row['text']})
    if not source_current:
        # Old text is not presented as fresh evidence after the original changes.
        evidence = []
    ordered_target = sorted((e for e in evidence if e['in_target']), key=lambda e: e['ordinal'])
    first = ordered_target[0] if ordered_target else {}
    last = ordered_target[-1] if ordered_target else {}
    title_event = edits.get((target_id, 'title'))
    title = title_event['value'] if title_event else target_data.get('title', work.get('title', 'Untitled'))
    read_url = first.get('read_url') or ('/read/' + passage_rows[0]['id'] if passage_rows else '/books/' + work_id)
    location = first.get('location', '')
    if last.get('location') and last['location'] != location:
        location += ' — ' + last['location']
    preview_parts = [e['text'] for e in ordered_target if e.get('kind') not in ('heading', 'pagebreak')]
    preview = ' '.join(preview_parts[:2])[:700] or first.get('text', '')[:700]
    first_page, last_page = first.get('locator', {}).get('page'), last.get('locator', {}).get('page')
    location_label = ('Source page markers in preview: ' + str(first_page) + ('–' + str(last_page) if last_page and last_page != first_page else '')) if first_page else 'Source location: ' + str(first.get('locator', {}).get('member') or first.get('location') or 'not recorded')
    fields = []
    if scope != 'passage':
        fields.append(_field('title', 'Browsing title', title, event=title_event,
                            help='The title shown in the shelves, contents, and reading views. Citation titles are recorded separately.',
                            example='Introduction', related_fields=['bibliography.title']))
    if scope == 'work':
        fields.append(_field('author', 'Browsing creator credit', work.get('author'), event=edits.get((target_id, 'author')),
                            help='The creator label used while browsing. Keep citation authors, editors, and translators in their own roles.',
                            example='Edited by Jane Doe', related_fields=['bibliography.authors', 'bibliography.editors']))
    else:
        attr_event = edits.get((target_id, 'attribution'))
        if scope == 'passage' and not attr_event:
            from .passage_curation import passage_credit
            credit = passage_credit(target_id, edits)
            if credit:
                target_data['attribution'] = credit['attribution']; attr_event = credit['event']
        if not attr_event:
            attr_event = edits.get((section_id, 'attribution'))
        attr = attr_event.get('value') if attr_event and isinstance(attr_event.get('value'), str) else target_data.get('attribution')
        if not attr_event and source_section.get('attribution_evidence'):
            attr_event = {'origin': 'source_byline', 'status': 'confirmed', 'evidence': source_section['attribution_evidence']}
        fields.append(_field('attribution', 'Displayed contributor credit', attr, event=attr_event,
                            inherited=scope == 'passage' and (attr_event or {}).get('target_id') != target_id,
                            help='A display credit for this entire target. It does not verify citation authors. Mixed contributions need narrower credits.',
                            example='Jane Doe', related_fields=['bibliography.authors']))
        type_event = edits.get((target_id, 'text_type')) or edits.get((section_id, 'text_type'))
        text_type = type_event['value'] if type_event else target_data.get('text_type', 'unknown')
        fields.append(_field('text_type', 'Kind of writing', text_type, kind='select', event=type_event,
                            inherited=scope == 'passage' and (type_event or {}).get('target_id') != target_id,
                            help='A category that helps you find similar material later. It does not identify the author or choose a citation format.',
                            options=[{'value': t, **TEXT_TYPE_GUIDE[t]} for t in TEXT_TYPES]))
        fields[-1]['display_value'] = TEXT_TYPE_GUIDE.get(text_type, TEXT_TYPE_GUIDE['unknown'])['label']
    note_event = edits.get((target_id, 'notes'))
    fields.append(_field('notes', 'Editorial notes', note_event['value'] if note_event else target_data.get('notes'), event=note_event,
                        help='Your editorial explanation or unresolved concern. Notes do not establish citation authorship.', example='The chapter contains separate signed essays.'))
    base_event, bibliography = _annotation(edits, section_id or target_id, source_section.get('source_bibliography'))
    if scope == 'passage':
        base_event, bibliography = _annotation(edits, target_id, base_event)
    book_event, book_bib = _annotation(edits, work_id)
    work_meta = work.get('metadata', {})
    candidates = {k: work_meta.get(v) for k, v in {'publisher': 'publisher', 'year': 'publication_year', 'edition': 'edition', 'url': 'source_url'}.items()}
    candidates['title'] = work.get('title')
    for key, (label, help_text) in BIB_TEXT.items():
        is_book_field = key not in ('title', 'short_title', 'bible_translation', 'bible_translation_short')
        if scope == 'work' and key.startswith('bible_translation'):
            continue
        editable = scope == 'work' or not is_book_field
        event = base_event if editable else book_event
        record = bibliography if editable else book_bib
        value = record.get(key)
        provenance = event.get('field_events', {}).get(key, event) if key in record else {}
        if key not in record and (scope == 'work' or not editable) and candidates.get(key):
            value = candidates[key]
            provenance = {'origin': 'acquisition_catalog', 'status': 'candidate', 'evidence': 'Catalog clue; verify against this edition.'}
        inherited = not editable or scope == 'passage' and key not in edits.get((target_id, 'bibliography'), {}).get('value', {}) and key in bibliography
        fields.append(_field('bibliography.' + key, label, value, group=GROUPS[1], event=provenance,
                            help=help_text, example='2014' if key == 'year' else '', inherited=inherited, editable=editable,
                            related_fields=['title'] if key == 'title' and scope != 'passage' else [],
                            edit_url='/review/' + work_id if not editable else None))
    for role in ('authors', 'editors', 'translators') if scope == 'work' else ('authors', 'translators'):
        value = bibliography.get(role)
        raw_value = copy.deepcopy(value)
        provenance = base_event.get('field_events', {}).get(role, base_event) if role in bibliography else {}
        inherited_value = None
        inherited = scope == 'passage' and role not in edits.get((target_id, 'bibliography'), {}).get('value', {}) and role in bibliography
        if scope != 'work' and role == 'translators' and value is None:
            inherited_value = book_bib.get('translators')
            value = inherited_value
            provenance = book_event.get('field_events', {}).get(role, book_event) if role in book_bib else {}
            inherited = True
        elif scope == 'work' and role not in bibliography:
            value = _name_candidates(work, role.rstrip('s')) or None
            raw_value = copy.deepcopy(value)
            if value:
                provenance = {'origin': 'acquisition_catalog', 'status': 'candidate', 'evidence': 'Catalog contributor role; verify against this edition.'}
        help_text = 'Names as printed, in their source order. Blank unknown is different from an explicitly verified absence of a named contributor.'
        if role == 'authors':
            help_text += ' An editor is not automatically the author of a contribution.'
        if role == 'translators' and scope != 'work':
            help_text += ' Inherit uses the book translation credit; an empty override explicitly means no named translator.'
        fields.append(_field('bibliography.' + role, 'Citation ' + role, value, group=GROUPS[1], kind='names', event=provenance,
                            help=help_text, example='Jane Doe', raw_value=raw_value, raw_supplied=True,
                            inherited=inherited, inherited_value=inherited_value,
                            related_fields=['author'] if scope == 'work' and role == 'authors' else ['attribution'] if role == 'authors' else []))
        fields[-1]['allow_inherit'] = scope != 'work' and role == 'translators'
        fields[-1]['allow_absent'] = True
    if scope != 'work':
        for key, label, options in [('type', 'Contribution type', ('chapter', 'poem', 'entry', 'scripture')),
                                    ('scripture_scope', 'Scope of biblical text', ('text_only', 'mixed'))]:
            event = base_event.get('field_events', {}).get(key, base_event) if key in bibliography else {}
            fields.append(_field('bibliography.' + key, label, bibliography.get(key), group=GROUPS[1], kind='select', event=event,
                                inherited=scope == 'passage' and key in bibliography and key not in edits.get((target_id, 'bibliography'), {}).get('value', {}),
                                options=[{'value': '', 'label': 'Not recorded'}] + [{'value': x, 'label': x.replace('_', ' ').capitalize()} for x in options],
                                help='Apply only to the full target. A mixed Scripture/commentary section requires separate component citations.'))
    if scope != 'passage':
        profile = copy.deepcopy(target_data.get('profile', {}))
        profile.update(edits.get((target_id, 'profile'), {}).get('value', {}))
        for facet in FACETS:
            meaning, example = FACET_HELP[facet]
            fields.append(_field('profile.' + facet, facet.replace('_', ' ').capitalize(), profile.get(facet, []),
                                group=GROUPS[2], kind='tags', event=_field_event(edits, target_id, 'profile.' + facet),
                                help=meaning + ' Use labels supported by the content. Editing replaces only this list; separate values with commas or new lines.',
                                example=example))
    coverage_complete = source_current and bool(content_rows) and len(ordered_target) == len(content_rows) and not target_truncated
    headings = [e['text'] for e in evidence if e['in_target'] and e['kind'] == 'heading']
    block_details = [json.loads(r['data']) for r in rows]
    known_types = {b.get('text_type') for b in block_details if b.get('text_type') not in (None, '', 'unknown')}
    boundary_risk = scope != 'work' and (not coverage_complete or len(headings) > 1 or target_data.get('text_type') == 'mixed'
                                         or len(known_types) > 1 or 'mixed' in known_types
                                         or any(b.get('attribution_scope') == 'segment' for b in block_details))
    warnings = []
    if scope != 'work':
        warnings.append('Changes apply to this entire ' + ('indexed passage' if scope == 'passage' else 'indexed section') + '. A heading does not prove that all included material has the same author.')
    if boundary_risk:
        warnings.append('This target may contain multiple contributions or content types. Check its full extent and use a narrower passage when needed; section splitting is a separate structural repair.')
    if not coverage_complete:
        warnings.append('The evidence preview is bounded. It is not a complete inspection of every included source block.')
    if not source_current:
        warnings.append('A source file has changed or is missing. Synchronize the library before requesting suggestions or applying this review.')
    target = {'id': target_id, 'work_id': work_id, 'title': title, 'work_title': work.get('title', ''), 'scope': scope,
              'section_id': section_id, 'read_url': read_url, 'location': location, 'location_label': location_label, 'preview': preview,
              'passage_count': len(passage_rows), 'block_count': len(content_rows), 'headings': headings[:20],
              'narrower_url': '/review/' + passage_rows[0]['id'] if scope == 'section' and passage_rows else None,
              'book_review_url': '/review/' + work_id, 'section_review_url': '/review/' + section_id if section_id else None,
              'bibliography': {**candidates, **book_bib}, 'isbn': work_meta.get('isbn'),
              'edition': book_bib['edition'] if 'edition' in book_bib else work_meta.get('edition'),
              'publisher': book_bib['publisher'] if 'publisher' in book_bib else work_meta.get('publisher')}
    formats = sorted({Path(file['path']).suffix.lstrip('.').upper() for file in files if file.get('path')})
    target['copy_format'] = ', '.join(formats) or 'Format not established'
    target['source_origin'] = work_meta.get('acquisition_source_label') or ('Bookshare accessible export' if work_meta.get('bookshare_id') else 'Local copy')
    from .intake_registration import grounded_inspection_leads
    inspection_leads = grounded_inspection_leads(work_meta, files, evidence)
    book_revision = _fingerprint({'work_id': work_id, 'sources': source_signature, 'bibliography': book_bib,
                                 'metadata': {key: work_meta.get(key) for key in ('isbn', 'edition', 'publisher', 'publication_year', 'format', 'bookshare_id')},
                                 'title': work.get('title')})
    revision = _fingerprint({'target': target_id, 'membership': membership, 'sources': source_signature,
                             'fields': [{k: f[k] for k in ('key', 'value', 'raw_value', 'status', 'origin', 'evidence', 'annotation_id', 'inherited')} for f in fields],
                             'work_title': target['work_title']})
    return {'target': target, 'fields': fields, 'groups': list(GROUPS), 'evidence': evidence, 'revision': revision,
            'book_revision': book_revision, 'inspection_leads': inspection_leads,
            'target_evidence_ids': [e['id'] for e in ordered_target],
            'supporting_evidence_ids': [e['id'] for e in evidence if not e['in_target']],
            'coverage': {'target_blocks_total': len(content_rows), 'target_blocks_supplied': len(ordered_target),
                         'target_truncated': target_truncated, 'supporting_blocks_supplied': sum(not e['in_target'] for e in evidence)},
            'source_fingerprint': _fingerprint(membership),
            'warnings': warnings, 'source_current': source_current, 'coverage_complete': coverage_complete,
            'context_complete': coverage_complete, 'boundary_risk': boundary_risk,
            'book_identifiers': {'isbn': target['isbn'], 'edition': target['edition'], 'publisher': target['publisher'],
                                 'year': book_bib['year'] if 'year' in book_bib else work_meta.get('publication_year')}}


def validate_field_value(field, value):
    if not field.get('editable', True):
        raise ValueError('Edit inherited book details on the book review page.')
    key = field['key']
    if key.startswith('bibliography.'):
        validate_bibliography({key.split('.', 1)[1]: value})
        if key == 'bibliography.type' and value is not None and value not in {o['value'] for o in field['options']}:
            raise ValueError('Choose a contribution type supported for this target.')
    elif key.startswith('profile.'):
        if not isinstance(value, list) or len(value) > 100 or any(not isinstance(v, str) or not v.strip() or len(v) > 500 for v in value):
            raise ValueError('Use up to 100 nonempty research labels, each at most 500 characters.')
    elif key == 'text_type':
        validate_text_type(value)
    elif key in ('title', 'author', 'attribution'):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError('Enter a nonempty value of at most 2,000 characters.')
    elif key == 'notes':
        if not isinstance(value, str) or len(value) > 20000:
            raise ValueError('Editorial notes must be text of at most 20,000 characters.')
    else:
        raise ValueError('That field cannot be edited in this review.')
    return copy.deepcopy(value)


def parse_field_value(field, raw, *, mode='', short_names=''):
    """Turn native form controls into values without flattening existing names."""
    if not isinstance(raw, str) or len(raw) > 30000:
        raise ValueError('Please shorten the submitted field value.')
    if field['kind'] == 'names':
        if mode in ('unknown', 'inherit'):
            if mode == 'inherit' and not field.get('allow_inherit'):
                raise ValueError('This name field cannot inherit a book credit.')
            value = None
        elif mode == 'absent':
            if raw.strip() or short_names.strip():
                raise ValueError('Clear the names before recording that no named contributor appears.')
            value = []
        else:
            names = [x.strip() for x in raw.splitlines() if x.strip()]
            shorts = short_names.splitlines()
            if any(x.strip() for x in shorts[len(names):]):
                raise ValueError('Each short name must correspond to a printed name.')
            previous = field.get('raw_value') if isinstance(field.get('raw_value'), list) else field.get('value') or []
            unused = list(previous)
            value = []
            for index, name in enumerate(names):
                match = next((i for i, old in enumerate(unused) if _display(old) == name), None)
                person = copy.deepcopy(unused.pop(match)) if match is not None else {'literal': name}
                short = shorts[index].strip() if index < len(shorts) else ''
                if short:
                    person['short'] = short
                else:
                    person.pop('short', None)
                value.append(person)
            value = value or None
    elif field['kind'] == 'tags':
        value = list(dict.fromkeys(x.strip() for x in re.split(r'[,\n]', raw) if x.strip()))
    else:
        value = raw.strip()
        if field['key'].startswith('bibliography.') and not value:
            value = None
        previous = field.get('raw_value')
        if field['key'] == 'bibliography.year' and previous is not None and str(previous) == value:
            value = previous
    return validate_field_value(field, value)


def draft_change(field, raw, previous=None, *, mode='', short_names='', evidence='', verified=False, proposal=None):
    """Build one review-draft change from text input; shared by the review page and CLI.

    `previous` is the field's existing draft change, if any; `proposal` is a
    review item whose value is being edited. Names keep their structured
    records where the printed name is unchanged. A changed value loses an
    earlier verification, and AI provenance is kept only while the value is
    still the suggested one.
    """
    parsing_field = copy.deepcopy(field)
    if field.get('kind') == 'names':
        if previous is not None:
            parsing_field['raw_value'] = copy.deepcopy(previous['value'])
        elif proposal:
            parsing_field['raw_value'] = copy.deepcopy(proposal['value'])
    value = parse_field_value(parsing_field, raw, mode=mode, short_names=short_names)
    evidence = evidence.strip()
    if len(evidence) > 4000:
        raise ValueError('Keep source evidence to 4,000 characters or fewer.')
    previous = previous or {}
    change = {'value': value, 'evidence': evidence, 'verified': bool(verified)}
    if previous.get('verified') and previous.get('value') != value:
        change['verified'] = False
    if value == previous.get('value') and previous.get('ai_provenance'):
        change['ai_provenance'] = previous['ai_provenance']
    elif proposal and value == proposal['value'] and proposal.get('ai_provenance'):
        change['ai_provenance'] = proposal['ai_provenance']
    return change


def _empty(context):
    return {'id': stable('review', context['target']['id'], context['revision']), 'target_id': context['target']['id'],
            'revision': context['revision'], 'version': 0, 'changes': {}, 'suggestions': [],
            'unresolved': [], 'assessments': [], 'decisions': {}, 'recommendation_decisions': {},
            'progress': {}, 'applications': [], 'created_at': now(), 'updated_at': now()}


def _load(lib, target_id, context):
    draft = read_json(_path(lib, target_id), None) or _empty(context)
    draft.setdefault('decisions', {})
    draft.setdefault('assessments', [])
    draft.setdefault('recommendation_decisions', {})
    draft.setdefault('progress', {})
    return draft


def load_draft(lib, target_id):
    context = review_context(lib, target_id)
    draft = _load(lib, target_id, context)
    return {**draft, 'stale': draft['revision'] != context['revision'], 'source_current': context['source_current']}


def _check_version(draft, expected_version):
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or draft['version'] != expected_version:
        raise ReviewConflict('This review was updated in another tab. Reload it before saving; your stored draft has been preserved.')


def _check_revision(context, draft, revision):
    if not context['source_current']:
        raise ReviewConflict('The source has changed or is missing. Synchronize it before continuing this review.')
    if revision != context['revision'] or draft['revision'] != context['revision']:
        raise ReviewConflict('Current information changed after this draft began. Recheck the current values before applying it; the draft has been preserved.')


def _validate_changes(context, changes):
    if not isinstance(changes, dict) or len(changes) > 80:
        raise ValueError('Choose supported fields for this review.')
    fields = {field['key']: field for field in context['fields']}
    cleaned = {}
    for key, change in changes.items():
        if key not in fields or not isinstance(change, dict) or 'value' not in change:
            raise ValueError('A proposed change needs a supported field and value.')
        value = validate_field_value(fields[key], change['value'])
        evidence = change.get('evidence', '')
        if not isinstance(evidence, str) or len(evidence) > 4000:
            raise ValueError('Keep each evidence explanation to 4,000 characters or fewer.')
        verified = change.get('verified', False)
        if not isinstance(verified, bool):
            raise ValueError('Verification must be an explicit yes or no.')
        clean = {'value': value, 'evidence': evidence.strip(), 'verified': verified}
        if isinstance(change.get('ai_provenance'), dict):
            if len(json.dumps(change['ai_provenance'])) > 12000:
                raise ValueError('Suggestion provenance is too large.')
            clean['ai_provenance'] = copy.deepcopy(change['ai_provenance'])
        cleaned[key] = clean
    return cleaned


def save_draft(lib, target_id, changes, *, revision, expected_version=0):
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        _check_version(draft, expected_version)
        _check_revision(context, draft, revision)
        cleaned = _validate_changes(context, changes)
        for decision in draft.get('decisions', {}).values():
            if decision.get('decision') == 'accepted' and decision.get('snapshot', {}).get('field') not in cleaned:
                decision.update(decision='pending', decided_at=now())
        draft.update(changes=cleaned, version=draft['version'] + 1, updated_at=now())
        atomic_json(_path(lib, target_id), draft)
    return {**draft, 'stale': False, 'source_current': True}


def _decide_review_in_memory(context, draft, item_id, action, *, replace=False, edited_change=None):
    """Atomically record an item decision and any explicitly associated draft."""
    from .review_items import review_items, item_snapshot
    if action not in {'accept', 'reject', 'keep', 'later', 'undo', 'restore'}:
        raise ValueError('Choose an available review action.')
    if not isinstance(replace, bool):
        raise ValueError('Replacing a draft requires an explicit choice.')
    if edited_change is not None and action != 'accept':
        raise ValueError('An edited suggestion can only be accepted for saving.')
    item = next((item for item in review_items(context, draft) if item['id'] == item_id), None)
    if item is None:
        raise ValueError('This review item is no longer available. Reload the review.')
    key = item['field']
    previous = copy.deepcopy(draft['decisions'].get(item_id, {}))
    changes = copy.deepcopy(draft['changes'])
    if action in {'accept', 'reject'} and item['kind'] != 'suggestion':
        raise ValueError('Unanswered questions can be investigated, kept, or deferred.')
    edited = _validate_changes(context, {key: edited_change})[key] if edited_change is not None else None
    if action == 'keep' and item['kind'] != 'question':
        raise ValueError('Use Reject to decline a suggested correction.')
    if action == 'accept' and item['stale']:
        raise ReviewConflict('This suggestion needs fresh evidence. Request a new review or edit the field manually.')
    if action == 'keep' and key in changes:
        raise ValueError('This field already has a queued correction. Review or remove that draft before keeping the current information.')
    if action == 'accept' and item['status'] == 'accepted' and edited is None:
        return {**draft, 'stale': False, 'source_current': True, 'decision_item_id': item_id,
                'message': 'This correction is already accepted for saving.'}
    decision = {'decision': {'accept': 'accepted', 'reject': 'rejected', 'keep': 'kept',
                            'later': 'later', 'undo': 'pending', 'restore': 'pending'}[action],
                'fingerprint': item['fingerprint'], 'snapshot': item_snapshot(item), 'decided_at': now()}
    history = previous.get('history', [])
    if previous:
        history = [*history, {k: previous[k] for k in ('decision', 'fingerprint', 'decided_at') if k in previous}][-20:]
    decision['history'] = history
    message = {'accept': 'Accepted for saving. The library has not changed.',
               'reject': 'Suggestion rejected. The library has not changed.',
               'keep': 'Current information kept without changing or verifying it.',
               'later': 'Item deferred. It remains outstanding.',
               'undo': 'Decision undone. The item is ready to review.',
               'restore': 'Item restored for review. Saved library information is unchanged.'}[action]
    if action == 'accept':
        existing = changes.get(key)
        proposed_value = edited['value'] if edited is not None else item['value']
        if existing and existing['value'] != proposed_value and not replace:
            raise ReviewConflict('You already have a different draft for this field. Review it or explicitly replace it with this suggestion.')
        evidence = []
        for source in item.get('evidence', []):
            detail = source.get('quote') or source.get('summary') or ''
            location = source.get('url') or source.get('read_url') or source.get('source_id', '')
            evidence.append(f'{detail} ({location})' if detail else location)
        change = copy.deepcopy(edited) if edited is not None else copy.deepcopy(existing) if existing and existing['value'] == item['value'] else {
            'value': copy.deepcopy(item['value']), 'evidence': '\n'.join(evidence)[:4000], 'verified': False,
            'ai_provenance': copy.deepcopy(item.get('ai_provenance', {}))}
        if edited is not None:
            message = 'Accepted after editing, ready for saving. The library has not changed.'
        changes[key] = change
        decision['accepted_change'] = copy.deepcopy(change)
        decision['replaced_change'] = copy.deepcopy(existing)
        # AI regeneration can offer a different value for an already queued
        # field. At most one proposal may represent that field's acceptance.
        replaced_decisions = {}
        for other_id, other in draft['decisions'].items():
            if other_id != item_id and other.get('decision') == 'accepted' and other.get('snapshot', {}).get('field') == key:
                replaced_decisions[other_id] = {k: copy.deepcopy(v) for k, v in other.items() if k != 'replaced_decisions'}
                other.update(decision='pending', superseded_by=item_id, decided_at=now())
        if replaced_decisions:
            decision['replaced_decisions'] = replaced_decisions
    elif previous.get('decision') == 'accepted':
        # Never remove input that was edited after accepting a suggestion.
        if changes.get(key) == previous.get('accepted_change'):
            if previous.get('replaced_change') is not None:
                changes[key] = copy.deepcopy(previous['replaced_change'])
            else:
                changes.pop(key, None)
            for other_id, old in previous.get('replaced_decisions', {}).items():
                other = draft['decisions'].get(other_id, {})
                if other.get('decision') == 'pending' and other.get('superseded_by') == item_id:
                    draft['decisions'][other_id] = copy.deepcopy(old)
        elif key in changes:
            message += ' Your subsequently edited draft has been kept.'
    draft['changes'] = _validate_changes(context, changes)
    draft['decisions'][item_id] = decision
    return {**draft, 'stale': False, 'source_current': True, 'decision_item_id': item_id, 'message': message}


def decide_review(lib, target_id, item_id, action, *, revision, expected_version, replace=False, edited_change=None):
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        _check_version(draft, expected_version)
        _check_revision(context, draft, revision)
        before = copy.deepcopy(draft)
        result = _decide_review_in_memory(context, draft, item_id, action, replace=replace, edited_change=edited_change)
        if draft != before:
            draft.update(version=draft['version'] + 1, updated_at=now())
            atomic_json(_path(lib, target_id), draft)
    return {**result, **draft, 'stale': False, 'source_current': True}


def decide_recommendation(lib, target_id, recommendation_id, action, *, revision, expected_version,
                          selected_item_ids=None, replace=False):
    """Stage a whole recommendation as one version-checked transaction.

    Individual decisions remain authoritative. The group receipt records what
    this operation changed so Undo cannot erase later individual decisions.
    """
    from .review_guidance import recommendations
    if action == 'accept':
        action = 'use'
    if action not in {'use', 'keep', 'later', 'undo', 'restore'}:
        raise ValueError('Choose an available recommendation action.')
    if not isinstance(replace, bool):
        raise ValueError('Replacing a draft requires an explicit choice.')
    with writer(lib.state):
        context = review_context(lib, target_id)
        original = _load(lib, target_id, context)
        _check_version(original, expected_version)
        _check_revision(context, original, revision)
        rec = next((item for item in recommendations(context, original) if item['id'] == recommendation_id), None)
        if rec is None:
            raise ValueError('This recommendation is no longer available. Reload the review.')
        selected = rec['item_ids'] if selected_item_ids is None else selected_item_ids
        if not isinstance(selected, (list, tuple)) or any(not isinstance(item, str) for item in selected) or not set(selected).issubset(rec['item_ids']):
            raise ValueError('Choose individual changes from this recommendation only.')
        selected = list(dict.fromkeys(selected))
        if action == 'use' and rec['items'] and not selected:
            raise ValueError('Choose at least one change, or choose Not now.')
        if action == 'use' and rec['stale']:
            raise ReviewConflict('This recommendation needs fresh evidence. Recheck it or edit the fields manually.')
        if action == 'keep' or action == 'use' and rec['kind'] != 'changes':
            if set(rec['fields']).intersection(original['changes']):
                raise ValueError('This information already has a queued correction. Review or remove that draft before keeping the current information.')
        draft = copy.deepcopy(original)
        previous = copy.deepcopy(draft['recommendation_decisions'].get(rec['id'], {}))
        if action == 'undo' and (not previous or previous.get('status') == 'pending'):
            return {**draft, 'stale': False, 'source_current': True, 'decision_recommendation_id': rec['id'],
                    'next_recommendation_id': rec['id'], 'message': 'This recommendation is already ready to review.'}
        before_decisions = copy.deepcopy(draft['decisions'])
        affected = []
        preserved = False
        if action in {'undo', 'restore'}:
            reversal = ({item['id']: draft['decisions'].get(item['id']) for item in rec['items']}
                        if action == 'restore' else previous.get('after_decisions', {}))
            for identity, expected in reversal.items():
                current = draft['decisions'].get(identity)
                saved_since = bool(current and expected and current.get('decision') == 'saved' and expected.get('decision') == 'accepted')
                if current != expected and not saved_since:
                    preserved = True
                    continue
                key = (current or expected or {}).get('snapshot', {}).get('field')
                current_change = copy.deepcopy(draft['changes'].get(key))
                result = _decide_review_in_memory(context, draft, identity, 'undo' if action == 'undo' else 'restore')
                preserved |= 'subsequently edited' in result.get('message', '')
                prior = previous.get('before_decisions', {}).get(identity)
                if action == 'undo' and prior and not saved_since:
                    draft['decisions'][identity] = copy.deepcopy(prior)
                if action == 'undo' and not saved_since and key in previous.get('after_changes', {}):
                    if current_change == previous['after_changes'][key]:
                        prior_change = previous.get('before_changes', {}).get(key)
                        if prior_change is None:
                            draft['changes'].pop(key, None)
                        else:
                            draft['changes'][key] = copy.deepcopy(prior_change)
                    else:
                        preserved = True
                affected.append(identity)
        else:
            chosen_fields = {}
            for item in rec['items']:
                if item['id'] not in selected:
                    continue
                item_action = 'accept' if action == 'use' and item['kind'] == 'suggestion' else 'keep' if action in {'use', 'keep'} and item['kind'] == 'question' else 'reject' if action == 'keep' else action
                if item_action == 'accept':
                    if item['field'] in chosen_fields and chosen_fields[item['field']] != item['value']:
                        raise ValueError('Choose only one proposed value for each field in Details.')
                    chosen_fields[item['field']] = item['value']
                before_item = copy.deepcopy(draft['decisions'].get(item['id']))
                _decide_review_in_memory(context, draft, item['id'], item_action, replace=replace)
                if draft['decisions'].get(item['id']) != before_item:
                    affected.append(item['id'])
        if action == 'use' and rec['items'] and not affected:
            return {**original, 'stale': False, 'source_current': True, 'decision_recommendation_id': rec['id'],
                    'next_recommendation_id': original.get('progress', {}).get('next_id', 'summary'),
                    'message': 'These corrections are already queued for saving.'}
        draft['changes'] = _validate_changes(context, draft['changes'])
        status = {'use': 'accepted' if rec['kind'] == 'changes' else 'kept', 'keep': 'kept',
                  'later': 'later', 'undo': 'pending', 'restore': 'pending'}[action]
        snapshot = {key: copy.deepcopy(value) for key, value in rec.items()
                    if key not in {'decision', 'previous_decision'}}
        decision = {'status': status, 'fingerprint': rec['fingerprint'], 'snapshot': snapshot,
                    'decided_at': now(), 'before_decisions': {key: before_decisions.get(key) for key in affected},
                    'after_decisions': {key: copy.deepcopy(draft['decisions'].get(key)) for key in affected},
                    'history': [*previous.get('history', []), *([{key: previous.get(key) for key in ('status', 'fingerprint', 'decided_at')}] if previous else [])][-20:]}
        affected_fields = {draft['decisions'][identity]['snapshot']['field'] for identity in affected if identity in draft['decisions']}
        decision['before_changes'] = {key: copy.deepcopy(original['changes'].get(key)) for key in affected_fields}
        decision['after_changes'] = {key: copy.deepcopy(draft['changes'].get(key)) for key in affected_fields}
        if action == 'undo' and previous.get('prior_recommendation'):
            decision = copy.deepcopy(previous['prior_recommendation'])
        elif previous and action not in {'undo', 'restore'}:
            decision['prior_recommendation'] = {key: copy.deepcopy(value) for key, value in previous.items() if key != 'prior_recommendation'}
        draft['recommendation_decisions'][rec['id']] = decision
        remaining = [item for item in recommendations(context, draft) if item['status'] == 'pending'
                     and item['id'] != rec['id']
                     and (item['priority'] == 'important' or rec['priority'] == 'optional')]
        next_id = rec['id'] if action in {'undo', 'restore'} else remaining[0]['id'] if remaining else 'summary'
        draft.update(progress={'last_id': rec['id'], 'next_id': next_id},
                     version=draft['version'] + 1, updated_at=now())
        atomic_json(_path(lib, target_id), draft)
    message = {'use': 'Changes queued for saving. The library has not changed.' if rec['kind'] == 'changes' else 'Current information kept without changing or verifying it.',
               'keep': 'Current information kept without changing or verifying it.',
               'later': 'Left for later. You can finish without deciding this now.',
               'undo': 'Recommendation undone. Saved library information is unchanged.',
               'restore': 'Recommendation reopened. Saved library information is unchanged.'}[action]
    if preserved:
        message += ' Your later edits and decisions have been kept.'
    return {**draft, 'stale': False, 'source_current': True, 'decision_recommendation_id': rec['id'],
            'next_recommendation_id': next_id, 'message': message}


def discard_draft(lib, target_id, *, expected_version):
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        _check_version(draft, expected_version)
        # Draft discard is not permission to forget previous review decisions.
        # Retain findings so accepted items can become actionable again.
        for decision in draft['decisions'].values():
            if decision.get('decision') == 'accepted':
                decision.update(decision='pending', decided_at=now())
        for suggestion in draft.get('suggestions', []):
            if draft['revision'] != context['revision']:
                suggestion['stale'] = True
        draft.update(changes={}, revision=context['revision'], version=draft['version'] + 1, updated_at=now())
        atomic_json(_path(lib, target_id), draft)
    return draft


def rebase_draft(lib, target_id, *, expected_version):
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        _check_version(draft, expected_version)
        if not context['source_current']:
            raise ReviewConflict('Synchronize changed or missing source files before rechecking this draft.')
        draft['changes'] = _validate_changes(context, draft['changes'])
        for change in draft['changes'].values():
            change['verified'] = False
        for proposal in draft.get('suggestions', []):
            proposal['stale'] = True
        for assessment in draft.get('assessments', []):
            assessment['stale'] = True
        draft.update(revision=context['revision'], version=draft['version'] + 1, updated_at=now())
        atomic_json(_path(lib, target_id), draft)
    return {**draft, 'stale': False, 'source_current': True}


def store_suggestions(lib, target_id, result, *, revision):
    """Merge completed AI proposals while retaining newer manual work."""
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        stale = revision != context['revision'] or draft['revision'] != context['revision'] or not context['source_current']
        suggestions = copy.deepcopy(result.get('suggestions', []))
        if not isinstance(suggestions, list) or len(suggestions) > 80:
            raise ValueError('The review returned too many suggestions.')
        fields = {f['key']: f for f in context['fields']}
        for proposal in suggestions:
            if not isinstance(proposal, dict) or proposal.get('field') not in fields:
                raise ValueError('A suggestion refers to an unavailable field.')
            validate_field_value(fields[proposal['field']], proposal.get('value'))
            proposal.setdefault('id', uid('proposal'))
            proposal['stale'] = stale
        assessments = copy.deepcopy(result.get('assessments', []))
        if not isinstance(assessments, list) or len(assessments) > 80:
            raise ValueError('The review returned too many assessments.')
        for assessment in assessments:
            if not isinstance(assessment, dict) or assessment.get('kind') not in {'field', 'edition'} or assessment.get('outcome') not in {'keep', 'change', 'unresolved'}:
                raise ValueError('The review returned an unsupported assessment.')
            if not isinstance(assessment.get('fields', []), list) or any(not isinstance(key, str) or key not in fields for key in assessment.get('fields', [])):
                raise ValueError('An assessment refers to an unavailable field.')
            assessment['stale'] = stale
            if assessment['kind'] == 'edition':
                assessment['book_revision'] = result.get('book_revision') or context['book_revision']
        # Book identity has one home, even when the useful evidence was found
        # while reading a section. Reuse the paid result without putting book
        # decisions or corrections into the section's draft.
        editions = [a for a in assessments if a['kind'] == 'edition']
        if editions and context['target']['scope'] != 'work':
            work_id = context['target']['work_id']
            book_context = review_context(lib, work_id)
            book_draft = _load(lib, work_id, book_context)
            associated = {reference.get('source_id') for assessment in editions for reference in assessment.get('evidence', [])}
            associated |= {fact.get('source_id') for assessment in editions for fact in assessment.get('reported_facts', [])}
            associated |= {identity for assessment in editions for record in (assessment.get('comparison') or {}).get('records', [])
                           for identity in record.get('source_ids', [])}
            external_sources = [source for source in (result.get('external_report') or {}).get('sources', [])
                                if source.get('id') in associated]
            # Request-local IDs such as web_1 can refer to different URLs in
            # separate reviews. Give copied sources stable identities so an
            # existing book suggestion never acquires somebody else's link.
            source_ids = {source['id']: 'book_source_' + hashlib.sha256(source.get('url', '').encode()).hexdigest()[:24]
                          for source in external_sources}
            for assessment in editions:
                for reference in [*assessment.get('evidence', []), *assessment.get('reported_facts', [])]:
                    reference['source_id'] = source_ids.get(reference.get('source_id'), reference.get('source_id'))
                for record in (assessment.get('comparison') or {}).get('records', []):
                    record['source_ids'] = [source_ids.get(identity, identity) for identity in record.get('source_ids', [])]
            if external_sources:
                report = copy.deepcopy(book_draft.get('external_report') or {'text': '', 'sources': []})
                merged_sources = {source['id']: source for source in report.get('sources', [])}
                for source in external_sources:
                    merged_sources[source_ids[source['id']]] = {**copy.deepcopy(source), 'id': source_ids[source['id']]}
                report['sources'] = list(merged_sources.values())
                book_draft['external_report'] = report
            for assessment in editions:
                assessment['stale'] = (assessment['book_revision'] != book_context['book_revision']
                                       or not book_context['source_current'])
            book_draft['assessments'] = [a for a in book_draft['assessments'] if a.get('kind') != 'edition'] + editions
            book_draft.update(version=book_draft['version'] + 1, updated_at=now())
            atomic_json(_path(lib, work_id), book_draft)
            draft['book_review_notice'] = {'url': '/review/' + work_id,
                'reason': 'The ISBN and publication comparison belongs to this book. Review it once in Book details.',
                'book_revision': context['book_revision']}
            assessments = [a for a in assessments if a['kind'] != 'edition']
        draft.update(suggestions=suggestions, unresolved=copy.deepcopy(result.get('unresolved', [])), assessments=assessments,
                     suggestion_outcome=result.get('outcome'), external_report=copy.deepcopy(result.get('external_report')),
                     suggestion_status=result.get('status', 'complete'), suggestion_warnings=copy.deepcopy(result.get('warnings', [])),
                     suggestion_model=result.get('model'), suggestion_cost=copy.deepcopy(result.get('cost')),
                     suggestion_revision=revision, version=draft['version'] + 1, updated_at=now())
        atomic_json(_path(lib, target_id), draft)
    return {**draft, 'stale': stale, 'source_current': context['source_current']}


def _mark_saved(draft, context, keys, values, evidence=None):
    from .review_items import comparison_fingerprint, review_items
    items = {item['id']: item for item in review_items(context, draft)}
    for identity, decision in draft.get('decisions', {}).items():
        key = decision.get('snapshot', {}).get('field')
        if decision.get('decision') != 'accepted' or key not in keys:
            continue
        item = items.get(identity) or decision['snapshot']
        decision.update(decision='saved', decided_at=now(),
                        fingerprint=comparison_fingerprint(context, item), saved_value=copy.deepcopy(values.get(key)),
                        saved_evidence=(evidence or {}).get(key, ''))


def apply_review(lib, target_id, selected_fields, *, revision, expected_version, request_id, changes=None):
    """Commit one selected batch, independently verifying each changed field."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', request_id):
        raise ValueError('A save request needs a valid idempotency identifier.')
    if not isinstance(selected_fields, (list, tuple)) or any(not isinstance(k, str) for k in selected_fields):
        raise ValueError('Choose the fields to apply.')
    selected = list(dict.fromkeys(selected_fields))
    with writer(lib.state):
        context = review_context(lib, target_id)
        draft = _load(lib, target_id, context)
        receipt = next((r for r in draft.get('applications', []) if r['request_id'] == request_id), None)
        prior = [event for event in lib._curation() if event.get('review_request_id') == request_id and event['target_id'] == target_id]
        if receipt:
            return {**receipt, 'duplicate': True}
        if prior:
            # A process may stop after the atomic curation write but before the
            # draft receipt is persisted. Recover the receipt without applying
            # or charging for the same changes a second time.
            receipt = {'request_id': request_id, 'saved': True, 'unchanged': False,
                       'fields': [e['review_field'] for e in prior],
                       'refresh_required': any(e['field'] != 'bibliography' for e in prior),
                       'saved_at': prior[0].get('created_at')}
            receipt['refresh_pending'] = receipt['refresh_required']
            for event in prior:
                key = event['review_field']
                applied_value = event['value'].get(key.split('.', 1)[1]) if '.' in key else event['value']
                if key in draft['changes'] and draft['changes'][key]['value'] == applied_value:
                    draft['changes'].pop(key)
            _mark_saved(draft, context, [e['review_field'] for e in prior], {
                e['review_field']: e['value'].get(e['review_field'].split('.', 1)[1]) if '.' in e['review_field'] else e['value']
                for e in prior}, {e['review_field']: e.get('evidence', '') for e in prior})
            draft['applications'] = [*draft.get('applications', []), receipt][-30:]
            draft.update(revision=context['revision'], version=draft['version'] + 1, updated_at=now())
            for proposal in draft.get('suggestions', []):
                proposal['stale'] = True
            atomic_json(_path(lib, target_id), draft)
            return {**receipt, 'duplicate': True}
        _check_version(draft, expected_version)
        _check_revision(context, draft, revision)
        proposed = _validate_changes(context, changes if changes is not None else draft['changes'])
        if changes is not None:
            if set(proposed) != set(draft['changes']) or any(proposed[k]['value'] != draft['changes'][k]['value'] for k in proposed):
                raise ReviewConflict('Save edited values to the draft before applying them. Only evidence and verification can change at final review.')
        if not set(selected).issubset(proposed):
            raise ValueError('Only fields already saved in this draft can be applied.')
        fields = {field['key']: field for field in context['fields']}
        events, changed = [], []
        for key in selected:
            field, change = fields[key], proposed[key]
            old_value = field['raw_value']
            same_value = change['value'] == old_value or key == 'bibliography.year' and str(change['value']) == str(old_value)
            adopting_candidate = field['origin'] == 'acquisition_catalog' and field['status'] == 'candidate'
            if same_value and not change['verified'] and not adopting_candidate:
                continue
            if same_value and field['status'] == 'confirmed' and change['evidence'] == field['evidence']:
                continue
            if not change['evidence']:
                raise ValueError('Give source evidence or an editorial reason for each selected change.')
            family, _, subfield = key.partition('.')
            annotation = {'field': family, 'value': {subfield: change['value']} if subfield else change['value'],
                          'origin': 'user', 'status': 'confirmed' if change['verified'] else 'provisional',
                          'evidence': change['evidence'], 'review_id': draft['id'], 'review_request_id': request_id,
                          'review_field': key}
            if 'ai_provenance' in change:
                annotation['ai_provenance'] = change['ai_provenance']
            events.append(lib._validated_annotation(target_id, annotation)); changed.append(key)
        append_jsonl_batch(lib.state / 'curation.jsonl', events)
        receipt = {'request_id': request_id, 'saved': bool(events), 'unchanged': not events,
                   'fields': changed, 'refresh_required': any(e['field'] != 'bibliography' for e in events),
                   'saved_at': now()}
        draft['changes'] = {key: value for key, value in proposed.items() if key not in selected}
        draft['applications'] = [*draft.get('applications', []), receipt][-30:]
        # The revision uses live curation, so citation-only saves are immediately visible.
        new_context = review_context(lib, target_id)
        _mark_saved(draft, new_context, selected, {key: proposed[key]['value'] for key in selected},
                    {key: proposed[key]['evidence'] for key in selected})
        draft.update(revision=new_context['revision'], version=draft['version'] + 1, updated_at=now())
        for proposal in draft.get('suggestions', []):
            if proposal.get('field') in changed:
                proposal['applied'] = True
            proposal['stale'] = True
        atomic_json(_path(lib, target_id), draft)
    if receipt['refresh_required']:
        try:
            refresh = lib.sync()
            receipt['refresh'] = refresh
            receipt['refresh_pending'] = refresh.get('semantic', {}).get('state') in ('pending', 'error', 'failed')
        except Exception:
            # The correction is durable even if rebuilding a derived view fails.
            receipt['refresh_pending'] = True
            receipt['refresh_error'] = 'The correction was saved, but refreshing library views did not finish. Run synchronization to retry.'
        # Refresh outcomes belong to the durable receipt too. Do not overwrite a
        # newer draft created while synchronization was running.
        with writer(lib.state):
            try:
                latest_context = review_context(lib, target_id)
            except (ValueError, LookupError):
                latest_context = None
                receipt['refresh_pending'] = True
                receipt['refresh_error'] = 'The correction was saved, but its source target changed during refresh. Recheck the source before continuing.'
            latest = read_json(_path(lib, target_id), None) or draft
            latest['applications'] = [receipt if item['request_id'] == request_id else item
                                      for item in latest.get('applications', [])]
            if latest_context and latest['version'] == draft['version'] and not latest['changes']:
                latest.update(revision=latest_context['revision'], version=latest['version'] + 1, updated_at=now())
            atomic_json(_path(lib, target_id), latest)
    return receipt
