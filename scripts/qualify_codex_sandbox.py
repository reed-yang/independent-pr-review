"""Qualify Codex's read-only sandbox on this host without any model provider.

The outer process starts a shell that holds canary secrets in its initial
environment (like an Actions step shell), which runs this script with --inner.
The inner process hardens itself, starts a loopback listener and runs a probe
through `codex sandbox -P :read-only` with the runner's generated config. The
probe must not write, connect, or see the canaries. Exit 1 on any violation.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from independent_review.codex_runner import config_toml, locate_codex  # noqa: E402
from independent_review.hardening import harden_process  # noqa: E402


CANARIES = ('REVIEW_PROXY_TOKEN', 'QUALIFY_API_KEY', 'QUALIFY_SECRET')

PROBE = r'''
import ctypes, ctypes.util, json, os, socket, subprocess, sys
checkout, tmpdir, port, canaries, pids = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4].split(','), [int(p) for p in sys.argv[5].split(',')]
pids.append(os.getppid())
result = {}

def write(path):
    try:
        with open(path, 'w') as handle:
            handle.write('x')
        return 'allowed'
    except OSError:
        return 'blocked'

def connect(host, port):
    try:
        socket.create_connection((host, port), timeout=3).close()
        return 'connected'
    except OSError:
        return 'blocked'

def mac_environ(pid):
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    argmax, size = ctypes.c_int(), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0) != 0:
        return None
    buffer, size = ctypes.create_string_buffer(argmax.value), ctypes.c_size_t(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buffer, ctypes.byref(size), None, 0) != 0:
        return None
    return buffer.raw[:size.value]

def linux_environ(pid):
    try:
        with open(f'/proc/{pid}/environ', 'rb') as handle:
            return handle.read()
    except OSError:
        return None

result['write_checkout'] = write(os.path.join(checkout, 'qualify-write'))
result['write_tmpdir'] = write(os.path.join(tmpdir, 'qualify-write'))
result['write_home'] = write(os.path.join(os.environ.get('HOME', tmpdir), 'qualify-write'))
result['loopback'] = connect('127.0.0.1', port)
try:
    socket.getaddrinfo('example.com', 443)
    result['dns'] = 'resolved'
except OSError:
    result['dns'] = 'blocked'
result['external'] = connect('1.1.1.1', 443)
values = [value.encode() for value in canaries]
result['env_canary'] = any(value.encode() in item.encode() for item in os.environ.values() for value in canaries)
result['env_sensitive_names'] = sorted(name for name in os.environ
                                       if name.startswith('REVIEW_') or any(w in name for w in ('TOKEN', 'KEY', 'SECRET')))
if sys.platform.startswith('linux'):
    visible = [int(name) for name in os.listdir('/proc') if name.isdigit()]
    reader = linux_environ
else:
    visible = sorted(set(pids))
    reader = mac_environ
roles = dict(zip(pids, ('engine', 'shell', 'parent')))
readable, found = 0, []
for pid in sorted(set(visible) | set(pids)):
    data = reader(pid)
    if data is not None:
        readable += 1
        if any(value in data for value in values):
            found.append(roles.get(pid, 'other'))
result['process_environ_readable'] = readable
result['process_environ_canary'] = bool(found)
result['process_environ_canary_in'] = sorted(set(found))
try:
    ps = subprocess.run(['ps', '-E', '-ww', '-A', '-o', 'command='], capture_output=True, timeout=10)
    result['ps_canary'] = any(value in ps.stdout for value in values)
except OSError:
    result['ps_canary'] = False
print(json.dumps(result))
'''

EXPECTED = {'write_checkout': 'blocked', 'write_tmpdir': 'blocked', 'write_home': 'blocked',
            'loopback': 'blocked', 'dns': 'blocked', 'external': 'blocked', 'env_canary': False,
            'env_sensitive_names': [], 'process_environ_canary': False, 'ps_canary': False,
            'listener_connections': 0}
# What the unsandboxed control run must observe for the sandbox result to mean anything.
CONTROL = {'write_checkout': 'allowed', 'write_tmpdir': 'allowed', 'write_home': 'allowed', 'loopback': 'connected',
           'env_canary': True, 'process_environ_canary': True, 'listener_connections': 1}


def find_codex(value):
    value = value or os.environ.get('CODEX_BIN') or shutil.which('codex') or ''
    return locate_codex(str(Path(value).absolute()) if value else '')


def inner(args):
    hardening = harden_process()
    binary, path_dir = find_codex(args.codex)
    accepted = []
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(5)
    listener.settimeout(0.2)
    done = threading.Event()

    def accept():
        while not done.is_set():
            try:
                connection, _ = listener.accept()
                accepted.append(1)
                connection.close()
            except OSError:
                pass

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='codex-qualify-') as temporary:
        root = Path(temporary).resolve()
        home, codex_home, tmp, checkout = (root / name for name in ('home', 'codex-home', 'tmp', 'checkout'))
        for directory in (home, codex_home, tmp, checkout):
            directory.mkdir(mode=0o700)
        (checkout / 'README').write_text('qualification checkout\n')
        subprocess.run(['git', 'init', '-q', str(checkout)], check=True, capture_output=True)
        (codex_home / 'config.toml').write_text(config_toml('gpt-6.1-sol', None, 'http://127.0.0.1:9/v1'))
        probe = root / 'probe.py'
        probe.write_text(PROBE)
        env = {'PATH': (str(path_dir) + os.pathsep if path_dir else '') + os.environ.get('PATH', '/usr/bin:/bin'),
               'HOME': str(home), 'CODEX_HOME': str(codex_home), 'TMPDIR': str(tmp), 'LANG': 'C.UTF-8',
               **{name: os.environ[name] for name in CANARIES}}
        canaries = ','.join(os.environ[name] for name in CANARIES)
        pids = f'{os.getpid()},{os.getppid()}'
        probe_args = [sys.executable, '-B', str(probe), str(checkout), str(tmp), str(listener.getsockname()[1]),
                      canaries, pids]
        results = {}
        # The unsandboxed control proves each probe can observe what the sandbox must block.
        for label, command in (('control', probe_args),
                               ('sandbox', [str(binary), 'sandbox', '-P', ':read-only', '-C', str(checkout), '--',
                                            *probe_args])):
            accepted.clear()
            process = subprocess.run(command, env=env, cwd=checkout, capture_output=True,
                                     stdin=subprocess.DEVNULL, timeout=120)
            lines = process.stdout.decode('utf-8', 'replace').strip().splitlines()
            try:
                results[label] = json.loads(lines[-1])
            except (IndexError, ValueError):
                print(f'{label} probe produced no result (exit {process.returncode})')
                return 2
            results[label]['listener_connections'] = len(accepted)
            for path in (checkout / 'qualify-write', tmp / 'qualify-write', home / 'qualify-write'):
                path.unlink(missing_ok=True)
        done.set()
        thread.join(1)
        listener.close()
    print(f'platform={sys.platform} hardening={hardening} codex={binary}')
    print(f'{"check":26} {"control":14} {"sandbox":14} {"expected":10} result')
    failures = 0
    for name, expected in EXPECTED.items():
        control, value = results['control'].get(name), results['sandbox'].get(name)
        ok = value == expected
        if name in CONTROL and control != CONTROL[name]:
            ok = False  # the probe could not observe the unsandboxed case; the check is vacuous
        failures += not ok
        print(f'{name:26} {json.dumps(control)[:14]:14} {json.dumps(value)[:14]:14} {json.dumps(expected):10} '
              f'{"pass" if ok else "FAIL"}')
    for name in ('process_environ_readable', 'process_environ_canary_in'):
        print(f'{name:26} {json.dumps(results["control"].get(name))[:14]:14} '
              f'{json.dumps(results["sandbox"].get(name))[:40]} (info)')
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--codex', help='Codex package root or binary (default: CODEX_BIN or PATH)')
    parser.add_argument('--inner', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.inner:
        return inner(args)
    env = {**os.environ, **{name: 'canary-' + secrets.token_hex(12) for name in CANARIES}}
    # The shell stays alive as the parent, holding the canaries in its initial environment.
    command = ['/bin/sh', '-c', '"$0" "$@"; exit $?', sys.executable, '-B', str(Path(__file__).resolve()), '--inner']
    if args.codex:
        command += ['--codex', args.codex]
    return subprocess.run(command, env=env).returncode


if __name__ == '__main__':
    sys.exit(main())
