"""Exercise a fresh permitted-book workflow without credentials or provider calls."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
from xml.sax.saxutils import escape
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def extract_checkout(archive_path, folder):
    """Extract a regular-file-only source archive without escaping its staging root."""
    destination = folder / 'Clean source archive'
    destination.mkdir()
    with tarfile.open(archive_path, 'r:*') as archive:
        members = archive.getmembers()
        if any(not (member.isfile() or member.isdir()) for member in members):
            raise ValueError('A public source archive must contain only regular files and directories.')
        archive.extractall(destination, members=members, filter='data')
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError('The source archive must contain exactly one project root directory.')
    checkout = roots[0]
    if not (checkout / 'bin' / 'setup').is_file() or not (checkout / 'pyproject.toml').is_file():
        raise ValueError('The source archive is missing bin/setup or pyproject.toml.')
    return checkout


def write_book(source):
    source.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source,'w') as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        body = []
        for heading, text in [('Shared paths', 'Copper birds gather beside the river. This is original synthetic text.'),
                              ('A second chapter', 'A café beside the water holds memories of music, mañana, and shared stories.')]:
            body.append('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>'+escape(heading)+'</w:t></w:r></w:p>')
            body.append('<w:p><w:r><w:t>'+escape(text)+'</w:t></w:r></w:p>')
        archive.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+''.join(body)+'</w:body></w:document>')


def exercise(checkout, folder):
    root = folder / 'A library with spaces'
    env = {k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','LIBRARY_OPENAI_ENV','LIBRARY_ROOT')}
    command = [str(checkout/'bin/library'), '--root', str(root)]
    def call(*args):
        result = subprocess.run([*command,*args,'--json'],cwd=folder,env=env,check=False,capture_output=True,text=True)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            result.check_returncode()
        return json.loads(result.stdout)
    call('init','--timezone','UTC')
    assert call('doctor')['ok']
    for name in ('bookshare-find', 'bookshare-download', 'library-search'):
        link = checkout / '.agents' / 'skills' / name
        assert link.is_symlink() and link.resolve() == (checkout / 'skills' / name).resolve()
        assert not link.readlink().is_absolute(), 'Project discovery links must move with the checkout'
    launcher = checkout / '.agents' / 'skills' / 'library-search' / 'scripts' / 'library'
    subprocess.run([str(launcher), '--root', str(root), 'status', '--json'],
                   cwd=folder, env=env, check=True, capture_output=True, text=True)
    book = root/'Example Book'; book.mkdir()
    source = book/'Example.docx'
    write_book(source)
    before = source.read_bytes()
    job = call('intake','--path',str(book),'--local-only')
    # Process only the local queue. A fake key is present to prove credentials
    # alone cannot activate either feature; all provider entrypoints fail closed.
    program = '''import sys
from pathlib import Path
from book_library.service import Library
from book_library import intake, semantic, review_ai
lib=Library(Path(sys.argv[1]))
def denied(*args,**kwargs): raise AssertionError("Unexpected paid provider call")
semantic.embed_openai=denied
review_ai.suggest=denied
for _ in range(20):
 if not intake.run_next(lib): break
'''
    subprocess.run([str(checkout/'.venv/bin/python'),'-c',program,str(root)],env={**env,'OPENAI_API_KEY':'not-a-real-key'},cwd=folder,check=True)
    hits = call('search','copper birds','--exact')['direct']; assert hits
    passage = hits[0]['id']
    assert call('read',passage)
    assert call('cite',passage)['full_note']
    books=call('books'); assert books
    assert source.read_bytes() == before
    assert not (root/'.library/costs.jsonl').exists()
    settings=call('review','status')
    assert not settings['automatic_metadata_review'] and not settings['semantic_enabled']
    print('Fresh source install: local intake, exact search, reading, citation, and no-paid checks passed.')


def exercise_semantic(checkout, folder, *, runtime_only=False):
    """Real estimate, tokenization, accounting, USearch, and cache; fake SDK only."""
    root = folder / 'Separate semantic library'
    env = {k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','LIBRARY_OPENAI_ENV','LIBRARY_ROOT')}
    command = [str(checkout/'bin/library'), '--root', str(root)]
    def call(*args):
        result = subprocess.run([*command,*args,'--json'],cwd=folder,env=env,check=False,capture_output=True,text=True)
        if result.returncode:
            print(result.stderr, file=sys.stderr)
            result.check_returncode()
        return json.loads(result.stdout)
    call('init','--timezone','UTC')
    source = root / 'Synthetic semantics' / 'Example.docx'
    write_book(source)
    original = source.read_bytes()
    call('sync','--exact')
    before = call('semantic','estimate')
    assert before['uncached_unique_windows'] > 0 and before['uncached_tokens'] > 0
    assert before['authorization_changed'] is False
    assert not call('review','status')['semantic_enabled']
    call('budget','configure','--initial-usd','10','--monthly-usd','10','--timezone','UTC')
    call('semantic','enable')
    after = call('semantic','estimate')
    assert after['uncached_tokens'] == before['uncached_tokens']
    settings = call('review','status')
    assert settings['semantic_enabled'] and not settings['automatic_metadata_review']
    assert not (root / '.library' / 'costs.jsonl').exists()
    program = '''import importlib.util, sys
from pathlib import Path
from types import SimpleNamespace
import tiktoken
from book_library.service import Library
from book_library import semantic_library as sem, review_ai
from book_library.budget import Budget
from book_library.storage import read_json
lib = Library(Path(sys.argv[1]))
heavy = ('torch', 'transformers', 'sentence_transformers', 'huggingface_hub')
if sys.argv[2] == 'runtime-only':
 assert all(importlib.util.find_spec(name) is None for name in heavy), 'Optional pilot packages leaked into normal installation'
calls = []
encoding = tiktoken.get_encoding('cl100k_base')
def factory(**options):
 assert options['max_retries'] == 0
 assert options['base_url'] == 'https://api.openai.com/v1'
 class Embeddings:
  def create(self, **request):
   assert request['model'] == 'text-embedding-3-large'
   assert Budget(lib.state).report()['requests'] > len(calls), 'Request was not reserved before SDK submission'
   calls.append(request)
   rows = []
   for i, text in enumerate(request['input']):
    vector = [0.0] * request['dimensions']
    vector[0] = 1.0
    rows.append(SimpleNamespace(index=i, embedding=vector))
   tokens = sum(len(encoding.encode(text)) for text in request['input'])
   return SimpleNamespace(data=list(reversed(rows)), usage=SimpleNamespace(total_tokens=tokens))
 return SimpleNamespace(embeddings=Embeddings(), close=lambda: None)
sys.modules['openai'] = SimpleNamespace(OpenAI=factory)
def denied(*args, **kwargs): raise AssertionError('Metadata review was not authorized')
review_ai.suggest = denied
result = sem.build_index(lib)
assert result['engine'] == 'openai' and result['embedded_windows'] > 0
assert result['coverage_books'] == 1
assert calls and Budget(lib.state).report()['actual_tokens'] > 0
count = len(calls)
sem.build_index(lib)
assert len(calls) == count, 'Completed embedding work was replayed'
assert sem.search(lib, 'river memory')
assert len(calls) == count + 1
assert sem.search(lib, 'river memory')
assert len(calls) == count + 1, 'Cached query was replayed'
assert read_json(lib.state / 'config.json')['semantic']['openai_initial_complete']
sem.disable_engine(lib)
try:
 sem.search(lib, 'another query')
except ValueError:
 pass
else:
 raise AssertionError('Disabled semantic search still ran')
assert len(calls) == count + 1
assert not any(name in sys.modules for name in heavy), 'Supported OpenAI pipeline imported pilot packages'
print('Supported semantics: real tokenization, full build, accounting, query/cache, and disable checks passed with fake SDK; no provider calls.')
'''
    subprocess.run([str(checkout/'.venv/bin/python'), '-c', program, str(root),
                    'runtime-only' if runtime_only else 'installed'],
                   env={**env,'OPENAI_API_KEY':'not-a-real-key'},cwd=folder,check=True)
    assert source.read_bytes() == original


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--installed',action='store_true',help='Use this already-installed clean checkout (for CI)')
    mode.add_argument('--archive',type=Path,help='Install and exercise an actual source .tar.gz instead of copying candidate files')
    args=parser.parse_args()
    with TemporaryDirectory(prefix='reading-room-install-') as name:
        folder=Path(name)
        checkout=ROOT
        if not args.installed:
            if args.archive:
                checkout=extract_checkout(args.archive.expanduser().resolve(), folder)
            else:
                from check_public_release import candidates
                checkout=folder/'Clean source checkout'
                for relative in candidates():
                    target=checkout/relative;target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(ROOT/relative,target)
            env={k:v for k,v in os.environ.items() if k not in ('OPENAI_API_KEY','LIBRARY_OPENAI_ENV','LIBRARY_ROOT')}
            # The supported Python used to invoke this smoke may live in a managed
            # runtime rather than the interactive shell's PATH. Make that
            # installed prerequisite discoverable only in the child setup.
            python_directory=Path(getattr(sys, '_base_executable', sys.executable)).resolve().parent
            env['PATH']=str(python_directory)+os.pathsep+env.get('PATH','')
            subprocess.run([str(checkout/'bin/setup'),'--root',str(folder/'Setup books'),'--non-interactive'],env=env,cwd=checkout,check=True)
        exercise(checkout,folder)
        exercise_semantic(checkout,folder,runtime_only=not args.installed)
    return 0

if __name__=='__main__':raise SystemExit(main())
