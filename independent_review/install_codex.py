"""Install a reviewed official Codex package without shell setup or auto-updating."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request


MAX_ARCHIVE_BYTES = 400_000_000
MAX_PACKAGE_BYTES = 1_500_000_000
MAX_MEMBER_BYTES = 600_000_000
MAX_MEMBERS = 2000


def release():
    return json.loads(Path(__file__).with_name('codex-release.json').read_text())


def platform_key():
    machine = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine(), platform.machine())
    return platform.system().lower() + '_' + machine


def check_package(root, version, target):
    """Validate an extracted package against the pin and return its manifest."""
    try:
        manifest = json.loads((Path(root) / 'codex-package.json').read_text())
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Invalid Codex package') from None
    if (not isinstance(manifest, dict) or manifest.get('version') != version or manifest.get('target') != target
            or manifest.get('entrypoint') != 'bin/codex' or manifest.get('pathDir') != 'codex-path'):
        raise ValueError('Codex package does not match the pinned release')
    if not (Path(root) / 'bin/codex').is_file():
        raise ValueError('Invalid Codex package')
    return manifest


def download(url, sha256, handle):
    value, total = hashlib.sha256(), 0
    with urllib.request.urlopen(url, timeout=90) as response:
        for block in iter(lambda: response.read(1 << 20), b''):
            total += len(block)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError('Codex archive too large')
            value.update(block)
            handle.write(block)
    if value.hexdigest() != sha256:
        raise ValueError('Official Codex release checksum mismatch')


def extract(archive_path, staging):
    with tarfile.open(archive_path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) > MAX_MEMBERS:
            raise ValueError('Invalid Codex archive')
        total = 0
        for member in members:
            name = member.name.removeprefix('./').rstrip('/')
            if (not (member.isfile() or member.isdir()) or member.size > MAX_MEMBER_BYTES or not name
                    or name.startswith('/') or any(part in ('', '.', '..') for part in name.split('/'))):
                raise ValueError('Invalid Codex archive')
            total += member.size
        if total > MAX_PACKAGE_BYTES:
            raise ValueError('Invalid Codex archive')
        archive.extractall(staging, members=members, filter='data')


def install(destination):
    """Download, verify and extract the pinned package; return the package root."""
    pin = release()
    key = platform_key()
    if key not in pin['platforms']:
        raise ValueError('Unsupported Codex platform')
    asset = pin['platforms'][key]
    destination = Path(destination).absolute()
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError('Codex destination is not empty')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.codex-install-', dir=destination.parent) as temporary:
        archive = Path(temporary) / 'codex-package.tar.gz'
        with archive.open('wb') as handle:
            download(asset['url'], asset['sha256'], handle)
        staging = Path(temporary) / 'package'
        staging.mkdir()
        extract(archive, staging)
        check_package(staging, pin['version'], asset['target'])
        if destination.exists():
            destination.rmdir()
        os.replace(staging, destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    root = install(parser.parse_args().out)
    print('Verified Codex', release()['version'], platform_key(), root)
