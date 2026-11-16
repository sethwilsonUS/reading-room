"""Evidence-backed book structure, separate from immutable extracted source text.

An outline is navigation. A reading unit is an explicitly assembled source
membership. Neither establishes authorship. The normalized blocks passed to
the indexer are disposable; original extraction caches are never overwritten.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import re

from .source_structure import apply_reviewed_structure
from .storage import atomic_json, stable

ANALYZER_VERSION = '8'
STRUCTURE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS outline_nodes(
 id TEXT PRIMARY KEY, work_id TEXT, parent_id TEXT, ordinal INTEGER,
 depth INTEGER, title TEXT, unit_id TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS outline_work ON outline_nodes(work_id,ordinal);
CREATE INDEX IF NOT EXISTS outline_parent ON outline_nodes(parent_id,ordinal);
CREATE TABLE IF NOT EXISTS reading_units(
 id TEXT PRIMARY KEY, work_id TEXT, node_id TEXT, title TEXT, ordinal INTEGER,data TEXT);
CREATE INDEX IF NOT EXISTS reading_units_work ON reading_units(work_id,ordinal);
CREATE TABLE IF NOT EXISTS unit_blocks(
 unit_id TEXT, block_id TEXT, ordinal INTEGER, role TEXT,
 PRIMARY KEY(unit_id,block_id));
CREATE INDEX IF NOT EXISTS unit_blocks_order ON unit_blocks(unit_id,role,ordinal);
CREATE INDEX IF NOT EXISTS unit_blocks_block ON unit_blocks(block_id);
'''

PERSPECTIVES = {
    'theological perspective': 'Theological Perspective',
    'pastoral perspective': 'Pastoral Perspective',
    'exegetical perspective': 'Exegetical Perspective',
    'exegetical essay': 'Exegetical Perspective',
    'homiletical perspective': 'Homiletical Perspective',
    'homiletical perspectives': 'Homiletical Perspective',
    'homiletical reflection': 'Homiletical Perspective',
    'homiletical perspectivexxj': 'Homiletical Perspective',
    'homiletiacal perspective': 'Homiletical Perspective',
}
_BOOKS = ('Genesis|Exodus|Leviticus|Numbers|Deuteronomy|Joshua|Judges|Ruth|Samuel|Kings|'
          'Chronicles|Ezra|Nehemiah|Esther|Job|Psalms?|Proverbs|Ecclesiastes|Song of (?:Songs|Solomon)|'
          'Isaiah|Jeremiah|Lamentations|Ezekiel|Daniel|Hosea|Joel|Amos|Obadiah|Jonah|Micah|'
          'Nahum|Habakkuk|Zephaniah|Haggai|Zechariah|Malachi|Matthew|Mark|Luke|John|Johon|Acts|'
          'Romans|Corinthians|Galatians|Ephesians|Philippians|Colossians|Thessalonians|Timothy|'
          'Titus|Philemon|Hebrews|James|Peter|Jude|Revelation|Wisdom(?: of Solomon)?|Sirach|Baruch|Tobit|Judith|Maccabees')
_REFERENCE = re.compile(r'(?<!\w)((?:[123]\s*)?(?:' + _BOOKS + r')\s*[\diIlLosSOny]+(?:\s*(?:[:(\-–—]|and\s+)\s*\S.*|\s*))$', re.I)
_FRONT = {'front matter', 'contents', 'table of contents', 'editorial board', 'contributors',
          'publisher’s note', "publisher's note", 'series introduction', 'a note about the lectionary',
          'copyright', 'acknowledgments', 'acknowledgements', 'permissions'}
_FIELDS = ('kind', 'level', 'heading_path', 'heading_evidence', 'source_kind', 'source_level', 'source_heading_path',
           'structure_parent_key', 'structure_role', 'structure_depth', 'structure_evidence',
           'structure_groups', 'structure_label_prefix', 'structure_label_target', 'perspective_kind',
           'structure_toc_level', 'structure_title', 'structure_heading_fragment')


def _norm(text):
    return ' '.join(str(text).split()).casefold()


def _key(file, block):
    return f"{file['id']}:{block['ordinal']}"


def _evidence(file, block):
    return {'file_id': file['id'], 'sha256': file.get('sha256'),
            'ordinal': block['ordinal'], 'locator': deepcopy(block.get('locator', {}))}


def _readable(block):
    return bool(block.get('text', '').strip() and not block.get('is_boilerplate')
                and not block.get('is_navigation'))


def _heading(block):
    return block.get('kind') == 'heading' and _readable(block)


def _main_feasting(work):
    title = str(work.get('metadata', {}).get('title') or work.get('title', ''))
    return title.startswith('Feasting on the Word') and bool(re.search(r'Year [ABC].*Volume', title, re.I))


def _reading(text):
    match = _REFERENCE.search(text.strip())
    return (text[:match.start()].strip(' \n—–:-'), match[1].strip()) if match else None


def _cache_key(work, files, extractions):
    # Extraction shape is part of the signature: a repaired/revised cache must
    # not reuse patches that were measured against older extraction ordinals.
    h = hashlib.sha256()
    h.update(json.dumps({'version': ANALYZER_VERSION, 'title': work.get('title'),
                         'metadata_title': work.get('metadata', {}).get('title'),
                         'profile': work.get('profile', {})}, sort_keys=True).encode())
    for file in files:
        h.update(json.dumps([file['id'], file.get('sha256'), file.get('available'),
                             file.get('state'), file.get('extract_version')]).encode())
        for b in extractions.get(file['id'], {}).get('blocks', []):
            h.update(json.dumps([b.get('ordinal'), b.get('kind'), b.get('level'), b.get('text'),
                                 b.get('locator'), b.get('heading_path'), b.get('links'),
                                 b.get('is_boilerplate')], ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()


def _epub_navigation(files, report):
    """Read declared EPUB navigation without rendering or fetching resources."""
    from .extract import _Package, _resolve, _local, ExtractionError, OPF, EPUB
    import zipfile
    for file in files:
        if not str(file.get('path', '')).lower().endswith('.epub') or not file.get('available', True):
            continue
        package = None
        try:
            package = _Package(__import__('pathlib').Path(file['path']))
            members = set(n for n in package.names if n.endswith('.ncx'))
            for opf in sorted(n for n in package.names if n.endswith('.opf')):
                for item in package.xml(opf).iter(OPF + 'item'):
                    if 'nav' in item.get('properties', '').split():
                        target = _resolve(opf, item.get('href', ''))
                        if target:
                            members.add(target['member'])
            for member in sorted(members):
                root = package.xml(member)
                if member.endswith('.ncx'):
                    def walk_ncx(node, depth=0):
                        for child in node:
                            level = depth + (_local(child.tag) == 'navPoint')
                            if _local(child.tag) == 'navPoint':
                                content = next((n for n in child if _local(n.tag) == 'content'), None)
                                label = next((n for n in child if _local(n.tag) == 'navLabel'), None)
                                if content is not None and label is not None:
                                    target = _resolve(member, content.get('src', ''))
                                    if target:
                                        yield file, target, ''.join(label.itertext()).strip(), level, member
                            yield from walk_ncx(child, level)
                    yield from walk_ncx(root)
                else:
                    for nav in root.iter():
                        if _local(nav.tag) != 'nav' or 'toc' not in nav.get(EPUB + 'type', '').split():
                            continue
                        def walk_html(node, depth=0):
                            level = depth + (_local(node.tag) == 'ol')
                            if _local(node.tag) == 'a' and node.get('href'):
                                target = _resolve(member, node.get('href'))
                                if target:
                                    yield file, target, ''.join(node.itertext()).strip(), max(1, level), member
                            for child in node:
                                yield from walk_html(child, level)
                        yield from walk_html(nav)
        except (OSError, ValueError, zipfile.BadZipFile, ExtractionError):
            report['issues'].append({'code': 'navigation_unavailable', 'file_id': file['id'],
                                     'message': 'Source navigation could not be inspected safely; extracted text remains available.'})
        finally:
            if package:
                package.zip.close()


def _navigation_recovery(entries, report, files):
    """Only explicit TOC links with matching, uniquely anchored source labels."""
    anchors = defaultdict(list)
    navigation = list({(f['id'], json.dumps(t, sort_keys=True), label, level): (f, t, label, level, member)
                       for f, t, label, level, member in _epub_navigation(files, report)}.values())
    contents_members = {(f['id'], target['member']) for f, target, label, *_ in navigation
                        if _norm(label) in {'contents', 'table of contents'}}
    # Some EPUBs put a navigation ID on an inline anchor or an enclosing
    # section, which is not itself an extraction block. Resolve those IDs in
    # source XML, then require exact agreement with an existing source block.
    fallback = _epub_anchor_targets(entries, files, navigation)
    for file, block in entries:
        loc = block.get('locator', {})
        markers = [loc.get('element_id'), loc.get('bookmark'), *block.get('bookmarks', [])]
        for anchor in set(a for a in markers if isinstance(a, str)):
            if isinstance(anchor, str):
                anchors[(file['id'], loc.get('member'), anchor)].append((file, block))
    for file, block in entries:
        in_contents = ((file['id'], block.get('locator', {}).get('member')) in contents_members
                       or block.get('is_navigation') or any(_norm(p) in {'contents', 'table of contents'}
                                                      for p in block.get('heading_path', []))
                       )
        if not in_contents:
            continue
        for link in block.get('links', []):
            target = link.get('target') or {}
            anchor = target.get('element_id') or target.get('bookmark')
            label = _norm(link.get('label', ''))
            if not anchor or not label or len(label) > 240:
                continue
            candidates = anchors.get((file['id'], target.get('member'), anchor), [])
            if len(candidates) != 1:
                candidates = fallback.get((file['id'], target.get('member'), anchor), candidates)
            matching = [(f, b) for f, b in candidates if _norm(b.get('text', '')) == label and _readable(b)]
            if len(matching) != 1:
                continue
            target_file, target_block = matching[0]
            if target_block.get('kind') != 'paragraph' or target_block.get('locator', {}).get('note_id'):
                continue
            target_block.update(kind='heading', level=1,
                                heading_evidence={'origin': 'contents_navigation_target', 'status': 'confirmed',
                                                  'contents': _evidence(file, block),
                                                  'target': _evidence(target_file, target_block)})
            report['recovered_headings'] += 1
    by_member = defaultdict(list)
    for f, b in entries:
        if _readable(b) and b.get('kind') != 'pagebreak':
            by_member[(f['id'], b.get('locator', {}).get('member'))].append((f, b))
    for file, target, label, level, nav_member in navigation:
        anchor = target.get('element_id')
        if not label or len(label) > 240:
            continue
        candidates = anchors.get((file['id'], target.get('member'), anchor), [])
        if len(candidates) != 1:
            candidates = fallback.get((file['id'], target.get('member'), anchor), candidates)
        matching = [(f, b) for f, b in candidates if _norm(b.get('text', '')) == _norm(label) and _readable(b)]
        fragments = []
        # A declared TOC label can span several source paragraphs (a chapter
        # number and title, or a poem title and creator). No text is rewritten;
        # the navigation label supplies display text, with all source members.
        if not matching and len(candidates) == 1:
            f, b = candidates[0]
            peers = by_member[(f['id'], target['member'])]
            start = next((i for i, (_, peer) in enumerate(peers) if peer is b), None)
            if start is not None:
                joined = []
                for _, peer in peers[start:start + 6]:
                    if peer.get('kind') not in {'paragraph', 'heading'} or len(peer.get('text', '')) > 240:
                        break
                    joined.append(peer)
                    plain = lambda value: ''.join(c for c in _norm(value) if c.isalnum())
                    if plain(' '.join(p['text'] for p in joined)) == plain(label):
                        matching = [(f, b)]; fragments = joined[1:]
                        break
        if len(matching) != 1:
            front = {'cover', 'cover page', 'title', 'title page', 'copyright', 'copyright page',
                     'dedication', 'dedication page', 'half title', 'halftitle page', 'series page'}
            if (_norm(label) not in front and _norm(label) != _norm(report.get('work_title', ''))
                    and not any(_heading(b) for _, b in candidates)
                    and not any(f['id'] == file['id'] and b.get('locator', {}).get('member') == target['member']
                                and b.get('source_kind', b.get('kind')) == 'heading' and _norm(b['text']) == _norm(label)
                                for f, b in entries)):
                report['issues'].append({'code': 'unresolved_navigation_target',
                    'source': {'file_id': file['id'], 'sha256': file.get('sha256'), 'locator': target},
                    'navigation_label': label,
                    'message': 'A declared contents target could not be mapped to a unique matching source heading; inspect this boundary.'})
            continue
        f, b = matching[0]
        if b.get('kind') not in {'paragraph', 'heading'} or b.get('locator', {}).get('note_id'):
            continue
        if b['kind'] != 'heading':
            report['recovered_headings'] += 1
        b.update(kind='heading', structure_toc_level=level,
                 heading_evidence={'origin': 'epub_navigation_hierarchy', 'status': 'confirmed',
                                   'navigation_member': nav_member, 'navigation_label': label,
                                   'navigation_level': level, 'source': _evidence(f, b)})
        if fragments:
            b['structure_title'] = label
            b['heading_evidence']['label_members'] = [_evidence(f, p) for p in [b, *fragments]]
            for fragment in fragments:
                fragment['structure_heading_fragment'] = _key(f, b)
        b.setdefault('level', level)
    _linked_navigation_recovery(by_member, anchors, files, navigation, report)


def _linked_navigation_recovery(by_member, anchors, files, navigation, report):
    """Follow declared navigation into title-and-links-only source indexes.

    Some books declare only an index in the EPUB TOC, leaving its explicitly
    linked sections out of the packaged navigation. Require the whole member
    after its confirmed opening heading to contain only link labels, then the
    same unique anchored title agreement as ordinary contents recovery. A
    numeric link into chapter prose does not establish a source heading.
    """
    entries = [pair for peers in by_member.values() for pair in peers]
    roots = [(f, b) for peers in by_member.values() for f, b in peers[:1]
             if b.get('heading_evidence', {}).get('origin') == 'epub_navigation_hierarchy']
    if not roots:
        return
    known_targets = {(f['id'], t.get('member'), t.get('element_id')) for f, t, *_ in navigation}
    headings_by_file = defaultdict(list)
    for f, b in entries:
        if _heading(b):
            headings_by_file[f['id']].append((b['ordinal'], b.get('structure_toc_level') or b.get('level') or 1))
    for headings in headings_by_file.values():
        headings.sort()

    def boundary(file, heading, level):
        return next((ordinal for ordinal, depth in headings_by_file[file['id']]
                     if ordinal > heading['ordinal'] and depth <= level), None)

    # Collect the source-index graph before promoting anything. An index reached
    # through another index may reveal a competing parent for an earlier target.
    indexes, proposals = {}, []
    for member_key, peers in by_member.items():
        if len(peers) < 2:
            continue
        file, heading = peers[0]
        if any(b.get('kind') not in {'paragraph', 'heading'}
               or b.get('locator', {}).get('note_id')
               or not b.get('links')
               or _norm(b['text']) != _norm(' '.join(link.get('label', '') for link in b['links']))
               for _, b in peers[1:]):
            continue
        indexes[_key(file, heading)] = (file, heading)
        for _, block in peers[1:]:
            for link in block['links']:
                target = link.get('target') or {}
                label = link.get('label', '').strip()
                key = (file['id'], target.get('member'), target.get('element_id'))
                if (not target.get('member') or not target.get('element_id') or not label
                        or len(label) > 240 or label.isdecimal() or key in known_targets
                        or (file['id'], target['member']) == member_key):
                    continue
                proposals.append((file, target, label, 1, member_key[1], heading, block))
    if not proposals:
        return
    fallback = _epub_anchor_targets(entries, files, [p[:5] for p in proposals])
    edges = defaultdict(dict)
    for file, target, label, _, member, heading, link_block in proposals:
        key = (file['id'], target['member'], target['element_id'])
        candidates = anchors.get(key, [])
        if len(candidates) != 1:
            candidates = fallback.get(key, candidates)
        if len(candidates) != 1:
            continue
        f, b = candidates[0]
        if (not _readable(b) or b.get('kind') not in {'paragraph', 'heading'}
                or b.get('locator', {}).get('note_id') or _norm(b['text']) != _norm(label)
                or b.get('structure_toc_level') is not None or b['ordinal'] <= heading['ordinal']):
            continue
        edges[_key(file, heading)][_key(f, b)] = (f, b, label, member, link_block)

    pending = [(_key(f, b), boundary(f, b, b['structure_toc_level'])) for f, b in roots]
    seen, parents = set(), defaultdict(set)
    while pending:
        parent, end = pending.pop()
        if (parent, end) in seen:
            continue
        seen.add((parent, end))
        for child, (_, b, *_) in edges[parent].items():
            if end is not None and b['ordinal'] >= end:
                continue
            parents[child].add(parent)
            if child in indexes:
                pending.append((child, end))

    pending = [(f, b, b['structure_toc_level'], boundary(f, b, b['structure_toc_level'])) for f, b in roots]
    while pending:
        file, heading, parent_level, end = pending.pop()
        children = sorted((edge for child, edge in edges[_key(file, heading)].items()
                           if len(parents[child]) == 1 and (end is None or edge[1]['ordinal'] < end)),
                          key=lambda edge: edge[1]['ordinal'])
        for i, (f, b, label, member, link_block) in enumerate(children):
            if b.get('structure_toc_level') is not None:
                continue
            level = parent_level + 1
            if b['kind'] != 'heading':
                report['recovered_headings'] += 1
            b.update(kind='heading', structure_toc_level=level,
                     heading_evidence={'origin': 'linked_navigation_index', 'status': 'confirmed',
                                       'navigation_member': member, 'navigation_label': label,
                                       'navigation_level': level, 'contents': _evidence(file, link_block),
                                       'source': _evidence(f, b)})
            b.setdefault('level', level)
            heading['heading_evidence']['linked_index'] = True
            limits = [end, boundary(f, b, level)]
            if i + 1 < len(children):
                limits.append(children[i + 1][1]['ordinal'])
            child_end = min((limit for limit in limits if limit is not None), default=None)
            if _key(f, b) in indexes:
                pending.append((f, b, level, child_end))


def _epub_anchor_targets(entries, files, navigation):
    """Resolve inline/container targets without changing extraction locators."""
    from .extract import _Package, _local, _is_page, HTML_BLOCKS, IGNORED_HTML, XML_ID
    from pathlib import Path
    wanted = defaultdict(set)
    contents_members = {(f['id'], target['member']) for f, target, label, *_ in navigation
                        if _norm(label) in {'contents', 'table of contents'}}
    for f, target, *_ in navigation:
        wanted[(f['id'], target['member'])].add(target.get('element_id'))
    for f, b in entries:
        if ((f['id'], b.get('locator', {}).get('member')) in contents_members or b.get('is_navigation')
                or any(_norm(p) in {'contents', 'table of contents'} for p in b.get('heading_path', []))):
            for link in b.get('links', []):
                target = link.get('target') or {}
                if target.get('member'):
                    wanted[(f['id'], target['member'])].add(target.get('element_id'))
    source_text = defaultdict(list)
    for f, b in entries:
        if _readable(b) and b.get('kind') in {'paragraph', 'heading'}:
            source_text[(f['id'], b.get('locator', {}).get('member'), _norm(b['text']))].append((f, b))
    result = {}
    def text_of(node):
        if _local(node.tag) in IGNORED_HTML or _is_page(node):
            return ''
        return (node.text or '') + ''.join(text_of(c) + (c.tail or '') for c in node)
    for f in files:
        if not str(f.get('path', '')).endswith('.epub') or not f.get('available', True):
            continue
        package = None
        try:
            package = _Package(Path(f['path']))
            for (fid, member), ids in wanted.items():
                if fid != f['id'] or member not in package.names:
                    continue
                root = package.xml(member)
                parents = {c: n for n in root.iter() for c in n}
                # Match the extractor's block walk counter, not a text search:
                # a poem may repeat its exact title as a later line.
                positions = {}
                counter = 0
                def locate(node):
                    nonlocal counter
                    if _local(node.tag) in IGNORED_HTML:
                        return
                    counter += 1; positions[node] = counter
                    if _local(node.tag) != 'table':
                        for child in node:
                            if _local(child.tag) in HTML_BLOCKS:
                                locate(child)
                for body in (n for n in root.iter() if _local(n.tag) == 'body'):
                    locate(body)
                indexed = defaultdict(list)
                for n in root.iter():
                    if n.get('id') or n.get(XML_ID):
                        indexed[n.get('id') or n.get(XML_ID)].append(n)
                for anchor in ids:
                    targets = indexed.get(anchor, []) if anchor else [n for n in root.iter() if _local(n.tag) == 'body']
                    if len(targets) != 1:
                        continue
                    node = targets[0]
                    while _local(node.tag) not in HTML_BLOCKS and node in parents:
                        node = parents[node]
                    # Container targets refer to their first actual text block.
                    while any(_local(c.tag) in HTML_BLOCKS for c in node):
                        candidates = [c for c in node if _local(c.tag) in HTML_BLOCKS and _norm(text_of(c))]
                        if not candidates:
                            break
                        node = candidates[0]
                    candidates = source_text.get((fid, member, _norm(text_of(node))), [])
                    candidates = [(sf, sb) for sf, sb in candidates
                                  if sb.get('locator', {}).get('element_ordinal') == positions.get(node)]
                    if len(candidates) == 1:
                        result[(fid, member, anchor)] = candidates
        except (OSError, ValueError):
            # Native navigation inspection separately reports unreadable packages.
            continue
        finally:
            if package:
                package.zip.close()
    return result


def _printed_contents_recovery(entries, report):
    """A unique standalone title on the TOC's actual printed page is evidence.

    This intentionally declines OCR page repairs, author/title splitting, and
    typography-only guesses. Those require narrower source review.
    """
    titles = defaultdict(list)
    for i, (file, block) in enumerate(entries):
        if _readable(block) and block.get('kind') in {'paragraph', 'heading'} and len(block['text']) <= 200:
            page = block.get('locator', {}).get('page')
            if page:
                titles[(_norm(block['text']), _norm(page))].append((i, file, block))
    for start, (file, block) in enumerate(entries[:400]):
        if not _readable(block) or _norm(block.get('text', '')) not in {'contents', 'table of contents'}:
            continue
        for i in range(start + 1, min(len(entries), start + 1000)):
            toc_file, toc = entries[i]
            if _heading(toc) and (toc.get('level') or 1) <= (block.get('level') or 1):
                break
            if not _readable(toc) or toc.get('kind') != 'paragraph':
                continue
            match = re.fullmatch(r'\s*(.{2,160}?)\s*[.·…\t ]+\s*(\d{1,4}|[ivxlcdm]{1,10})\s*', toc['text'], re.I)
            if not match:
                continue
            possible = [(j, f, b) for j, f, b in titles.get((_norm(match[1]), _norm(match[2])), []) if j > i]
            if len(possible) != 1:
                continue
            _, target_file, target = possible[0]
            if target['kind'] != 'paragraph' or target.get('locator', {}).get('note_id'):
                continue
            target.update(kind='heading', level=1,
                          heading_evidence={'origin': 'printed_contents_title_and_page', 'status': 'confirmed',
                                            'contents': _evidence(toc_file, toc),
                                            'source': _evidence(target_file, target),
                                            'evidence': 'Unique standalone source title matches its printed contents entry and explicit printed-page label.'})
            report['recovered_headings'] += 1


def _feasting_recovery(entries, report, roster_entries=None):
    """Recover a missing slot after a roster-supported closing byline.

    The identical label inside an already present perspective is a known
    running-label pattern, and never starts a second contribution.
    """
    from .feasting import _name_prefix
    names = set()
    in_roster = False
    for _, block in roster_entries or entries:
        if _heading(block):
            in_roster = _norm(block['text']) == 'contributors'
        if in_roster and block.get('kind') == 'paragraph':
            name = _name_prefix(block.get('text', ''))
            if name:
                names.add(_norm(name))
    spans, start = [], None
    for i, (_, block) in enumerate(entries):
        if _heading(block) and _reading(block['text']):
            if start is not None:
                spans.append((start, i))
            start = i
        elif _heading(block) and _norm(block['text']) in _FRONT and start is not None:
            spans.append((start, i)); start = None
    if start is not None:
        spans.append((start, len(entries)))
    for start, end in spans:
        span = entries[start:end]
        present = Counter(PERSPECTIVES.get(_norm(b['text'])) for _, b in span if _heading(b))
        for i in range(start + 1, end):
            file, block = entries[i]
            perspective = PERSPECTIVES.get(_norm(block.get('text', '')))
            if not perspective or block.get('kind') != 'paragraph' or not _readable(block):
                continue
            if present[perspective]:
                continue
            previous = next(((f, b) for f, b in reversed(entries[start:i])
                             if _readable(b) and b.get('kind') != 'pagebreak'), None)
            following = next((b for _, b in entries[i + 1:end]
                              if _readable(b) and b.get('kind') != 'pagebreak'), None)
            if (not previous or _norm(previous[1]['text']) not in names or not following
                    or following.get('kind') != 'paragraph' or len(following['text']) < 80):
                report['issues'].append({'code': 'unresolved_perspective_boundary',
                                         'message': 'A perspective label lacks sufficient boundary evidence.',
                                         'source': _evidence(file, block)})
                continue
            block.update(kind='heading', level=3, perspective_kind=perspective,
                         heading_evidence={'origin': 'feasting_missing_perspective_after_byline',
                                           'status': 'confirmed', 'source': _evidence(file, block),
                                           'preceding_closing_byline': _evidence(*previous),
                                           'evidence': 'A missing perspective slot follows a closing byline in the same-volume contributor roster and precedes substantive essay text.'})
            present[perspective] += 1
            report['recovered_headings'] += 1
        missing = sorted(set(PERSPECTIVES.values()) - {k for k, v in present.items() if v and k})
        duplicates = sorted(k for k, v in present.items() if k and v > 1)
        if missing or duplicates:
            repeated_sources = []
            for offset, (f, b) in enumerate(span):
                if _heading(b) and PERSPECTIVES.get(_norm(b['text'])) in duplicates:
                    end_index = next((j for j in range(offset + 1, len(span)) if _heading(span[j][1])), len(span))
                    body = '\n'.join(sb['text'] for _, sb in span[offset + 1:end_index] if _readable(sb))
                    repeated_sources.append({**_evidence(f, b), 'perspective': PERSPECTIVES[_norm(b['text'])],
                                             'body_sha256': hashlib.sha256(body.encode()).hexdigest()})
            report['issues'].append({'code': 'perspective_coverage', 'source': _evidence(*entries[start]),
                                     'message': 'Perspective structure requires review.',
                                     'missing': missing, 'repeated': duplicates, 'repeated_heading_evidence': repeated_sources})


def _normalize(files, extractions, work, report):
    main = _main_feasting(work)
    stack = []
    occasion = reading = None
    all_entries = [(f, b) for f in files for b in extractions.get(f['id'], {}).get('blocks', [])]
    _navigation_recovery(all_entries, report, files)
    _printed_contents_recovery(all_entries, report)
    known_levels = sorted({max(1, int(b.get('structure_toc_level') or b.get('level') or 1))
                           for _, b in all_entries if _heading(b)})
    if main:
        segment = []
        for file in files:
            blocks = extractions.get(file['id'], {}).get('blocks', [])
            if not any(_readable(b) for b in blocks):
                if segment:
                    _feasting_recovery(segment, report, all_entries)
                    segment = []
            else:
                segment.extend((file, b) for b in blocks)
        if segment:
            _feasting_recovery(segment, report, all_entries)
    previous_source = None
    continuity_by_file = {}
    continuity_segment = 0
    for file in files:
        blocks = extractions.get(file['id'], {}).get('blocks')
        if not blocks or not any(_readable(b) for b in blocks):
            stack = []; occasion = reading = None
            continuity_segment += 1
            report['issues'].append({'code': 'source_gap', 'file_id': file['id'],
                                     **({'source': previous_source} if previous_source else {}),
                                     'message': 'Unavailable or textless source part interrupts structural continuity.'})
            continue
        continuity_by_file[file['id']] = continuity_segment
        for i, block in enumerate(blocks):
            if not _readable(block):
                continue
            block.setdefault('source_level', block.get('level'))
            block.setdefault('source_heading_path', list(block.get('heading_path', [])))
            if block.get('kind') == 'note' or block.get('locator', {}).get('note_id'):
                continue
            if block.get('kind') != 'pagebreak':
                previous_source = _evidence(file, block)
            if _heading(block):
                key = _key(file, block)
                title = block.get('structure_title') or block['text'].strip()
                level = max(1, int(block.get('structure_toc_level') or block.get('level') or 1))
                evidence = block.get('heading_evidence') or {'origin': 'native_heading', 'status': 'confirmed',
                                                            'source': _evidence(file, block)}
                role = 'group' if evidence.get('linked_index') else 'section'
                parent = next((item for item in reversed(stack) if item[0] < level), None)
                reference = _reading(title) if main else None
                perspective = PERSPECTIVES.get(_norm(title)) if main else None
                if main and reference:
                    prefix, label = reference
                    if prefix:
                        group_key = key + ':occasion'
                        group = {'key': group_key, 'title': prefix, 'role': 'occasion',
                                 'source': _evidence(file, block)}
                        block['structure_groups'] = [group]
                        occasion = (1, group_key, prefix)
                        stack = [occasion]
                        evidence = {'origin': 'feasting_combined_occasion_reading', 'status': 'confirmed',
                                    'source': _evidence(file, block), 'reading_label': label}
                    parent = occasion
                    level = 2 if occasion else 1
                    role = 'scripture_reading'
                    reading = (level, key, title)
                    stack = [occasion] if occasion else []
                elif main and perspective:
                    block['perspective_kind'] = perspective
                    parent = reading
                    level = reading[0] + 1 if reading else 1
                    role = 'contribution'
                    stack = [s for s in (occasion, reading) if s]
                    if reading is None:
                        report['issues'].append({'code': 'unresolved_parent', 'source': _evidence(file, block),
                                                 'message': 'Perspective has no supported Scripture-reading parent.'})
                elif main and _norm(title) not in _FRONT:
                    # A non-perspective heading immediately preceding a reading
                    # establishes an occasion in this explicitly identified series.
                    following = next((b for b in blocks[i + 1:] if _heading(b)), None)
                    if following and _reading(following['text']):
                        level = 1; role = 'occasion'; parent = None
                        occasion = (1, key, title); reading = None; stack = []
                if _norm(title) in _FRONT:
                    occasion = reading = None
                if role != 'group' and re.match(r'^(?:part|book)\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five)\b', title, re.I):
                    role = 'group'
                elif role != 'group' and re.match(r'^chapter\s+(?:\d+|[ivxlcdm]+|one|two|three)\b', title, re.I):
                    role = 'chapter'
                stack = [s for s in stack if s[0] < level]
                # Missing ancestors are represented honestly: native depths remain
                # evidence, while display depth follows the actual known parents.
                # A book using only h2/h4 is a valid two-level outline. Only
                # flag a level that demonstrably exists elsewhere but is absent
                # from this branch, rather than mistaking typography for loss.
                prior_level = stack[-1][0] if stack else 0
                missing_levels = [n for n in known_levels if prior_level < n < level]
                if not main and missing_levels:
                    report['issues'].append({'code': 'skipped_heading_level', 'source': _evidence(file, block),
                                             'missing_source_levels': missing_levels,
                                             'message': 'A source level used elsewhere is absent from this branch; only known parents are displayed.'})
                if not main or role not in {'contribution', 'scripture_reading'}:
                    parent = stack[-1] if stack else None
                block.update(level=level, structure_parent_key=parent[1] if parent else None,
                             structure_role=role, structure_depth=len(stack) + 1,
                             structure_evidence=deepcopy(evidence))
                stack.append((level, key, title))
            block['heading_path'] = [s[2] for s in stack]
    # Adjacent chapter number/title labels are one navigation label, while both
    # original source blocks and section identities remain independently stored.
    previous = None
    for file, block in all_entries:
        if not _readable(block) or block.get('kind') == 'pagebreak':
            continue
        if previous and continuity_by_file.get(previous[0]['id']) != continuity_by_file.get(file['id']):
            previous = None
        if previous and _heading(block) and _heading(previous[1]):
            old_file, old = previous
            short = old['text'].strip()
            if (old.get('level') == block.get('level') and len(block['text']) <= 200
                    and (re.fullmatch(r'\d{1,3}|[IVXLC]+', short)
                         or re.fullmatch(r'(?:Book|Part|Chapter)\s+(?:\w+|\d+)', short, re.I))):
                old['structure_label_target'] = _key(file, block)
                block['structure_label_prefix'] = short
                if old.get('structure_role') in {'group', 'chapter'}:
                    block['structure_role'] = old['structure_role']
                block['structure_parent_key'] = old.get('structure_parent_key')
        previous = (file, block)


def prepare_work(lib, work, files, extractions):
    """Normalize all healthy ordered parts before contributor analysis.

    Only derived structure fields are cached; cache signatures include original
    source fingerprints and extraction shape, not merely book titles.
    """
    signature = _cache_key(work, files, extractions)
    cache = lib.state / 'structure-analysis' / (signature + '.json')
    if cache.exists():
        try:
            saved = json.loads(cache.read_text())
            if saved.get('version') == ANALYZER_VERSION:
                for fid, patches in saved['patches'].items():
                    for ordinal, fields in patches:
                        extractions[fid]['blocks'][ordinal].update(fields)
                return {**saved['report'], 'cache_reused': True}
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            pass
    report = {'analyzer_version': ANALYZER_VERSION, 'state': 'supported', 'issues': [],
              'work_title': work.get('title', ''),
              'recovered_headings': 0, 'evidence': [], 'cache_reused': False,
              'source_signature': signature, 'scope': 'Source structure; authorship and citation metadata are separate checks.'}
    originals = {fid: [{k: deepcopy(b[k]) for k in _FIELDS if k in b} for b in e['blocks']]
                 for fid, e in extractions.items()}
    for extraction in extractions.values():
        for block in extraction['blocks']:
            block.setdefault('source_kind', block.get('kind'))
            block.setdefault('source_level', block.get('level'))
            block.setdefault('source_heading_path', list(block.get('heading_path', [])))
    for file in files:
        if file['id'] in extractions:
            apply_reviewed_structure(file, extractions[file['id']])
    _normalize(files, extractions, work, report)
    headings = [(f, b) for f in files for b in extractions.get(f['id'], {}).get('blocks', []) if _heading(b)]
    all_blocks = [b for e in extractions.values() for b in e['blocks'] if _readable(b)]
    report['heading_count'] = len(headings)
    report['readable_blocks'] = len(all_blocks)
    report['text_characters'] = sum(len(b['text']) for b in all_blocks)
    if not headings and all_blocks:
        report['issues'].append({'code': 'no_headings', 'message': 'Readable text has no established section boundaries; inspect the source contents.'})
    elif len(headings) <= 2 and report['text_characters'] > 20000:
        report['issues'].append({'code': 'sparse_outline', 'message': 'A substantial book has very few section headings; individual reading-unit boundaries need review.'})
    if any('anthology' in _norm(v) for v in [work.get('title', ''), *work.get('profile', {}).get('forms', [])]):
        if headings and not any(b.get('level', 1) > 1 for _, b in headings):
            report['issues'].append({'code': 'anthology_contributions_unresolved', 'message': 'Flat anthology headings do not establish all individual contribution boundaries.'})
    for i in sorted({0, len(headings) // 2, len(headings) - 1}):
        if 0 <= i < len(headings):
            f, b = headings[i]
            report['evidence'].append({**_evidence(f, b), 'heading': b['text'][:240],
                                       'origin': b.get('structure_evidence', {}).get('origin', 'native_heading')})
    entries = [(f, b) for f in files for b in extractions.get(f['id'], {}).get('blocks', []) if _readable(b)]
    positions = {_key(f, b): i for i, (f, b) in enumerate(entries)}
    report['contents_evidence'] = []
    for i, (f, b) in enumerate(entries[:500]):
        if _norm(b['text']) in {'contents', 'table of contents'}:
            report['contents_evidence'].append({**_evidence(f, b), 'label': b['text'][:240],
                'following_entries': [{**_evidence(ef, eb), 'label': eb['text'][:240], 'kind': eb['kind']}
                                      for ef, eb in entries[i + 1:i + 5]]})
    report['boundary_samples'] = []
    for heading in report['evidence']:
        index = positions.get(f"{heading['file_id']}:{heading['ordinal']}")
        if index is None:
            continue
        context = []
        for j in (index - 1, index + 1):
            if 0 <= j < len(entries):
                f, b = entries[j]
                context.append({**_evidence(f, b), 'kind': b['kind'], 'characters': len(b['text']),
                                'text_sha256': hashlib.sha256(b['text'].encode()).hexdigest(),
                                'excerpt': b['text'][:180]})
        report['boundary_samples'].append({'heading': heading, 'neighbors': context})
    report['state'] = 'needs_review' if report['issues'] else 'supported'
    patches = {}
    for fid, extraction in extractions.items():
        values = []
        for ordinal, b in enumerate(extraction['blocks']):
            update = {k: deepcopy(b[k]) for k in _FIELDS if k in b and
                      (k not in originals[fid][ordinal] or originals[fid][ordinal][k] != b[k])}
            if update:
                values.append([ordinal, update])
        patches[fid] = values
    atomic_json(cache, {'version': ANALYZER_VERSION, 'report': report, 'patches': patches})
    return report


def index_work_structure(db, work, files, report):
    """Project normalized headings and explicit unit membership after indexing."""
    db.row_factory = __import__('sqlite3').Row
    sections = [dict(r) for r in db.execute('SELECT * FROM sections WHERE work_id=? ORDER BY ordinal', (work['id'],))]
    blocks = [dict(r) for r in db.execute('SELECT * FROM blocks WHERE work_id=? ORDER BY ordinal', (work['id'],))]
    by_section = defaultdict(list)
    by_key, file_map = {}, {f['id']: f for f in files}
    for row in blocks:
        row['source'] = json.loads(row['data'])
        by_section[row['section_id']].append(row)
        by_key[f"{row['file_id']}:{row['source']['ordinal']}"] = row
    nodes, section_nodes, key_nodes = [], {}, {}
    for section in sections:
        info = json.loads(section['data'])
        owned = by_section[section['id']]
        visible = [b for b in owned if _readable(b['source'])]
        if not visible:
            continue
        h = next((b for b in visible if _heading(b['source'])), None)
        source = h['source'] if h else {}
        key = f"{h['file_id']}:{source['ordinal']}" if h else None
        if source.get('structure_label_target'):
            continue
        for group in source.get('structure_groups', []):
            gid = stable('outline', work['id'], group['key'])
            g = {'id': gid, 'work_id': work['id'], 'parent_id': None,
                 'ordinal': section['ordinal'] * 10 - 1, 'depth': 1,
                 'title': group['title'], 'unit_id': None,
                 'data': {'kind': 'occasion', 'section_id': None, 'attribution_verified': False,
                          'evidence': group['source'], 'synthetic': True}}
            nodes.append(g); key_nodes[group['key']] = gid
        source_title = source.get('text', '').strip()
        title = section['title']
        if title == source_title and source.get('structure_title'):
            title = source['structure_title']
        # User display-title corrections still win over the combined source label.
        if title == source_title and source.get('structure_label_prefix'):
            title = source['structure_label_prefix'] + ': ' + title
        if title == source_title and source.get('structure_groups'):
            title = source.get('structure_evidence', {}).get('reading_label') or title
        kind = source.get('structure_role', 'section')
        if section['title'].startswith('Source notes —'):
            kind = 'unassigned_notes'
        elif not h:
            kind = 'front_matter' if section['title'] == 'Front matter' else 'section'
        if 'authors' in info.get('bibliography', {}):
            verified = bool(info['bibliography']['authors'] and
                            info.get('bibliography_authors_status', info.get('bibliography_status')) == 'confirmed')
        else:
            verified = info.get('source_bibliography', {}).get('status') == 'confirmed'
        if info.get('attribution_status'):
            verified = bool(info.get('attribution') and info['attribution_status'] == 'confirmed')
        node = {'id': section['id'], 'work_id': work['id'], 'parent_id': None,
                'ordinal': section['ordinal'] * 10, 'depth': 1, 'title': title, 'unit_id': None,
                'parent_key': source.get('structure_parent_key'),
                'data': {'kind': kind, 'section_id': section['id'],
                         'anchor_block_id': h['id'] if h else visible[0]['id'],
                         'attribution': info.get('attribution'), 'attribution_verified': verified,
                         'attribution_status': 'confirmed' if verified else 'unknown',
                         'evidence': source.get('structure_evidence', {}),
                         'source_level': source.get('source_level'), 'section_ids': [section['id']]}}
        nodes.append(node); section_nodes[section['id']] = node
        if key:
            key_nodes[key] = node['id']
    node_map = {n['id']: n for n in nodes}
    # Connect by native identity, not title. Parent must precede child, preventing
    # cycles from malformed evidence and keeping independent repeated labels apart.
    for node in nodes:
        pid = key_nodes.get(node.pop('parent_key', None))
        if pid in node_map and node_map[pid]['ordinal'] < node['ordinal']:
            node['parent_id'] = pid; node['depth'] = node_map[pid]['depth'] + 1
    children = defaultdict(list)
    for node in nodes:
        children[node['parent_id']].append(node)
    # Reconnect suppressed chapter-number labels to their following title node.
    for section in sections:
        if section['id'] in section_nodes:
            continue
        heading = next((b['source'] for b in by_section[section['id']] if _heading(b['source'])), {})
        target = key_nodes.get(heading.get('structure_label_target'))
        if target:
            section_nodes[section['id']] = node_map[target]
            node_map[target]['data']['section_ids'].insert(0, section['id'])
    anthology = 'editor' in {c.get('role') for c in work.get('contributors', [])}
    unit_roots = {}
    for node in nodes:
        kind = node['data']['kind']
        own_sections = node['data'].get('section_ids', [])
        own_blocks = [r for sid in own_sections for r in by_section[sid] if _readable(r['source'])]
        substantive = any(r['source']['kind'] not in {'heading', 'pagebreak'} and not r['source'].get('structure_heading_fragment')
                          and r['source']['text'].strip() != '[blank page]'
                          for r in own_blocks)
        parent_unit = unit_roots.get(node['parent_id'])
        parent_kind = node_map[node['parent_id']]['data']['kind'] if node['parent_id'] else None
        # A heading-only subsection is still part of the complete chapter.
        # Inherit before classifying it as a standalone navigation grouping so
        # its heading and all deeper descendants retain chapter membership.
        if (parent_unit and parent_kind not in {'occasion', 'scripture_reading'}
                and (parent_kind != 'group' or parent_unit != node['parent_id'])
                and kind not in {'occasion', 'scripture_reading', 'group', 'contribution', 'chapter', 'unassigned_notes'}
                and (not anthology or node_map[parent_unit]['data']['kind'] in {'contribution', 'chapter'})):
            unit_roots[node['id']] = parent_unit
            continue
        if kind in {'occasion', 'scripture_reading', 'group'}:
            if substantive:
                unit_roots[node['id']] = node['id']
            continue
        if not substantive and children[node['id']]:
            node['data']['kind'] = 'group'
            continue
        if substantive or own_blocks:
            unit_roots[node['id']] = node['id']
    members = defaultdict(list)
    unit_sections = defaultdict(list)
    for sid, node in section_nodes.items():
        root = unit_roots.get(node['id'])
        if not root:
            continue
        unit_sections[root].append(sid)
        for row in by_section[sid]:
            b = row['source']
            if not b.get('is_boilerplate') and not b.get('is_navigation') and (b.get('text', '').strip() or b.get('image_refs')):
                members[root].append(row)
    count = 0
    for root, rows in members.items():
        node = node_map[root]
        if not rows:
            continue
        uid = stable('unit', root)
        node['unit_id'] = uid
        root_section = node['data']['section_id']
        section_info = next((json.loads(s['data']) for s in sections if s['id'] == root_section), {})
        info = {'id': uid, 'work_id': work['id'], 'root_section_id': root_section,
                'section_ids': unit_sections[root], 'kind': node['data']['kind'],
                'attribution': section_info.get('attribution'),
                'attribution_verified': node['data']['attribution_verified'],
                'structure_status': 'needs_review' if node['data']['kind'] in {'front_matter', 'unassigned_notes'} else 'supported',
                'evidence': node['data']['evidence'], 'body_blocks': 0, 'note_blocks': 0,
                'source_files': sorted({r['file_id'] for r in rows}),
                'source_fingerprints': {fid: file_map[fid]['sha256'] for fid in {r['file_id'] for r in rows}}}
        source_keys = {(r['file_id'], r['source']['ordinal']) for r in rows}
        ancestor = node
        while ancestor:
            ev = ancestor['data'].get('evidence', {})
            ev_source = ev.get('source', ev)
            if ev_source.get('file_id') is not None:
                source_keys.add((ev_source['file_id'], ev_source.get('ordinal')))
            ancestor = node_map.get(ancestor['parent_id'])
        global_issues = {'no_headings', 'sparse_outline', 'anthology_contributions_unresolved', 'navigation_unavailable',
                         'unresolved_navigation_target'}
        issues = [issue for issue in report.get('issues', []) if issue.get('code') in global_issues
                  or (issue.get('source', {}).get('file_id'), issue.get('source', {}).get('ordinal')) in source_keys]
        for member_section in sections:
            if member_section['id'] in info['section_ids']:
                continuity = json.loads(member_section['data']).get('continuity_warning')
                if continuity:
                    issues.append({'code': 'interrupted_source_continuity', 'section_id': member_section['id'],
                                   'message': continuity})
        if issues:
            info['structure_status'] = 'needs_review'
            info['structure_issues'] = issues
        for row in sorted(rows, key=lambda r: r['ordinal']):
            role = 'note' if row['source'].get('kind') == 'note' else 'body'
            info[role + '_blocks'] += 1
            db.execute('INSERT INTO unit_blocks VALUES(?,?,?,?)', (uid, row['id'], row['ordinal'], role))
        db.execute('INSERT INTO reading_units VALUES(?,?,?,?,?,?)',
                   (uid, work['id'], root, node['title'], node['ordinal'], json.dumps(info, ensure_ascii=False)))
        count += 1
    for node in nodes:
        # Internal subheadings link to their containing complete reading unit.
        root = unit_roots.get(node['id'])
        if root and root in members:
            node['unit_id'] = stable('unit', root)
        node['data']['is_unit_root'] = root == node['id']
        db.execute('INSERT INTO outline_nodes VALUES(?,?,?,?,?,?,?,?)',
                   (node['id'], work['id'], node['parent_id'], node['ordinal'], node['depth'],
                    node['title'], node['unit_id'], json.dumps(node['data'], ensure_ascii=False)))
    unassigned_notes = [r for r in blocks if r['source'].get('note_ownership', {}).get('origin') == 'native_note_reference'
                        and r['source'].get('note_ownership', {}).get('status') == 'unresolved']
    if unassigned_notes:
        report = deepcopy(report)
        report['issues'].append({'code': 'unassigned_source_notes', 'count': len(unassigned_notes),
                                 'message': 'Source notes with unresolved reference relationships remain explicitly separate.',
                                 'examples': [r['source']['note_ownership'] for r in unassigned_notes[:3]]})
        report['state'] = 'needs_review'
    work['structure'] = {**report, 'outline_nodes': len(nodes), 'reading_units': count}
    return work['structure']
