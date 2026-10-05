import functools
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import sys
import tarfile
import tempfile
import threading
import time
import tomllib
import unittest
from unittest.mock import patch

from independent_review import codex_runner, install_codex
from independent_review.core import ReviewError
from independent_review.credential_proxy import CredentialProxy
from independent_review.hardening import harden_process, take_secret


USAGE = {'input_tokens': 100, 'input_tokens_details': {'cached_tokens': 40}, 'output_tokens': 30,
         'output_tokens_details': {'reasoning_tokens': 20}, 'total_tokens': 130}


class Upstream:
    """A fake Responses endpoint that records what the proxy forwarded."""

    def __init__(self):
        self.requests = []
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                upstream.requests.append({'path': self.path, 'body': json.loads(body),
                                          'headers': {k.lower(): v for k, v in self.headers.items()}})
                if self.path.endswith('/compact'):
                    data = json.dumps({'output': [], 'usage': USAGE}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('X-Upstream-Secret', 'upstream-header')
                self.end_headers()
                events = [{'type': 'response.created', 'response': {}},
                          {'type': 'response.output_text.delta', 'delta': 'text "usage" response.completed'},
                          {'type': 'response.completed', 'response': {'model': 'gpt-test', 'usage': USAGE}}]
                for event in events:
                    self.wfile.write(b'data: ' + json.dumps(event).encode() + b'\n\n')
                    self.wfile.flush()

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}/v1'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def post(proxy, path='/v1/responses', payload=None, token=None, headers=None):
    body = json.dumps(payload if payload is not None else {'model': 'gpt-test', 'reasoning': {'effort': 'xhigh'},
                                                            'input': 'private prompt'}).encode()
    connection = http.client.HTTPConnection('127.0.0.1', proxy.port, timeout=10)
    connection.request('POST', path, body=body, headers={
        'Authorization': 'Bearer ' + (proxy.token if token is None else token),
        'Content-Type': 'application/json', **(headers or {})})
    response = connection.getresponse()
    data = response.read()
    connection.close()
    return response, data


class CredentialProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)

    def proxy(self, **limits):
        proxy = CredentialProxy(self.upstream.url, 'real-provider-key', 'gpt-test',
                                {'max_requests': 10, 'max_total_tokens': 10_000, **limits}, allow_http=True)
        proxy.__enter__()
        self.addCleanup(proxy.__exit__, None, None, None)
        return proxy

    def test_http_upstream_requires_explicit_test_flag(self):
        with self.assertRaisesRegex(ReviewError, 'https_endpoint_required'):
            CredentialProxy(self.upstream.url, 'key', 'gpt-test', {})
        with self.assertRaisesRegex(ReviewError, 'https_endpoint_required'):
            CredentialProxy('https://user:pass@example.com/v1', 'key', 'gpt-test', {})

    def test_requests_need_the_local_token(self):
        proxy = self.proxy()
        for token in ('', 'wrong', 'real-provider-key'):
            response, _ = post(proxy, token=token)
            self.assertEqual(response.status, 401)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(proxy.metrics()['rejected'], {'unauthorized': 3})

    def test_only_responses_paths_and_post_are_forwarded(self):
        proxy = self.proxy()
        for path in ('/v1/models', '/v1/responses?x=1', '/responses', '/v1/responses/../models', '/v1/chat/completions'):
            self.assertEqual(post(proxy, path=path)[0].status, 404, path)
        connection = http.client.HTTPConnection('127.0.0.1', proxy.port, timeout=10)
        connection.request('GET', '/v1/responses', headers={'Authorization': 'Bearer ' + proxy.token})
        self.assertEqual(connection.getresponse().status, 405)
        connection.close()
        self.assertEqual(self.upstream.requests, [])
        response, data = post(proxy, path='/v1/responses/compact')
        self.assertEqual(response.status, 200)
        self.assertEqual(self.upstream.requests[0]['path'], '/v1/responses/compact')
        self.assertEqual(proxy.metrics()['input_tokens'], 100)

    def test_model_and_encoding_are_enforced(self):
        proxy = self.proxy()
        self.assertEqual(post(proxy, payload={'model': 'other-model'})[0].status, 403)
        self.assertEqual(post(proxy, payload=['gpt-test'])[0].status, 403)
        self.assertEqual(post(proxy, headers={'Content-Encoding': 'gzip'})[0].status, 415)
        self.assertEqual(post(proxy, headers={'Content-Encoding': 'zstd'})[0].status, 415)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(proxy.metrics()['rejected'], {'model': 2, 'encoding': 2})

    def test_stream_passes_through_with_injected_key_and_allowlisted_headers(self):
        proxy = self.proxy()
        response, data = post(proxy, headers={'Session-Id': 'session-1', 'Cookie': 'c=1', 'X-Forwarded-For': '1.2.3.4',
                                              'OpenAI-Organization': 'org', 'Originator': 'codex_exec'})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Connection'), 'close')
        self.assertIsNone(response.getheader('X-Upstream-Secret'))
        self.assertIn(b'"response.completed"', data)
        self.assertEqual(data.count(b'data: '), 3)
        forwarded = self.upstream.requests[0]['headers']
        self.assertEqual(forwarded['authorization'], 'Bearer real-provider-key')
        self.assertEqual(forwarded['session-id'], 'session-1')
        self.assertEqual(forwarded['originator'], 'codex_exec')
        for name in ('cookie', 'x-forwarded-for', 'openai-organization'):
            self.assertNotIn(name, forwarded)
        self.assertEqual(self.upstream.requests[0]['body']['input'], 'private prompt')

    def test_usage_is_summed_and_metrics_hold_no_secrets_or_bodies(self):
        proxy = self.proxy()
        post(proxy)
        post(proxy, payload={'model': 'gpt-test', 'reasoning': {'effort': 'low'}})
        metrics = proxy.metrics()
        self.assertEqual((metrics['requests'], metrics['input_tokens'], metrics['cached_input_tokens'],
                          metrics['output_tokens'], metrics['reasoning_tokens'], metrics['total_tokens']),
                         (2, 200, 80, 60, 40, 260))
        self.assertEqual(metrics['efforts'], {'xhigh': 1, 'low': 1})
        self.assertEqual(metrics['models'], {'gpt-test': 2})
        self.assertEqual(metrics['statuses'], {'200': 2})
        text = json.dumps(metrics)
        for value in ('real-provider-key', proxy.token, 'private prompt', 'session'):
            self.assertNotIn(value, text)

    def test_request_cap_stops_further_requests(self):
        proxy = self.proxy(max_requests=1)
        self.assertEqual(post(proxy)[0].status, 200)
        self.assertEqual(post(proxy)[0].status, 429)
        self.assertTrue(proxy.stopped.is_set())
        self.assertEqual(proxy.metrics()['stopped_reason'], 'max_requests')
        self.assertEqual(len(self.upstream.requests), 1)

    def test_token_cap_stops_after_reported_usage(self):
        proxy = self.proxy(max_total_tokens=100)
        self.assertEqual(post(proxy)[0].status, 200)
        self.assertTrue(proxy.stopped.is_set())
        self.assertEqual(post(proxy)[0].status, 429)
        self.assertEqual(proxy.metrics()['stopped_reason'], 'max_total_tokens')


class ConfigTests(unittest.TestCase):
    def test_config_is_read_only_and_routes_through_the_proxy(self):
        config = tomllib.loads(codex_runner.config_toml('gpt-6.1-sol', 'ultra', 'http://127.0.0.1:4321/v1'))
        self.assertEqual((config['model'], config['model_reasoning_effort'], config['model_provider']),
                         ('gpt-6.1-sol', 'ultra', 'review'))
        self.assertEqual((config['approval_policy'], config['sandbox_mode'], config['web_search']),
                         ('never', 'read-only', 'disabled'))
        self.assertEqual(config['model_providers']['review'], {
            'name': 'review', 'base_url': 'http://127.0.0.1:4321/v1', 'wire_api': 'responses',
            'env_key': 'REVIEW_PROXY_TOKEN', 'supports_websockets': False})
        self.assertEqual(config['shell_environment_policy'], {
            'inherit': 'core', 'exclude': ['REVIEW_*', '*TOKEN*', '*KEY*', '*SECRET*']})
        self.assertEqual(config['project_doc_max_bytes'], 0)
        for name in ('apps', 'browser_use', 'computer_use', 'image_generation', 'memories', 'goals', 'hooks',
                     'plugins', 'enable_request_compression'):
            self.assertIs(config['features'][name], False)
        self.assertNotIn('code_mode_host', config['features'])
        self.assertNotIn('multi_agent', config['features'])
        self.assertIs(config['analytics']['enabled'], False)
        self.assertEqual(config['otel']['metrics_exporter'], 'none')
        self.assertNotIn('model_reasoning_effort', tomllib.loads(
            codex_runner.config_toml('gpt-6.1-sol', None, 'http://127.0.0.1:4321/v1')))

    def test_values_cannot_inject_toml(self):
        for model, effort, url in (('gpt"\nsandbox_mode = "danger-full-access', 'low', 'http://127.0.0.1:1/v1'),
                                   ('gpt', 'low"\nx = "y', 'http://127.0.0.1:1/v1'),
                                   ('gpt', 'low', 'https://example.com/v1')):
            with self.assertRaises(ReviewError):
                codex_runner.config_toml(model, effort, url)

    def test_redaction_and_paths(self):
        text = codex_runner.redact('curl -H "Authorization: Bearer abcdefghijkl" OPENAI_API_KEY=sk-123 tok', ['tok'])
        self.assertNotIn('abcdefghijkl', text)
        self.assertNotIn('sk-123', text)
        self.assertNotIn(' tok', text)
        with tempfile.TemporaryDirectory() as directory:
            head = Path(directory)
            (head / 'src').mkdir()
            (head / 'src/a.py').write_text('x')
            command = f"/bin/zsh -lc 'sed -n 1,5p src/a.py | rg x {head}/src/a.py; git show HEAD:src/a.py ../etc'"
            self.assertEqual(codex_runner.command_paths(command, head), ['src/a.py', 'src/a.py', 'src/a.py'])


FAKE_CODEX = r'''#!{python}
import http.client, json, os, re, subprocess, sys, time
args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(__file__))
if args == ['--version']:
    print('codex-cli ' + open(os.path.join(here, 'VERSION')).read().strip())
    sys.exit(0)
prompt = sys.stdin.read()
mode = re.search(r'MODE=(\w+)', prompt).group(1)
options = dict(zip(args, args[1:]))
config = open(os.path.join(os.environ['CODEX_HOME'], 'config.toml')).read()
port = int(re.search(r'base_url = "http://127.0.0.1:(\d+)/v1"', config).group(1))
token = os.environ['REVIEW_PROXY_TOKEN']

def call():
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    connection.request('POST', '/v1/responses', body=json.dumps({'model': 'gpt-test', 'reasoning': {'effort': 'xhigh'}}),
                       headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    status = connection.getresponse().status
    connection.close()
    return status

def emit(event):
    print(json.dumps(event), flush=True)

def command(text, output='', code=0):
    emit({'type': 'item.completed', 'item': {'id': 'c', 'type': 'command_execution', 'command': text,
          'aggregated_output': output, 'exit_code': code, 'status': 'completed'}})

emit({'type': 'thread.started', 'thread_id': 't'})
emit({'type': 'turn.started'})
if mode == 'timeout':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    open(re.search(r'PIDFILE=(\S+)', prompt).group(1), 'w').write(str(child.pid))
    time.sleep(60)
if mode == 'budget':
    statuses = [call() for _ in range(3)]
    time.sleep(60)
if mode == 'fail':
    sys.exit(2)
if mode == 'turnfail':
    emit({'type': 'turn.failed', 'error': {'message': 'upstream said no'}})
    sys.exit(0)
if mode == 'nofinal':
    sys.exit(0)
call()
call()
command("/bin/zsh -lc 'sed -n 1,5p src/a.py'", 'line')
command('curl -H "Authorization: Bearer abcdefghijklmnop" ' + 'x' * 400, code=1)
if mode == 'leakevent':
    command('env', 'REVIEW_PROXY_TOKEN=' + token)
emit({'type': 'item.completed', 'item': {'id': 's', 'type': 'collab_tool_call', 'tool': 'spawn_agent'}})
emit({'type': 'item.completed', 'item': {'id': 'r', 'type': 'reasoning', 'text': 'thinking'}})
emit({'type': 'turn.completed', 'usage': {'input_tokens': 20, 'cached_input_tokens': 8, 'output_tokens': 10,
                                          'reasoning_output_tokens': 4}})
final = {'env': sorted(name for name in os.environ if not name.startswith('__CF')), 'args': args,
         'cwd': os.getcwd(), 'schema': json.load(open(options['--output-schema'])),
         'prompt_tail': prompt[-12:], 'leak': token if mode == 'leakfinal' else ''}
open(options['-o'], 'w').write(json.dumps(final))
'''


class FakeWorkspace:
    def __init__(self, head):
        self.head = head

    def checkout_head(self):
        return self.head


class CodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        package = self.root / 'package'
        (package / 'bin').mkdir(parents=True)
        (package / 'codex-path').mkdir()
        (package / 'codex-package.json').write_text('{}')
        (package / 'bin/VERSION').write_text(codex_runner.release_version())
        binary = package / 'bin/codex'
        binary.write_text(FAKE_CODEX.replace('{python}', sys.executable))
        binary.chmod(0o755)
        self.package = package
        self.head = self.root / 'head'
        (self.head / 'src').mkdir(parents=True)
        (self.head / 'src/a.py').write_text('print(1)\n')
        self.backend = {'key_env': 'TEST_GPT_KEY', 'base_url_env': 'TEST_GPT_BASE', 'model_env': 'TEST_GPT_MODEL',
                        'binary_env': 'TEST_CODEX_BIN', 'timeout_seconds': 20, 'max_requests': 10,
                        'max_total_tokens': 1_000_000}
        environment = patch.dict(os.environ, {'TEST_GPT_KEY': 'real-provider-key', 'TEST_GPT_BASE': self.upstream.url,
                                              'TEST_GPT_MODEL': 'gpt-test', 'TEST_CODEX_BIN': str(package)})
        environment.start()
        self.addCleanup(environment.stop)
        proxy = patch.object(codex_runner, 'CredentialProxy', functools.partial(CredentialProxy, allow_http=True))
        proxy.start()
        self.addCleanup(proxy.stop)

    def run_fake(self, mode, **backend):
        task = {'kind': 'review', 'prompt': f'review MODE={mode} PIDFILE={self.root}/pid END', 'effort': 'xhigh',
                'schema': {'type': 'object', 'properties': {}}}
        return codex_runner.run({**self.backend, **backend}, task, FakeWorkspace(self.head))

    def test_successful_run_reports_proxy_usage_and_bounded_trace(self):
        raw, model, usage, trace = self.run_fake('ok')
        final = json.loads(raw)
        self.assertEqual(final['env'], ['CODEX_HOME', 'HOME', 'LANG', 'PATH', 'REVIEW_PROXY_TOKEN', 'TMPDIR'])
        self.assertNotIn('TEST_GPT_KEY', os.environ)
        self.assertEqual(final['cwd'], str(self.head))
        self.assertTrue(final['prompt_tail'].endswith('/pid END'))
        self.assertEqual(final['schema'], {'type': 'object', 'properties': {}})
        for flag in ('--ephemeral', '--json', '--strict-config', '--ignore-rules'):
            self.assertIn(flag, final['args'])
        self.assertEqual(final['args'][-3:], ['-C', str(self.head), '-'])
        self.assertEqual(model, 'gpt-test')
        self.assertEqual((usage['requests'], usage['input_tokens'], usage['cached_input_tokens'],
                          usage['output_tokens'], usage['reasoning_tokens'], usage['total_tokens']),
                         (2, 200, 80, 60, 40, 260))
        self.assertEqual(usage['transport']['efforts'], {'xhigh': 2})
        self.assertEqual(trace['tool_calls'], 3)
        self.assertEqual(trace['files_read'], ['src/a.py'])
        self.assertEqual(trace['commands_failed'], 1)
        self.assertEqual(trace['subagents_spawned'], 1)
        self.assertEqual(trace['stopped_reason'], 'completed')
        self.assertEqual(trace['codex_turn_usage']['reasoning_output_tokens'], 4)
        self.assertEqual(len(trace['commands'][1]), 300)
        self.assertNotIn('abcdefghijklmnop', json.dumps(trace))
        self.assertNotIn('real-provider-key', json.dumps([raw, usage, trace]))

    def test_timeout_kills_the_process_group(self):
        start = time.monotonic()
        with self.assertRaisesRegex(ReviewError, 'codex_timeout'):
            self.run_fake('timeout', timeout_seconds=2)
        self.assertLess(time.monotonic() - start, 10)
        child = int((self.root / 'pid').read_text())
        time.sleep(0.2)
        try:
            os.kill(child, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive:
            os.kill(child, signal.SIGKILL)
        self.assertFalse(alive)

    def test_credentials_in_events_or_final_message_fail_closed(self):
        for mode in ('leakevent', 'leakfinal'):
            with self.subTest(mode=mode), patch.dict(os.environ, {'TEST_GPT_KEY': 'real-provider-key'}):
                with self.assertRaises(ReviewError) as caught:
                    self.run_fake(mode)
                self.assertEqual(str(caught.exception), 'credential_in_output')
                self.assertNotIn('real-provider-key', json.dumps(caught.exception.diagnostics))

    def test_budget_exhaustion_stops_the_run(self):
        with self.assertRaises(ReviewError) as caught:
            self.run_fake('budget', max_requests=1)
        self.assertEqual(str(caught.exception), 'proxy_budget_exhausted')
        self.assertEqual(caught.exception.diagnostics['stopped_reason'], 'max_requests')

    def test_failures_have_redacted_codes(self):
        for mode, code in (('fail', 'codex_process_failed'), ('turnfail', 'codex_turn_failed'),
                           ('nofinal', 'codex_no_final_message')):
            with self.subTest(mode=mode), patch.dict(os.environ, {'TEST_GPT_KEY': 'real-provider-key'}):
                with self.assertRaises(ReviewError) as caught:
                    self.run_fake(mode)
                self.assertEqual(str(caught.exception), code)
                self.assertNotIn('upstream said no', json.dumps(caught.exception.diagnostics))

    def test_missing_binary_key_or_wrong_version_is_rejected(self):
        with patch.dict(os.environ, {'TEST_CODEX_BIN': str(self.root / 'missing')}):
            with self.assertRaisesRegex(ReviewError, 'codex_not_found'):
                self.run_fake('ok')
        with patch.dict(os.environ, {'TEST_GPT_KEY': ''}):
            with self.assertRaisesRegex(ReviewError, 'backend_not_configured'):
                self.run_fake('ok')
        (self.package / 'bin/VERSION').write_text('0.0.1')
        with patch.dict(os.environ, {'TEST_GPT_KEY': 'real-provider-key'}):
            with self.assertRaisesRegex(ReviewError, 'codex_version_mismatch'):
                self.run_fake('ok')


class HardeningTests(unittest.TestCase):
    def test_take_secret_removes_the_variable(self):
        with patch.dict(os.environ, {'TEST_SECRET_VALUE': 'value'}):
            self.assertEqual(take_secret('TEST_SECRET_VALUE'), 'value')
            self.assertNotIn('TEST_SECRET_VALUE', os.environ)
            with self.assertRaisesRegex(ReviewError, 'backend_not_configured'):
                take_secret('TEST_SECRET_VALUE')
        with self.assertRaisesRegex(ReviewError, 'backend_not_configured'):
            take_secret('')

    def test_harden_process_reports_a_status(self):
        expected = 'linux_nondumpable' if sys.platform.startswith('linux') else 'unsupported_platform'
        self.assertEqual(harden_process(), expected)


def package_archive(members):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as bundle:
        for name, content in members:
            member = tarfile.TarInfo(name)
            if content is None:
                member.type, member.linkname = tarfile.SYMTYPE, '/etc/passwd'
                bundle.addfile(member)
            else:
                member.size, member.mode = len(content), 0o755
                bundle.addfile(member, io.BytesIO(content))
    return data.getvalue()


class InstallCodexTests(unittest.TestCase):
    def install(self, archive, digest=None):
        pin = {'version': '9.9.9', 'platforms': {install_codex.platform_key(): {
            'target': 'test-target', 'url': 'https://example.invalid/codex.tar.gz',
            'sha256': digest or hashlib.sha256(archive).hexdigest()}}}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        destination = Path(directory.name) / 'codex'
        with patch.object(install_codex, 'release', return_value=pin), \
                patch.object(install_codex.urllib.request, 'urlopen', return_value=io.BytesIO(archive)):
            return destination, install_codex.install(destination)

    def manifest(self, version='9.9.9'):
        return json.dumps({'version': version, 'target': 'test-target', 'entrypoint': 'bin/codex',
                           'pathDir': 'codex-path'}).encode()

    def test_verified_package_is_installed(self):
        archive = package_archive([('codex-package.json', self.manifest()), ('bin/codex', b'binary'),
                                   ('codex-path/rg', b'rg')])
        destination, root = self.install(archive)
        self.assertEqual(root, destination.absolute())
        self.assertEqual((root / 'bin/codex').read_bytes(), b'binary')
        self.assertTrue(os.access(root / 'bin/codex', os.X_OK))

    def test_digest_mismatch_installs_nothing(self):
        archive = package_archive([('codex-package.json', self.manifest()), ('bin/codex', b'binary')])
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(archive, digest='0' * 64)

    def test_unsafe_or_mismatched_packages_are_rejected(self):
        for members in ([('codex-package.json', self.manifest()), ('bin/codex', None)],
                        [('codex-package.json', self.manifest()), ('../escape', b'x')],
                        [('codex-package.json', self.manifest('1.0.0')), ('bin/codex', b'binary')]):
            with self.subTest(members=[name for name, _ in members]):
                with self.assertRaises(ValueError):
                    self.install(package_archive(members))


if __name__ == '__main__':
    unittest.main()
