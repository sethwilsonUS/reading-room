"""Public setup never scans personal books, grants AI consent, or replaces services."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from book_library import autostart, onboarding
from book_library.demo import create_demo, create_demo_app
from book_library.service import Library

CHECKOUT = Path(__file__).resolve().parents[1]


def script(name):
    spec = importlib.util.spec_from_file_location(name, CHECKOUT / 'scripts' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_and_diagnostics_do_not_scan_or_authorize(tmp_path, monkeypatch):
    lib = Library(tmp_path / 'private books')
    monkeypatch.setenv('OPENAI_API_KEY', 'dummy-secret-must-not-appear')
    onboarding.initialize(lib, 'UTC')
    config = json.loads((lib.state / 'config.json').read_text())
    assert not config.get('semantic', {}).get('enabled')
    assert not config.get('metadata_review', {}).get('enabled')
    assert not (lib.state / 'current.json').exists()
    assert not (lib.state / 'costs.jsonl').exists()
    before = {str(path): path.read_bytes() for path in lib.state.rglob('*') if path.is_file()}
    result = onboarding.doctor(lib, explicit_root=True)
    after = {str(path): path.read_bytes() for path in lib.state.rglob('*') if path.is_file()}
    assert before == after
    assert result['root_source'] == 'explicit --root'
    assert result['provider_requests'] == 0
    assert 'dummy-secret' not in json.dumps(result)


def test_init_preserves_existing_configuration(tmp_path):
    lib = Library(tmp_path)
    onboarding.initialize(lib, 'UTC')
    path = lib.state / 'config.json'
    config = json.loads(path.read_text())
    config['personal_setting'] = {'keep': True}
    path.write_text(json.dumps(config))
    onboarding.initialize(lib, 'America/New_York')
    assert json.loads(path.read_text()) == config


def test_doctor_missing_root_is_read_only(tmp_path):
    root = tmp_path / 'missing'
    report = onboarding.doctor(Library(root))
    assert not root.exists()
    assert report['ok'] is False
    assert any(c['name'] == 'library_root' and not c['ok'] for c in report['checks'])


def test_checkout_binding_precedence_and_missing_root(tmp_path):
    launcher = script('run_library')
    (tmp_path / '.reading-room').mkdir()
    root = tmp_path / 'book root'
    root.mkdir()
    (tmp_path / '.reading-room' / 'local.json').write_text(json.dumps({'library_root': str(root)}))
    assert launcher.environment(tmp_path, ['status'], {})['LIBRARY_ROOT'] == str(root)
    assert launcher.environment(tmp_path, ['status'], {'LIBRARY_ROOT': '/explicit/env'})['LIBRARY_ROOT'] == '/explicit/env'
    assert 'LIBRARY_ROOT' not in launcher.environment(tmp_path, ['--root', '/explicit/flag', 'status'], {})
    root.rmdir()
    with pytest.raises(ValueError, match='missing'):
        launcher.environment(tmp_path, ['status'], {})
    with pytest.raises(ValueError, match='missing'):
        launcher.environment(tmp_path, ['search', 'init'], {})
    assert launcher.environment(tmp_path, ['init'], {})['LIBRARY_ROOT'] == str(root)
    assert launcher.environment(tmp_path, ['doctor'], {})['LIBRARY_ROOT'] == str(root)


def test_invalid_binding_never_silently_selects_another_library(tmp_path):
    launcher = script('run_library')
    (tmp_path / '.reading-room').mkdir()
    path = tmp_path / '.reading-room' / 'local.json'
    path.write_text('{bad json')
    with pytest.raises(ValueError, match='invalid'):
        launcher.environment(tmp_path, ['status'], {})
    path.write_text(json.dumps({'library_root': 'relative'}))
    with pytest.raises(ValueError, match='absolute'):
        launcher.environment(tmp_path, ['status'], {})


def test_binding_save_preserves_other_local_state(tmp_path):
    setup = script('setup')
    (tmp_path / '.reading-room').mkdir()
    path = tmp_path / '.reading-room' / 'local.json'
    path.write_text(json.dumps({'unrelated': True}))
    setup.save_binding(tmp_path, tmp_path / 'books')
    assert json.loads(path.read_text())['unrelated'] is True


@pytest.mark.parametrize('version', ['v22.22.2', '22.23.0', 'v24.15.0', 'v24.16.1', 'v26.0.0', 'v27.1.0'])
def test_setup_accepts_locked_node_engine_ranges(version):
    assert script('setup').supported_node(version)


@pytest.mark.parametrize('version', ['v20.19.0', 'v22.15.0', 'v22.22.1', 'v23.11.0', 'v24.14.9', 'v25.0.0', 'v26.0.0-rc.1', 'not-node'])
def test_setup_rejects_unsupported_node_versions(version):
    assert not script('setup').supported_node(version)


@pytest.mark.parametrize(('version', 'expected'), [((3, 11, 9), False), ((3, 12, 0), True), ((3, 15, 2), True), ((3, 16, 0), False)])
def test_supported_python_range(version, expected):
    from book_library.install_defaults import supported_python
    assert supported_python(version) is expected
    assert script('setup').DEFAULTS.supported_python(version) is expected


def test_python_range_matches_package_metadata_and_launcher():
    import tomllib
    from book_library.install_defaults import MAX_PYTHON, MIN_PYTHON
    requires = tomllib.loads((CHECKOUT / 'pyproject.toml').read_text())['project']['requires-python']
    assert requires == f'>={MIN_PYTHON[0]}.{MIN_PYTHON[1]},<{MAX_PYTHON[0]}.{MAX_PYTHON[1] + 1}'
    launcher = (CHECKOUT / 'bin' / 'setup').read_text()
    expected = ' '.join(f'python3.{minor}' for minor in range(MAX_PYTHON[1], MIN_PYTHON[1] - 1, -1))
    assert f'for candidate in {expected}; do' in launcher


def test_default_library_root_is_documents_folder_after_explicit_choices(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('LIBRARY_ROOT', raising=False)
    assert Library().root == (tmp_path / 'Documents' / 'Reading Room').resolve()
    assert script('setup').DEFAULTS.default_library_root() == tmp_path / 'Documents' / 'Reading Room'
    monkeypatch.setenv('LIBRARY_ROOT', str(tmp_path / 'env books'))
    assert Library().root == (tmp_path / 'env books').resolve()
    assert Library(tmp_path / 'explicit').root == (tmp_path / 'explicit').resolve()


def test_synthetic_demo_search_read_cite_without_provider(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'dummy-do-not-use')
    from book_library import semantic, review_ai
    def fail(*args, **kwargs):
        raise AssertionError('Demo contacted a paid provider')
    monkeypatch.setattr(semantic, 'embed_openai', fail)
    monkeypatch.setattr(review_ai, 'suggest', fail)
    lib = create_demo(tmp_path / 'demo')
    hits = lib.search('copper birds', engine='openai')['direct']
    assert hits
    assert lib.read(hits[0]['id'])['blocks']
    assert 'full_note' in lib.cite(hits[0]['id'])
    assert not (lib.state / 'costs.jsonl').exists()
    app = create_demo_app(lib)
    with TestClient(app, base_url='http://127.0.0.1:18765') as client:
        token = client.get('/api/ui/bootstrap').json()['csrf']
        result = client.post('/api/ui/search', json={'fields': {'q': 'copper birds'}},
                             headers={'Origin': 'http://127.0.0.1:18765', 'X-CSRF-Token': token})
        assert result.status_code == 200, result.text
        assert result.json()['data']['result']['direct']
        assert client.post('/api/ui/sync', json={'fields': {}}).status_code == 403
        assert client.get('/__testing/state').status_code == 404


def service_fixture(tmp_path, monkeypatch):
    checkout = tmp_path / 'source checkout'
    (checkout / '.venv' / 'bin').mkdir(parents=True)
    (checkout / '.venv' / 'bin' / 'python').touch()
    bundle = checkout / 'src' / 'book_library' / 'frontend_dist'
    bundle.mkdir(parents=True)
    (bundle / 'index.html').write_text('demo')
    lib = Library(tmp_path / 'books')
    lib.root.mkdir()
    monkeypatch.setattr(autostart.sys, 'platform', 'darwin')
    monkeypatch.setattr(onboarding, 'port_in_use', lambda *args: False)
    calls = []
    loaded = False
    def run(args, **kwargs):
        nonlocal loaded
        calls.append(args)
        if args[1] == 'print':
            return SimpleNamespace(returncode=0 if loaded else 1)
        loaded = args[1] == 'bootstrap'
        return SimpleNamespace(returncode=0)
    return checkout, lib, calls, run


def test_autostart_install_status_remove_and_idempotency(tmp_path, monkeypatch):
    checkout, lib, calls, run = service_fixture(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    kwargs = {'checkout': checkout, 'home': home, 'run': run}
    created = autostart.manage(lib, 'install', **kwargs)
    assert created['installed']
    path = Path(created['plist'])
    payload = plistlib.loads(path.read_bytes())
    assert payload['ProgramArguments'][0] == str(checkout / '.venv' / 'bin' / 'python')
    assert payload['ProgramArguments'][4] == str(lib.root)
    assert 'EnvironmentVariables' not in payload
    assert autostart.manage(lib, 'install', **kwargs)['already_installed']
    assert sum(call[1] == 'bootstrap' for call in calls) == 1
    assert autostart.manage(lib, 'status', **kwargs)['loaded']
    assert autostart.manage(lib, 'remove', **kwargs)['removed']
    assert not path.exists()
    assert lib.root.is_dir()
    assert not autostart.manage(lib, 'remove', **kwargs)['removed']


def test_autostart_preserves_foreign_or_edited_services(tmp_path, monkeypatch):
    checkout, lib, calls, run = service_fixture(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    target = home / 'Library' / 'LaunchAgents' / f'{autostart.LABEL}.plist'
    target.parent.mkdir(parents=True)
    target.write_text('foreign service')
    kwargs = {'checkout': checkout, 'home': home, 'run': run}
    with pytest.raises(ValueError, match='left untouched'):
        autostart.manage(lib, 'install', **kwargs)
    with pytest.raises(ValueError, match='left untouched'):
        autostart.manage(lib, 'remove', **kwargs)
    assert target.read_text() == 'foreign service'
    assert not any(call[1] in {'bootstrap', 'bootout'} for call in calls)


def test_autostart_busy_port_never_stops_existing_server(tmp_path, monkeypatch):
    checkout, lib, calls, run = service_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(onboarding, 'port_in_use', lambda *args: True)
    with pytest.raises(ValueError, match='already in use'):
        autostart.manage(lib, 'install', checkout=checkout, home=tmp_path / 'home', run=run)
    assert not any(call[1] in {'bootstrap', 'bootout'} for call in calls)


def test_skill_bundle_preflight_idempotency_and_safe_uninstall(tmp_path):
    manager = script('manage_skills')
    checkout = tmp_path / 'source checkout'
    for name in manager.NAMES:
        folder = checkout / 'skills' / name
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text(name)
    home = tmp_path / 'home'
    destination = home / '.agents' / 'skills'
    destination.mkdir(parents=True)
    conflict = destination / 'bookshare-download'
    conflict.mkdir()
    with pytest.raises(ValueError, match='Nothing was changed'):
        manager.manage('install', checkout=checkout, home=home)
    assert list(destination.iterdir()) == [conflict]
    conflict.rmdir()
    assert manager.manage('install', checkout=checkout, home=home)['created'] == 3
    assert manager.manage('install', checkout=checkout, home=home)['created'] == 0
    assert manager.manage('status', checkout=checkout, home=home)['skills'][0]['state'] == 'managed'
    (destination / 'bookshare-find').unlink()
    (destination / 'bookshare-find').mkdir()
    assert len(manager.manage('uninstall', checkout=checkout, home=home)['removed']) == 2
    assert (destination / 'bookshare-find').is_dir()
    assert manager.manage('uninstall', checkout=checkout, home=home)['removed'] == []


def test_legacy_personal_skill_is_not_shadowed(tmp_path, monkeypatch):
    manager = script('manage_skills')
    monkeypatch.delenv('CODEX_HOME', raising=False)
    checkout = tmp_path / 'checkout'
    for name in manager.NAMES:
        folder = checkout / 'skills' / name
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text(name)
    home = tmp_path / 'home'
    (home / '.codex' / 'skills' / 'library-search').mkdir(parents=True)
    with pytest.raises(ValueError, match='library-search'):
        manager.manage('install', checkout=checkout, home=home)
    assert not (home / '.agents').exists()


def test_symlinked_skill_launcher_works_from_other_directory(tmp_path):
    checkout = tmp_path / 'source checkout'
    scripts = checkout / 'skills' / 'library-search' / 'scripts'
    scripts.mkdir(parents=True)
    shutil.copy2(CHECKOUT / 'skills' / 'library-search' / 'scripts' / 'library', scripts / 'library')
    (checkout / 'pyproject.toml').write_text('')
    (checkout / 'bin').mkdir()
    executable = checkout / 'bin' / 'library'
    executable.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    executable.chmod(0o755)
    installed = tmp_path / 'installed skills'
    installed.mkdir()
    (installed / 'library-search').symlink_to(scripts.parent, target_is_directory=True)
    other = tmp_path / 'another project'
    other.mkdir()
    result = subprocess.run([str(installed / 'library-search' / 'scripts' / 'library'), '--root', '/books with spaces', 'status', '--json'], cwd=other, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ['--root', '/books with spaces', 'status', '--json']


def skill_checkout(folder):
    manager = script('manage_skills')
    for name in manager.NAMES:
        source = folder / 'skills' / name
        source.mkdir(parents=True)
        (source / 'SKILL.md').write_text(name)
    return manager


def test_project_discovery_links_move_with_checkout_and_install_idempotently(tmp_path):
    checkout = tmp_path / 'source checkout'
    manager = skill_checkout(checkout)
    assert manager.manage('check', checkout=checkout, project=True)['ready']
    assert not (checkout / '.agents').exists()
    assert manager.manage('install', checkout=checkout, project=True)['created'] == 3
    assert manager.manage('install', checkout=checkout, project=True)['created'] == 0
    for name in manager.NAMES:
        link = checkout / '.agents' / 'skills' / name
        assert link.readlink() == Path('../../skills') / name
        assert link.resolve() == checkout / 'skills' / name
    moved = tmp_path / 'moved checkout'
    checkout.rename(moved)
    assert all(row['state'] == 'managed' for row in manager.manage('status', checkout=moved, project=True)['skills'])
    assert len(manager.manage('uninstall', checkout=moved, project=True)['removed']) == 3
    assert manager.manage('uninstall', checkout=moved, project=True)['removed'] == []
    assert all((moved / 'skills' / name / 'SKILL.md').is_file() for name in manager.NAMES)


def test_project_preflight_preserves_customized_skill_and_creates_no_other_links(tmp_path):
    checkout = tmp_path / 'checkout'
    manager = skill_checkout(checkout)
    destination = checkout / '.agents' / 'skills'
    custom = destination / 'bookshare-download'
    custom.mkdir(parents=True)
    (custom / 'SKILL.md').write_text('Customized instructions')
    for action in ('check', 'install'):
        with pytest.raises(ValueError, match='Nothing was changed'):
            manager.manage(action, checkout=checkout, project=True)
    assert list(destination.iterdir()) == [custom]
    assert (custom / 'SKILL.md').read_text() == 'Customized instructions'


@pytest.mark.parametrize('relative', ['.agents', '.agents/skills'])
def test_project_discovery_never_writes_through_external_parent_symlinks(tmp_path, relative):
    checkout = tmp_path / 'checkout'
    manager = skill_checkout(checkout)
    external = tmp_path / 'personal skills'
    external.mkdir()
    target = checkout / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match='left untouched'):
        manager.manage('install', checkout=checkout, project=True)
    assert list(external.iterdir()) == []
    assert target.is_symlink()


def test_setup_project_links_restore_older_managed_global_links(tmp_path):
    checkout = tmp_path / 'checkout'
    manager = skill_checkout(checkout)
    home = tmp_path / 'home'
    destination = home / '.agents' / 'skills'
    destination.mkdir(parents=True)
    for name in manager.NAMES:
        (destination / name).symlink_to(checkout / '.agents' / 'skills' / name, target_is_directory=True)
    manager.manage('install', checkout=checkout, project=True)
    assert manager.manage('install', checkout=checkout, home=home)['created'] == 0
    assert all(row['state'] == 'managed' for row in manager.manage('status', checkout=checkout, home=home)['skills'])
    assert len(manager.manage('uninstall', checkout=checkout, home=home)['removed']) == 3
    assert all((checkout / 'skills' / name / 'SKILL.md').is_file() for name in manager.NAMES)
