import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from independent_review import core, prompts, snapshot, tool_loop, transport


GIT_ENV = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@example.invalid', 'HOME': tempfile.gettempdir()}
APP_BASE = 'def parse(value):\n    return value.strip()\n\n\ndef unused():\n    pass\n'
APP_HEAD = 'def parse(value):\n    if not value:\n        raise ValueError("empty")\n    return value.strip()\n\n\ndef unused():\n    pass\n'
BIG = ''.join(f'line {index:05d} of the large file\n' for index in range(1, 3001))


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], env=GIT_ENV, check=True, capture_output=True,
                          stdin=subprocess.DEVNULL).stdout.decode().strip()


class SnapshotFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        root = Path(cls.temporary.name)
        repo = root / 'repo'
        (repo / 'src').mkdir(parents=True)
        git(repo, 'init', '-q')
        (repo / 'src' / 'app.py').write_text(APP_BASE)
        (repo / 'README.md').write_text('# Fixture\n')
        (repo / 'bin.dat').write_bytes(b'\x00\x01binary')
        (repo / 'latin.txt').write_bytes(b'caf\xe9\n')
        (repo / 'empty.txt').write_text('')
        os.symlink('/etc/passwd', repo / 'link')
        git(repo, 'add', '-A')
        git(repo, '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'base')
        base = git(repo, 'rev-parse', 'HEAD')
        (repo / 'src' / 'app.py').write_text(APP_HEAD)
        (repo / 'src' / 'new.py').write_text('from app import parse\n\nprint(parse(" x "))\n')
        (repo / 'big.txt').write_text(BIG)
        (repo / 'long.txt').write_text('x' * 5000 + '\nshort\n')
        (repo / 'crlf.txt').write_bytes(b'one\r\ntwo\r\n')
        git(repo, 'add', '-A')
        git(repo, '-c', 'commit.gpgsign=false', 'commit', '-q', '-m', 'head')
        head = git(repo, 'rev-parse', 'HEAD')
        record = snapshot.create('file://' + str(repo), head, base, root / 'out')
        cls.workspace = snapshot.Snapshot.open(root / 'out', record, root / 'work')
        cls.head, cls.base = head, base

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def call(self, name, limit=60000, **args):
        return tool_loop.execute(self.workspace, name, json.dumps(args), limit)


class ToolTests(SnapshotFixture):
    def test_read_file_returns_exact_lines_after_a_range_header(self):
        output, command, path, truncated, error = self.call('read_file', rev='head', path='src/app.py', start_line=2, end_line=4)
        header, body = output.split('\n', 1)
        self.assertEqual(header, '[src/app.py at head: lines 2-4 of 8]')
        self.assertEqual(body, ''.join(APP_HEAD.splitlines(True)[1:4]))
        self.assertIn('raise ValueError("empty")', body)
        self.assertEqual((command, path, truncated, error), ('read_file head src/app.py:2-4', 'src/app.py', False, False))
        base = self.call('read_file', rev='base', path='src/app.py', start_line=1, end_line=999)[0]
        self.assertEqual(base, '[src/app.py at base: lines 1-6 of 6]\n' + APP_BASE)

    def test_line_numbers_follow_newlines_and_keep_carriage_returns(self):
        output = self.call('read_file', rev='head', path='crlf.txt', start_line=2, end_line=2)[0]
        self.assertEqual(output, '[crlf.txt at head: lines 2-2 of 2]\ntwo\r\n')
        self.assertEqual(self.call('read_file', rev='head', path='empty.txt', start_line=1, end_line=1)[0],
                         '[empty.txt at head: empty file, 0 lines]')

    def test_paths_cannot_escape_the_snapshot(self):
        for path in ('../etc/passwd', '/etc/passwd', 'src/../README.md', '.git/config', 'src/', '', 'a\nb'):
            output, command, read, _, error = self.call('read_file', rev='head', path=path, start_line=1, end_line=5)
            self.assertEqual(output, 'error: invalid repository path')
            self.assertTrue(error)
            self.assertIsNone(read)
            self.assertNotIn('\n', command)
        # A symlink is a blob holding its target text; the target is never opened.
        output = self.call('read_file', rev='head', path='link', start_line=1, end_line=5)[0]
        self.assertEqual(output, '[link at head: lines 1-1 of 1]\n/etc/passwd')
        for name, args in (('list_files', {'rev': 'head', 'prefix': '../'}), ('diff', {'path': '../x'}),
                           ('grep', {'rev': 'head', 'pattern': 'x', 'paths': ['../'], 'ignore_case': False})):
            self.assertEqual(self.call(name, **args)[0], 'error: invalid repository path')

    def test_binary_missing_non_utf8_and_out_of_range_reads_are_errors(self):
        cases = {('bin.dat', 1): 'binary file', ('latin.txt', 1): 'not UTF-8', ('missing.py', 1): 'is not a file',
                 ('src', 1): 'is not a file', ('src/new.py', 99): 'beyond the end', ('src/new.py', 0): 'start_line <= end_line'}
        for (path, start), message in cases.items():
            output, _, read, _, error = self.call('read_file', rev='head', path=path, start_line=start, end_line=max(start, 1))
            self.assertIn(message, output)
            self.assertTrue(output.startswith('error: '))
            self.assertTrue(error)
            self.assertIsNone(read)
        self.assertIn('is not a file', self.call('read_file', rev='base', path='src/new.py', start_line=1, end_line=1)[0])

    def test_invalid_arguments_are_returned_to_the_model(self):
        cases = [('nope', '{}', 'unknown tool'), ('read_file', 'not json', 'not valid JSON'),
                 ('read_file', json.dumps({'rev': 'head', 'path': 'a', 'start_line': '1', 'end_line': 2}), 'schema'),
                 ('read_file', json.dumps({'rev': 'head', 'path': 'a', 'start_line': True, 'end_line': 2}), 'schema'),
                 ('read_file', json.dumps({'rev': 'head', 'path': 'a', 'start_line': 1, 'end_line': 2, 'x': 1}), 'schema'),
                 ('read_file', json.dumps({'rev': 'HEAD', 'path': 'a', 'start_line': 1, 'end_line': 2}), 'rev must be'),
                 ('grep', json.dumps({'rev': 'head', 'pattern': 'x', 'paths': [1], 'ignore_case': False}), 'paths must be'),
                 ('list_files', '[]', 'schema')]
        for name, arguments, message in cases:
            output, command, _, _, error = tool_loop.execute(self.workspace, name, arguments, 60000)
            self.assertIn(message, output)
            self.assertTrue(error)
            self.assertIsNone(command)

    def test_grep_searches_one_revision_and_rejects_bad_patterns(self):
        output, command, path, _, error = self.call('grep', rev='head', pattern='raise [A-Z][a-z]+Error', paths=[], ignore_case=False)
        self.assertEqual(output, '[grep at head: 1 matching lines]\nsrc/app.py:3:        raise ValueError("empty")')
        self.assertEqual((command, path, error), ('grep "raise [A-Z][a-z]+Error" head', None, False))
        self.assertIn(': 0 matching lines]', self.call('grep', rev='base', pattern='ValueError', paths=[], ignore_case=False)[0])
        output, command = self.call('grep', rev='head', pattern='PARSE', paths=['src/new.py'], ignore_case=True)[:2]
        self.assertEqual(output.splitlines()[1:], ['src/new.py:1:from app import parse', 'src/new.py:3:print(parse(" x "))'])
        self.assertEqual(command, 'grep -i "PARSE" head -- src/new.py')
        with patch.dict(os.environ, {'GROK_API_KEY': 'fixture-secret-key-value'}):
            command = self.call('grep', rev='head', pattern='fixture-secret-key-value', paths=[], ignore_case=False)[1]
        self.assertEqual(command, 'grep "[REDACTED]" head')
        for pattern in ('(', '', 'x' * 501):
            output, _, _, _, error = self.call('grep', rev='head', pattern=pattern, paths=[], ignore_case=False)
            self.assertEqual(output, 'error: invalid or unsupported extended regular expression')
            self.assertTrue(error)
        self.assertNotIn('bin.dat', self.call('grep', rev='head', pattern='binary', paths=[], ignore_case=False)[0])

    def test_list_files_and_diff(self):
        listed = self.call('list_files', rev='base', prefix='')[0].splitlines()
        self.assertEqual(listed[0], '[list_files at base under "": 6 paths]')
        self.assertNotIn('src/new.py', listed)
        self.assertEqual(self.call('list_files', rev='head', prefix='src')[0].splitlines()[1:], ['src/app.py', 'src/new.py'])
        self.assertEqual(self.call('list_files', rev='head', prefix='.')[0], self.call('list_files', rev='head', prefix='')[0])
        whole, command, path, _, _ = self.call('diff', limit=500000, path='')
        self.assertIn(f'[diff {self.base}..{self.head} (whole PR):', whole)
        self.assertIn('+++ b/src/new.py', whole)
        self.assertEqual((command, path), ('diff (whole PR)', None))
        one, command, path, _, _ = self.call('diff', path='src/app.py')
        self.assertIn('+        raise ValueError("empty")', one)
        self.assertNotIn('new.py', one)
        self.assertEqual((command, path), ('diff src/app.py', 'src/app.py'))

    def test_outputs_are_truncated_with_explicit_markers(self):
        output, _, _, truncated, _ = self.call('read_file', limit=2000, rev='head', path='big.txt', start_line=10, end_line=3000)
        self.assertTrue(truncated)
        self.assertLessEqual(len(output), 2000)
        lines = output.splitlines(True)
        last = int(lines[-1].split('after line ')[1].split(';')[0])
        self.assertTrue(lines[-1].startswith('[truncated: output limit reached after line'))
        self.assertEqual(lines[0], f'[big.txt at head: lines 10-{last} of 3000]\n')
        self.assertEqual(''.join(lines[1:-1]), ''.join(BIG.splitlines(True)[9:last]))
        output, _, _, truncated, _ = self.call('read_file', limit=1000, rev='head', path='long.txt', start_line=1, end_line=2)
        self.assertTrue(truncated)
        self.assertIn('line 1 exceeds the output limit', output)
        self.assertLessEqual(len(output), 1000)
        for name, args in (('grep', {'rev': 'head', 'pattern': 'large file', 'paths': [], 'ignore_case': False}),
                           ('list_files', {'rev': 'head', 'prefix': ''}), ('diff', {'path': 'big.txt'})):
            output, _, _, truncated, _ = self.call(name, limit=1000 if name != 'list_files' else 260, **args)
            self.assertTrue(truncated, name)
            self.assertIn('[truncated: output limit reached after', output.splitlines()[-1])
            self.assertLessEqual(len(output), 1000)


def call_item(call_id, name, **args):
    return {'type': 'function_call', 'id': 'fc_' + call_id, 'call_id': call_id, 'name': name,
            'arguments': json.dumps(args), 'status': 'completed'}


def message(text):
    return {'type': 'message', 'id': 'msg', 'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}


REASONING = {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'status': 'completed', 'encrypted_content': 'opaque-blob'}
BACKEND = {'harness': 'responses_tools', 'api': 'responses', 'opinion_family': 'grok', 'auth_mode': 'gateway_api_key',
           'base_url_env': 'GROK_BASE_URL', 'key_env': 'GROK_API_KEY', 'model_env': 'GROK_MODEL',
           'effort_env': 'GROK_EFFORT', 'context_window_env': 'GROK_CONTEXT_WINDOW', 'default_effort': 'xhigh',
           'default_context_window_tokens': 500000, 'output_reserve_tokens': 128000, 'timeout_seconds': 3600,
           'connect_timeout_seconds': 20, 'idle_timeout_seconds': 120, 'max_turns': 40, 'max_tool_calls': 80,
           'max_tool_output_chars': 60000, 'max_total_tool_output_chars': 1200000}
ENV = {'GROK_API_KEY': 'fixture-secret-key', 'GROK_BASE_URL': 'https://gateway.example/v1', 'GROK_MODEL': 'grok-4.7'}


class FakeProvider:
    """Scripted turns; records a deep copy of every request."""

    def __init__(self, turns, clock=None, step=0):
        self.turns, self.requests, self.clock, self.step = list(turns), [], clock, step

    def __call__(self, url, key, payload, backend):
        self.requests.append({'url': url, 'key': key, 'payload': json.loads(json.dumps(payload)),
                              'timeout': backend['timeout_seconds']})
        if self.clock is not None:
            self.clock[0] += self.step
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        usage = {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20, 'reasoning_tokens': 5,
                 'total_tokens': 120, 'transport': {'transport': 'stream', 'bytes_received': 10, 'events': 3,
                                                    'elapsed_seconds': 1.5, 'http_status': 200}}
        return turn, 'grok-4.7-build', usage


class LoopTests(SnapshotFixture):
    def task(self, kind='review'):
        return {'kind': kind, 'prompt': 'Review the PR.\nEND_OF_INPUT_NONCE=abc',
                'schema': prompts.REVIEW_SCHEMA if kind == 'review' else prompts.VERIFY_SCHEMA, 'effort': 'xhigh'}

    def run_loop(self, turns, backend=None, task=None, **fake):
        provider = FakeProvider(turns, **fake)
        with patch.dict(os.environ, ENV, clear=True):
            result = tool_loop.run({**BACKEND, **(backend or {})}, task or self.task(), self.workspace, provider)
        return result, provider

    def test_stateless_turns_resend_output_items_and_pair_every_call(self):
        first = [REASONING, call_item('c1', 'read_file', rev='head', path='src/app.py', start_line=1, end_line=3),
                 call_item('c2', 'grep', rev='base', pattern='parse', paths=['src'], ignore_case=False)]
        (text, model, usage, trace), provider = self.run_loop([first, [REASONING, message('{"ok":1}')]])
        self.assertEqual((text, model), ('{"ok":1}', 'grok-4.7-build'))
        one, two = (request['payload'] for request in provider.requests)
        self.assertEqual(provider.requests[0]['url'], 'https://gateway.example/v1/responses')
        self.assertEqual(one['input'], [{'role': 'user', 'content': self.task()['prompt']}])
        self.assertEqual(two['input'][:4], [one['input'][0], *first])
        self.assertEqual([(item['type'], item['call_id']) for item in two['input'][4:]],
                         [('function_call_output', 'c1'), ('function_call_output', 'c2')])
        self.assertTrue(two['input'][4]['output'].startswith('[src/app.py at head: lines 1-3 of 8]\n'))
        for payload in (one, two):
            self.assertEqual(payload['reasoning'], {'effort': 'xhigh'})
            self.assertEqual(payload['tool_choice'], 'auto')
            self.assertEqual(payload['text']['format'], {'type': 'json_schema', 'name': 'review', 'strict': True,
                                                         'schema': prompts.REVIEW_SCHEMA})
            self.assertEqual([tool['name'] for tool in payload['tools']], ['read_file', 'grep', 'list_files', 'diff'])
            self.assertTrue(all(tool['strict'] for tool in payload['tools']))
            self.assertIs(payload['stream'], True)
        self.assertEqual(one['prompt_cache_key'], two['prompt_cache_key'])
        _, again = self.run_loop([[message('{}')]])
        self.assertNotEqual(again.requests[0]['payload']['prompt_cache_key'], one['prompt_cache_key'])
        self.assertEqual({key: usage[key] for key in tool_loop.USAGE_KEYS + ('requests',)},
                         {'input_tokens': 200, 'cached_input_tokens': 80, 'output_tokens': 40, 'reasoning_tokens': 10,
                          'total_tokens': 240, 'requests': 2})
        self.assertEqual(usage['transport']['bytes_received'], 20)
        self.assertEqual(usage['transport']['max_turn_elapsed_seconds'], 1.5)
        self.assertNotIn('http_status', usage['transport'])
        self.assertEqual(trace['tool_calls'], 2)
        self.assertEqual(trace['files_read'], ['src/app.py'])
        self.assertEqual(trace['commands'], ['read_file head src/app.py:1-3', 'grep "parse" base -- src'])
        self.assertEqual((trace['turns'], trace['stopped_reason'], trace['truncated_outputs']), (2, 'final_answer', 0))
        self.assertNotIn('fixture-secret-key', json.dumps([usage, trace]))

    def test_invalid_calls_are_answered_without_failing_the_loop(self):
        bad = [{'type': 'function_call', 'call_id': 'c1', 'name': 'shell', 'arguments': '{}', 'status': 'completed'},
               call_item('c2', 'read_file', rev='head', path='../../etc/passwd', start_line=1, end_line=2)]
        (_, _, _, trace), provider = self.run_loop([bad, [message('{}')]], task=self.task('verify'))
        outputs = [item['output'] for item in provider.requests[1]['payload']['input'] if item.get('type') == 'function_call_output']
        self.assertEqual(outputs, ['error: unknown tool', 'error: invalid repository path'])
        self.assertEqual((trace['tool_calls'], trace['tool_errors'], trace['files_read']), (2, 2, []))
        self.assertEqual(provider.requests[0]['payload']['text']['format']['name'], 'verify')

    def test_tool_call_budget_forces_a_final_turn_without_tools(self):
        turn = [call_item('c1', 'list_files', rev='head', prefix='src'), call_item('c2', 'diff', path='src/app.py')]
        (text, _, _, trace), provider = self.run_loop([turn, [message('{"done":true}')]], backend={'max_tool_calls': 1})
        final = provider.requests[1]['payload']
        self.assertEqual(final['tool_choice'], 'none')
        self.assertIn('not executed; the tool call budget is exhausted', final['input'][-2]['output'])
        self.assertEqual(final['input'][-1]['role'], 'user')
        self.assertIn('(max_tool_calls)', final['input'][-1]['content'])
        self.assertEqual((text, trace['stopped_reason'], trace['tool_calls'], trace['refused_tool_calls']),
                         ('{"done":true}', 'max_tool_calls', 1, 1))

    def test_turn_budget_reserves_the_last_turn_and_fails_if_tools_continue(self):
        turn = lambda index: [call_item(f'c{index}', 'list_files', rev='head', prefix='')]
        (_, _, _, trace), provider = self.run_loop([turn(1), turn(2), [message('{}')]], backend={'max_turns': 3})
        self.assertEqual([request['payload']['tool_choice'] for request in provider.requests], ['auto', 'auto', 'none'])
        self.assertEqual((trace['turns'], trace['stopped_reason']), (3, 'max_turns'))
        with self.assertRaises(core.ReviewError) as caught:
            self.run_loop([turn(1), turn(2)], backend={'max_turns': 2})
        self.assertEqual(str(caught.exception), 'tool_budget_exhausted')
        diagnostics = json.dumps(caught.exception.diagnostics)
        self.assertEqual(caught.exception.diagnostics['stopped_reason'], 'max_turns')
        self.assertEqual(caught.exception.diagnostics['loop_usage']['requests'], 2)
        for secret in ('fixture-secret-key', 'Review the PR', 'opaque-blob', 'src/'):
            self.assertNotIn(secret, diagnostics)

    def test_final_turn_message_is_used_even_with_a_stray_call(self):
        turns = [[call_item('c1', 'list_files', rev='head', prefix='')],
                 [call_item('c2', 'list_files', rev='head', prefix=''), message('{"late":1}')]]
        (text, _, _, trace), _ = self.run_loop(turns, backend={'max_turns': 2})
        self.assertEqual((text, trace['stopped_reason']), ('{"late":1}', 'max_turns'))

    def test_total_output_budget_truncates_then_finishes(self):
        turn = [call_item('c1', 'read_file', rev='head', path='big.txt', start_line=1, end_line=3000)]
        (_, _, _, trace), provider = self.run_loop(
            [turn, [message('{}')]], backend={'max_tool_output_chars': 50000, 'max_total_tool_output_chars': 1200})
        output = provider.requests[1]['payload']['input'][-2]['output']
        self.assertLessEqual(len(output), 1200)
        self.assertIn('[truncated: output limit reached after line', output)
        self.assertEqual((trace['truncated_outputs'], trace['stopped_reason']), (1, 'max_total_tool_output_chars'))
        self.assertEqual(provider.requests[1]['payload']['tool_choice'], 'none')

    def test_context_budget_refuses_large_results_and_finishes(self):
        class Small(FakeProvider):
            def __call__(self, *args):
                items, model, usage = super().__call__(*args)
                return items, model, {**usage, 'input_tokens': 16700, 'output_tokens': 0}
        provider = Small([[call_item('c1', 'list_files', rev='head', prefix='')], [message('{}')]])
        with patch.dict(os.environ, {**ENV, 'GROK_CONTEXT_WINDOW': '32768'}, clear=True):
            trace = tool_loop.run({**BACKEND, 'output_reserve_tokens': 6500}, self.task(), self.workspace, provider)[3]
        self.assertIn('tool output budget is exhausted', provider.requests[1]['payload']['input'][-2]['output'])
        self.assertEqual((trace['stopped_reason'], trace['refused_tool_calls']), ('context_budget', 1))
        with patch.dict(os.environ, {**ENV, 'GROK_CONTEXT_WINDOW': '32768'}, clear=True):
            with self.assertRaisesRegex(core.ReviewError, 'prompt_exceeds_configured_context_budget'):
                tool_loop.run(BACKEND, {**self.task(), 'prompt': 'x' * 60000}, self.workspace, FakeProvider([]))

    def test_total_deadline_keeps_a_reserve_for_the_final_answer(self):
        clock = [0.0]
        backend = {'timeout_seconds': 100, 'final_turn_reserve_seconds': 30}
        with patch.object(tool_loop.time, 'monotonic', lambda: clock[0]):
            (_, _, _, trace), provider = self.run_loop(
                [[call_item('c1', 'diff', path='')], [message('{}')]], backend=backend, clock=clock, step=75)
            self.assertEqual([request['timeout'] for request in provider.requests], [70, 25])
            self.assertEqual(provider.requests[1]['payload']['tool_choice'], 'none')
            self.assertEqual(trace['stopped_reason'], 'timeout')
            clock[0] = 0.0
            interrupted = core.ReviewError('provider_deadline_exceeded', {'stage': 'read'})
            (text, _, usage, trace), provider = self.run_loop([interrupted, [message('{"x":1}')]], backend=backend,
                                                             clock=clock, step=70)
            self.assertEqual((text, trace['stopped_reason'], trace['interrupted_turns'], usage['requests']),
                             ('{"x":1}', 'timeout', 1, 2))
            self.assertEqual(provider.requests[1]['timeout'], 30)

    def test_provider_failures_keep_transport_diagnostics_and_loop_counters(self):
        failure = core.ReviewError('provider_stream_error', {'stage': 'read', 'events': 4})
        with self.assertRaises(core.ReviewError) as caught:
            self.run_loop([[call_item('c1', 'diff', path='')], failure])
        error = caught.exception
        self.assertEqual(str(error), 'provider_stream_error')
        self.assertEqual((error.diagnostics['stage'], error.diagnostics['events'], error.diagnostics['turns'],
                          error.diagnostics['tool_calls']), ('read', 4, 2, 1))
        with self.assertRaisesRegex(core.ReviewError, 'empty_model_output'):
            self.run_loop([[REASONING, message('  ')]])

    def test_transient_failures_resend_the_same_turn_a_bounded_number_of_times(self):
        turn = [call_item('c1', 'diff', path='')]
        bad_gateway = core.ReviewError('http_502', {'stage': 'headers'})
        with patch('time.sleep') as sleep:
            (text, _, usage, trace), provider = self.run_loop(
                [turn, bad_gateway, core.ReviewError('provider_connection_error'), [message('{"ok":1}')]])
        self.assertEqual((text, usage['requests'], trace['turns'], trace['retried_requests']), ('{"ok":1}', 4, 2, 2))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10, 30])
        self.assertEqual(provider.requests[1]['payload']['input'], provider.requests[3]['payload']['input'])
        with patch('time.sleep'), self.assertRaises(core.ReviewError) as caught:
            self.run_loop([bad_gateway, bad_gateway, bad_gateway, [message('{"ok":1}')]])
        self.assertEqual((str(caught.exception), caught.exception.diagnostics['retried_requests']), ('http_502', 2))
        with patch('time.sleep') as sleep, self.assertRaisesRegex(core.ReviewError, 'http_401'):
            self.run_loop([core.ReviewError('http_401'), [message('{"ok":1}')]])
        sleep.assert_not_called()

    def test_configuration_and_task_are_checked_before_any_request(self):
        provider = FakeProvider([])
        cases = [({**ENV, 'GROK_MODEL': ''}, BACKEND, self.task(), 'backend_not_configured'),
                 (ENV, {**BACKEND, 'max_turns': 1}, self.task(), 'invalid_tool_budget'),
                 (ENV, {**BACKEND, 'max_tool_calls': '80'}, self.task(), 'invalid_tool_budget'),
                 (ENV, {**BACKEND, 'final_turn_reserve_seconds': 3600}, self.task(), 'invalid_tool_budget'),
                 (ENV, {**BACKEND, 'api': 'chat_completions'}, self.task(), 'unsupported_compatible_api'),
                 (ENV, BACKEND, {**self.task(), 'kind': 'other'}, 'invalid_harness_task'),
                 ({**ENV, 'GROK_CONTEXT_WINDOW': 'big'}, BACKEND, self.task(), 'invalid_context_window')]
        for env, backend, task, code in cases:
            with patch.dict(os.environ, env, clear=True), self.assertRaises(core.ReviewError) as caught:
                tool_loop.run(backend, task, self.workspace, provider)
            self.assertEqual(str(caught.exception), code)
        self.assertEqual(provider.requests, [])

    def test_default_completion_is_the_item_transport(self):
        with patch.dict(os.environ, ENV, clear=True), \
                patch.object(transport, 'completion', return_value=([message('{}')], 'grok-4.7', {})) as request:
            text = tool_loop.run(BACKEND, self.task(), self.workspace)[0]
        self.assertEqual(text, '{}')
        self.assertIs(request.call_args.kwargs['items'], True)
        self.assertEqual(request.call_args.args[1], 'fixture-secret-key')


class ItemTransportTests(unittest.TestCase):
    def events(self, output):
        """Event sequence observed from grok-4.7 through sub2api for one function-call turn."""
        call = output[-1]
        events = [{'type': 'response.created', 'response': {'status': 'in_progress'}},
                  {'type': 'response.output_item.added', 'output_index': 0, 'item': {'type': 'reasoning', 'id': 'rs_1', 'summary': []}},
                  {'type': 'response.reasoning_summary_text.delta', 'delta': 'thinking'},
                  {'type': 'response.output_item.done', 'output_index': 0, 'item': REASONING},
                  {'type': 'response.output_item.added', 'output_index': 1, 'item': {**call, 'arguments': '', 'status': 'in_progress'}},
                  {'type': 'response.function_call_arguments.delta', 'item_id': call['id'], 'delta': call['arguments']},
                  {'type': 'response.function_call_arguments.done', 'item_id': call['id'], 'arguments': call['arguments'], 'name': call['name']},
                  {'type': 'response.output_item.done', 'output_index': 1, 'item': call},
                  {'type': 'response.completed', 'response': {
                      'object': 'response', 'status': 'completed', 'model': 'grok-4.7', 'output': output,
                      'usage': {'input_tokens': 1430, 'input_tokens_details': {'cached_tokens': 1152}, 'output_tokens': 130,
                                'output_tokens_details': {'reasoning_tokens': 106}, 'total_tokens': 1560}}}]
        return [json.dumps(event) for event in events]

    def test_function_call_turn_returns_unchanged_items_and_cached_usage(self):
        output = [REASONING, call_item('c1', 'read_file', rev='head', path='a.py', start_line=1, end_line=5)]
        decoder = transport.ResponsesItems()
        for event in self.events(output):
            decoder.event(event)
        decoder.event('[DONE]')
        items, model, usage = decoder.result('requested')
        self.assertEqual(items, output)
        self.assertEqual(model, 'grok-4.7')
        self.assertEqual(usage, {'input_tokens': 1430, 'cached_input_tokens': 1152, 'output_tokens': 130,
                                 'total_tokens': 1560, 'reasoning_tokens': 106})
        self.assertEqual(decoder.counts['tool_argument_events'], 1)
        plain = transport.ResponsesCompletion()
        with self.assertRaisesRegex(core.ReviewError, 'unexpected_model_output'):
            for event in self.events(output):
                plain.event(event)

    def test_incomplete_or_malformed_function_calls_fail_closed(self):
        for change in ({'status': 'in_progress'}, {'call_id': ''}, {'arguments': None}, {'name': 5}):
            output = [REASONING, {**call_item('c1', 'diff', path=''), **change}]
            decoder = transport.ResponsesItems()
            with self.assertRaisesRegex(core.ReviewError, 'unexpected_model_output'):
                decoder.event(self.events(output)[-1])
        decoder = transport.ResponsesItems()
        with self.assertRaisesRegex(core.ReviewError, 'unexpected_model_output'):
            decoder.event(json.dumps({'type': 'response.output_item.added', 'item': {'type': 'web_search_call'}}))
        decoder = transport.ResponsesItems()
        decoder.event(json.dumps({'type': 'response.completed', 'response': {'object': 'response', 'status': 'completed', 'output': []}}))
        with self.assertRaisesRegex(core.ReviewError, 'empty_model_output'):
            decoder.result('requested')

    def test_completion_with_items_streams_through_the_bounded_transport(self):
        output = [REASONING, call_item('c1', 'diff', path='')]
        raw = b': keepalive\n\n' + b''.join(f'event: x\ndata: {event}\n\n'.encode() for event in self.events(output))
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.getheader.return_value = 'text/event-stream'
        response.read1.side_effect = [raw[index:index + 50] for index in range(0, len(raw), 50)] + [b'']
        conn = MagicMock()
        conn.getresponse.return_value = response
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            items, model, usage = transport.completion('https://gateway.example/v1/responses', 'do-not-log-this-key',
                                                       {'model': 'grok-4.7'}, {'timeout_seconds': 60, 'api': 'responses'}, items=True)
        self.assertEqual(items, output)
        self.assertEqual(usage['transport']['keepalive_lines'], 1)
        self.assertEqual(usage['transport']['tool_argument_events'], 1)
        self.assertNotIn('opaque-blob', json.dumps(usage))
        with self.assertRaisesRegex(core.ReviewError, 'unsupported_compatible_api'):
            transport.completion('https://gateway.example/v1/chat/completions', 'k', {'model': 'm'},
                                 {'timeout_seconds': 60}, items=True)


if __name__ == '__main__':
    unittest.main()
