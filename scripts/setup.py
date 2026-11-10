"""Repeatable source installation; never enables AI or starts a server."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CHECKOUT = Path(__file__).resolve().parents[1]
NODE_REQUIREMENT = 'Node.js 22.22.2+ within 22.x, 24.15.0+ within 24.x, or 26.0.0+ (24.x is recommended)'


def _install_defaults():
    # Load the dependency-free defaults module directly: the package's runtime
    # dependencies are not installed yet when setup starts.
    path = CHECKOUT / 'src' / 'book_library' / 'install_defaults.py'
    spec = importlib.util.spec_from_file_location('reading_room_install_defaults', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DEFAULTS = _install_defaults()


def supported_node(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value.strip())
    if not match:
        return False
    version = tuple(map(int, match.groups()))
    return ((version[0] == 22 and version >= (22, 22, 2)) or
            (version[0] == 24 and version >= (24, 15, 0)) or version >= (26, 0, 0))


def prerequisites(run=subprocess.run):
    if not DEFAULTS.supported_python(sys.version_info):
        raise ValueError(f'Use Python {DEFAULTS.python_range()} to run setup.')
    if sys.platform != 'darwin':
        raise ValueError('This technical preview supports macOS. Other platforms are experimental.')
    if not shutil.which('node') or not shutil.which('npm'):
        raise ValueError(f'Install {NODE_REQUIREMENT}, including npm, from https://nodejs.org/, then retry.')
    value = run(['node', '--version'], capture_output=True, text=True, check=True).stdout.strip()
    if not supported_node(value):
        raise ValueError(f'{NODE_REQUIREMENT} is required by the locked frontend dependencies. Found {value!r}; install a supported version and retry.')


def save_binding(checkout, root):
    folder = checkout / '.reading-room'
    folder.mkdir(exist_ok=True)
    path = folder / 'local.json'
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError('Existing checkout setup state is not a JSON object; repair it before setup.')
    data.update(schema_version=1, library_root=str(root))
    fd, name = tempfile.mkstemp(prefix='.setup-', dir=folder)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if Path(name).exists():
            Path(name).unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='Directory containing your books; private state stays inside its .library folder')
    parser.add_argument('--non-interactive', action='store_true', help='Use supplied or saved root without prompting; never opt in to paid features')
    args = parser.parse_args()
    try:
        prerequisites()
        # Check all discovery destinations before installing dependencies or
        # initializing a library; customized project skills are never replaced.
        skill_manager = CHECKOUT / 'scripts' / 'manage_skills.py'
        subprocess.run([sys.executable, str(skill_manager), 'check', '--project'], check=True)
        saved = CHECKOUT / '.reading-room' / 'local.json'
        saved_data = json.loads(saved.read_text()) if saved.exists() else {}
        if not isinstance(saved_data, dict):
            raise ValueError('Existing checkout setup state is not a JSON object; repair it before setup.')
        saved_root = saved_data.get('library_root')
        root = args.root or os.environ.get('LIBRARY_ROOT') or saved_root or DEFAULTS.default_library_root()
        if not args.non_interactive and args.root is None and sys.stdin.isatty():
            value = input(f'Books directory [{root}]: ').strip()
            if value:
                root = value
        root = Path(root).expanduser().resolve()
        if root == CHECKOUT or CHECKOUT in root.parents:
            raise ValueError('Choose a books directory outside the source checkout, so books stay out of Git.')
        print(f'Application: {CHECKOUT}\nBooks: {root}\nPrivate state: {root / ".library"}', flush=True)
        python = CHECKOUT / '.venv' / 'bin' / 'python'
        if python.exists():
            version = subprocess.run([str(python), '-c', 'import sys; print("%s.%s" % sys.version_info[:2])'], check=True, capture_output=True, text=True).stdout.strip()
            if not DEFAULTS.supported_python(tuple(map(int, version.split('.')))):
                raise ValueError(f'The existing .venv uses unsupported Python {version}; Python {DEFAULTS.python_range()} is required. Preserve or remove it yourself before retrying; setup will not replace it.')
        else:
            subprocess.run([sys.executable, '-m', 'venv', str(CHECKOUT / '.venv')], check=True)
        has_pip = subprocess.run([str(python), '-c', 'import importlib.util; raise SystemExit(0 if importlib.util.find_spec("pip") else 1)'], check=False)
        if has_pip.returncode:
            subprocess.run([str(python), '-m', 'ensurepip', '--upgrade'], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(CHECKOUT / 'requirements.lock.txt')], cwd=CHECKOUT, check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--no-deps', '-e', str(CHECKOUT)], cwd=CHECKOUT, check=True)
        for operation in (['ci'], ['run', 'types'], ['run', 'build']):
            subprocess.run(['npm', '--prefix', str(CHECKOUT / 'frontend'), *operation], cwd=CHECKOUT, check=True)
        # Double-clickable launcher at the checkout root (generated, ignored by Git).
        subprocess.run([str(python), str(CHECKOUT / 'scripts' / 'build_mac_app.py')], cwd=CHECKOUT, check=True)
        subprocess.run([str(python), str(skill_manager), 'install', '--project'], check=True)
        subprocess.run([str(CHECKOUT / 'bin' / 'library'), '--root', str(root), 'init', '--json'], cwd=CHECKOUT, check=True)
        save_binding(CHECKOUT, root)
        result = subprocess.run([str(CHECKOUT / 'bin' / 'library'), 'doctor', '--json'], cwd=CHECKOUT, check=True, text=True, capture_output=True)
        print(result.stdout, end='')
        if not json.loads(result.stdout).get('ok'):
            raise ValueError('Setup finished, but diagnostics need attention. Fix the reported issue and rerun bin/library doctor --json.')
        print('\nReady. Project skills are linked for Codex discovery; restart Codex if needed.\nOpen your library by double-clicking Reading Room.app in this folder (or run bin/library app).\nTry bin/library demo first if you like; bin/library serve still serves the browser interface.\nAI remains disabled for new libraries. Existing library permissions are preserved.')
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Setup stopped: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
