"""Build `Reading Room.app` at the checkout root from the sources in macos/.

The app is generated, not committed: an AppleScript applet that runs
macos/launch.sh, which opens `bin/library app`. An applet is used because
macOS attributes privacy (TCC) access to it, so it can ask for permission to
open a checkout or library inside ~/Documents; a shell-script executable is
attributed to /bin/sh and silently denied.

The build is skipped when the existing app was made from identical sources,
because a rebuilt ad-hoc signature makes macOS ask for permission again.
Uses macOS built-ins (osacompile, qlmanage, sips, iconutil, plutil, codesign)
and Pillow from the project environment.

    .venv/bin/python scripts/build_mac_app.py [--force]
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = 'Reading Room.app'
SOURCES = (ROOT / 'macos' / 'launcher.applescript', ROOT / 'macos' / 'launch.sh',
           ROOT / 'src' / 'book_library' / 'static' / 'reading-room-mark.svg', Path(__file__).resolve())
DIGEST_KEY = 'ReadingRoomSourceDigest'
BUNDLE_ID = 'org.reading-room.launcher'
CANVAS = 1024
ARTWORK = 824  # macOS icon grid: artwork inside a transparent margin
ICON_SIZES = (16, 32, 128, 256, 512)


def source_digest(sources=SOURCES) -> str:
    digest = hashlib.sha256()
    for path in sources:
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def is_current(app: Path, digest: str) -> bool:
    plist = app / 'Contents' / 'Info.plist'
    if not plist.is_file():
        return False
    with plist.open('rb') as stream:
        return plistlib.load(stream).get(DIGEST_KEY) == digest


def corner_radius(svg: str) -> float:
    """Corner radius of the mark's background, as a fraction of its width."""
    view = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    rect = re.search(r'<rect[^>]*\brx="([\d.]+)"', svg)
    if not view or view.group(1) != view.group(2) or not rect:
        raise ValueError('The mark must be a square SVG whose first rect has an rx corner radius.')
    return float(rect.group(1)) / int(view.group(1))


def transparent_artwork(rendered: Path, radius: float) -> Image.Image:
    """Mask qlmanage's opaque render to its rounded square on a clear canvas."""
    art = Image.open(rendered).convert('RGBA')
    big = art.width * 4  # draw the mask large, then downsample for smooth corners
    mask = Image.new('L', (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius * big, fill=255)
    art.putalpha(mask.resize(art.size, Image.LANCZOS))
    canvas = Image.new('RGBA', (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - ARTWORK) // 2
    canvas.paste(art.resize((ARTWORK, ARTWORK), Image.LANCZOS), (offset, offset))
    return canvas


def build_icon(mark: Path, destination: Path, work: Path) -> None:
    svg = mark.read_text()
    subprocess.run(['qlmanage', '-t', '-s', str(CANVAS), '-o', str(work), str(mark)], check=True, capture_output=True)
    master = work / 'master.png'
    transparent_artwork(work / (mark.name + '.png'), corner_radius(svg)).save(master)
    iconset = work / 'AppIcon.iconset'
    iconset.mkdir()
    for size in ICON_SIZES:
        for scale in (1, 2):
            pixels = str(size * scale)
            name = f'icon_{size}x{size}' + ('@2x' if scale == 2 else '') + '.png'
            subprocess.run(['sips', '-z', pixels, pixels, str(master), '--out', str(iconset / name)], check=True, capture_output=True)
    subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(destination)], check=True)


def build(target_dir: Path = ROOT, *, force: bool = False) -> tuple[Path, bool]:
    """Build the app in `target_dir` (the checkout root); return (path, rebuilt)."""
    app = target_dir / APP_NAME
    digest = source_digest()
    if not force and is_current(app, digest):
        return app, False
    applescript, launcher, mark = SOURCES[:3]
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        staged = work / APP_NAME
        subprocess.run(['osacompile', '-o', str(staged), str(applescript)], check=True, capture_output=True)
        resources = staged / 'Contents' / 'Resources'
        shutil.copy2(launcher, resources / 'launch.sh')
        (resources / 'launch.sh').chmod(0o755)
        # Replace the default applet icon. Its asset catalog would override
        # CFBundleIconFile, so remove it with the old icon.
        for default in ('applet.icns', 'Assets.car'):
            (resources / default).unlink(missing_ok=True)
        build_icon(mark, resources / 'AppIcon.icns', work)
        plist_path = staged / 'Contents' / 'Info.plist'
        with plist_path.open('rb') as stream:
            info = plistlib.load(stream)
        info.pop('CFBundleIconName', None)
        info.update(CFBundleIconFile='AppIcon', CFBundleName='Reading Room', CFBundleDisplayName='The Reading Room',
                    CFBundleIdentifier=BUNDLE_ID, **{DIGEST_KEY: digest})
        with plist_path.open('wb') as stream:
            plistlib.dump(info, stream)
        # Editing the bundle invalidates osacompile's signature; re-sign ad hoc.
        subprocess.run(['codesign', '--force', '--sign', '-', str(staged)], check=True, capture_output=True)
        subprocess.run(['codesign', '--verify', '--strict', str(staged)], check=True, capture_output=True)
        if app.exists():
            shutil.rmtree(app)
        shutil.move(str(staged), str(app))
    return app, True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--force', action='store_true', help='Rebuild even if the app matches its sources')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        print('Reading Room.app is built only on macOS.', file=sys.stderr)
        return 1
    app, rebuilt = build(force=args.force)
    print(f'{"Built" if rebuilt else "Up to date:"} {app.relative_to(ROOT)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
