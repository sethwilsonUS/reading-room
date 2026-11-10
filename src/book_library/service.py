from __future__ import annotations
import collections, copy, gzip, hashlib, json, os, re, sqlite3, time, unicodedata
from pathlib import Path
from .storage import now, uid, stable, digest, read_json, atomic_json, writer, append_jsonl, jsonl, SCHEMA, connect
from .defaults import FOCUSES, TAXONOMY
from .install_defaults import default_library_root

FACETS = {'subjects','disciplines','themes','motifs','questions','forms','people','places','works','artworks','biblical_references','periods','cultures','traditions','media','approaches'}
STOP = set('a an the of for to in on with and or is are was were how what which who why when does do can about from by into as that this me find show passages passage book books discuss discussion'.split())
EXTRACT_VERSION = '2'
INDEX_VERSION = '6'  # Source-backed outlines, reading units, and preserved excerpt extents.


def norm(text):
    return ''.join(c for c in unicodedata.normalize('NFKD',str(text)).lower() if not unicodedata.combining(c))


def terms(text): return [t for t in re.findall(r'[^\W_]+',norm(text)) if t not in STOP]


def compact(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'))


def catalog_signature(cat):
    return hashlib.sha256(compact({'files':[(f['id'],f['path'],f.get('sha256'),f.get('state'),f.get('available')) for f in cat['files'].values()],'works':cat['works']}).encode()).hexdigest()


def excerpt_range(text,needles,limit=700):
    normalized=norm(text)
    positions=[normalized.find(norm(t)) for t in sorted(needles,key=len,reverse=True) if len(t)>2 and norm(t) in normalized]
    start=0
    if positions:
        # Normalization can remove combining accents or expand ligatures. Map
        # its match offset back to source characters before slicing citation text.
        normalized_offset=0
        for source_offset,char in enumerate(text):
            normalized_offset+=len(norm(char))
            if normalized_offset>positions[0]:
                start=max(0,source_offset-180)
                break
    if start+limit>len(text): start=max(0,len(text)-limit)
    end=min(len(text),start+limit)
    return {'text':('…' if start else '')+text[start:end]+('…' if end<len(text) else ''),
            'excerpt_start':start,'excerpt_end':end}


def excerpt_near(text,needles,limit=700):
    return excerpt_range(text,needles,limit)['text']



def location(block, file):
    loc=block.get('locator',{}); parts=[Path(file['path']).name]
    if loc.get('member'): parts.append(loc['member'])
    if loc.get('page'): parts.append('page '+str(loc['page']))
    elif block.get('page'): parts.append('page '+str(block['page']))
    else: parts.append('block '+str(block.get('ordinal',0)+1))
    return ' · '.join(parts)


class Library:
    def __init__(self,root=None):
        self.root=Path(root or os.environ.get('LIBRARY_ROOT') or default_library_root()).expanduser().resolve()
        self.state=self.root/'.library'

    def _catalog(self): return read_json(self.state/'catalog.json',{'schema_version':1,'works':{},'files':{},'acquisitions':{},'updated_at':None})
    def _curation(self): return jsonl(self.state/'curation.jsonl')
    def _active(self): return read_json(self.state/'current.json',{})
    def _db(self):
        active=self._active()
        if not active: raise ValueError('No index yet. Run library sync.')
        return connect(self.state/'generations'/active['database'])

    def _initialize(self):
        from .public_config import initialize, is_existing_library
        existing = is_existing_library(self) or any((self.state/'focuses').glob('*.json'))
        initialize(self, locked=True)
        for d in ('extractions','generations','focuses','pilot'): (self.state/d).mkdir(parents=True,exist_ok=True)
        marker = self.state/'defaults-initialized.json'
        if not marker.exists():
            if not existing:
                if not (self.state/'taxonomy.json').exists(): atomic_json(self.state/'taxonomy.json',TAXONOMY)
                for f in FOCUSES:
                    p=self.state/'focuses'/(f['id']+'.json')
                    if not p.exists(): atomic_json(p,{**f,'books':[],'origin':'codex','status':'provisional'})
            atomic_json(marker, {'version': 1, 'existing_library': bool(existing), 'created_at': now()})

    def _inventory(self):
        # Never follow directory symlinks or treat derived state as source material.
        found={}
        aliases=read_json(self.state/'source-aliases.json',{})
        if not self.root.is_dir(): raise ValueError('Library root does not exist: '+str(self.root))
        def onerror(exc): raise exc
        for base,dirs,files in os.walk(self.root,followlinks=False,onerror=onerror):
            dirs[:]=[d for d in dirs if not d.startswith('.') and not (Path(base)/d).is_symlink()]
            for name in files:
                p=Path(base)/name
                if p.suffix.lower() in {'.docx','.epub'} and not p.is_symlink():
                    st=p.stat()
                    alias=aliases.get(str(p),{})
                    if alias and (st.st_size,st.st_mtime_ns)==(alias.get('size'),alias.get('mtime_ns')) and [st.st_dev,st.st_ino,st.st_ctime_ns]==alias.get('signature'):
                        canonical=Path(alias.get('canonical_path',''))
                        if canonical.is_file() and not canonical.is_symlink():
                            original=canonical.stat()
                            if (original.st_size,original.st_mtime_ns)==(alias.get('canonical_size'),alias.get('canonical_mtime_ns')) and [original.st_dev,original.st_ino,original.st_ctime_ns]==alias.get('canonical_signature'):
                                continue
                    found[str(p)]={'size':st.st_size,'mtime_ns':st.st_mtime_ns}
        return found

    def _bootstrap(self,cat):
        before=compact(cat)
        source=read_json(self.root/'.catalog/library.json',{})
        books=source.get('books',[]) if isinstance(source,dict) else source
        if isinstance(books,dict): books=list(books.values())
        books=[b for b in books if isinstance(b,dict) and b.get('folder') and b.get('title')]
        folders={b.get('folder') for b in books}
        for manifest_path in sorted((self.root/'.catalog').glob('*.json')):
            if manifest_path.name in ('library.json','verification.json'): continue
            raw=read_json(manifest_path,{})
            records=raw if isinstance(raw,list) else raw.get('books',[]) if isinstance(raw,dict) else []
            if isinstance(records,dict): records=list(records.values())
            if isinstance(raw,dict) and raw.get('title') and raw.get('files'): records=[raw]
            for b in records:
                if isinstance(b,dict) and not b.get('files') and isinstance(raw,dict):
                    b={**b,'files':[f for f in raw.get('files',[]) if b.get('bookshare_id') is not None and str(f.get('bookshare_id'))==str(b.get('bookshare_id'))]}
                if isinstance(b,dict) and b.get('folder') and b.get('title') and b.get('files') and b['folder'] not in folders:
                    books.append({**b,'source_catalog':manifest_path.name}); folders.add(b['folder'])
        from .intake_registration import acquisition_books
        books.extend(acquisition_books(self))
        for raw_book in books:
            b={**raw_book,'folder':str(Path(raw_book['folder']).expanduser().resolve()),'files':[{**f,'path':str(Path(f['path']).expanduser().resolve())} for f in raw_book.get('files',[])]}
            if self.root not in Path(b['folder']).parents and Path(b['folder'])!=self.root: continue
            if any(self.root not in Path(f['path']).parents for f in b['files']): continue
            enriched={}
            manifest=b.get('source_catalog')
            if manifest:
                raw=read_json(self.root/'.catalog'/manifest,{})
                candidates=raw if isinstance(raw,list) else raw.get('books',[]) if isinstance(raw,dict) else []
                if isinstance(candidates,dict): candidates=list(candidates.values())
                for old in candidates:
                    if isinstance(old,dict) and (old.get('folder')==b.get('folder') or (b.get('bookshare_id') and str(old.get('bookshare_id'))==str(b['bookshare_id']))):
                        enriched.update(old); break
            enriched.update(b)
            existing=next((w for w in cat['works'].values() if (w.get('folder')==b.get('folder') or b.get('folder') in w.get('previous_folders',[]))),None)
            paths={f['path'] for f in b.get('files',[])}
            if existing is None:
                matches={f['work_id'] for f in cat['files'].values() if paths.intersection([f['path']]+f.get('previous_paths',[]))}
                if len(matches)==1: existing=cat['works'][matches.pop()]
            # A refreshed acquisition catalog may already contain a renamed folder. Verify
            # every destination's bytes before reconnecting it to missing historical files.
            rename_matches={}
            if existing is None and paths:
                possible={}
                for path in paths:
                    if not Path(path).is_file(): continue
                    sha=digest(path)
                    old=[f for f in cat['files'].values() if f.get('sha256')==sha and not Path(f['path']).exists()]
                    if len(old)==1: possible[path]=old[0]
                workids={f['work_id'] for f in possible.values()}
                if len(possible)==len(paths) and len(workids)==1:
                    existing=cat['works'][workids.pop()]; rename_matches=possible
            title=enriched['title']; author=enriched.get('author') or enriched.get('authors') or enriched.get('editors') or 'Unknown'
            if isinstance(author,list):
                from .citations import _display
                author=', '.join(_display(person) if isinstance(person,dict) else str(person) for person in author)
            canonical_intake=bool(b.get('_intake_acquisition_id'))
            edited=bool(enriched.get('editors')) or (not canonical_intake and bool(re.search(r'Anthology|Commentary|Feasting|Annotated|Study Bible|Quotations|Torah Queeries',title,re.I)))
            if author in ('Robert Alter','Willie James Jennings','Greg Carey'): edited=False
            if existing is None:
                workid=uid('work'); editionid=uid('edition')
                contributors=[] if author=='Unknown' or canonical_intake and not any(enriched.get(k) for k in ('authors','editors','translators')) else [{'id':uid('person'),'name':author,'role':'editor' if edited else 'author','origin':'catalog','status':'provisional'}]
                if canonical_intake:
                    from .citations import _display
                    contributors=[{'id':uid('person'),'name':_display(person) if isinstance(person,dict) else person,
                                   'role':role,'origin':'catalog','status':'provisional'}
                                  for key,role in (('authors','author'),('editors','editor'),('translators','translator'))
                                  for person in enriched.get(key,[]) or []]
                existing={'id':workid,'edition_id':editionid,'volume_id':uid('volume'),'title':title,'author':author,'contributors':contributors,'folder':b.get('folder'),'metadata':{},'profile':{},'metadata_pending':True,'created_at':now()}
                cat['works'][workid]=existing
            workid=existing['id']; editionid=existing['edition_id']
            if not existing.get('metadata'):
                existing.update(title=title,author=author)
                if not existing.get('contributors') and not canonical_intake:
                    existing['contributors']=[{'id':uid('person'),'name':author,'role':'editor' if edited else 'author','origin':'catalog','status':'provisional'}]
            existing['metadata'].update({k:v for k,v in enriched.items() if k!='files' and not k.startswith('_')})
            if existing.get('folder')!=b.get('folder') and Path(b['folder']).is_dir():
                existing.setdefault('previous_folders',[]).append(existing.get('folder')); existing['folder']=b.get('folder')
            provider=b.get('_provider') or b.get('provider') or ('bookshare' if b.get('bookshare_id') is not None else 'local')
            provider_id=b.get('_provider_id',b.get('provider_id',b.get('bookshare_id')))
            acquisition=cat['acquisitions'].get(b.get('_intake_acquisition_id')) if canonical_intake else next((a for a in cat['acquisitions'].values() if a['work_id']==workid and a.get('provider')==provider and a.get('provider_id')==provider_id),None)
            if acquisition is None:
                aid=b.get('_intake_acquisition_id') or uid('acquisition'); acquisition={'id':aid,'work_id':workid,'edition_id':editionid,'provider':provider,'provider_id':provider_id,'record':enriched,'stage':'downloaded','created_at':now()};cat['acquisitions'][aid]=acquisition
            elif acquisition['record']!=enriched:
                acquisition.setdefault('previous_records',[]).append(acquisition['record']); acquisition['record']=enriched
            for order,metadata in enumerate(b.get('files',[])):
                path=metadata['path']
                file=next((f for f in cat['files'].values() if f['path']==path or path in f.get('previous_paths',[])),None) or rename_matches.get(path)
                if file is None:
                    fid=uid('file'); file={'id':fid,'work_id':workid,'edition_id':editionid,'path':path,'previous_paths':[],'versions':[],'available':False};cat['files'][fid]=file
                if file['path']!=path and Path(path).exists():
                    file['previous_paths'].append(file['path']);file['path']=path
                file.update(acquisition_id=acquisition['id'],sequence=metadata.get('sequence',order),catalog_metadata=metadata)
        return compact(cat)!=before

    def _effective(self,cat):
        from .intake_curation import active_curation
        result=copy.deepcopy(cat['works']); slots={}
        for event in active_curation(self._curation()):
            fields=event['value'].items() if event['field'] in ('profile','bibliography') else [(None,event['value'])]
            for subfield,value in fields:
                key=(event['target_id'],event['field'],subfield); old=slots.get(key)
                if old and old['origin']=='user' and event['origin']!='user': continue
                slots[key]={**event,'value':value}
        edits={}
        for (target,field,subfield),event in slots.items():
            if field in ('profile','bibliography'):
                combined=edits.setdefault((target,field),{**event,'value':{}})
                combined['value'][subfield]=event['value']
                combined.setdefault('field_events',{})[subfield]=event
            else: edits[(target,field)]=event
            if target in result:
                if field in ('profile','bibliography'):
                    result[target].setdefault(field,{})[subfield]=event['value']
                    if field=='profile':result[target]['metadata_pending']=False
                else: result[target][field]=event['value']
                result[target].setdefault('classifications',[]).append({**event,'profile_field':subfield})
        return result,edits

    def sync(self,path=None,rebuild=False,*,semantic=True):
        result=self._sync_exact(path,rebuild)
        settings=read_json(self.state/'config.json',{}).get('semantic',{})
        if semantic and settings.get('enabled') and settings.get('full_build_approved'):
            try:
                from .semantic_library import build_index
                result['semantic']=build_index(self)
            except Exception as exc:
                result['semantic']={'state':'pending','error':str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__,'exact_search_available':True}
        return result

    def _sync_exact(self,path=None,rebuild=False):
        start=time.perf_counter()
        if path:
            hint=Path(path).expanduser().resolve()
            if hint!=self.root and self.root not in hint.parents: raise ValueError('sync --path must be inside the library.')
        with writer(self.state):
            self._initialize(); cat=self._catalog(); bootstrapped=self._bootstrap(cat)
            inventory=self._inventory()  # Complete scan must succeed before missing-file tombstones.
            bypath={f['path']:f for f in cat['files'].values()}
            missing=[f for f in cat['files'].values() if f['path'] not in inventory]
            changed=bootstrapped; extracted=0; errors=[]
            from .extract import extract_file
            import zlib
            for source,stat in inventory.items():
                f=bypath.get(source)
                same_source=bool(f and f.get('size')==stat['size'] and f.get('mtime_ns')==stat['mtime_ns'] and f.get('sha256'))
                cache_present=bool(f and f.get('cache') and (self.state/'extractions'/f['cache']).is_file())
                if same_source and cache_present and f.get('state')=='ready' and f.get('extract_version')==EXTRACT_VERSION:
                    if not f.get('available'): changed=True
                    f['available']=True; continue
                sha=f['sha256'] if same_source else digest(source)
                if not f:
                    matches=[m for m in missing if m.get('sha256')==sha]
                    if len(matches)==1:
                        f=matches[0]; missing.remove(f); f['previous_paths'].append(f['path']); f['path']=source
                    else:
                        folder=str(Path(source).parent)
                        w=next((w for w in cat['works'].values() if w.get('folder')==folder),None)
                        if w is None:
                            candidates={x['work_id'] for x in cat['files'].values() if str(Path(x['path']).parent)==folder}
                            if len(candidates)==1:w=cat['works'][candidates.pop()]
                        if not w:
                            wid=uid('work'); w={'id':wid,'edition_id':uid('edition'),'volume_id':uid('volume'),'title':Path(source).stem,'author':'Unknown','contributors':[],'folder':folder,'metadata':{},'profile':{},'metadata_pending':True,'created_at':now()}; cat['works'][wid]=w
                        fid=uid('file'); f={'id':fid,'work_id':w['id'],'edition_id':w['edition_id'],'path':source,'previous_paths':[],'versions':[],'sequence':sum(x['work_id']==w['id'] for x in cat['files'].values()),'catalog_metadata':{}}
                        cat['files'][fid]=f
                changed=True; cache=self.state/'extractions'/(sha+'-v'+EXTRACT_VERSION+'.json.gz')
                f.update(stat,available=True,sha256=sha,extract_version=EXTRACT_VERSION)
                if not any(v['sha256']==sha for v in f['versions']): f['versions'].append({'sha256':sha,'size':stat['size'],'path':source,'seen_at':now()})
                try:
                    if not cache.exists():
                        data=extract_file(Path(source)); tmp=cache.with_suffix('.tmp')
                        with gzip.open(tmp,'wt',encoding='utf-8') as out: json.dump(data,out,ensure_ascii=False)
                        os.replace(tmp,cache); extracted+=1
                    f.update(state='ready',cache=cache.name,error=None)
                except Exception as exc:
                    f.update(state='failed',error=f'{type(exc).__name__}: {exc}'); errors.append({'path':source,'error':f['error']})
            for f in missing:
                if f.get('available'): changed=True
                f['available']=False; f['state']='missing'
            for w in cat['works'].values():
                parents={str(Path(f['path']).parent) for f in cat['files'].values() if f['work_id']==w['id'] and f.get('available')}
                if len(parents)==1 and w.get('folder') not in parents:
                    new_folder=parents.pop();w.setdefault('previous_folders',[]).append(w.get('folder'));w['folder']=new_folder
            cat['updated_at']=now(); atomic_json(self.state/'catalog.json',cat)
            current=self._active()
            from .structure import ANALYZER_VERSION
            curation_hash=digest(self.state/'curation.jsonl') if (self.state/'curation.jsonl').exists() else ''
            source_signature=catalog_signature(cat)
            if not changed and not rebuild and current and current.get('index_version')==INDEX_VERSION and current.get('structure_version')==ANALYZER_VERSION and current.get('curation_hash')==curation_hash and current.get('source_signature')==source_signature:
                from .semantic_library import refresh_acquisition_statuses
                if refresh_acquisition_statuses(self,cat):
                    atomic_json(self.state/'catalog.json',cat)
                return {'changed':False,'extracted':0,'generation':current['generation'],'seconds':round(time.perf_counter()-start,3),'errors':errors}
            # Extraction caches are disposable. Before publishing a new generation,
            # repair missing/corrupt derived data from its intact source. A damaged
            # cache must not disable healthy books or require another download.
            for f in cat['files'].values():
                if not f.get('available') or f.get('state')!='ready': continue
                cache=self.state/'extractions'/f['cache']
                try:
                    with gzip.open(cache,'rt',encoding='utf-8') as inp: cached=json.load(inp)
                    if not isinstance(cached,dict) or not isinstance(cached.get('blocks'),list):
                        raise ValueError('Invalid extraction cache structure')
                    if any(not isinstance(b,dict) or b.get('ordinal')!=i or not isinstance(b.get('kind'),str) or not isinstance(b.get('text'),str) or not isinstance(b.get('locator'),dict) for i,b in enumerate(cached['blocks'])):
                        raise ValueError('Invalid extraction cache block')
                except (OSError,EOFError,ValueError,TypeError,zlib.error):
                    try:
                        cached=extract_file(Path(f['path']));tmp=cache.with_suffix('.repair.tmp')
                        with gzip.open(tmp,'wt',encoding='utf-8') as out: json.dump(cached,out,ensure_ascii=False)
                        os.replace(tmp,cache);extracted+=1
                        f['error']=None
                    except Exception as exc:
                        f.update(state='failed',error=f'{type(exc).__name__}: {exc}')
                        errors.append({'path':f['path'],'error':f['error']})
            source_signature=catalog_signature(cat)
            atomic_json(self.state/'catalog.json',cat)
            gen=uid('gen'); dbfile=self.state/'generations'/(gen+'.sqlite3'); db=sqlite3.connect(dbfile)
            try:
                from .passage_history import snapshot
                snapshot(self)
                from .structure import STRUCTURE_SCHEMA
                db.executescript(SCHEMA)
                db.executescript(STRUCTURE_SCHEMA)
                self._prepare_section_registry()
                works,edits=self._effective(cat)
                for workid,w in works.items():
                    files=sorted([f for f in cat['files'].values() if f['work_id']==workid],key=lambda f:(f['sequence'],f['path']))
                    w['files']=files; w['available']=any(f['available'] and f['state']=='ready' for f in files)
                    self._index_work(db,w,files,edits)
                    db.execute('INSERT INTO works VALUES(?,?,?,?)',(workid,w['title'],w['author'],compact(w)))
                db.execute("INSERT INTO passage_fts(passage_fts) VALUES('rebuild')")
                db.execute('INSERT INTO metadata VALUES(?,?)',('generation',gen)); db.commit()
                db.execute('PRAGMA optimize'); db.close()
                atomic_json(self.state/'section-identities.json',self._section_registry)
                atomic_json(self.state/'current.json',{'generation':gen,'database':dbfile.name,'built_at':now(),'curation_hash':curation_hash,'source_signature':source_signature,'extract_version':EXTRACT_VERSION,'index_version':INDEX_VERSION,'structure_version':ANALYZER_VERSION})
            except BaseException:
                db.close(); dbfile.unlink(missing_ok=True); raise
            from .semantic_library import refresh_acquisition_statuses
            refresh_acquisition_statuses(self,cat)
            atomic_json(self.state/'catalog.json',cat)
            # Retain previous generation for recovery. In-flight readers retain an open file descriptor.
            generations=sorted((self.state/'generations').glob('*.sqlite3'),key=lambda p:p.stat().st_mtime,reverse=True)
            for obsolete in generations[2:]: obsolete.unlink(missing_ok=True)
            return {'changed':True,'extracted':extracted,'generation':gen,'seconds':round(time.perf_counter()-start,3),'errors':errors}

    @staticmethod
    def _section_evidence(file,block,following=''):
        locator=block.get('locator',{}); member=locator.get('member','')
        anchor=locator.get('bookmark') or locator.get('element_id')
        if isinstance(anchor,list): anchor=anchor[0] if anchor else None
        native=stable('anchor',file['id'],member,anchor) if anchor else None
        signature=stable('section_signature',file['id'],block['text'].strip(),following.strip()[:1000])
        return native,signature

    def _prepare_section_registry(self):
        self._section_registry=read_json(self.state/'section-identities.json',{'schema_version':1,'records':{}})
        self._section_seen=set();self._section_lookup=collections.defaultdict(list)
        records=self._section_registry['records']
        # Migrate old derived IDs only when the old index supplies the same native
        # anchor or heading-plus-following-text evidence. Never match by occurrence.
        if not records and self._active():
            with self._db() as old:
                for section in old.execute('SELECT id,work_id,title FROM sections'):
                    blocks=old.execute('SELECT data FROM blocks WHERE section_id=? ORDER BY ordinal LIMIT 8',(section['id'],)).fetchall()
                    blocks=[json.loads(b['data']) for b in blocks]
                    blocks=[b for b in blocks if not b.get('is_boilerplate')]
                    first=blocks[0] if blocks else None
                    if section['title']=='Front matter':
                        native='front:'+section['work_id'];signature=native
                    elif first and first['kind']=='heading':
                        following=next((b['text'] for b in blocks[1:] if b['kind']!='heading' and b['text'].strip()),'')
                        native,signature=self._section_evidence({'id':first['file_id']},first,following)
                    else: continue
                    records[section['id']]={'id':section['id'],'work_id':section['work_id'],'anchors':[native] if native else [],'signatures':[signature],'created_at':now(),'origin':'verified_index_migration','title':section['title'][:500]}
        for record in records.values():
            for key in record.get('anchors',[])+record.get('signatures',[]):
                self._section_lookup[(record['work_id'],key)].append(record['id'])

    def _section_identity(self,workid,title,file=None,block=None,following='',ambiguous=False):
        if block is None: native='front:'+workid;signature=native
        else: native,signature=self._section_evidence(file,block,following)
        if ambiguous:
            # Identical repeated headings and content cannot safely inherit an edit
            # after source bytes change. Retain their identity only within that version.
            native=None;signature=stable('ambiguous_section',file['id'],file['sha256'],block['ordinal'])
        choices=[]
        for key in [native,signature]:
            if key:
                choices=self._section_lookup.get((workid,key),[])
                if len(choices)==1 and choices[0] not in self._section_seen: break
                choices=[]
        sid=choices[0] if choices else uid('section');self._section_seen.add(sid)
        records=self._section_registry['records']
        record=records.setdefault(sid,{'id':sid,'work_id':workid,'anchors':[],'signatures':[],'created_at':now(),'origin':'source_anchor' if native else 'content_anchor'})
        for field,key in [('anchors',native),('signatures',signature)]:
            if key and key not in record[field]: record[field].append(key)
            if key and sid not in self._section_lookup[(workid,key)]: self._section_lookup[(workid,key)].append(sid)
        record.update(title=title[:500],identity_status='ambiguous_version_only' if ambiguous else 'source_anchored')
        if block: record.update(file_id=file['id'],source_locator=block.get('locator',{}))
        return sid

    def _index_work(self,db,w,files,edits):
        workid=w['id']; ordinal=0; section_no=0; passage_no=0; section=None; buffer=[]; occurrences=collections.Counter()
        # Feasting prints contributors at the end of each essay and gathers
        # several essays' notes afterward. Analyze the ordered work, including
        # split-file seams, before assigning section and note ownership.
        from .feasting import analyze_feasting, block_key
        from .structure import prepare_work, index_work_structure
        extractions={}
        for source_file in files:
            if source_file.get('available') and source_file.get('state')=='ready':
                with gzip.open(self.state/'extractions'/source_file['cache'],'rt',encoding='utf-8') as inp:
                    extractions[source_file['id']]=json.load(inp)
        structure_report=prepare_work(self,w,files,extractions)
        credits=analyze_feasting(w,[(f,extractions[f['id']]['blocks'] if f['id'] in extractions else None) for f in files])
        from .native_notes import analyze_native_notes
        credits['notes']={**analyze_native_notes(w,files,extractions),**credits['notes']}
        source_sections={}; unowned_notes_section=None
        if credits.get('stats'):w['contributor_detection']=credits['stats']
        author_roles=[c.get('role') for c in w.get('contributors',[])]
        base_attr=('Contributor unverified; edited by '+w['author']) if 'editor' in author_roles else (w['author']+' (translator and commentator; underlying text has separate authorship)' if 'translator' in author_roles else w['author'])
        def begin(title,file=None,block=None,following='',ambiguous=False):
            nonlocal section,section_no
            sid=self._section_identity(workid,title,file,block,following,ambiguous); section_no+=1
            section={'id':sid,'title':title[:500]+('…' if len(title)>500 else ''),'attribution':base_attr,'profile':{},'identity_status':'ambiguous_version_only' if ambiguous else 'source_anchored'}
            source_key=block_key(file['id'],block['ordinal']) if file is not None and block is not None else None
            credit=credits['contributions'].get(source_key)
            if credit:
                section.update(attribution=credit['author'],heading_path=credit['heading_path'],source_bibliography=credit['source_bibliography'],attribution_evidence=credit['evidence'])
            for field in ('attribution','profile','bibliography','title','text_type'):
                e=edits.get((sid,field))
                if e:
                    section[field]=e['value']
                    if field in ('attribution','bibliography'):
                        section[field+'_status']=e.get('status','provisional')
                    if field=='bibliography':
                        author_event=e.get('field_events',{}).get('authors',e)
                        section['bibliography_authors_status']=author_event.get('status','provisional')
                        section['bibliography_authors_evidence']=author_event.get('evidence','')
            if not edits.get((sid,'attribution')):
                bibliography_event=edits.get((sid,'bibliography'),{})
                if section.get('bibliography',{}).get('type')=='scripture':
                    from .passage_curation import scripture_credit
                    section['scripture_credit']=scripture_credit(section['bibliography'],bibliography_event)
                    section['attribution']=section['scripture_credit']['attribution']
                elif section.get('bibliography',{}).get('authors'):
                    from .citations import _display
                    section['attribution']='; '.join(_display(person) for person in section['bibliography']['authors'])
            db.execute('INSERT INTO sections VALUES(?,?,?,?,?)',(sid,workid,section['title'],section_no,compact(section)))
            if source_key:source_sections[source_key]=section
            self._facets(db,sid,workid,section.get('profile',{}),edits.get((sid,'profile')))
        def flush():
            nonlocal buffer,passage_no
            if not buffer: return
            text='\n\n'.join(b['text'] for b in buffer); passage_no+=1
            pid=stable('passage',buffer[0]['id'],buffer[-1]['id'])
            info={'file_ids':list(dict.fromkeys(b['file_id'] for b in buffer)),'source_paths':list(dict.fromkeys(b['path'] for b in buffer)), 'location':buffer[0]['location'],'limitations':[],'kind':buffer[0]['kind']}
            if len(info['file_ids'])>1: info['location']+=' → '+buffer[-1]['location']
            meta=w.get('metadata',{})
            for k in ('source_quality_note','catalog_discrepancy','source_scope_note'):
                if meta.get(k): info['limitations'].append(str(meta[k]))
            if section['attribution'].startswith('Contributor unverified'): info['limitations'].append('Section contributor requires verification; the named people edit the volume.')
            if section.get('identity_status')=='ambiguous_version_only':info['limitations'].append('Repeated section has ambiguous identity; editorial corrections do not automatically transfer to changed source versions.')
            if section.get('continuity_warning'):info['limitations'].append(section['continuity_warning'])
            if any(b.get('extraction_warnings') for b in buffer): info['limitations'].append('Extraction has warnings; inspect source context.')
            from .passage_curation import passage_credit, credit_limitations
            credit=passage_credit(pid,edits)
            attribution=credit['attribution'] if credit else section['attribution']
            info['limitations']=credit_limitations(info['limitations'],credit or section.get('scripture_credit'))
            type_event=edits.get((pid,'text_type')) or edits.get((section['id'],'text_type'))
            bibliography_event=edits.get((pid,'bibliography')) or edits.get((section['id'],'bibliography'),{})
            bibliography=bibliography_event.get('value',{})
            scripture_classification=None
            if bibliography.get('type')=='scripture':
                from .passage_curation import scripture_credit
                scripture_classification=scripture_credit(bibliography,bibliography_event)
            info['text_type']=type_event['value'] if type_event else ('scripture' if bibliography.get('type')=='scripture' else 'unknown')
            info['text_type_status']=type_event.get('status') if type_event else ('confirmed' if scripture_classification and scripture_classification['verified'] else 'unknown')
            if type_event and (type_event.get('status')!='confirmed' or not type_event.get('evidence')):
                info['limitations'].append('Text type is provisional or disputed; inspect the source before relying on it.')
            from .passage_segments import segments_for_passage, segment_summary, segment_conflict
            segments=segments_for_passage(pid,edits,buffer)
            if conflict:=segment_conflict(pid,edits):info['limitations'].append(conflict)
            if segments:
                summary=segment_summary(segments);info.update(summary)
                attribution=summary['attribution'];info['attribution_scope']='segments'
                segment_event=segments[0]['event'];info['text_type_status']=segment_event.get('status','provisional')
                info['limitations']=credit_limitations(info['limitations'],{'attribution':attribution,'event':segment_event})
                if any(s['text_type']=='unknown' or 'unverified' in s['attribution'].lower() for s in segments):
                    info['limitations'].append('A described portion still has unresolved text type or attribution; consult its source notes.')
                info['limitations'].append('This passage contains separately credited portions. Use its component citations for the portion quoted.')
                for segment in segments:
                    for block in segment['blocks']:
                        block.update(attribution=segment['attribution'],attribution_scope='segment',text_type=segment['text_type'],segment_id=segment['segment_id'],passage_id=pid)
                        db.execute('UPDATE blocks SET data=? WHERE id=?',(compact(block),block['id']))
            if not segments:
                if credit:info['attribution_scope']='passage'
                for block in buffer:
                    block['text_type']=info['text_type']
                    if credit:
                        block['attribution']=attribution;block['attribution_scope']='passage';block['text_type']=info['text_type']
                    db.execute('UPDATE blocks SET data=? WHERE id=?',(compact(block),block['id']))
            if (pid,'notes') in edits:info['notes']=edits[(pid,'notes')]['value']
            boiler=int(all(b.get('is_boilerplate') or not b['text'].strip() for b in buffer))
            db.execute('INSERT INTO passages VALUES(?,?,?,?,?,?,?,?,?,?,?)',(pid,workid,section['id'],passage_no,buffer[0]['global_ordinal'],buffer[-1]['global_ordinal'],text,section['title'],str(attribution),boiler,compact(info)))
            buffer=[]
        begin('Front matter')
        source_sections['front:'+workid]=section
        self._facets(db,workid,workid,w.get('profile',{}),edits.get((workid,'profile')))
        continuity_gap=False
        for f in files:
            if not f.get('available') or f.get('state')!='ready':
                flush();continuity_gap=True
                continue
            if f['id'] in extractions:
                extraction=extractions[f['id']]
            else:
                with gzip.open(self.state/'extractions'/f['cache'],'rt',encoding='utf-8') as inp:
                    extraction=json.load(inp)
            f['extraction_warnings']=extraction.get('warnings',[]);f['block_count']=len(extraction['blocks']);f['illustration_count']=len(extraction.get('images',[]))
            source_blocks=extraction['blocks']; heading_evidence={}; evidence_counts=collections.Counter()
            if not any(b['text'].strip() and not b.get('is_boilerplate') for b in source_blocks):
                flush();continuity_gap=True
            # A verified Word-part seam may end in a bare pericope heading. The
            # next part continues its scripture text; retain that logical identity.
            if w['title'].startswith('Feasting on the Gospels') and source_blocks:
                last=next((b for b in reversed(source_blocks) if b['text'].strip() and not b.get('is_boilerplate')),None)
                if last and last['kind']=='paragraph' and re.fullmatch(r'(?:Matthew|Mark|Luke|John) \d+:\d+[–-]\d+',last['text'].strip()):
                    last['kind']='heading';last['level']=1;last['heading_path']=[last['text'].strip()]
                    last['heading_evidence']={'origin':'split_part_boundary','status':'provisional','evidence':'Standalone Gospel pericope reference at a Word-part boundary; original paragraph is retained in extraction cache.'}

            for i,b in enumerate(source_blocks):
                if b['kind']=='heading' and b['text'].strip() and not b.get('is_boilerplate'):
                    following=''
                    for candidate_i in range(i+1,len(source_blocks)):
                        candidate=source_blocks[candidate_i]
                        if candidate['kind']=='heading' and not candidate.get('is_boilerplate'): break
                        if candidate['text'].strip() and not candidate.get('is_boilerplate'):
                            following=candidate['text'];break
                    native,signature=self._section_evidence(f,b,following)
                    heading_evidence[b['ordinal']]=(following,native or signature);evidence_counts[native or signature]+=1
            for b in source_blocks:
                # Heading boundaries are logical; a Word-part boundary is not.
                if b['kind']=='heading' and b['text'].strip() and not b.get('is_boilerplate'):
                    following,key=heading_evidence[b['ordinal']]
                    flush(); begin(b['text'].strip(),f,b,following,evidence_counts[key]>1)
                    continuity_gap=False
                elif continuity_gap and b['text'].strip() and not b.get('is_boilerplate'):
                    flush();begin('Continuation after unavailable source part',f,b)
                    section['continuity_warning']='An unavailable or unreadable source part interrupts the section boundary; contributor continuity requires verification.'
                    db.execute('UPDATE sections SET data=? WHERE id=?',(compact(section),section['id']))
                    continuity_gap=False
                normal_section=section
                note=credits['notes'].get(block_key(f['id'],b['ordinal']))
                if note:
                    flush()
                    owner_section=source_sections.get(note.get('owner'))
                    if owner_section is None:
                        if unowned_notes_section is None:
                            begin('Source notes — contributor unresolved',f,b)
                            section['attribution']='Contributor unverified'
                            db.execute('UPDATE sections SET data=? WHERE id=?',(compact(section),section['id']))
                            unowned_notes_section=section
                        section=unowned_notes_section
                    else:
                        section=owner_section
                    b={**b,'kind':'note','source_heading_path':b.get('heading_path',[]),
                       'heading_path':section.get('heading_path',[]),'note_ownership':note}
                ordinal+=1; bid=stable('block',f['id'],f['sha256'],b['ordinal'])
                rec={**b,'id':bid,'file_id':f['id'],'path':f['path'],'location':location(b,f),'global_ordinal':ordinal,'attribution':b.get('attribution') or section['attribution']}
                if extraction.get('warnings'): rec['extraction_warnings']=extraction['warnings']
                db.execute('INSERT INTO blocks VALUES(?,?,?,?,?,?,?)',(bid,workid,section['id'],ordinal,f['id'],b['text'],compact(rec)))
                if not b['text'].strip():
                    if note:section=normal_section
                    continue
                if buffer and (buffer[-1].get('is_boilerplate')!=b.get('is_boilerplate') or b['kind']=='note'): flush()
                buffer.append(rec)
                if sum(len(x['text']) for x in buffer)>=1800 or b['kind'] in ('table','note'): flush()
                if note:section=normal_section
        flush()
        index_work_structure(db,w,files,structure_report)

    def _facets(self,db,target,work,profile,event):
        for facet,values in profile.items():
            if facet not in FACETS: continue
            if isinstance(values,str): values=[values]
            provenance=(event or {}).get('field_events',{}).get(facet,event or {})
            for v in values:
                db.execute('INSERT INTO facets VALUES(?,?,?,?,?,?,?)',(target,work,facet,str(v),provenance.get('origin','catalog'),provenance.get('status','provisional'),provenance.get('evidence','')))

    def _freshness(self,cat):
        inventory=self._inventory(); known={f['path']:f for f in cat['files'].values()}
        added=[p for p in inventory if p not in known]
        changed=[p for p,s in inventory.items() if p in known and (s['size']!=known[p].get('size') or s['mtime_ns']!=known[p].get('mtime_ns') or known[p].get('state')!='ready')]
        removed=[p for p,f in known.items() if f.get('available') and p not in inventory]
        return {'fresh':not(added or changed or removed),'added':added[:50],'changed':changed[:50],'removed':removed[:50],'counts':{'added':len(added),'changed':len(changed),'removed':len(removed)},'method':'directory entries and stat; no source content opened'}

    def status(self):
        cat=self._catalog(); works,edits=self._effective(cat); current=self._active()
        result={'root':str(self.root),'state':str(self.state),'generation':current.get('generation'),'built_at':current.get('built_at'),'books':len(works),'files':len(cat['files']),'ready_files':sum(f.get('available') and f.get('state')=='ready' for f in cat['files'].values()),'metadata_pending':sum(w.get('metadata_pending',True) for w in works.values()),'failures':[{'path':f['path'],'error':f.get('error')} for f in cat['files'].values() if f.get('state')=='failed'],'missing_files':[f['path'] for f in cat['files'].values() if not f.get('available')],'freshness':self._freshness(cat),'semantic':read_json(self.state/'semantic.json',{'state':'awaiting_pilot_review','coverage_books':0,'engine':None})}
        from .budget import Budget
        result['budget']=Budget(self.state).report()
        from .public_config import settings_status
        result['settings']=settings_status(self)
        from .semantic_library import status as semantic_status
        result['semantic']=semantic_status(self)
        from .citation_status import metadata_coverage
        result['citations']=metadata_coverage(works,edits)
        source_signature=catalog_signature(cat)
        from .structure import ANALYZER_VERSION
        result['index_pending']=bool(current and (current.get('source_signature')!=source_signature or current.get('index_version')!=INDEX_VERSION or current.get('structure_version')!=ANALYZER_VERSION))
        result['extraction_warnings']=[{'path':f['path'],'warnings':f.get('extraction_warnings',[])} for f in cat['files'].values() if f.get('extraction_warnings')]
        result['curation_pending']=(digest(self.state/'curation.jsonl') if (self.state/'curation.jsonl').exists() else '')!=current.get('curation_hash','')
        if current:
            with self._db() as db:
                result['passages']=db.execute('SELECT count(*) FROM passages WHERE boilerplate=0').fetchone()[0]
                result['sections']=db.execute('SELECT count(*) FROM sections').fetchone()[0]
                active_sections={r[0] for r in db.execute('SELECT id FROM sections')}
                curated_targets={e['target_id'] for e in self._curation()}
                passage_targets={target for target in curated_targets if target.startswith('passage_')}
                active_passages={r[0] for r in db.execute('SELECT id FROM passages')} if passage_targets else set()
                orphaned={target for target in curated_targets if (target.startswith('section_') and target not in active_sections) or (target.startswith('passage_') and target not in active_passages)}
                from .passage_history import resolve
                retained={target for target in orphaned if target.startswith('passage_') and resolve(self,target,db)}
                orphaned-=retained
                result['historical_passage_curation_count']=len(retained)
                result['orphaned_curation_count']=len(orphaned);result['orphaned_curation']=sorted(orphaned)[:50]
                result['orphaned_passage_curation_count']=len(orphaned & passage_targets)
                reports=[{'work_id':r['id'],'title':r['title'],**json.loads(r['data']).get('structure',{'state':'not_analyzed'})} for r in db.execute('SELECT id,title,data FROM works')]
                result['structure']={'books':len(reports),'supported':sum(r.get('state')=='supported' for r in reports),
                                     'needs_review':sum(r.get('state')=='needs_review' for r in reports),'reports':reports,
                                     'review_books':[r for r in reports if r.get('state')=='needs_review']}
        return result

    def focuses(self):
        return [read_json(p) for p in sorted((self.state/'focuses').glob('*.json'))]

    def save_focus(self,id,data):
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}',id): raise ValueError('Focus ID must use lowercase letters, digits, and hyphens.')
        if data.get('breadth','balanced') not in ('focused','balanced','exploratory'): raise ValueError('Unknown breadth.')
        for field in ('subjects','forms','books','related_subjects'):
            if field in data and (not isinstance(data[field],list) or any(not isinstance(x,str) for x in data[field])): raise ValueError(field+' must be a list of strings.')
        with writer(self.state):
            self._initialize(); record={**data,'id':id,'updated_at':now()}; atomic_json(self.state/'focuses'/(id+'.json'),record)
        return record

    def _validated_annotation(self,target_id,data):
        field=data.get('field')
        if field not in {'profile','bibliography','title','author','attribution','contributors','coverage','notes','text_type','segments'}: raise ValueError('Unsupported annotation field.')
        if data.get('origin','user') not in ('user','codex','import'): raise ValueError('Unknown origin.')
        if data.get('status','provisional') not in ('provisional','confirmed','disputed'): raise ValueError('Unknown annotation status.')
        if not data.get('evidence'): raise ValueError('Include source evidence or an explicit editorial rationale.')
        if field=='profile':
            if not isinstance(data.get('value'),dict): raise ValueError('Profile must be an object.')
            for facet,values in data['value'].items():
                if facet not in FACETS or not isinstance(values,list) or any(not isinstance(v,str) for v in values): raise ValueError('Profile facets must be known facet names with lists of strings.')
        value=data.get('value')
        if field=='text_type':
            from .passage_segments import validate_text_type
            validate_text_type(value)
        if field=='bibliography':
            from .citations import validate_bibliography
            validate_bibliography(value)
        if field in ('title','author','attribution') and (not isinstance(value,str) or not value.strip() or len(value)>2000): raise ValueError(field+' must be a nonempty string of at most 2000 characters.')
        if field=='notes' and (not isinstance(value,str) or len(value)>20000): raise ValueError('Notes must be text of at most 20000 characters.')
        if field=='contributors' and (not isinstance(value,list) or len(value)>200 or any(not isinstance(c,dict) or not isinstance(c.get('name'),str) or not c['name'].strip() or not isinstance(c.get('role'),str) or not c['role'].strip() for c in value)): raise ValueError('Contributors must be a list of named people with roles.')
        if field=='coverage' and not isinstance(value,(list,dict)): raise ValueError('Coverage must be a list or object with source evidence.')
        exists=target_id in self._catalog()['works']
        if exists and field=='text_type':
            raise ValueError('Text type belongs to a section or passage, not a whole book that may mix kinds of content.')
        is_passage=False
        if not exists:
            with self._db() as db:
                exists=bool(db.execute('SELECT 1 FROM sections WHERE id=?',(target_id,)).fetchone())
                if not exists:
                    is_passage=bool(db.execute('SELECT 1 FROM passages WHERE id=?',(target_id,)).fetchone());exists=is_passage
        if not exists: raise ValueError('Unknown work, section, or passage ID.')
        if is_passage and field not in {'bibliography','attribution','notes','text_type','segments'}:
            raise ValueError('Passage annotations support only bibliography, attribution, notes, text_type, and segments.')
        if field=='segments':
            if not is_passage: raise ValueError('Source segments belong to a passage, not a whole book or section.')
            from .passage_segments import validate_segments
            with self._db() as db:
                p=db.execute('SELECT * FROM passages WHERE id=?',(target_id,)).fetchone()
                rows=db.execute('SELECT data FROM blocks WHERE work_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',(p['work_id'],p['first_ordinal'],p['last_ordinal'])).fetchall()
            validate_segments(value,rows)
        rec={**data,'id':uid('edit'),'target_id':target_id,'origin':data.get('origin','user'),'status':data.get('status','provisional'),'created_at':now()}
        return rec

    def annotate(self,target_id,data):
        rec=self._validated_annotation(target_id,data)
        with writer(self.state): append_jsonl(self.state/'curation.jsonl',rec)
        return {**rec,'index_refresh_required':True}

    def intake(self,path,context=None,*,review_metadata=None,local_only=False):
        from .intake import enqueue_intake
        return enqueue_intake(self,path,context,review_metadata=review_metadata,local_only=local_only)

    def review_intake(self,intake_id,*,expected_version=None):
        from .intake import authorize_review
        return authorize_review(self,intake_id,expected_version=expected_version)

    def intake_status(self,intake_id=None):
        from .intake import intake_status
        return intake_status(self,intake_id)

    def pause_intake(self,intake_id,*,expected_version=None):
        from .intake import pause_intake
        return pause_intake(self,intake_id,expected_version=expected_version)

    def resume_intake(self,intake_id,*,expected_version=None):
        from .intake import resume_intake
        return resume_intake(self,intake_id,expected_version=expected_version)

    def undo_intake(self,intake_id,*,event_id=None,expected_version=None):
        from .intake import undo_intake
        return undo_intake(self,intake_id,event_id=event_id,expected_version=expected_version)

    def review_context(self,target_id):
        from .review import review_context
        return review_context(self,target_id)

    def review_draft(self,target_id):
        from .review import load_draft
        return load_draft(self,target_id)

    def save_review_draft(self,target_id,changes,*,revision,expected_version=0):
        from .review import save_draft
        return save_draft(self,target_id,changes,revision=revision,expected_version=expected_version)

    def decide_review(self,target_id,item_id,action,*,revision,expected_version,replace=False,edited_change=None):
        from .review import decide_review
        return decide_review(self,target_id,item_id,action,revision=revision,expected_version=expected_version,replace=replace,edited_change=edited_change)

    def review_recommendations(self,target_id):
        from .review_guidance import recommendations
        return recommendations(self.review_context(target_id),self.review_draft(target_id))

    def decide_recommendation(self,target_id,recommendation_id,action,*,revision,expected_version,selected_item_ids=None,replace=False):
        from .review import decide_recommendation
        return decide_recommendation(self,target_id,recommendation_id,action,revision=revision,expected_version=expected_version,
                                     selected_item_ids=selected_item_ids,replace=replace)

    def review_recommendation_preview(self,target_id,recommendation_id,selected_item_ids=None):
        from .review_guidance import recommendations,preview_recommendation
        context,draft=self.review_context(target_id),self.review_draft(target_id)
        recommendation=next((item for item in recommendations(context,draft) if item['id']==recommendation_id),None)
        if recommendation is None:
            raise ValueError('This recommendation is no longer available.')
        return preview_recommendation(self,context,draft,recommendation,selected_item_ids)

    def discard_review_draft(self,target_id,*,expected_version):
        from .review import discard_draft
        return discard_draft(self,target_id,expected_version=expected_version)

    def rebase_review_draft(self,target_id,*,expected_version):
        from .review import rebase_draft
        return rebase_draft(self,target_id,expected_version=expected_version)

    def store_review_suggestions(self,target_id,result,*,revision):
        from .review import store_suggestions
        return store_suggestions(self,target_id,result,revision=revision)

    def apply_review(self,target_id,selected_fields,*,revision,expected_version,request_id,changes=None):
        from .review import apply_review
        return apply_review(self,target_id,selected_fields,revision=revision,expected_version=expected_version,request_id=request_id,changes=changes)

    def books(self,query='',facet=None,limit=50,offset=0):
        limit=max(1,min(int(limit),100)); offset=max(0,int(offset))
        return self._catalog_books(query,facet)[offset:offset+limit]

    def browse_books(self,query='',facet=None,sort='title',limit=50,offset=0):
        from .catalog import page
        return page(self._catalog_books(query,facet,structured_creators=True),sort=sort,limit=limit,offset=offset)

    def _catalog_books(self,query='',facet=None,*,structured_creators=False):
        with self._db() as db:
            rows=db.execute('SELECT data FROM works ORDER BY title').fetchall()
        books=[json.loads(r['data']) for r in rows]
        if structured_creators:
            # Match form suggestions, which expose saved book bibliography
            # corrections immediately without requiring a source/index rebuild.
            _,edits=self._effective({'works':{}})
            for book in books:
                correction=edits.get((book['id'],'bibliography'),{}).get('value',{})
                if correction: book['bibliography']={**book.get('bibliography',{}),**correction}
        if query:
            from .catalog import creator_text
            books=[b for b in books if norm(query) in norm(b['title']+' '+(creator_text(b) if structured_creators else (b.get('author') or '')))]
        if facet:
            if isinstance(facet,str):facet={'subject':facet}
            eligible={b['id'] for b in books}
            with self._db() as db:
                for key,value in facet.items():
                    if not value:continue
                    if key in ('subject','form'):
                        matches={r[0] for r in db.execute('SELECT DISTINCT work_id FROM facets WHERE facet=? AND value=? COLLATE NOCASE',(key+'s',value))}
                    elif key=='author':
                        matches={b['id'] for b in books if norm(value) in norm(b['author']) or any(norm(value) in norm(c.get('name','')) for c in b.get('contributors',[]))}
                        matches.update(r['work_id'] for r in db.execute('SELECT DISTINCT work_id,attribution FROM passages') if norm(value) in norm(r['attribution']))
                    else:matches={b['id'] for b in books if self._filter_work(b,{key:value})}
                    eligible.intersection_update(matches)
            books=[b for b in books if b['id'] in eligible]
        return books

    def book(self,id,section_offset=0,section_limit=100):
        section_offset=max(0,int(section_offset)); section_limit=max(1,min(int(section_limit),200))
        with self._db() as db:
            row=db.execute('SELECT data FROM works WHERE id=?',(id,)).fetchone()
            if not row: raise ValueError('Unknown book.')
            w=json.loads(row['data'])
            w['sections']=[dict(r) for r in db.execute('SELECT s.id,s.title,(SELECT id FROM passages WHERE section_id=s.id AND boilerplate=0 ORDER BY ordinal LIMIT 1) passage_id,(SELECT count(*) FROM passages WHERE section_id=s.id AND boilerplate=0) count FROM sections s WHERE work_id=? ORDER BY ordinal LIMIT ? OFFSET ?',(id,section_limit,section_offset))]
            w['sections_total']=db.execute('SELECT count(*) FROM sections WHERE work_id=?',(id,)).fetchone()[0]
            w['next_section_offset']=section_offset+section_limit if section_offset+section_limit<w['sections_total'] else None
            w['previous_section_offset']=max(0,section_offset-section_limit) if section_offset else None
        return w

    def _filter_work(self,w,filters):
        for k,v in (filters or {}).items():
            if not v: continue
            if k=='book' and v!=w['id'] and norm(v) not in norm(w['title']): return False
            if k=='author' and norm(v) not in norm(w['author']): return False
            if k in ('subject','form') and norm(v) not in [norm(t) for t in w.get('profile',{}).get(k+'s',[])]: return False
        return True

    def _hit(self,row,w):
        data=json.loads(row['data']); text=row['text']
        return {'id':row['id'],'work_id':row['work_id'],'section_id':row['section_id'],'title':w['title'],'edition':w.get('metadata',{}).get('edition'),'attribution':row['attribution'],'attribution_scope':data.get('attribution_scope','section'),'location':data['location'][:2000],'text':text[:700]+('…' if len(text)>700 else ''),'reason':'Exact text match','limitations':data.get('limitations',[]),'source_paths':data['source_paths'],'heading':row['heading'][:500]+('…' if len(row['heading'])>500 else ''),'score':0,'text_type':data.get('text_type','unknown'),'text_types':data.get('text_types',[data.get('text_type','unknown')]),'text_type_status':data.get('text_type_status','unknown'),'segments':data.get('segments',[]),**({'notes':data['notes']} if 'notes' in data else {})}

    def search(self,query,focus=None,mode=None,filters=None,limit=12,connections=5,engine=None):
        if mode is not None and mode not in ('focused','balanced','exploratory'): raise ValueError('Unknown search mode.')
        if not query.strip() or len(query)>1500: raise ValueError('Enter a query of 1–1500 characters.')
        filters=filters or {}
        if any(k not in ('subject','form','book','author','scripture','text_type') for k in filters): raise ValueError('Unknown filter.')
        if filters.get('text_type'):
            from .passage_segments import validate_text_type
            validate_text_type(filters['text_type'])
        ts=list(dict.fromkeys(terms(query)))[:20]
        if not ts: ts=re.findall(r'\w+',norm(query))[:10]
        if not ts: return {'direct':[],'connections':[],'coverage':{},'warnings':[],'holdings_gap':'No searchable words.'}
        focusdata=next((f for f in self.focuses() if f['id']==focus),{})
        if focus and not focusdata: raise ValueError('Unknown focus: '+focus)
        mode=mode or focusdata.get('breadth','balanced')
        with self._db() as db:
            ws={r['id']:json.loads(r['data']) for r in db.execute('SELECT id,data FROM works')}
            eligibility=[k for k,w in ws.items() if self._filter_work(w,{k:v for k,v in filters.items() if k not in ('subject','form','author')})]
            section_profiles={}
            for r in db.execute('SELECT target_id,facet,value FROM facets'):
                section_profiles.setdefault(r['target_id'],{}).setdefault(r['facet'],[]).append(r['value'])
            if filters.get('author'):
                wanted=norm(filters['author']);author_matches=set()
                for r in db.execute('SELECT DISTINCT work_id,attribution FROM passages'):
                    if wanted in norm(r['attribution']): author_matches.add(r['work_id'])
                eligibility=[k for k in eligibility if wanted in norm(ws[k]['author']) or any(wanted in norm(c.get('name','')) for c in ws[k].get('contributors',[])) or k in author_matches]
            if filters.get('scripture'):
                eligibility=[k for k in eligibility if self._covers(ws[k],filters['scripture'])]
            exact=[]; candidates={}; connection_candidates={}; ranks=[]
            sql='SELECT p.*,bm25(passage_fts,1,2,1) rank FROM passage_fts JOIN passages p ON p.rowid=passage_fts.rowid WHERE passage_fts MATCH ? AND p.boilerplate=0'
            facet_args=[]
            if filters.get('text_type'):
                sql+=" AND (COALESCE(json_extract(p.data,'$.text_type'),'unknown')=? OR EXISTS (SELECT 1 FROM json_each(p.data,'$.text_types') WHERE value=?))"
                facet_args.extend([filters['text_type'],filters['text_type']])
            for key in ('subject','form'):
                if filters.get(key):
                    sql+=' AND EXISTS (SELECT 1 FROM facets f WHERE f.target_id IN (p.work_id,p.section_id) AND f.facet=? AND f.value=? COLLATE NOCASE)'
                    facet_args.extend([key+'s',filters[key]])
            if len(eligibility)!=len(ws):
                sql+=' AND p.work_id IN ('+','.join('?'*len(eligibility))+')'
            condition= '"'+query.strip()[1:-1].replace('"','""')+'"' if query.startswith('"') and query.endswith('"') else ' OR '.join('"'+t+'"' for t in ts)
            rows=db.execute(sql+' ORDER BY rank LIMIT 400',[condition]+facet_args+(eligibility if len(eligibility)!=len(ws) else [])).fetchall()
            for i,r in enumerate(rows):
                w=ws[r['work_id']]
                if not self._current_files(json.loads(r['data']),w): continue
                if filters.get('author') and norm(filters['author']) not in norm(r['attribution']) and norm(filters['author']) not in norm(w['author']): continue
                hit=self._hit(r,w); token_set=set(terms(r['text']+' '+r['heading']+' '+r['attribution'])); matched=[t for t in ts if t in token_set]
                hit.update(excerpt_range(r['text'],[query.strip('\"')]+matched))
                hit['matched_terms']=matched; hit['score']=1/(60+i+1)
                is_direct=len(matched)>=max(1, int(len(ts)*0.6+0.5))
                # Incidental mentions and catalog mislabels cannot establish primary commentary coverage.
                ref=re.search(r'\b(Matthew|Mark|Luke|John)\s+(\d+)',query,re.I)
                if ref and not self._covers(w,ref.group(0),unknown_ok=True): is_direct=False; hit['limitations'].append('This volume does not provide primary commentary on the requested chapter; any mention is incidental.')
                body_matches=[t for t in matched if t in set(terms(r['text']))]
                hit['reason']=('Text contains '+', '.join(body_matches[:7]) if body_matches else 'Matched the indexed heading or credit; this excerpt does not contain the query wording.') if is_direct else 'Partial wording overlap; verify whether it supports the question.'
                (candidates if is_direct else connection_candidates)[r['id']]=hit
            # Small vocabulary expansion provides transparent lateral leads even without embeddings.
            taxonomy=read_json(self.state/'taxonomy.json',TAXONOMY); related=[]
            for term in taxonomy['terms']:
                if any(norm(label) in norm(query) for label in [term['id'],term['label']]+term.get('aliases',[])):
                    for rel in term.get('related',[]):
                        target=next((x for x in taxonomy['terms'] if x['id']==rel),None)
                        if target: related.append((term['label'],target['label']))
            if related:
                expr=' OR '.join('"'+label+'"' for _,label in related[:6])
                for r in db.execute(sql+' ORDER BY rank LIMIT 100',[expr]+facet_args+(eligibility if len(eligibility)!=len(ws) else [])):
                    if filters.get('author') and norm(filters['author']) not in norm(r['attribution']) and norm(filters['author']) not in norm(ws[r['work_id']]['author']): continue
                    if re.search(r'contributors|bibliograph|index|contents|copyright|acknowledg',r['heading'],re.I): continue
                    lines=[line for line in r['text'].splitlines() if line.strip()]
                    if len(lines)>4 and sum(bool(re.match(r'^\d+\.',line)) for line in lines)/len(lines)>.4: continue
                    if r['id'] in candidates or not self._current_files(json.loads(r['data']),ws[r['work_id']]): continue
                    hit=self._hit(r,ws[r['work_id']]); pair=next(((a,b) for a,b in related if norm(b) in norm(r['text'])),related[0]); hit['reason']=f'Vocabulary connection: {pair[0]} → {pair[1]}. This passage may supply another angle; it is not evidence of influence or direct support.'; hit.update(excerpt_range(r['text'],[pair[1]])); hit['score']=.009 + (1/(100+len(connection_candidates)))
                    connection_candidates[r['id']]=hit
        from .semantic_library import status as semantic_status
        warnings=[];semcoverage=semantic_status(self)
        settings=read_json(self.state/'config.json',{}).get('semantic',{})
        chosen=settings.get('engine') if settings.get('enabled') and settings.get('full_build_approved') else None
        if engine not in (None,'exact','local','openai'):raise ValueError('Unknown semantic engine.')
        use_full=bool(chosen and engine in (None,chosen))
        if use_full or engine in ('local','openai'):
            try:
                if use_full:
                    from .semantic_library import search as semantic_search
                    semantic_results=semantic_search(self,query,limit=60)
                    if semcoverage.get('pending'):warnings.append('Semantic indexing has pending changes; exact search includes the current text index. Stale semantic candidates are excluded.')
                else:
                    from .semantic import semantic_search
                    semantic_results=semantic_search(self,query,engine,limit=60)
                    semcoverage=read_json(self.state/'pilot/manifest.json',{})
                    semcoverage={'scope':'frozen pilot only','coverage_books':len(semcoverage.get('work_ids',[])),'engine':engine}
                    warnings.append('This requested comparison engine searches only the frozen pilot subset.')
                for rank,result in enumerate(semantic_results):
                    pid=result['passage_id']
                    if pid in candidates: candidates[pid]['score']+=1/(61+rank); candidates[pid]['reason']+='; also retrieved by semantic search (reciprocal-rank fusion).'
                    else:
                        offset=max(0,int(result.get('char_start',0))-180)
                        max_chars=max(500,min(20000,int(result.get('char_end',20000))-offset+180))
                        try: hit=self.read(pid,context=0,offset=offset,max_chars=max_chars)['passage']
                        except ValueError: continue
                        if hit['work_id'] not in eligibility: continue
                        if filters.get('text_type') and filters['text_type'] not in [hit.get('text_type','unknown'),*hit.get('text_types',[])]:continue
                        if filters.get('author') and norm(filters['author']) not in norm(hit['attribution']) and norm(filters['author']) not in norm(ws[hit['work_id']]['author']): continue
                        profile={**ws[hit['work_id']].get('profile',{}),**section_profiles.get(hit['section_id'],{})}
                        if any(filters.get(k) and norm(filters[k]) not in [norm(v) for v in profile.get(k+'s',[])] for k in ('subject','form')): continue
                        sample=excerpt_range(hit['text'],ts);hit.update(sample)
                        hit['excerpt_start']+=offset;hit['excerpt_end']+=offset
                        hit['read_offset']=offset
                        if 'char_start' in result:
                            hit['semantic_window']={key:result[key] for key in ('window_id','char_start','char_end')}
                        overlap=[term for term in ts if term in set(terms(hit['text']+' '+hit.get('heading','')))]
                        hit['score']=1/(61+rank); hit['reason']=('Possible connection sharing language about '+', '.join(overlap[:5])+'. ' if overlap else 'Possible conceptual connection suggested by semantic search. ')+'Read the argument before treating it as support.'; connection_candidates[pid]=hit
            except Exception as exc: warnings.append('Semantic search unavailable; exact search remains active: '+str(exc))
        elif engine=='exact': warnings.append('Exact-text search only; this query was processed locally.')
        else: warnings.append('Exact-text search across the library. No full-library semantic engine is configured.')
        used=collections.Counter(e.get('work_id') for e in self.usage(500) if e.get('kind') in ('used','selected'))
        def score(hit):
            w=ws[hit['work_id']]; profile={k:list(set(v+section_profiles.get(hit['section_id'],{}).get(k,[]))) for k,v in w.get('profile',{}).items()}
            for k,v in section_profiles.get(hit['section_id'],{}).items(): profile.setdefault(k,v)
            boost=sum(t in profile.get('subjects',[]) for t in focusdata.get('subjects',[]))*0.002 +sum(t in profile.get('forms',[]) for t in focusdata.get('forms',[]))*.001
            if hit['work_id'] in focusdata.get('books',[]): boost+=.005
            reference_heading=bool(re.fullmatch(r'(?:Table of )?Contents|List of (?:Figures|Illustrations|Tables)|Contributors|Bibliography|Index|Loans|Credits|Acknowledgments',hit.get('heading',''),re.I))
            # Weak ebook markup can leave real chapter prose under a lingering
            # Contents heading. Never demote long prose solely for that label.
            coarse_contents=bool(re.fullmatch(r'(?:Table of )?Contents',hit.get('heading',''),re.I)) and any(len(line)>250 for line in hit['text'].splitlines())
            reference_penalty=.008 if reference_heading and not coarse_contents else 0
            return hit['score']+boost-reference_penalty-(min(used[hit['work_id']],5)*.0001 if mode=='exploratory' else 0)
        def grouped(pool,n,connection=False):
            result=[]; counts=collections.Counter(); max_per=n if filters.get('book') else (1 if mode=='exploratory' or connection else 2)
            for hit in sorted(pool.values(),key=score,reverse=True):
                # Repeated volumes in a large reference series should not crowd out other voices.
                group='feasting-word' if hit['title'].startswith('Feasting on the Word') else ('feasting-gospels' if hit['title'].startswith('Feasting on the Gospels') else hit['work_id'])
                if counts[group]>=max_per: continue
                hit['score']=round(score(hit),6); result.append(hit); counts[group]+=1
                if len(result)>=n: break
            return result
        direct=grouped(candidates,max(1,min(limit,30))); indirect=grouped({k:v for k,v in connection_candidates.items() if k not in candidates},max(0,min(connections,10)),connection=True) if connections else []
        gap=None
        if not direct: gap='No direct passage matched this query in the currently indexed, available holdings. This is a holdings/search gap, not evidence that the subject has no literature.'
        if 'enneagram' in ts and not any('enneagram' in norm(w['title']) or 'enneagram' in w.get('profile',{}).get('subjects',[]) for w in ws.values()): gap='No dedicated Enneagram book is currently identified in this library. Any incidental mentions or formation-related connections are not substitutes for Enneagram holdings.'
        from .unit_reading import attach_search_targets
        return attach_search_targets(self,{'query':query,'focus':focus,'mode':mode,'direct':direct,'connections':indirect,'coverage':{'exact_books':len(ws),'eligible_books':len(eligibility),'semantic':semcoverage},'warnings':warnings,'holdings_gap':gap})

    def _covers(self,w,reference,unknown_ok=False):
        title=w['title']; match=re.search(r'Feasting on the Gospels: (Matthew|Mark|Luke|John).*Chapters (\d+)[–-](\d+)',title,re.I)
        if match:
            # These source-verified ranges override a conflicting catalog label.
            # Match the whole reference so 1 John is not mistaken for John and
            # a range extending outside the volume is not approved by its start.
            ref=re.fullmatch(r'(Matthew|Mark|Luke|John)\s+(\d+)(?::(\d+))?(?:\s*[–-]\s*(\d+)(?::(\d+))?)?',reference.strip(),re.I)
            if not ref: return False
            first=int(ref[2]); last=int(ref[4]) if ref[4] and (not ref[3] or ref[5]) else first
            return norm(ref[1])==norm(match[1]) and int(match[2])<=first<=last<=int(match[3])
        coverage=w.get('coverage',[])
        if coverage:
            # Other books use explicit labels (or dictionary keys), without
            # inferring unrecorded chapter/verse ranges from substring matches.
            def label(value): return ' '.join(norm(value).replace('–','-').split())
            return any(isinstance(x,str) and label(reference)==label(x) for x in coverage)
        return unknown_ok

    def _current_files(self,data,w):
        # Stat only the handful of candidate files; stale/deleted text never appears as current evidence.
        lookup={f['id']:f for f in w['files']}
        for fid in data['file_ids']:
            f=lookup[fid]
            try: st=Path(f['path']).stat()
            except OSError: return False
            if st.st_size!=f.get('size') or st.st_mtime_ns!=f.get('mtime_ns'): return False
        return True

    @staticmethod
    def _reading_block(data,text=None,fragment=False):
        """A bounded public representation; the lossless block remains in the index."""
        b={k:data[k] for k in ('id','kind','file_id','path','locator','global_ordinal','attribution_scope','text_type','segment_id','passage_id') if k in data}
        b.update(text=data['text'] if text is None else text,location=str(data.get('location',''))[:2000],attribution=str(data.get('attribution',''))[:2000],selected=data.get('selected',False))
        b['heading_path']=[str(h)[:500] for h in data.get('heading_path',[])[:8]]
        for key in ('note_refs','image_refs','links'): b[key]=data.get(key,[])[:40]
        if not fragment and data.get('kind')=='table' and len(compact(data.get('cells',[])))<30000:
            b['cells']=data.get('cells',[])
        if fragment:
            b.update(fragment=True,source_kind=b.get('kind'))
            if b.get('kind') in ('heading','table'):b['kind']='paragraph'
        return b

    def read(self,passage_id,context=3,offset=0,max_chars=20000):
        context=max(0,min(int(context),8));offset=int(offset);max_chars=max(500,min(int(max_chars),45000))
        if offset<0: raise ValueError('Reading offset must be nonnegative.')
        with self._db() as db:
            p=db.execute('SELECT * FROM passages WHERE id=?',(passage_id,)).fetchone()
            if not p:
                from .passage_history import historical_read
                return historical_read(self,passage_id,context=context,offset=offset,max_chars=max_chars)
            w=json.loads(db.execute('SELECT data FROM works WHERE id=?',(p['work_id'],)).fetchone()[0]);data=json.loads(p['data'])
            if not self._current_files(data,w): raise ValueError('Source file changed or disappeared; synchronize before reading this passage.')
            if offset>=len(p['text']) and offset: raise ValueError('Reading offset is beyond the passage.')
            nearby=db.execute('SELECT * FROM passages WHERE work_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',(p['work_id'],p['ordinal']-context,p['ordinal']+context)).fetchall()
            context_incomplete=any(not self._current_files(json.loads(r['data']),w) for r in nearby)
            nearby=[r for r in nearby if self._current_files(json.loads(r['data']),w)]
            allowed_files={fid for r in nearby for fid in json.loads(r['data'])['file_ids']}
            first=min(r['first_ordinal'] for r in nearby);last=max(r['last_ordinal'] for r in nearby)
            raw=[]
            for row in db.execute('SELECT * FROM blocks WHERE work_id=? AND ordinal BETWEEN ? AND ? ORDER BY ordinal',(p['work_id'],first-1,last+1)):
                if row['file_id'] not in allowed_files:continue
                b=json.loads(row['data'])
                if b.get('is_boilerplate'):continue
                if not first<=row['ordinal']<=last and not (b.get('image_refs') and not b['text'].strip()):continue
                b['selected']=p['first_ordinal']<=row['ordinal']<=p['last_ordinal'];raw.append(b)
            selected=[b for b in raw if b['selected'] and b['text'].strip()]
            cursor=0;end=min(len(p['text']),offset+max_chars);chosen=[]
            for b in selected:
                block_start=cursor;block_end=cursor+len(b['text']);cursor=block_end+2
                lo=max(offset,block_start);hi=min(end,block_end)
                if lo<hi:
                    if len(chosen)>=80:
                        end=block_start;break
                    fragment=lo>block_start or hi<block_end
                    part=self._reading_block(b,b['text'][lo-block_start:hi-block_start],fragment)
                    part['source_char_start']=lo-block_start;part['source_char_end']=hi-block_start
                    chosen.append(part)
                if block_end>=end:break
            # Surrounding text uses only the remaining character/block allowance. A
            # huge matched block is continued by offset instead of silently skipped.
            remaining=max_chars-(end-offset);before=[];after=[]
            earlier=[b for b in raw if b['global_ordinal']<p['first_ordinal']]
            later=[b for b in raw if b['global_ordinal']>p['last_ordinal']]
            before_budget=remaining//2 if earlier and later else remaining if earlier else 0
            def context_blocks(items,budget,reverse=False):
                out=[];consumed=0
                for b in (reversed(items) if reverse else items):
                    if len(chosen)+len(before)+len(out)>=80 or consumed>=budget:break
                    allowance=budget-consumed;text=b['text']
                    if len(text)>allowance:
                        text=text[-allowance:] if reverse else text[:allowance]
                    if not text:continue
                    out.append(self._reading_block(b,text,len(text)<len(b['text'])));consumed+=len(text)
                return list(reversed(out)) if reverse else out,consumed
            before,before_used=context_blocks(earlier,before_budget,True)
            after,after_used=context_blocks(later,remaining-before_used)
            blocks=before+chosen+after
            # Textless illustrations have no character offset, but must remain
            # reachable beside visible captions. Bound them by the displayed
            # source span, including an immediately adjacent image block.
            if blocks:
                low=min(b['global_ordinal'] for b in blocks);high=max(b['global_ordinal'] for b in blocks)
                images=[self._reading_block(b) for b in raw if b.get('image_refs') and not b['text'].strip() and low-1<=b['global_ordinal']<=high+1]
                blocks=sorted(blocks+images[:max(0,80-len(blocks))],key=lambda b:b['global_ordinal'])
            omitted_context=len(before)<len(earlier) or len(after)<len(later) or any(b.get('fragment') for b in before+after)
            section=json.loads(db.execute('SELECT data FROM sections WHERE id=?',(p['section_id'],)).fetchone()[0]);section['title']=section['title'][:500]
            prev=db.execute('SELECT id FROM passages WHERE work_id=? AND ordinal=?',(p['work_id'],p['ordinal']-1)).fetchone();nxt=db.execute('SELECT id FROM passages WHERE work_id=? AND ordinal=?',(p['work_id'],p['ordinal']+1)).fetchone()
            hit=self._hit(p,w);hit['text']=p['text'][offset:end];hit['reason']='Source passage'
            warnings=[]
            if context_incomplete:
                message='Surrounding context is incomplete because an adjacent source file changed or is missing. Run sync.'
                warnings.append(message);hit['limitations'].append(message)
            if offset or end<len(p['text']):warnings.append('This is a bounded fragment of the matched passage. Use continuation offsets to read the rest.')
            if omitted_context:warnings.append('Surrounding context reached the display limit; adjacent passages remain available.')
            public_work={k:w[k] for k in ('id','edition_id','volume_id','title','author','contributors','profile') if k in w}
            public_work['metadata']={k:w.get('metadata',{}).get(k) for k in ('edition','publication_year','publisher','isbn','source_url') if w.get('metadata',{}).get(k) is not None}
        return {'passage':hit,'blocks':blocks,'work':public_work,'section':section,'previous':prev[0] if prev else None,'next':nxt[0] if nxt else None,'context_limit':context,'truncated':end<len(p['text']) or offset>0 or omitted_context,'warnings':warnings,'offset':offset,'next_offset':end if end<len(p['text']) else None,'previous_offset':max(0,offset-max_chars) if offset else None,'passage_chars':len(p['text']),'max_chars':max_chars}

    def cite(self,passage_id,locator=None,segment_id=None):
        with self._db() as db:
            exists=db.execute('SELECT 1 FROM passages WHERE id=?',(passage_id,)).fetchone()
        if not exists:
            from .passage_history import historical_cite
            return historical_cite(self,passage_id,locator,segment_id)
        from .citations import cite_passage
        return cite_passage(self,passage_id,locator=locator,segment_id=segment_id)

    def section(self,section_id,work_id=None):
        from .unit_reading import section
        return section(self,section_id,work_id)

    def contents(self,work_id,offset=0,limit=100):
        from .unit_reading import contents
        return contents(self,work_id,offset,limit)

    def read_unit(self,unit_id,anchor=None,cursor=0,max_chars=200000,match=None,anchor_offset=0):
        from .unit_reading import read_unit
        return read_unit(self,unit_id,anchor,cursor,max_chars,match=match,anchor_offset=anchor_offset)

    def create_selection(self,unit_id,source_ranges):
        from .unit_citations import create_selection
        return create_selection(self,unit_id,source_ranges)

    def selection(self,selection_id):
        from .unit_citations import selection
        return selection(self,selection_id)

    def cite_unit(self,unit_id,locator=None):
        from .unit_citations import cite_unit
        return cite_unit(self,unit_id,locator=locator)

    def cite_selection(self,selection_id,locator=None):
        from .unit_citations import cite_selection
        return cite_selection(self,selection_id,locator=locator)

    def record_target_usage(self,scope,target_id,event):
        from .unit_citations import record_target_usage
        return record_target_usage(self,scope,target_id,event)

    def bibliography_record(self,target_id):
        works,edits=self._effective(self._catalog())
        is_passage=False;section_id=None
        if target_id in works:
            work=works[target_id];target=work;work_id=target_id
        else:
            with self._db() as db:
                row=db.execute('SELECT work_id,data FROM sections WHERE id=?',(target_id,)).fetchone()
                if row:
                    work_id=row['work_id'];target=json.loads(row['data']);section_id=target_id
                else:
                    passage=db.execute('SELECT work_id,section_id FROM passages WHERE id=?',(target_id,)).fetchone()
                    if not passage:raise ValueError('Unknown book, section, or passage.')
                    is_passage=True;work_id=passage['work_id'];section_id=passage['section_id']
                    target=json.loads(db.execute('SELECT data FROM sections WHERE id=?',(section_id,)).fetchone()[0])
                work=works[work_id]
        from .citations import _annotation
        base_id=section_id or target_id
        _,record=_annotation(edits,base_id,target.get('source_bibliography'))
        if is_passage:
            base_event,_=_annotation(edits,section_id,target.get('source_bibliography'))
            _,record=_annotation(edits,target_id,base_event)
        return {'id':target_id,'title':target['title'],'work_id':work_id,'work_title':work['title'],'is_section':target_id!=work_id,'is_passage':is_passage,'scope':'passage' if is_passage else 'section' if section_id else 'work','section_id':section_id,'record':record,'catalog_hints':{key:work.get('metadata',{}).get(key) for key in ('publisher','publication_year','edition','isbn','source_url')},'catalog_author':work.get('author','')}

    def usage(self,limit=50): return list(reversed(jsonl(self.state/'usage.jsonl')))[:max(1,min(int(limit),1000))]

    def record_usage(self,data):
        if data.get('kind') not in ('inspected','offered','recommended','selected','used'): raise ValueError('Unknown usage event kind.')
        if not data.get('evidence'): raise ValueError('Usage events need an evidence link or explicit action.')
        if data.get('passage_id') and not data.get('target_scope'):
            data={**data,'target_scope':'passage','target_id':data['passage_id']}
        rec={**data,'id':data.get('id') or uid('use'),'created_at':data.get('created_at') or now()}
        with writer(self.state):
            if any(e['id']==rec['id'] for e in jsonl(self.state/'usage.jsonl')): return {'duplicate':True,**rec}
            append_jsonl(self.state/'usage.jsonl',rec)
        return rec
