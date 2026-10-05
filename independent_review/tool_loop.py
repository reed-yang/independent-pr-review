"""Grok lane harness: a stateless Responses API loop over read-only snapshot tools.

The model never executes anything. Every tool reads git objects of the immutable
snapshot (snapshot.Snapshot); paths are repository-relative, symlinks are plain
blobs and nothing on the local filesystem is reachable. Tool results are
untrusted repository data and are bounded per call and per run.
"""

import functools
import json
import os
import re
import secrets
import time

from . import transport
from .core import ReviewError, estimate_tokens, safe_path
from .evidence import redact
from .snapshot import MAX_BLOB_BYTES


REVS = ['head', 'base']
REV = {'type': 'string', 'enum': REVS, 'description': '"head" is the PR head, "base" the merge base.'}
TOOLS = [
    {'type': 'function', 'name': 'read_file', 'strict': True,
     'description': 'Read an inclusive 1-based line range of a text file. The result starts with one header line '
                    'giving the returned range and the total line count; the file lines follow exactly as stored, '
                    'without line numbers.',
     'parameters': {'type': 'object', 'additionalProperties': False,
                    'required': ['rev', 'path', 'start_line', 'end_line'],
                    'properties': {'rev': REV, 'path': {'type': 'string', 'description': 'Repository-relative path.'},
                                   'start_line': {'type': 'integer'}, 'end_line': {'type': 'integer'}}}},
    {'type': 'function', 'name': 'grep', 'strict': True,
     'description': 'Search text files with a POSIX extended regular expression (git grep -E; use [0-9] instead '
                    'of \\d). Returns path:line:text rows. paths limits the search to files or directories '
                    '(git pathspecs); use [] for the whole tree.',
     'parameters': {'type': 'object', 'additionalProperties': False,
                    'required': ['rev', 'pattern', 'paths', 'ignore_case'],
                    'properties': {'rev': REV, 'pattern': {'type': 'string'},
                                   'paths': {'type': 'array', 'items': {'type': 'string'}},
                                   'ignore_case': {'type': 'boolean'}}}},
    {'type': 'function', 'name': 'list_files', 'strict': True,
     'description': 'List file paths, optionally under a directory prefix ("" lists every file).',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['rev', 'prefix'],
                    'properties': {'rev': REV, 'prefix': {'type': 'string'}}}},
    {'type': 'function', 'name': 'diff', 'strict': True,
     'description': 'Unified merge-base-to-head diff of one path, or of the whole PR when path is "".',
     'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['path'],
                    'properties': {'path': {'type': 'string'}}}},
]
SIGNATURES = {'read_file': {'rev': str, 'path': str, 'start_line': int, 'end_line': int},
              'grep': {'rev': str, 'pattern': str, 'paths': list, 'ignore_case': bool},
              'list_files': {'rev': str, 'prefix': str}, 'diff': {'path': str}}
# Snapshot errors caused by model arguments go back to the model; others fail the lane.
ARGUMENT_ERRORS = {'invalid_grep_pattern': 'invalid or unsupported extended regular expression',
                   'invalid_snapshot_path': 'invalid repository path',
                   'invalid_snapshot_revision': 'rev must be "head" or "base"',
                   'snapshot_git_timeout': 'the operation took too long; narrow it'}
USAGE_KEYS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')
MIN_ROOM_CHARS = 500
# Each turn resends the whole conversation, so a turn that failed in transit can be sent again.
TRANSIENT_ERRORS = frozenset({'http_429', 'http_500', 'http_502', 'http_503', 'http_504', 'provider_connection_error',
                              'provider_connect_timeout', 'provider_request_timeout', 'provider_headers_timeout',
                              'provider_idle_timeout', 'provider_stream_incomplete'})
RETRY_DELAYS = (10, 30)
MAX_LINE_CHARS = 1000
TOOL_SCHEMA_TOKENS = 1500
FINISH = ('The tool budget of this task is exhausted ({}). Tools are no longer available. Return the final JSON '
          'answer now from what you have already read, and record anything you could not establish.')


def bounded_rows(header, rows, limit, hint):
    """Join whole rows under limit characters, ending with an explicit marker when cut."""
    shown, size = [], len(header) + 1
    for row in rows:
        if size + len(row) + 1 > limit - 200:
            break
        shown.append(row)
        size += len(row) + 1
    text = '\n'.join([header, *shown])
    if len(shown) == len(rows):
        return text, False
    return text + f'\n[truncated: output limit reached after {len(shown)} of {len(rows)} rows; {hint}]', True


def file_lines(text):
    """Split on newline only, keeping line ends, so numbering matches git and quotes stay exact."""
    parts = text.split('\n')
    return [part + '\n' for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def read_file(workspace, rev, path, start_line, end_line, limit):
    if not safe_path(path):
        return 'error: invalid repository path', False
    data = workspace.read_bytes(rev, path)
    if data is None:
        return f'error: {path} is not a file at {rev}, or it is larger than {MAX_BLOB_BYTES} bytes', False
    if b'\x00' in data[:8192]:
        return f'error: {path} is a binary file', False
    try:
        lines = file_lines(data.decode('utf-8'))
    except UnicodeDecodeError:
        return f'error: {path} is not UTF-8 text', False
    total = len(lines)
    if not total:
        return f'[{path} at {rev}: empty file, 0 lines]', False
    if start_line < 1 or end_line < start_line:
        return 'error: require 1 <= start_line <= end_line', False
    if start_line > total:
        return f'error: start_line {start_line} is beyond the end of {path} ({total} lines)', False
    end = min(end_line, total)
    room = limit - 300
    body, last = '', start_line - 1
    for line in lines[start_line - 1:end]:
        if len(body) + len(line) > room:
            break
        body += line
        last += 1
    if last < start_line:
        body = lines[start_line - 1][:max(room, 0)]
        marker = f'[truncated: line {start_line} exceeds the output limit; only its first {len(body)} characters are shown]'
        last = start_line
    elif last < end:
        marker = f'[truncated: output limit reached after line {last}; read from line {last + 1} to continue]'
    else:
        marker = None
    text = f'[{path} at {rev}: lines {start_line}-{last} of {total}]\n' + body
    if marker is None:
        return text, False
    return text + ('' if text.endswith('\n') else '\n') + marker, True


def grep(workspace, rev, pattern, paths, ignore_case, limit):
    matches = workspace.grep(rev, pattern, paths, ignore_case)
    rows = [f'{path}:{line}:' + (text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + ' [line truncated]')
            for path, line, text in matches]
    return bounded_rows(f'[grep at {rev}: {len(rows)} matching lines]', rows, limit,
                        'narrow the pattern or paths')


def list_files(workspace, rev, prefix, limit):
    prefix = '' if prefix in ('.', './', '/') else prefix
    paths = workspace.list_files(rev, prefix)
    return bounded_rows(f'[list_files at {rev} under "{prefix}": {len(paths)} paths]', paths, limit,
                        'use a narrower prefix')


def diff(workspace, path, limit):
    rows = workspace.diff(path or None).split('\n')
    if rows and not rows[-1]:
        rows.pop()
    header = f'[diff {workspace.shas["base"]}..{workspace.shas["head"]} {path or "(whole PR)"}: {len(rows)} lines]'
    return bounded_rows(header, rows, limit, 'request the diff of one path or read_file the changed lines')


def describe(name, args):
    if name == 'read_file':
        text = f"read_file {args['rev']} {args['path']}:{args['start_line']}-{args['end_line']}"
    elif name == 'grep':
        text = ('grep ' + ('-i ' if args['ignore_case'] else '') + json.dumps(args['pattern'], ensure_ascii=False)
                + ' ' + args['rev'] + (' -- ' + ' '.join(args['paths']) if args['paths'] else ''))
    elif name == 'list_files':
        text = f"list_files {args['rev']} {args['prefix'] or '.'}"
    else:
        text = f"diff {args['path'] or '(whole PR)'}"
    # Model-controlled text: no control characters, credential-redacted, bounded.
    return redact(re.sub(r'[\x00-\x1f\x7f]', '?', text))[:300]


def execute(workspace, name, arguments, limit):
    """Run one tool call. Return (output, command, path, truncated, error); never raise for bad arguments."""
    signature = SIGNATURES.get(name)
    if signature is None:
        return 'error: unknown tool', None, None, False, True
    try:
        args = json.loads(arguments)
    except (TypeError, ValueError):
        return 'error: arguments are not valid JSON', None, None, False, True
    if not isinstance(args, dict) or set(args) != set(signature) or any(
            type(args[key]) is not kind for key, kind in signature.items()):
        return 'error: arguments do not match the tool schema', None, None, False, True
    if 'rev' in args and args['rev'] not in REVS:
        return 'error: rev must be "head" or "base"', None, None, False, True
    if name == 'grep' and (len(args['paths']) > 50 or not all(isinstance(item, str) for item in args['paths'])):
        return 'error: paths must be a list of at most 50 strings', None, None, False, True
    command = describe(name, args)
    path = args['path'] if name == 'read_file' or (name == 'diff' and args['path']) else None
    try:
        if name == 'read_file':
            output, truncated = read_file(workspace, args['rev'], args['path'], args['start_line'], args['end_line'], limit)
        elif name == 'grep':
            output, truncated = grep(workspace, args['rev'], args['pattern'], args['paths'], args['ignore_case'], limit)
        elif name == 'list_files':
            output, truncated = list_files(workspace, args['rev'], args['prefix'], limit)
        else:
            output, truncated = diff(workspace, args['path'], limit)
    except ReviewError as exc:
        if str(exc) not in ARGUMENT_ERRORS:
            raise
        return 'error: ' + ARGUMENT_ERRORS[str(exc)], command, None, False, True
    error = output.startswith('error: ')
    return output, command, None if error else path, truncated, error


def positive(backend, name, minimum=1):
    value = backend.get(name)
    if type(value) is not int or value < minimum:
        raise ReviewError('invalid_tool_budget')
    return value


def context_limit(backend):
    raw = os.environ.get(backend.get('context_window_env', ''), '')
    try:
        window = int(raw) if raw else backend.get('default_context_window_tokens')
    except ValueError:
        raise ReviewError('invalid_context_window') from None
    if type(window) is not int or not 32768 <= window <= 2097152:
        raise ReviewError('invalid_context_window')
    return window - max(16000, window // 10, backend.get('output_reserve_tokens', 6500))


class ToolLoop:
    def __init__(self, backend, task, workspace, complete):
        if not isinstance(task.get('prompt'), str) or not task['prompt'] or not isinstance(task.get('schema'), dict) \
                or task.get('kind') not in ('review', 'verify') or not isinstance(task.get('effort'), (str, type(None))):
            raise ReviewError('invalid_harness_task')
        if backend.get('api', 'responses') != 'responses':
            raise ReviewError('unsupported_compatible_api')
        self.key = os.environ.get(backend.get('key_env', ''), '')
        base = os.environ.get(backend.get('base_url_env', ''), '').rstrip('/')
        self.model = os.environ.get(backend.get('model_env', ''), '')
        if not self.key or not base or not self.model:
            raise ReviewError('backend_not_configured')
        self.url = base + '/responses'
        self.backend, self.task, self.workspace, self.complete = backend, task, workspace, complete
        self.max_turns = positive(backend, 'max_turns', 2)
        self.max_calls = positive(backend, 'max_tool_calls')
        self.max_output = positive(backend, 'max_tool_output_chars', 2 * MIN_ROOM_CHARS)
        self.max_total = positive(backend, 'max_total_tool_output_chars', 2 * MIN_ROOM_CHARS)
        self.timeout = positive(backend, 'timeout_seconds', 2)
        self.reserve = backend.get('final_turn_reserve_seconds', self.timeout // 6)
        if type(self.reserve) is not int or not 0 <= self.reserve < self.timeout:
            raise ReviewError('invalid_tool_budget')
        self.context_limit = context_limit(backend)
        self.projected = estimate_tokens(task['prompt']) + TOOL_SCHEMA_TOKENS
        if self.projected >= self.context_limit:
            raise ReviewError('prompt_exceeds_configured_context_budget')
        self.counters = {'turns': 0, 'tool_calls': 0, 'refused_tool_calls': 0, 'tool_errors': 0,
                         'truncated_outputs': 0, 'tool_output_chars': 0, 'interrupted_turns': 0,
                         'turn_retries': 0, 'retried_requests': 0}
        self.usage = {**dict.fromkeys(USAGE_KEYS, 0), 'requests': 0}
        self.metrics = {'transport': 'stream_tool_loop', 'api': 'responses'}
        self.files, self.commands, self.stopped = [], [], None
        self.items = [{'role': 'user', 'content': task['prompt']}]
        self.cache_key = 'review-' + secrets.token_hex(16)

    def output_room(self):
        return min(self.max_output, self.max_total - self.counters['tool_output_chars'],
                   (self.context_limit - self.projected) * 3)

    def budget_reason(self):
        if self.counters['turns'] >= self.max_turns - 1:
            return 'max_turns'
        if self.counters['tool_calls'] >= self.max_calls:
            return 'max_tool_calls'
        if self.max_total - self.counters['tool_output_chars'] < MIN_ROOM_CHARS:
            return 'max_total_tool_output_chars'
        if (self.context_limit - self.projected) * 3 < MIN_ROOM_CHARS:
            return 'context_budget'
        if self.deadline - time.monotonic() <= self.reserve:
            return 'timeout'
        return None

    def finish(self, reason):
        self.stopped = reason
        self.items.append({'role': 'user', 'content': FINISH.format(reason)})

    def diagnostics(self):
        return {**self.counters, 'stopped_reason': self.stopped or 'none', 'loop_usage': dict(self.usage),
                'loop_elapsed_seconds': round(time.monotonic() - self.start, 2)}

    def record(self, usage):
        for key in USAGE_KEYS:
            if type(usage.get(key)) is int and usage[key] >= 0:
                self.usage[key] += usage[key]
        for name, value in (usage.get('transport') or {}).items():
            if name.endswith('_seconds') and type(value) in (int, float):
                self.metrics['max_turn_' + name] = max(self.metrics.get('max_turn_' + name, 0), value)
            elif type(value) is int and name != 'http_status':
                self.metrics[name] = self.metrics.get(name, 0) + value

    def call(self, item):
        counters = self.counters
        reason = 'tool call' if counters['tool_calls'] >= self.max_calls else None
        room = self.output_room()
        if reason is None and room < MIN_ROOM_CHARS:
            reason = 'tool output'
        if reason:
            counters['refused_tool_calls'] += 1
            return f'error: not executed; the {reason} budget is exhausted. Return your final answer.'
        counters['tool_calls'] += 1
        output, command, path, truncated, error = execute(self.workspace, item['name'], item['arguments'], room)
        if len(output) > room:
            # Defensive: every tool already bounds its own output.
            output, truncated = output[:room - 120] + '\n[truncated: output limit reached]', True
        counters['tool_errors'] += error
        counters['truncated_outputs'] += truncated
        counters['tool_output_chars'] += len(output)
        if command and len(self.commands) < 100:
            self.commands.append(command)
        if path and path not in self.files and len(self.files) < 200:
            self.files.append(path)
        return output

    def request(self, final):
        left = self.deadline - time.monotonic()
        budget = left if final else left - self.reserve
        if budget <= 0:
            raise ReviewError('provider_deadline_exceeded', self.diagnostics())
        payload = {'model': self.model, 'input': list(self.items), 'stream': True, 'store': False,
                   'tools': TOOLS, 'tool_choice': 'none' if final else 'auto', 'prompt_cache_key': self.cache_key}
        if self.task.get('effort'):
            payload['reasoning'] = {'effort': self.task['effort']}
        if self.backend.get('structured_output', True):
            payload['text'] = {'format': {'type': 'json_schema', 'name': self.task['kind'], 'strict': True,
                                          'schema': self.task['schema']}}
        if type(self.backend.get('max_output_tokens')) is int:
            payload['max_output_tokens'] = self.backend['max_output_tokens']
        self.counters['turns'] += 1
        self.usage['requests'] += 1
        return self.complete(self.url, self.key, payload, {**self.backend, 'timeout_seconds': budget})

    def run(self):
        self.start = time.monotonic()
        self.deadline = self.start + self.timeout
        while True:
            if self.stopped is None:
                reason = self.budget_reason()
                if reason:
                    self.finish(reason)
            final = self.stopped is not None
            try:
                output, model, usage = self.request(final)
            except ReviewError as exc:
                # A tool turn cut at the reserve boundary still leaves time for a final answer.
                if str(exc) == 'provider_deadline_exceeded' and not final and self.deadline - time.monotonic() > 1:
                    self.counters['interrupted_turns'] += 1
                    self.finish('timeout')
                    continue
                if self.retry(str(exc), final):
                    continue
                raise ReviewError(str(exc), {**exc.diagnostics, **self.diagnostics()}) from None
            self.counters['turn_retries'] = 0
            self.record(usage)
            calls = [item for item in output if item.get('type') == 'function_call']
            text = ''.join(part['text'] for item in output if item.get('type') == 'message'
                           for part in item.get('content', []) if part.get('type') == 'output_text')
            if not calls or (final and text.strip()):
                if not text.strip():
                    raise ReviewError('empty_model_output', self.diagnostics())
                return text, model, {**self.usage, 'transport': {
                    **self.metrics, 'elapsed_seconds': round(time.monotonic() - self.start, 2)}}, self.trace()
            if final:
                raise ReviewError('tool_budget_exhausted', self.diagnostics())
            self.items.extend(output)
            # Provider-reported size of this turn already includes the tool definitions.
            turn_size = [usage.get(key) for key in ('input_tokens', 'output_tokens')]
            self.projected = (sum(turn_size) if all(type(value) is int for value in turn_size)
                              else estimate_tokens(json.dumps(self.items)) + TOOL_SCHEMA_TOKENS)
            for item in calls:
                result = self.call(item)
                self.projected += estimate_tokens(result) + 10
                self.items.append({'type': 'function_call_output', 'call_id': item['call_id'], 'output': result})

    def retry(self, error, final):
        """Wait and resend the same turn after a transient failure while time remains."""
        attempt = self.counters['turn_retries']
        if error not in TRANSIENT_ERRORS or attempt >= len(RETRY_DELAYS):
            return False
        delay = RETRY_DELAYS[attempt]
        if self.deadline - time.monotonic() <= delay + (0 if final else self.reserve) + 60:
            return False
        self.counters['turn_retries'] += 1
        self.counters['retried_requests'] += 1
        self.counters['turns'] -= 1
        time.sleep(delay)
        return True

    def trace(self):
        return {'tool_calls': self.counters['tool_calls'], 'files_read': list(self.files),
                'commands': list(self.commands), 'truncated_outputs': self.counters['truncated_outputs'],
                'stopped_reason': self.stopped or 'final_answer',
                **{key: value for key, value in self.counters.items() if key not in ('tool_calls', 'truncated_outputs')}}


def run(backend, task, workspace, complete=None):
    """Harness contract: return (raw_text, model, usage, trace) or raise ReviewError."""
    complete = complete or functools.partial(transport.completion, items=True)
    return ToolLoop(backend, task, workspace, complete).run()
