from __future__ import annotations
import argparse,json,sys
from pathlib import Path
from .service import Library
from .local_server import DEFAULT_PORT, server_address


def parser():
    p=argparse.ArgumentParser(prog='library',description='Local book research: exact sources, explicit connections.')
    p.add_argument('--root',type=Path); sub=p.add_subparsers(dest='command',required=True)
    from . import correction_cli, onboarding, paid_cli
    onboarding.add_parsers(sub)
    paid_cli.add_parsers(sub)
    correction_cli.add_parser(sub)
    sync=sub.add_parser('sync'); sync.add_argument('--path'); sync.add_argument('--rebuild',action='store_true'); sync.add_argument('--exact',action='store_true',help='Update exact index only; leave semantic work pending')
    intake=sub.add_parser('intake',help='Prepare a book using your saved optional AI choices')
    intake.add_argument('action',nargs='?',choices=['add','status','pause','resume','undo','review'],default='add')
    intake.add_argument('intake_id',nargs='?'); intake.add_argument('--path',type=Path); intake.add_argument('--context',type=Path)
    intake.add_argument('--version',type=int); intake.add_argument('--event',help='Undo only this automatic correction event')
    review_choice=intake.add_mutually_exclusive_group()
    review_choice.add_argument('--review-metadata',dest='review_metadata',action='store_true',help='Authorize automatic provisional metadata review for this intake')
    review_choice.add_argument('--no-review-metadata',dest='review_metadata',action='store_false',help='Skip automatic metadata review for this intake')
    intake.set_defaults(review_metadata=None)
    intake.add_argument('--local-only',action='store_true',help='Skip both paid stages for this intake')
    search=sub.add_parser('search'); search.add_argument('query'); search.add_argument('--focus'); search.add_argument('--mode',choices=['focused','balanced','exploratory'],default=None); search.add_argument('--filter',action='append',default=[]); search.add_argument('--limit',type=int,default=12); search.add_argument('--connections',type=int,default=5); engine=search.add_mutually_exclusive_group(); engine.add_argument('--engine',choices=['local','openai']); engine.add_argument('--exact',action='store_true',help='Wholly local exact-text search')
    search.add_argument('--with-context',action='store_true',help='Attach surrounding source text and a citation draft to every hit (for agents; pair with --json)')
    search.add_argument('--context',type=int,default=2,help='With --with-context: neighboring passages on each side (0-8, default 2)')
    search.add_argument('--max-chars',type=int,default=6000,help='With --with-context: source characters per hit (default 6000)')
    search.add_argument('--total-chars',type=int,default=60000,help='With --with-context: source characters across all hits (default 60000)')
    read=sub.add_parser('read'); read.add_argument('passage_id'); read.add_argument('--context',type=int,default=3); read.add_argument('--offset',type=int,default=0); read.add_argument('--max-chars',type=int,default=20000)
    contents=sub.add_parser('contents'); contents.add_argument('work_id'); contents.add_argument('--offset',type=int,default=0); contents.add_argument('--limit',type=int,default=100)
    unit=sub.add_parser('read-unit'); unit.add_argument('unit_id'); unit.add_argument('--anchor'); unit.add_argument('--cursor',type=int,default=0); unit.add_argument('--max-chars',type=int,default=200000)
    cite_unit=sub.add_parser('cite-unit'); cite_unit.add_argument('unit_id'); cite_unit.add_argument('--locator',help='JSON source-verified locator')
    select=sub.add_parser('create-selection'); select.add_argument('unit_id'); select.add_argument('--ranges',help='JSON list of block_id/start/end source ranges'); select.add_argument('--file',type=Path)
    selection=sub.add_parser('selection'); selection.add_argument('selection_id')
    cite_selection=sub.add_parser('cite-selection'); cite_selection.add_argument('selection_id'); cite_selection.add_argument('--locator',help='JSON source-verified locator')
    cite=sub.add_parser('cite'); cite.add_argument('passage_id'); cite.add_argument('--locator',help='JSON locator with type, value, verified and evidence'); cite.add_argument('--format',choices=['all','note','short','bibliography','markdown'],default='all')
    cite.add_argument('--segment',help='First source block ID of a separately credited portion; see component_citations')
    sub.add_parser('status')
    books=sub.add_parser('books'); books.add_argument('query',nargs='?',default=''); books.add_argument('--limit',type=int,default=50); books.add_argument('--offset',type=int,default=0)
    browse=sub.add_parser('browse',help='Browse the shelves like the interface: sort, filter, or list books with one classification')
    browse.add_argument('query',nargs='?',default='',help='Title or creator text')
    browse.add_argument('--filter',action='append',default=[],help='subject=VALUE, form=VALUE, or author=VALUE; repeatable')
    browse.add_argument('--classification',help='FACET=VALUE from `taxonomy list`, e.g. themes=water; not combined with query or filters')
    browse.add_argument('--sort',choices=['title','creator'],default='title'); browse.add_argument('--limit',type=int,default=50); browse.add_argument('--offset',type=int,default=0)
    book=sub.add_parser('book'); book.add_argument('id'); book.add_argument('--section-offset',type=int,default=0); book.add_argument('--section-limit',type=int,default=100)
    focus=sub.add_parser('focus'); focus.add_argument('action',choices=['list','show','set']); focus.add_argument('id',nargs='?'); focus.add_argument('--value'); focus.add_argument('--file',type=Path)
    annotate=sub.add_parser('annotate'); annotate.add_argument('target'); annotate.add_argument('--field',required=True); annotate.add_argument('--value',required=True); annotate.add_argument('--evidence',required=True); annotate.add_argument('--origin',choices=['user','codex','import'],default='user'); annotate.add_argument('--status',choices=['provisional','confirmed','disputed'],default='confirmed')
    usage=sub.add_parser('usage'); usage.add_argument('action',choices=['list','record','import'],default='list',nargs='?'); usage.add_argument('path',type=Path,nargs='?'); usage.add_argument('--event'); usage.add_argument('--limit',type=int,default=50)
    taxonomy=sub.add_parser('taxonomy'); taxonomy.add_argument('action',choices=['list','set']); taxonomy.add_argument('id',nargs='?'); taxonomy.add_argument('--value'); taxonomy.add_argument('--file',type=Path); taxonomy.add_argument('--query',default=''); taxonomy.add_argument('--offset',type=int,default=0)
    serve=sub.add_parser('serve'); serve.add_argument('--port',type=int,default=None,help=f'Override the configured local port (default {DEFAULT_PORT})')
    app=sub.add_parser('app',help='Open the Reading Room in its own window; reuses a running server for this library'); app.add_argument('--port',type=int,default=None,help=f'Override the configured local port (default {DEFAULT_PORT})')
    for cmd in sub.choices.values(): cmd.add_argument('--json',action='store_true')
    return p


BROWSE_FILTERS=('subject','form','author')


def _pairs(values,allowed,usage):
    pairs={}
    for value in values:
        key,sep,item=value.partition('=')
        if not sep or not item or key not in allowed: raise ValueError(usage)
        pairs[key]=item
    return pairs


def browse(lib,args):
    """The shelves page (browse_books) or a classification's books (facet_books)."""
    if args.classification:
        if args.query or args.filter: raise ValueError('Use --classification alone; it lists every book with that classification.')
        from .service import FACETS
        from .taxonomy import facet_books
        facet,value=next(iter(_pairs([args.classification],FACETS,'Classification syntax: FACET=VALUE, where FACET is one of '+', '.join(sorted(FACETS))+'.').items()))
        return facet_books(lib,facet,value,offset=args.offset,limit=args.limit,sort=args.sort)
    filters=_pairs(args.filter,BROWSE_FILTERS,'Browse filter syntax: subject=VALUE, form=VALUE, or author=VALUE.')
    return lib.browse_books(args.query,facet=filters or None,sort=args.sort,limit=args.limit,offset=args.offset)


def run(args):
    lib=Library(args.root)
    if args.command in {'init','doctor','demo','autostart'}:
        from . import onboarding
        return onboarding.run(args,lib)
    if args.command=='correction':
        from . import correction_cli
        return correction_cli.run(lib,args)
    if args.command in {'semantic','review','budget'}:
        from . import paid_cli
        return paid_cli.run(lib,args)
    match args.command:
        case 'sync': return lib.sync(args.path,args.rebuild,semantic=not args.exact)
        case 'intake':
            if args.action=='add':
                if not args.path: raise ValueError('Provide --path for one book file or folder.')
                if args.context and args.context.stat().st_size>1_000_000: raise ValueError('Intake context must be at most one million bytes.')
                return lib.intake(args.path,json.loads(args.context.read_text()) if args.context else None,review_metadata=args.review_metadata,local_only=args.local_only)
            if args.action=='status': return lib.intake_status(args.intake_id)
            if not args.intake_id: raise ValueError('Provide the intake identifier from intake status.')
            if args.action=='review': return lib.review_intake(args.intake_id,expected_version=args.version)
            if args.action=='pause': return lib.pause_intake(args.intake_id,expected_version=args.version)
            if args.action=='resume': return lib.resume_intake(args.intake_id,expected_version=args.version)
            return lib.undo_intake(args.intake_id,event_id=args.event,expected_version=args.version)
        case 'status': return lib.status()
        case 'search':
            filters={}
            for f in args.filter:
                if '=' not in f: raise ValueError('Filter syntax: subject=value, form=value, book=value, author=value, scripture=value, text_type=value')
                k,v=f.split('=',1);filters[k]=v
            options=dict(focus=args.focus,mode=args.mode,filters=filters,limit=args.limit,connections=args.connections,engine='exact' if args.exact else args.engine)
            if args.with_context:
                from .research import search_with_context
                return search_with_context(lib,args.query,context=args.context,max_chars=args.max_chars,total_chars=args.total_chars,**options)
            return lib.search(args.query,**options)
        case 'read': return lib.read(args.passage_id,args.context,offset=args.offset,max_chars=args.max_chars)
        case 'contents': return lib.contents(args.work_id,offset=args.offset,limit=args.limit)
        case 'read-unit': return lib.read_unit(args.unit_id,anchor=args.anchor,cursor=args.cursor,max_chars=args.max_chars)
        case 'cite-unit': return lib.cite_unit(args.unit_id,locator=json.loads(args.locator) if args.locator else None)
        case 'create-selection': return lib.create_selection(args.unit_id,json.loads(args.file.read_text() if args.file else args.ranges or '[]'))
        case 'selection': return lib.selection(args.selection_id)
        case 'cite-selection': return lib.cite_selection(args.selection_id,locator=json.loads(args.locator) if args.locator else None)
        case 'cite': return lib.cite(args.passage_id,locator=json.loads(args.locator) if args.locator else None,segment_id=args.segment)
        case 'books': return lib.books(args.query,limit=args.limit,offset=args.offset)
        case 'browse': return browse(lib,args)
        case 'book': return lib.book(args.id,args.section_offset,args.section_limit)
        case 'focus':
            if args.action=='list': return lib.focuses()
            if args.action=='show': return next((f for f in lib.focuses() if f['id']==args.id),None)
            if not args.id: raise ValueError('Provide a focus ID.')
            data=json.loads(args.file.read_text() if args.file else args.value or '{}')
            return lib.save_focus(args.id,data)
        case 'annotate': return lib.annotate(args.target,{'field':args.field,'value':json.loads(args.value),'evidence':args.evidence,'origin':args.origin,'status':args.status})
        case 'usage':
            if args.action=='record':
                event=json.loads(args.event or '{}')
                if event.get('target_scope') in ('unit','selection'):
                    return lib.record_target_usage(event['target_scope'],event.get('target_id',''),event)
                return lib.record_usage(event)
            if args.action=='import':
                from .usage_import import import_usage
                return import_usage(lib,args.path)
            return lib.usage(args.limit)
        case 'taxonomy':
            from .taxonomy import list_taxonomy,save_term
            if args.action=='list': return list_taxonomy(lib,query=args.query,offset=args.offset)
            if not args.id: raise ValueError('Provide a term ID.')
            return save_term(lib,args.id,json.loads(args.file.read_text() if args.file else args.value or '{}'))
        case 'serve':
            import uvicorn
            from .web import create_app
            host,port,base_url=server_address(lib,args.port)
            lib._local_base_url=base_url
            uvicorn.run(create_app(lib,enable_intake_worker=True),host=host,port=port)
        case 'app':
            from .desktop import run_app
            run_app(lib,args.port)


def main():
    args=parser().parse_args()
    try: result=run(args)
    except (ValueError,OSError,KeyError) as exc:
        print(json.dumps({'error':str(exc)}) if args.json else 'Error: '+str(exc),file=sys.stderr); return 1
    if result is None: return 0
    if args.json: print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='cite':
        keys={'note':'full_note','short':'short_note','bibliography':'bibliography'}
        if args.format in keys:print(result[keys[args.format]])
        elif args.format=='markdown':print(json.dumps(result.get('markdown',{}),ensure_ascii=False,indent=2))
        else:
            for label,key in [('Full note','full_note'),('Shortened note','short_note'),('Bibliography','bibliography')]:print(label+'\n'+result[key]+'\n')
        for warning in result.get('warnings',[]):print('Review: '+str(warning),file=sys.stderr)
    elif args.command=='search':
        if result['holdings_gap']: print(result['holdings_gap']+'\n')
        for label,key in [('Direct results','direct'),('Possible connections','connections')]:
            print(label)
            for i,h in enumerate(result[key],1):
                print(f"{i}. {h['title']}\n   {h['attribution']} · {h['heading']}\n   {h['text'][:500]}\n   {h['reason']}\n   {h['id']}")
                if h.get('context'):
                    for part in ('before','passage','after'):
                        if h['context'][part]: print(f"   [{part}]\n   "+h['context'][part].replace('\n','\n   '))
                if h.get('citation'): print('   Citation (draft): '+h['citation'].get('full_note',''))
                print()
        print('\n'.join(result['warnings']))
    else: print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__': raise SystemExit(main())
