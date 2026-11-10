"""The generated Reading Room.app is a signed applet that opens its own checkout."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

import pytest

CHECKOUT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != 'darwin' or not shutil.which('osacompile'),
                                reason='Reading Room.app is built with macOS tools')


def builder():
    spec = importlib.util.spec_from_file_location('build_mac_app', CHECKOUT / 'scripts' / 'build_mac_app.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    target = tmp_path_factory.mktemp('checkout')
    app, rebuilt = builder().build(target)
    assert rebuilt
    return target, app


def test_bundle_is_a_signed_applet_with_reading_room_identity(built):
    _, app = built
    with (app / 'Contents' / 'Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    assert info['CFBundleExecutable'] == 'applet'
    assert info['CFBundleName'] == 'Reading Room'
    assert info['CFBundleIconFile'] == 'AppIcon'
    assert 'CFBundleIconName' not in info  # would let the default applet icon win
    resources = app / 'Contents' / 'Resources'
    assert (resources / 'AppIcon.icns').stat().st_size > 0
    assert not (resources / 'Assets.car').exists()
    assert (resources / 'launch.sh').stat().st_mode & 0o111
    subprocess.run(['codesign', '--verify', '--strict', str(app)], check=True)


def test_unchanged_sources_do_not_rebuild_but_force_does(built):
    target, app = built
    module = builder()
    assert module.build(target) == (app, False)
    assert module.build(target, force=True) == (app, True)


def test_launcher_runs_the_containing_checkouts_library_app(built, tmp_path):
    _, app = built
    checkout = tmp_path / 'reading room checkout'
    shutil.copytree(app, checkout / 'Reading Room.app')
    called = tmp_path / 'called'
    (checkout / 'bin').mkdir()
    stub = checkout / 'bin' / 'library'
    stub.write_text(f'#!/bin/sh\nprintf "%s" "$*" > "{called}"\n')
    stub.chmod(0o755)
    subprocess.run([str(checkout / 'Reading Room.app' / 'Contents' / 'Resources' / 'launch.sh')], check=True)
    assert called.read_text() == 'app'
    assert (checkout / '.reading-room' / 'app.log').exists()


def test_transparent_icon_margin_and_corners(tmp_path):
    from PIL import Image
    module = builder()
    rendered = tmp_path / 'render.png'
    Image.new('RGBA', (256, 256), (51, 39, 31, 255)).save(rendered)
    icon = module.transparent_artwork(rendered, module.corner_radius('<svg viewBox="0 0 64 64"><rect rx="8"/></svg>'))
    assert icon.size == (1024, 1024)
    assert icon.getpixel((0, 0))[3] == 0          # outside the icon grid
    assert icon.getpixel((101, 101))[3] == 0      # rounded corner of the artwork
    assert icon.getpixel((512, 512)) == (51, 39, 31, 255)
