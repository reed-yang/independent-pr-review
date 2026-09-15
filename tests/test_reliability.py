import base64
import copy
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from independent_review import cli, core, context, evidence, service, state, transport
from test_engine import BASE, HEAD, REPO, answer, bundle, candidate, packet, pr, settings


class EvidenceTests(unittest.TestCase):
    def test_literal_multiline_source_survives_diff_prefixes_without_full_file(self):
        entry = {'patch': '@@ -1,2 +1,2 @@\n def first(value):\n-    return None\n+    return value[0]'}
        self.assertEqual(evidence.match_evidence(entry, 'def first(value):\n    return value[0]'), 'diff_head')
        self.assertEqual(evidence.match_evidence(entry, 'def first(value):\n    return None'), 'diff_base')
        self.assertIsNone(evidence.match_evidence(entry, 'def first(value):\n    return None', current_only=True))

    def test_quotes_cannot_bridge_hunks_or_mix_old_and_new_source(self):
        entry = {'patch': '@@ -1,2 +1,2 @@\n context_one\n-old_value\n+new_value\n@@ -8 +8 @@\n later_line'}
        for quote in ('new_value\nlater_line', 'old_value\nnew_value', 'context_one\nnew_ value'):
            self.assertIsNone(evidence.match_evidence(entry, quote))

    def test_one_bad_candidate_retains_valid_findings_but_never_clean_status(self):
        data = bundle()
        bad = {**candidate(), 'evidence': 'invented_evidence()'}
        response = answer([candidate(), bad])
        runners = {'compatible_packet': lambda *args: (response, 'grok', {'total_tokens': 100}),
                   'antigravity_packet': lambda *args: (answer(), 'gemini', {'total_tokens': 10})}
        lane = core.run_slot(data['config']['backends']['slots'][0], data['config']['backends']['backends'], data['packet'], runners)
        self.assertEqual(lane['status'], 'partial')
        self.assertEqual(len(lane['findings']), 1)
        self.assertEqual(lane['rejected_findings'][0]['error'], 'finding_evidence_not_in_packet')
        self.assertEqual(lane['usage']['total_tokens'], 100)

    def test_rejected_evidence_is_redacted_and_bounded(self):
        oauth = {'token': {'refresh_token': 'refresh-fixture-secret-value'}}
        with patch.dict(os.environ, {'GROK_API_KEY': 'fixture-secret-key', 'AGY_OAUTH_JSON': json.dumps(oauth)}):
            rejected = evidence.rejection(0, {'path': 'not/a/packet/path',
                                            'evidence': 'fixture-secret-key refresh-fixture-secret-value ' + 'x' * 4000}, 'bad', {})
        self.assertIsNone(rejected['path'])
        self.assertNotIn('secret', rejected['evidence_preview'])
        self.assertLessEqual(len(rejected['evidence_preview']), 1000)

    def test_short_quote_has_its_own_reason_and_invalid_json_keeps_usage(self):
        obj = json.loads(answer([candidate()]))
        obj['findings'][0]['evidence'] = 'return'
        parsed = core.parse_findings(json.dumps(obj), packet(), allow_partial=True)
        self.assertEqual(parsed['rejected_findings'][0]['error'], 'finding_evidence_too_short')
        config = settings()['backends']
        result = core.run_slot(config['slots'][0], config['backends'], packet(),
                               {'compatible_packet': lambda *args: ('broken json', 'grok', {'total_tokens': 321})})
        self.assertEqual(result['usage']['total_tokens'], 321)
        self.assertEqual(result['attempts'][0]['stage'], 'validation')

    def test_dead_provider_is_not_called_again_for_cross_verification(self):
        calls = []
        def failed(*args):
            calls.append(True)
            raise core.ReviewError('provider_idle_timeout', {'stage': 'read'})
        result = service.run(bundle(), {'compatible_packet': failed,
                                       'antigravity_packet': lambda *args: (answer([candidate()]), 'gemini', {})})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result['findings'][0]['status'], 'uncertain')
        self.assertEqual(result['verifications'][0]['error'], 'verification_skipped_after_provider_failure')
        reserved = state.reserve(bundle()['state'], bundle(), '1', 'url')
        accepted = state.accept(reserved, result, '1')
        self.assertFalse(accepted['lanes'])


class StreamingTests(unittest.TestCase):
    def invoke(self, chunks=(), status=200, connect_error=None, read_error=None, content_type='text/event-stream', clock=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.getheader.return_value = content_type
        response.read1.side_effect = list(chunks) + [read_error or b'']
        conn = MagicMock()
        conn.getresponse.return_value = response
        if connect_error:
            conn.connect.side_effect = connect_error
        self.connection = conn
        with patch.object(transport.http.client, 'HTTPSConnection', return_value=conn):
            if clock:
                with patch.object(transport.time, 'monotonic', side_effect=clock):
                    return transport.completion('https://gateway.example/v1/chat/completions', 'do-not-log-this-key',
                                                {'model': 'grok'}, {'timeout_seconds': 600})
            return transport.completion('https://gateway.example/v1/chat/completions', 'do-not-log-this-key',
                                        {'model': 'grok'}, {'timeout_seconds': 600})

    def stream(self, finish='stop'):
        events = [ {'model': 'grok-4.6', 'choices': [{'delta': {'reasoning_content': 'private reasoning'}}]},
                   {'choices': [{'delta': {'content': '{"summary":"ok"}'}}]},
                   {'choices': [{'delta': {}, 'finish_reason': finish}]},
                   {'choices': [], 'usage': {'total_tokens': 42}}]
        return b': ping\r\n\r\n' + b''.join(('data: ' + json.dumps(event) + '\r\n\r\n').encode() for event in events) + b'data: [DONE]\r\n\r\n'

    def test_fragmented_sse_keeps_content_usage_and_safe_timings(self):
        raw = self.stream()
        text, model, usage = self.invoke([raw[i:i+7] for i in range(0, len(raw), 7)])
        self.assertEqual(text, '{"summary":"ok"}')
        self.assertEqual(model, 'grok-4.6')
        self.assertEqual(usage['total_tokens'], 42)
        self.assertIn('first_content_seconds', usage['transport'])
        self.assertNotIn('private reasoning', json.dumps(usage))
        self.connection.close.assert_called_once()

    def test_errors_preserve_stage_without_raw_exception_or_key(self):
        for error, code in [(socket.gaierror('do-not-log-this-key'), 'provider_dns_error'),
                            (ssl.SSLError('private TLS diagnostic'), 'provider_tls_error'),
                            (TimeoutError('private address'), 'provider_connect_timeout')]:
            with self.subTest(code=code), self.assertRaises(core.ReviewError) as caught:
                self.invoke(connect_error=error)
            self.assertEqual(str(caught.exception), code)
            self.assertEqual(caught.exception.diagnostics['stage'], 'connect')
            self.assertNotIn('private', json.dumps(caught.exception.diagnostics))
        with self.assertRaisesRegex(core.ReviewError, 'provider_idle_timeout') as caught:
            self.invoke([b': ping\n\n'], read_error=TimeoutError())
        self.assertEqual(caught.exception.diagnostics['bytes_received'], 8)

    def test_total_deadline_applies_even_with_keepalives(self):
        values = iter([0, 0, 0, 0, 0, 0, 601, 601])
        with self.assertRaisesRegex(core.ReviewError, 'provider_deadline_exceeded'):
            self.invoke([b': ping\n\n'], clock=lambda: next(values, 601))

    def test_truncated_stream_non_stop_finish_and_tool_calls_fail_closed(self):
        for raw in (self.stream().replace(b'data: [DONE]\r\n\r\n', b''), self.stream('length'),
                    b'data: {"choices":[{"delta":{"tool_calls":[{}]}}]}\n\n'):
            with self.subTest(raw=raw), self.assertRaises(core.ReviewError):
                self.invoke([raw])

    def test_redirect_and_http_errors_do_not_follow_or_read_error_bodies(self):
        for status, code in [(302, 'redirect_not_allowed'), (401, 'http_401'), (503, 'http_503')]:
            with self.assertRaisesRegex(core.ReviewError, code):
                self.invoke(status=status)
            self.connection.getresponse.return_value.read1.assert_not_called()

    def test_gateway_ignoring_stream_returns_complete_json_on_same_request(self):
        body = json.dumps({'model': 'grok', 'choices': [{'message': {'content': '{}'}, 'finish_reason': 'stop'}]}).encode()
        result = self.invoke([body], content_type='application/json; charset=utf-8')
        self.assertEqual(result[0], '{}')
        self.assertEqual(result[2]['transport']['transport'], 'json_response_to_stream_request')

    def test_real_tls_stream_with_connection_close(self):
        content = self.stream()
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *args):
                pass
        with tempfile.TemporaryDirectory() as directory:
            cert, key = Path(directory)/'cert.pem', Path(directory)/'key.pem'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                            '-keyout', str(key), '-out', str(cert), '-days', '1',
                            '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                           check=True, capture_output=True)
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(cert, key)
            client_context = ssl.create_default_context(cafile=str(cert))
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.socket = server_context.wrap_socket(server.socket, server_side=True)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                with patch.object(transport.ssl, 'create_default_context', return_value=client_context):
                    result = transport.completion(f'https://localhost:{server.server_port}/v1/chat/completions',
                                                  'fixture', {'model': 'grok'}, {'timeout_seconds': 5})
                self.assertEqual(result[0], '{"summary":"ok"}')
            finally:
                server.shutdown()
                server.server_close()
                worker.join()


class CollectionEfficiencyTests(unittest.TestCase):
    def test_identical_successful_snapshot_skips_collection_and_reservation(self):
        config = settings()
        prior = state.initial(REPO, 7)
        prior['lanes'] = {slot['id']: {'head_sha': HEAD, 'base_sha': BASE, 'config_id': config['config_id']}
                          for slot in config['backends']['slots']}
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'REVIEW_PUBLISH': 'false'}), \
             patch.object(cli, 'metadata', return_value=(REPO, {'inputs': {'pr_number': '7'}}, 'main')), \
             patch.object(cli.configuration, 'load', return_value=config), \
             patch.object(cli.state, 'read', return_value=(prior, 1)), \
             patch.object(cli, 'github', return_value=pr()), \
             patch.object(cli, 'run_identity', return_value=('1:1', 'url')), \
             patch.object(cli, 'collect') as collect, patch.object(cli.state, 'reserve') as reserve:
            cli.prepare(SimpleNamespace(root='.', config='unused', mode='review'))
            collect.assert_not_called()
            reserve.assert_not_called()

    def test_oversized_source_is_skipped_before_download_with_budget_intact(self):
        calls = []
        def api(repo, path):
            calls.append(path)
            if path == 'pulls/7':
                return pr()
            if path.startswith('pulls/7/files'):
                return [{'filename': 'src/a.py', 'status': 'modified', 'patch': packet()['files'][0]['patch']}]
            if path.startswith('compare/'):
                return {'merge_base_commit': {'sha': BASE}}
            if path.startswith('git/trees/'):
                return {'tree': [{'path': 'src/a.py', 'type': 'blob', 'size': 900000},
                                 {'path': 'src/caller.py', 'type': 'blob', 'size': 24}]}
            if path.startswith('contents/src/caller.py'):
                return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(b'from a import first\n').decode()}
            self.fail('Unnecessary source download: ' + path)
        config = settings()
        config['limits']['context_chars'] = 100
        result = context.collect(REPO, 7, pr(), config, state.initial(REPO, 7), api=api)
        self.assertIsNone(result['files'][0]['head_text'])
        self.assertEqual(result['context'][0]['path'], 'src/caller.py')
        self.assertEqual(len([path for path in calls if path.startswith('contents/')]), 1)
