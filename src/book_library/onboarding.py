"""Local setup and read-only diagnostics for a fresh Reading Room installation."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import platform
import socket
import sqlite3
import sys

from .install_defaults import python_range, supported_python
from .local_server import server_address
from .storage import open_sqlite, read_json, writer

COMMANDS = {'init', 'doctor', 'demo', 'autostart'}


def add_parsers(subparsers):
    init = subparsers.add_parser('init', help='Initialize private library settings without scanning books or enabling AI')
    init.add_argument('--timezone', help='IANA timezone for new-library monthly accounting, e.g. America/New_York')
    subparsers.add_parser('doctor', help='Inspect local setup; never starts a server or calls a paid provider')
    demo = subparsers.add_parser('demo', help='Serve a temporary synthetic library with paid operations disabled')
    demo.add_argument('--port', type=int, default=18768)
    autostart = subparsers.add_parser('autostart', help='Manage an optional user-local macOS login service')
    autostart.add_argument('action', choices=['install', 'status', 'remove'])
    autostart.add_argument('--port', type=int)


def initialize(lib, timezone=None):
    from .public_config import initialize as initialize_config
    initialize_config(lib, timezone=timezone)
    with writer(lib.state):
        lib._initialize()
    return {'initialized': True, 'root': str(lib.root), 'state': str(lib.state),
            'message': 'Private settings are ready. No books were scanned and no AI work was started.'}


def checkout_path():
    root = Path(__file__).resolve().parents[2]
    return root if (root / 'bin' / 'library').is_file() and (root / 'pyproject.toml').is_file() else None


def port_in_use(host, port):
    family = socket.AF_INET6 if ':' in host else socket.AF_INET
    try:
        with socket.socket(family) as connection:
            connection.settimeout(0.15)
            return connection.connect_ex((host, port)) == 0
    except OSError:
        return False


def doctor(lib, *, explicit_root=False):
    from .public_config import settings_status
    checks = []

    def check(name, ok, detail, *, required=True):
        checks.append({'name': name, 'ok': bool(ok), 'required': required, 'detail': detail})

    check('python', supported_python(sys.version_info), f'{platform.python_version()}; supported source setup uses Python {python_range()}')
    check('platform', sys.platform == 'darwin', f'{platform.system()} {platform.release()} ({platform.machine()}); macOS is the supported preview platform', required=False)
    check('library_root', lib.root.is_dir(), str(lib.root) if lib.root.is_dir() else 'Directory missing. Run library init or restore the configured directory.')
    ancestor = lib.state
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    check('private_state_writable', ancestor.is_dir() and os.access(ancestor, os.W_OK | os.X_OK), str(lib.state))
    try:
        with open_sqlite(':memory:') as database:
            database.execute('CREATE VIRTUAL TABLE diagnostic_fts USING fts5(text)')
        check('sqlite_fts5', True, f'SQLite {sqlite3.sqlite_version} supports FTS5')
    except sqlite3.Error:
        check('sqlite_fts5', False, 'This Python SQLite build lacks FTS5.')
    for module in ('fastapi', 'uvicorn', 'lxml', 'numpy', 'usearch', 'httpx', 'tiktoken', 'PIL', 'openai'):
        check(f'dependency:{module}', importlib.util.find_spec(module) is not None, 'Installed' if importlib.util.find_spec(module) else 'Run bin/setup to install dependencies.')
    built = Path(__file__).parent / 'frontend_dist' / 'index.html'
    check('frontend', built.is_file(), 'Built UI found' if built.is_file() else 'Run npm --prefix frontend ci and npm --prefix frontend run build.')
    try:
        host, port, address = server_address(lib)
        check('local_address', True, address)
        busy = port_in_use(host, port)
        check('port_available', not busy, 'Already in use; reuse the existing service or choose another port.' if busy else 'Available', required=False)
    except ValueError as exc:
        check('local_address', False, str(exc))
        address = None
    config = read_json(lib.state / 'config.json', {})
    key_source = 'OPENAI_API_KEY' if os.environ.get('OPENAI_API_KEY', '').strip() else 'LIBRARY_OPENAI_ENV' if os.environ.get('LIBRARY_OPENAI_ENV') else 'config.openai_env_file' if config.get('openai_env_file') else 'not configured'
    try:
        from .semantic import api_key
        ready = bool(api_key(lib.state))
        key_detail = f'Available via {key_source}; account/model access has not been checked'
    except ValueError:
        ready = False
        key_detail = 'Unavailable or unreadable; optional AI remains unavailable. No secret value is shown.'
    check('openai_credentials', ready, key_detail, required=False)
    if ready and key_source in {'OPENAI_API_KEY', 'LIBRARY_OPENAI_ENV'} and not config.get('openai_env_file'):
        check('background_credentials', False,
              'Shell credentials are not copied to login startup. Set config.openai_env_file to an existing private credential file before background AI work.', required=False)
    legacy = Path.home() / 'Library' / 'LaunchAgents' / 'org.sethwilson.book-library.plist'
    check('legacy_autostart', not legacy.exists(), 'Existing legacy login service found; it was left untouched.' if legacy.exists() else 'No legacy login service found', required=False)
    source = 'explicit --root' if explicit_root else os.environ.get('READING_ROOM_ROOT_SOURCE', 'LIBRARY_ROOT' if os.environ.get('LIBRARY_ROOT') else 'default')
    return {'ok': all(c['ok'] or not c['required'] for c in checks), 'root': str(lib.root),
            'root_source': source, 'state': str(lib.state), 'local_base_url': address,
            'checks': checks, 'settings': settings_status(lib),
            'provider_requests': 0, 'message': 'Diagnostics are local and read-only. No paid request or book scan was made.'}


def run(args, lib):
    if args.command == 'init':
        return initialize(lib, args.timezone)
    if args.command == 'doctor':
        return doctor(lib, explicit_root=args.root is not None)
    if args.command == 'demo':
        from .demo import serve
        return serve(args.port)
    if args.command == 'autostart':
        from .autostart import manage
        return manage(lib, args.action, port=args.port)
    raise ValueError('Unknown onboarding command.')
