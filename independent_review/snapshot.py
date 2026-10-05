"""Immutable two-commit repository snapshot read through git plumbing.

Prepare fetches the PR head and its merge base into a bare repository and
archives it. Inference jobs extract that archive and either read objects
directly (engine-implemented tools) or check out the head for a sandboxed CLI.
Git never runs hooks, filters, credential helpers or network operations here.
"""

import base64
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

from .core import ReviewError, safe_path


ARCHIVE = 'snapshot.tar.gz'
REFS = {'head': 'refs/review/head', 'base': 'refs/review/base'}
MAX_ARCHIVE_BYTES = 1_000_000_000
MAX_BLOB_BYTES = 4_000_000
GIT_TIMEOUT_SECONDS = 60
SHA = re.compile(r'[0-9a-f]{40}')


def git_environment(home):
    """Return an environment without user or system git configuration."""
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home), 'LANG': 'C.UTF-8',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
           'GIT_ASKPASS': 'true', 'GIT_OPTIONAL_LOCKS': '0', 'GIT_NO_REPLACE_OBJECTS': '1'}
    return env


# Applied to every invocation; repository config cannot re-enable these.
SAFE_OPTIONS = ('-c', 'core.hooksPath=' + os.devnull, '-c', 'core.fsmonitor=false',
                '-c', 'core.symlinks=false', '-c', 'protocol.allow=never',
                '-c', 'protocol.https.allow=user', '-c', 'protocol.file.allow=user',
                '-c', 'credential.helper=', '-c', 'diff.external=', '-c', 'core.pager=cat')


def run_git(git_dir, args, home, extra_env=None, timeout=GIT_TIMEOUT_SECONDS):
    env = git_environment(home)
    env.update(extra_env or {})
    try:
        process = subprocess.run(['git', '--git-dir', str(git_dir), *SAFE_OPTIONS, *args],
                                 env=env, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ReviewError('snapshot_git_timeout') from None
    except OSError:
        raise ReviewError('snapshot_git_unavailable') from None
    return process


def checked(process, code):
    if process.returncode != 0:
        raise ReviewError(code)
    return process.stdout


def create(remote, head_sha, base_sha, out_dir, token=None):
    """Fetch two commits (depth 1) into a bare repository and archive it.

    remote is an https URL (prepare) or a local file:// URL (offline replay).
    The token, when given, travels only in this process's environment.
    """
    if not SHA.fullmatch(head_sha or '') or not SHA.fullmatch(base_sha or ''):
        raise ReviewError('invalid_snapshot_identity')
    if not (remote.startswith('https://') or remote.startswith('file://')):
        raise ReviewError('invalid_snapshot_remote')
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='snapshot-') as temporary:
        root = Path(temporary)
        home = root / 'home'
        home.mkdir()
        git_dir = root / 'repo.git'
        env = {}
        if token:
            # Same header form as actions/checkout; never written to repository config.
            basic = base64.b64encode(('x-access-token:' + token).encode()).decode()
            env = {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'http.extraHeader',
                   'GIT_CONFIG_VALUE_0': 'AUTHORIZATION: basic ' + basic}
        subprocess.run(['git', 'init', '-q', '--bare', str(git_dir)], env=git_environment(home),
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)
        for sample in (git_dir / 'hooks').glob('*'):
            sample.unlink()
        wants = [head_sha] if head_sha == base_sha else [head_sha, base_sha]
        # A local replay source must serve unadvertised commits by SHA.
        local = ['-c', 'uploadpack.allowAnySHA1InWant=true'] if remote.startswith('file://') else []
        checked(run_git(git_dir, [*local, 'fetch', '-q', '--depth=1', '--no-tags', '--no-write-fetch-head',
                                  '--no-recurse-submodules', remote, *wants], home, env, timeout=600),
                'snapshot_fetch_failed')
        for name, sha in (('head', head_sha), ('base', base_sha)):
            checked(run_git(git_dir, ['cat-file', '-e', sha + '^{commit}'], home), 'snapshot_commit_missing')
            checked(run_git(git_dir, ['update-ref', REFS[name], sha], home), 'snapshot_ref_failed')
        archive = out_dir / ARCHIVE
        with tarfile.open(archive, 'w:gz') as handle:
            handle.add(git_dir, arcname='repo.git')
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            archive.unlink()
            raise ReviewError('snapshot_too_large')
    return {'archive': ARCHIVE, 'sha256': file_digest(archive), 'head_sha': head_sha, 'base_sha': base_sha}


def file_digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            value.update(block)
    return value.hexdigest()


class Snapshot:
    """Read-only access to the head and base commits of one archived snapshot."""

    def __init__(self, root, head_sha, base_sha):
        self.root = Path(root)
        self.git_dir = self.root / 'head' / '.git'
        self.home = self.root / 'git-home'
        self.shas = {'head': head_sha, 'base': base_sha}
        self._worktree = False

    @classmethod
    def open(cls, out_dir, record, root):
        """Verify and extract an archive created by create()."""
        archive = Path(out_dir) / record['archive']
        if record.get('archive') != ARCHIVE or file_digest(archive) != record['sha256']:
            raise ReviewError('snapshot_identity_mismatch')
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        staging = root / 'extract'
        with tarfile.open(archive, 'r:gz') as handle:
            for member in handle.getmembers():
                if not (member.isfile() or member.isdir()) or not member.name.startswith('repo.git'):
                    raise ReviewError('snapshot_archive_invalid')
            handle.extractall(staging, filter='data')
        value = cls(root, record['head_sha'], record['base_sha'])
        value.home.mkdir(exist_ok=True)
        (root / 'head').mkdir()
        shutil.move(str(staging / 'repo.git'), str(value.git_dir))
        staging.rmdir()
        for name, sha in value.shas.items():
            ref = checked(value.git(['rev-parse', '--verify', REFS[name] + '^{commit}']), 'snapshot_ref_missing')
            if ref.decode().strip() != sha:
                raise ReviewError('snapshot_identity_mismatch')
        return value

    def git(self, args, timeout=GIT_TIMEOUT_SECONDS):
        return run_git(self.git_dir, args, self.home, timeout=timeout)

    def checkout_head(self):
        """Materialize the head tree for a sandboxed CLI; symlinks become plain files."""
        if not self._worktree:
            checked(self.git(['config', 'core.bare', 'false']), 'snapshot_checkout_failed')
            checked(self.git(['config', 'core.symlinks', 'false']), 'snapshot_checkout_failed')
            checked(run_git(self.git_dir, ['--work-tree', str(self.git_dir.parent), 'checkout', '-q', '-f',
                                           '--detach', self.shas['head']], self.home, timeout=600),
                    'snapshot_checkout_failed')
            self._worktree = True
        return self.git_dir.parent

    def sha(self, rev):
        if rev not in self.shas:
            raise ReviewError('invalid_snapshot_revision')
        return self.shas[rev]

    def read_bytes(self, rev, path, limit=MAX_BLOB_BYTES):
        """Return blob bytes, or None when the path is absent, not a file or too large."""
        if not safe_path(path):
            return None
        spec = self.sha(rev) + ':' + path
        kind = self.git(['cat-file', '-t', spec])
        if kind.returncode != 0 or kind.stdout.strip() != b'blob':
            return None
        size = self.git(['cat-file', '-s', spec])
        if size.returncode != 0 or int(size.stdout.strip() or 0) > limit:
            return None
        return checked(self.git(['cat-file', 'blob', spec]), 'snapshot_read_failed')

    def read_text(self, rev, path, limit=MAX_BLOB_BYTES):
        """Return UTF-8 text or None for absent, binary, oversized or undecodable files."""
        data = self.read_bytes(rev, path, limit)
        if data is None or b'\x00' in data[:8192]:
            return None
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def list_files(self, rev, prefix=''):
        args = ['ls-tree', '-r', '--name-only', '-z', self.sha(rev)]
        if prefix:
            if not safe_path(prefix.rstrip('/')):
                raise ReviewError('invalid_snapshot_path')
            args += ['--', prefix]
        output = checked(self.git(args), 'snapshot_list_failed')
        return [item.decode('utf-8', 'replace') for item in output.split(b'\0') if item]

    def grep(self, rev, pattern, paths=(), ignore_case=False):
        """Return (path, line, text) matches of an extended regular expression."""
        if not isinstance(pattern, str) or not 1 <= len(pattern) <= 500 or '\0' in pattern:
            raise ReviewError('invalid_grep_pattern')
        args = ['grep', '-n', '-I', '-E', '--no-color', '--full-name']
        if ignore_case:
            args.append('-i')
        args += ['-e', pattern, self.sha(rev)]
        clean = [path for path in paths if path]
        if clean:
            if not all(safe_path(path.rstrip('/').replace('*', 'x')) for path in clean):
                raise ReviewError('invalid_snapshot_path')
            args += ['--', *clean]
        process = self.git(args)
        if process.returncode == 1:
            return []
        output = checked(process, 'invalid_grep_pattern')
        prefix = self.sha(rev) + ':'
        matches = []
        for row in output.decode('utf-8', 'replace').splitlines():
            if row.startswith(prefix):
                row = row[len(prefix):]
            path, _, rest = row.partition(':')
            line, _, text = rest.partition(':')
            if line.isdigit():
                matches.append((path, int(line), text))
        return matches

    def diff(self, path=None, context=3):
        """Unified merge-base...head diff, optionally for one path."""
        args = ['diff', '--no-ext-diff', '--no-textconv', '--no-color', '-M', f'-U{int(context)}',
                self.shas['base'], self.shas['head']]
        if path:
            if not safe_path(path):
                raise ReviewError('invalid_snapshot_path')
            args += ['--', path]
        return checked(self.git(args), 'snapshot_diff_failed').decode('utf-8', 'replace')

    def evidence_entry(self, path, patch=None, previous_filename=None):
        """Assemble the sources a quote may come from for one path."""
        if patch is None:
            body = self.diff(path)
            patch = body[body.find('\n@@') + 1:] if '\n@@' in body else ''
        return {'path': path, 'patch': patch,
                'head_text': self.read_text('head', path),
                'base_text': self.read_text('base', previous_filename or path)}
