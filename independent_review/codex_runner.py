"""Run the pinned official Codex CLI read-only over a snapshot checkout.

Codex reaches the provider only through a loopback credential proxy and holds
just a random per-run proxy token, which its shell environment policy hides
from sandboxed commands. Usage comes from the proxy (it includes subagents).
"""

import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import tempfile
import time

from .core import ReviewError, safe_path
from .credential_proxy import CredentialProxy
from .hardening import harden_process, take_secret


MAX_STDOUT_BYTES = 256_000_000
MAX_LINE_BYTES = 4_000_000
MAX_FINAL_BYTES = 1_000_000
MAX_COMMANDS, MAX_COMMAND_CHARS, MAX_FILES = 100, 300, 200
VERSION_TIMEOUT_SECONDS = 30
EXIT_DRAIN_SECONDS = 5
MODEL = re.compile(r'[A-Za-z0-9._:-]{1,100}')
EFFORT = re.compile(r'[a-z]{1,16}')
# Features that add external reach, persistent state or unneeded surfaces.
# code_mode_host stays enabled: gpt-6.1-sol runs shell commands through code mode.
DISABLED_FEATURES = (
    'apps', 'auth_elicitation', 'browser_use', 'browser_use_external', 'browser_use_full_cdp_access',
    'computer_use', 'daemon_auto_start', 'enable_request_compression', 'fast_mode', 'goals',
    'guardian_approval', 'hooks', 'image_generation', 'in_app_browser', 'in_app_chat', 'in_app_dictation',
    'in_app_local_automation', 'in_app_updates', 'memories', 'plugin_sharing', 'plugins',
    'realtime_conversation', 'remote_plugin', 'shell_snapshot', 'skill_mcp_dependency_install',
    'skill_search', 'sleep_tool', 'system_proxy_fallback', 'tool_call_mcp_elicitation', 'tool_suggest',
    'unbounded_connection_retries', 'view_image', 'workspace_dependencies', 'worktrees')
SECRET_PATTERNS = (
    re.compile(r'(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}'),
    re.compile(r'\b(?:sk-|xai-|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_-]{8,}'),
    re.compile(r'(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=)\S+'),
)


def release_version():
    return json.loads(Path(__file__).with_name('codex-release.json').read_text())['version']


def toml_string(value):
    # JSON string escapes (ASCII-only) are valid TOML basic strings.
    return json.dumps(value, ensure_ascii=True)


def config_toml(model, effort, base_url):
    """Return the complete CODEX_HOME/config.toml for one read-only run."""
    if not MODEL.fullmatch(model or ''):
        raise ReviewError('invalid_codex_model')
    if effort is not None and not EFFORT.fullmatch(effort):
        raise ReviewError('unsupported_reasoning_effort')
    if not re.fullmatch(r'http://127\.0\.0\.1:\d{1,5}/v1', base_url):
        raise ReviewError('invalid_proxy_url')
    lines = [f'model = {toml_string(model)}', 'model_provider = "review"']
    if effort is not None:
        lines.append(f'model_reasoning_effort = {toml_string(effort)}')
    lines += [
        'approval_policy = "never"', 'sandbox_mode = "read-only"', 'web_search = "disabled"',
        'check_for_update_on_startup = false', 'allow_login_shell = false',
        # PR-controlled AGENTS.md must not become instructions.
        'project_doc_max_bytes = 0', 'include_apps_instructions = false', '',
        '[model_providers.review]', 'name = "review"', f'base_url = {toml_string(base_url)}',
        'wire_api = "responses"', 'env_key = "REVIEW_PROXY_TOKEN"', 'supports_websockets = false', '',
        '[shell_environment_policy]', 'inherit = "core"',
        'exclude = ["REVIEW_*", "*TOKEN*", "*KEY*", "*SECRET*"]', '',
        '[features]', *[f'{name} = false' for name in DISABLED_FEATURES], '',
        '[analytics]', 'enabled = false', '', '[feedback]', 'enabled = false', '',
        '[history]', 'persistence = "none"', '',
        '[memories]', 'generate_memories = false', 'use_memories = false', '',
        '[otel]', 'exporter = "none"', 'metrics_exporter = "none"', 'trace_exporter = "none"', '',
        '[skills]', 'include_instructions = false', '', '[skills.bundled]', 'enabled = false', '']
    return '\n'.join(lines)


def locate_codex(value):
    """Return (binary, path_dir) from CODEX_BIN: a package root or a binary path."""
    if not value or not Path(value).is_absolute():
        raise ReviewError('codex_not_found')
    path = Path(value)
    if path.is_dir():
        root, binary = path, path / 'bin/codex'
    else:
        binary = path.resolve()
        root = binary.parent.parent
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ReviewError('codex_not_found')
    path_dir = root / 'codex-path'
    return binary, (path_dir if (root / 'codex-package.json').is_file() and path_dir.is_dir() else None)


def codex_version(binary, env):
    try:
        process = subprocess.run([str(binary), '--version'], env=env, capture_output=True,
                                 stdin=subprocess.DEVNULL, timeout=VERSION_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        raise ReviewError('codex_not_found') from None
    match = re.fullmatch(rb'codex-cli (\d+\.\d+\.\d+)\s*', process.stdout)
    if process.returncode != 0 or not match:
        raise ReviewError('codex_version_unknown')
    version = match.group(1).decode()
    if version != release_version():
        raise ReviewError('codex_version_mismatch', {'version': version})
    return version


def redact(text, secrets):
    for value in secrets:
        if value:
            text = text.replace(value, '[REDACTED]')
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.groups() else '') + '[REDACTED]', text)
    return text


def command_paths(command, head):
    """Best-effort repository paths named by one shell command."""
    try:
        words = shlex.split(command)
        if len(words) >= 3 and Path(words[0]).name in ('sh', 'bash', 'zsh') and words[1] in ('-c', '-lc'):
            lexer = shlex.shlex(words[2], posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            words = list(lexer)
    except ValueError:
        return []
    found = []
    for word in words:
        word = word.split(':', 1)[1] if re.match(r'^(?:HEAD|[0-9a-f]{7,40}):', word) else word
        if word.startswith(str(head) + '/'):
            word = word[len(str(head)) + 1:]
        word = word.removeprefix('./')
        if len(word) <= 1024 and safe_path(word):
            try:
                if (head / word).is_file():
                    found.append(word)
            except OSError:
                # Python 3.11 raises for over-long names instead of returning False.
                pass
    return found


class Trace:
    """Count Codex JSONL events; keep only bounded, redacted command strings."""

    def __init__(self, head, secrets):
        self.head = head
        self.secrets = secrets
        self.commands, self.files = [], []
        self.counts = {name: 0 for name in (
            'events', 'invalid_lines', 'truncated_outputs', 'commands_total', 'commands_failed',
            'commands_declined', 'commands_omitted', 'collab_calls', 'subagents_spawned', 'mcp_calls',
            'web_searches', 'file_changes', 'reasoning_items', 'agent_messages', 'error_items',
            'error_events', 'turns_completed', 'turns_failed', 'unknown_events')}
        self.turn_usage = {name: 0 for name in ('input_tokens', 'cached_input_tokens', 'output_tokens',
                                                'reasoning_output_tokens')}

    def line(self, raw):
        counts = self.counts
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            counts['invalid_lines'] += 1
            return
        counts['events'] += 1
        kind = event.get('type')
        if kind == 'item.completed' and isinstance(event.get('item'), dict):
            self.item(event['item'])
        elif kind == 'turn.completed':
            counts['turns_completed'] += 1
            usage = event.get('usage') if isinstance(event.get('usage'), dict) else {}
            for name in self.turn_usage:
                if type(usage.get(name)) is int and usage[name] >= 0:
                    self.turn_usage[name] += usage[name]
        elif kind == 'turn.failed':
            counts['turns_failed'] += 1
        elif kind == 'error':
            counts['error_events'] += 1
        elif kind not in ('thread.started', 'turn.started', 'item.started', 'item.updated'):
            counts['unknown_events'] += 1

    def item(self, item):
        counts, kind = self.counts, item.get('type')
        if kind == 'command_execution':
            counts['commands_total'] += 1
            counts['commands_failed'] += item.get('exit_code') not in (0, None)
            counts['commands_declined'] += item.get('status') == 'declined'
            command = item.get('command') if isinstance(item.get('command'), str) else ''
            if len(self.commands) < MAX_COMMANDS:
                self.commands.append(redact(command, self.secrets)[:MAX_COMMAND_CHARS])
            else:
                counts['commands_omitted'] += 1
            for path in command_paths(command, self.head):
                if path not in self.files and len(self.files) < MAX_FILES:
                    self.files.append(path)
        elif kind == 'collab_tool_call':
            counts['collab_calls'] += 1
            counts['subagents_spawned'] += item.get('tool') == 'spawn_agent'
        else:
            name = {'mcp_tool_call': 'mcp_calls', 'web_search': 'web_searches', 'file_change': 'file_changes',
                    'reasoning': 'reasoning_items', 'agent_message': 'agent_messages', 'error': 'error_items'}.get(kind)
            if name:
                counts[name] += 1

    def result(self, stopped_reason, extra):
        counts = self.counts
        return {'tool_calls': counts['commands_total'] + counts['collab_calls'] + counts['mcp_calls'] + counts['web_searches'],
                'files_read': self.files, 'commands': self.commands,
                'truncated_outputs': counts['truncated_outputs'], 'stopped_reason': stopped_reason,
                **counts, 'codex_turn_usage': self.turn_usage, **extra}


class SecretScanner:
    """Detect known secret values across chunk boundaries of one stream."""

    def __init__(self, secrets):
        self.values = [value.encode() for value in secrets if value]
        self.keep = max((len(value) for value in self.values), default=1) - 1
        self.tail = b''
        self.found = False

    def feed(self, chunk):
        window = self.tail + chunk
        self.found = self.found or any(value in window for value in self.values)
        self.tail = window[-self.keep:] if self.keep else b''


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Darwin can return EPERM for a group whose last member already exited.
        pass


def stream(process, trace, scanners, deadline, proxy):
    """Read both pipes until exit, timeout or budget stop; return an outcome name."""
    pending = b''
    skipping = False
    total = {'stdout': 0, 'stderr': 0}
    drain_until = None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
        selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
        while selector.get_map():
            if proxy.stopped.is_set():
                return 'proxy_budget_exhausted', total
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'codex_timeout', total
            # A lingering descendant may hold the pipes open after Codex exits.
            if drain_until is None and process.poll() is not None:
                drain_until = time.monotonic() + EXIT_DRAIN_SECONDS
            elif drain_until is not None and time.monotonic() > drain_until:
                break
            for key, _ in selector.select(min(remaining, 0.5)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total[key.data] += len(chunk)
                scanners[key.data].feed(chunk)
                if key.data == 'stderr':
                    continue
                if total['stdout'] > MAX_STDOUT_BYTES:
                    return 'codex_output_too_large', total
                pending += chunk
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    if skipping:
                        skipping = False
                    elif line.strip():
                        trace.line(line)
                if len(pending) > MAX_LINE_BYTES:
                    trace.counts['truncated_outputs'] += 1 - skipping
                    pending, skipping = b'', True
    if pending.strip() and not skipping:
        trace.line(pending)
    return None, total


def run(backend, task, workspace):
    start = time.monotonic()
    hardening = harden_process()
    key = take_secret(backend.get('key_env'))
    base_url = os.environ.get(backend.get('base_url_env', ''), '')
    model = os.environ.get(backend.get('model_env', ''), '')
    if not base_url or not model:
        raise ReviewError('backend_not_configured')
    effort = task.get('effort')
    binary, path_dir = locate_codex(os.environ.get(backend.get('binary_env', ''), ''))
    head = Path(workspace.checkout_head()).resolve()
    timeout = backend.get('timeout_seconds', 3600)
    with tempfile.TemporaryDirectory(prefix='codex-run-') as temporary:
        root = Path(temporary).resolve()
        home, codex_home, tmp = root / 'home', root / 'codex-home', root / 'tmp'
        for directory in (home, codex_home, tmp):
            directory.mkdir(mode=0o700)
        system_path = os.environ.get('PATH') or '/usr/bin:/bin'
        env = {'PATH': (str(path_dir) + os.pathsep if path_dir else '') + system_path, 'HOME': str(home),
               'CODEX_HOME': str(codex_home), 'TMPDIR': str(tmp), 'LANG': 'C.UTF-8'}
        version = codex_version(binary, env)
        schema_path, last_path, prompt_path = root / 'schema.json', root / 'last-message.txt', root / 'prompt.txt'
        schema_path.write_text(json.dumps(task['schema']))
        prompt_path.write_text(task['prompt'])
        with CredentialProxy(base_url, key, model, backend) as proxy:
            secrets = (key, proxy.token)
            (codex_home / 'config.toml').write_text(config_toml(model, effort, proxy.base_url))
            # The prompt travels on stdin from a file (EOF after it): a single argv
            # element is limited to 128 KiB on Linux.
            command = [str(binary), 'exec', '--ephemeral', '--json', '--strict-config', '--ignore-rules',
                       '--output-schema', str(schema_path), '-o', str(last_path), '-C', str(head), '-']
            trace = Trace(head, secrets)
            scanners = {'stdout': SecretScanner(secrets), 'stderr': SecretScanner(secrets)}
            deadline = start + timeout
            code = None
            with prompt_path.open('rb') as prompt_input:
                try:
                    process = subprocess.Popen(command, cwd=str(head), env={**env, 'REVIEW_PROXY_TOKEN': proxy.token},
                                               stdin=prompt_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               start_new_session=True)
                except OSError:
                    raise ReviewError('codex_not_found') from None
            try:
                outcome, total = stream(process, trace, scanners, deadline, proxy)
                if outcome is None:
                    try:
                        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        outcome = 'codex_timeout'
            finally:
                kill_group(process)
                process.wait()
                process.stdout.close()
                process.stderr.close()
            metrics = proxy.metrics()
        leaked = any(scanner.found for scanner in scanners.values())
        raw = ''
        if last_path.is_file() and last_path.stat().st_size <= MAX_FINAL_BYTES:
            raw = last_path.read_text(errors='replace').strip()
    elapsed = round(time.monotonic() - start, 2)
    stopped = outcome or ('process_failed' if code else 'turn_failed' if trace.counts['turns_failed'] else 'completed')
    result = trace.result(stopped, {'codex_version': version, 'process_hardening': hardening,
                                    'stdout_bytes': total['stdout'], 'stderr_bytes': total['stderr'],
                                    'proxy_stopped_reason': metrics['stopped_reason']})
    diagnostics = {'elapsed_seconds': elapsed, 'requests': metrics['requests'], 'total_tokens': metrics['total_tokens'],
                   'events': trace.counts['events'], 'commands_total': trace.counts['commands_total']}
    if leaked or any(value in raw or value in json.dumps(result) for value in secrets):
        raise ReviewError('credential_in_output', diagnostics)
    if outcome == 'proxy_budget_exhausted' or metrics['stopped_reason']:
        raise ReviewError('proxy_budget_exhausted', {**diagnostics, 'stopped_reason': metrics['stopped_reason']})
    if outcome:
        raise ReviewError(outcome, diagnostics)
    if code:
        raise ReviewError('codex_process_failed', {**diagnostics, 'exit_code': code,
                                                   'turns_failed': trace.counts['turns_failed']})
    if trace.counts['turns_failed']:
        raise ReviewError('codex_turn_failed', diagnostics)
    if trace.counts['mcp_calls'] or trace.counts['web_searches']:
        raise ReviewError('codex_unexpected_tool', diagnostics)
    if not raw:
        raise ReviewError('codex_no_final_message', diagnostics)
    usage = {name: metrics[name] for name in ('input_tokens', 'cached_input_tokens', 'output_tokens',
                                              'reasoning_tokens', 'total_tokens', 'requests')}
    transport = {name: value for name, value in metrics.items() if name not in usage}
    usage['transport'] = {**transport, 'elapsed_seconds': elapsed}
    reported = next(iter(metrics['models']), None)
    return raw, reported or model, usage, result
