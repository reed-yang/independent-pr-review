"""Loopback proxy that lends a CLI one model route without exposing the provider key.

The CLI authenticates with a random per-run token. The proxy checks the token,
path, encoding and model, injects the real key, streams the upstream response
back and passively sums reported usage. It records counts only; request and
response bodies and headers are never stored or logged.
"""

import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .core import ReviewError


LOCAL_PREFIX = '/v1'
ALLOWED_PATHS = ('/responses', '/responses/compact')
# Codex request headers without credentials; every other header is dropped.
REQUEST_HEADERS = ('accept', 'content-type', 'user-agent', 'originator', 'version', 'openai-beta',
                   'session-id', 'session_id', 'thread-id', 'conversation_id', 'x-client-request-id',
                   'x-codex-beta-features', 'x-codex-turn-metadata', 'x-codex-window-id',
                   'x-openai-internal-codex-responses-lite')
RESPONSE_HEADERS = ('content-type', 'content-length', 'retry-after')
MAX_REQUEST_BYTES = 64_000_000
MAX_RESPONSE_BYTES = 64_000_000
MAX_EVENT_BYTES = 16_000_000
USAGE_EVENTS = ('response.completed', 'response.done', 'response.incomplete', 'response.failed')
USAGE_MARKERS = tuple(f'"{kind}"'.encode() for kind in USAGE_EVENTS)
USAGE_FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_tokens', 'total_tokens')


def bump(table, key, amount=1):
    table[key] = table.get(key, 0) + amount


def usage_record(usage):
    """Normalize one Responses usage object; ignore malformed values."""
    if not isinstance(usage, dict):
        return None
    def number(container, key):
        value = container.get(key) if isinstance(container, dict) else None
        return value if type(value) is int and value >= 0 else 0
    record = {'input_tokens': number(usage, 'input_tokens'), 'output_tokens': number(usage, 'output_tokens'),
              'cached_input_tokens': number(usage.get('input_tokens_details'), 'cached_tokens'),
              'reasoning_tokens': number(usage.get('output_tokens_details'), 'reasoning_tokens')}
    record['total_tokens'] = number(usage, 'total_tokens') or record['input_tokens'] + record['output_tokens']
    return record


class UsageParser:
    """Find usage in SSE terminal events (or a JSON body) without keeping the stream."""

    def __init__(self, is_json):
        self.is_json = is_json
        self.pending = b''
        self.fields = []
        self.size = 0
        self.records = []
        self.models = []
        self.overflow = False

    def feed(self, chunk):
        self.pending += chunk
        if self.is_json:
            if len(self.pending) > MAX_EVENT_BYTES:
                self.pending, self.overflow, self.is_json = b'', True, None
            return
        if self.is_json is None:
            return
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if line.startswith(b'data:'):
                part = line[5:].removeprefix(b' ')
                self.size += len(part)
                if self.size <= MAX_EVENT_BYTES:
                    self.fields.append(part)
                else:
                    self.overflow = True
            elif not line:
                if self.fields and self.size <= MAX_EVENT_BYTES:
                    self.event(b'\n'.join(self.fields))
                self.fields, self.size = [], 0
        if len(self.pending) > MAX_EVENT_BYTES:
            self.pending, self.overflow = b'', True

    def event(self, data):
        # Deltas are skipped cheaply; escaped text cannot contain the quoted key "usage".
        if b'"usage"' not in data or not any(marker in data for marker in USAGE_MARKERS):
            return
        try:
            obj = json.loads(data)
        except (ValueError, UnicodeError):
            return
        if isinstance(obj, dict) and obj.get('type') in USAGE_EVENTS and isinstance(obj.get('response'), dict):
            self.response(obj['response'])

    def response(self, response):
        record = usage_record(response.get('usage'))
        if record:
            self.records.append(record)
        model = response.get('model')
        if isinstance(model, str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,100}', model):
            self.models.append(model)

    def finish(self):
        if self.is_json and self.pending:
            try:
                obj = json.loads(self.pending)
            except (ValueError, UnicodeError):
                return
            if isinstance(obj, dict):
                self.response(obj)


def positive(limits, key, default):
    value = limits.get(key, default)
    if type(value) not in (int, float) or isinstance(value, bool) or value <= 0:
        raise ReviewError('invalid_proxy_limits')
    return value


class CredentialProxy:
    """Context manager serving http://127.0.0.1:<port>/v1 for one run."""

    def __init__(self, upstream_base_url, real_key, expected_model, limits, allow_http=False):
        parsed = urlsplit((upstream_base_url or '').rstrip('/'))
        schemes = ('https', 'http') if allow_http else ('https',)
        if (parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ReviewError('https_endpoint_required')
        if not real_key or not expected_model:
            raise ReviewError('backend_not_configured')
        self.upstream = parsed
        self._key = real_key
        self.model = expected_model
        self.token = secrets.token_urlsafe(32)
        self.max_requests = positive(limits, 'max_requests', 400)
        self.max_total_tokens = positive(limits, 'max_total_tokens', 30_000_000)
        self.connect_timeout = positive(limits, 'connect_timeout_seconds', 20)
        self.idle_timeout = positive(limits, 'idle_timeout_seconds', 300)
        self.total_timeout = positive(limits, 'timeout_seconds', 3600)
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.stopped_reason = None
        self.active = set()
        self.server = None
        self.started = None
        self.counters = {'requests': 0, 'in_flight': 0, 'max_in_flight': 0, 'rejected': {}, 'statuses': {},
                         'paths': {}, 'efforts': {}, 'upstream_errors': {}, 'models': {}, 'usage_events': 0,
                         'usage_overflows': 0, 'request_bytes': 0, 'response_bytes': 0,
                         **{name: 0 for name in USAGE_FIELDS}}

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}{LOCAL_PREFIX}'

    def __enter__(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'review-proxy'
            sys_version = ''

            def log_message(self, *args):
                pass

            def do_POST(self):
                proxy.handle(self)

            def reject_method(self):
                proxy.reject(self, 405, 'method')

            do_GET = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = reject_method

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # Never print tracebacks; they could include request data.
                proxy.count('rejected', 'handler_error')

        self.server = Server(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.2}, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        with self.lock:
            sockets = list(self.active)
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server.server_close()
        self.thread.join(5)
        return False

    def remaining(self):
        return self.started + self.total_timeout - time.monotonic()

    def count(self, table, key, amount=1):
        with self.lock:
            bump(self.counters[table], key, amount)

    def stop(self, reason):
        # Caller holds the lock.
        if self.stopped_reason is None:
            self.stopped_reason = reason
        self.stopped.set()

    def metrics(self):
        with self.lock:
            value = json.loads(json.dumps(self.counters))
        value['stopped_reason'] = self.stopped_reason
        value['elapsed_seconds'] = round(time.monotonic() - self.started, 2) if self.started else 0
        return value

    def reject(self, handler, status, reason):
        self.count('rejected', reason)
        body = json.dumps({'error': {'message': 'review proxy rejected the request: ' + reason,
                                     'type': 'review_proxy'}}).encode()
        try:
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(body)))
            handler.send_header('Connection', 'close')
            handler.end_headers()
            handler.wfile.write(body)
        except OSError:
            pass
        handler.close_connection = True

    def handle(self, handler):
        handler.close_connection = True
        supplied = handler.headers.get('Authorization', '').encode('latin-1', 'replace')
        if not hmac.compare_digest(supplied, ('Bearer ' + self.token).encode()):
            return self.reject(handler, 401, 'unauthorized')
        path = handler.path
        suffix = path[len(LOCAL_PREFIX):] if path.startswith(LOCAL_PREFIX + '/') else None
        if suffix not in ALLOWED_PATHS:
            return self.reject(handler, 404, 'path')
        if handler.headers.get('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
            return self.reject(handler, 415, 'encoding')
        length = handler.headers.get('Content-Length', '')
        if handler.headers.get('Transfer-Encoding') or not length.isdigit():
            return self.reject(handler, 411, 'length')
        if int(length) > MAX_REQUEST_BYTES:
            return self.reject(handler, 413, 'size')
        body = handler.rfile.read(int(length))
        if len(body) != int(length):
            return self.reject(handler, 400, 'body')
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            return self.reject(handler, 400, 'body')
        if not isinstance(payload, dict) or payload.get('model') != self.model:
            return self.reject(handler, 403, 'model')
        reasoning = payload.get('reasoning')
        effort = reasoning.get('effort') if isinstance(reasoning, dict) else None
        effort = effort if isinstance(effort, str) and re.fullmatch(r'[a-z]{1,16}', effort) else 'none' if effort is None else 'other'
        with self.lock:
            if not self.stopped.is_set():
                if self.counters['requests'] >= self.max_requests:
                    self.stop('max_requests')
                elif self.counters['total_tokens'] >= self.max_total_tokens:
                    self.stop('max_total_tokens')
                elif self.remaining() <= 0:
                    self.stop('deadline')
            admitted = not self.stopped.is_set()
            if admitted:
                counters = self.counters
                counters['requests'] += 1
                counters['in_flight'] += 1
                counters['max_in_flight'] = max(counters['max_in_flight'], counters['in_flight'])
                counters['request_bytes'] += len(body)
                bump(counters['paths'], suffix.strip('/'))
                bump(counters['efforts'], effort)
        if not admitted:
            return self.reject(handler, 429, 'budget')
        try:
            self.forward(handler, suffix, body)
        finally:
            with self.lock:
                self.counters['in_flight'] -= 1

    def forward(self, handler, suffix, body):
        upstream = self.upstream
        headers = {name: handler.headers[name] for name in REQUEST_HEADERS if handler.headers.get(name) is not None}
        headers.update({'Authorization': 'Bearer ' + self._key, 'Content-Length': str(len(body)),
                        'Accept-Encoding': 'identity'})
        timeout = max(0.01, min(self.connect_timeout, self.remaining()))
        if upstream.scheme == 'https':
            conn = http.client.HTTPSConnection(upstream.hostname, upstream.port or 443, timeout=timeout,
                                               context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=timeout)
        stage, sock, parser = 'connect', None, None
        try:
            conn.connect()
            sock = conn.sock
            with self.lock:
                self.active.add(sock)
            stage = 'request'
            sock.settimeout(max(0.01, min(self.idle_timeout, self.remaining())))
            conn.request('POST', upstream.path.rstrip('/') + suffix, body=body, headers=headers)
            stage = 'headers'
            response = conn.getresponse()
            stage = 'read'
            self.count('statuses', str(response.status))
            if 300 <= response.status < 400:
                return self.reject(handler, 502, 'upstream_redirect')
            if response.getheader('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
                return self.reject(handler, 502, 'upstream_encoding')
            content_type = response.getheader('Content-Type', '')
            parser = UsageParser(content_type.split(';')[0].strip().lower() == 'application/json')
            handler.send_response(response.status)
            for name in RESPONSE_HEADERS:
                value = response.getheader(name)
                if value is not None and (name != 'content-length' or value.isdigit()):
                    handler.send_header(name, value)
            handler.send_header('Connection', 'close')
            handler.end_headers()
            stage = 'stream'
            received, held = 0, b''
            # Usage is counted before the client receives the bytes that complete a
            # response, so metrics read after the CLI exits include every request.
            while not response.isclosed():
                remaining = self.remaining()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(max(0.01, min(self.idle_timeout, remaining)))
                chunk = response.read1(65536)
                if not chunk:
                    break
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    self.count('upstream_errors', 'response_too_large')
                    break
                parser.feed(chunk)
                self.account(parser)
                if parser.is_json:
                    held += chunk
                    continue
                if not self.relay(handler, held + chunk):
                    held = b''
                    break
                held = b''
            parser.finish()
            self.account(parser)
            if held:
                self.relay(handler, held)
            with self.lock:
                self.counters['response_bytes'] += received
                self.counters['usage_overflows'] += int(parser.overflow)
        except (TimeoutError, socket.timeout):
            self.count('upstream_errors', stage + '_timeout')
            if stage in ('connect', 'request', 'headers'):
                self.reject(handler, 504, 'upstream_timeout')
        except (OSError, ValueError, http.client.HTTPException, ssl.SSLError):
            self.count('upstream_errors', stage + '_failed')
            if stage in ('connect', 'request', 'headers'):
                self.reject(handler, 502, 'upstream_unavailable')
        finally:
            if sock is not None:
                with self.lock:
                    self.active.discard(sock)
            conn.close()

    def relay(self, handler, data):
        try:
            handler.wfile.write(data)
            handler.wfile.flush()
            return True
        except OSError:
            self.count('upstream_errors', 'client_disconnected')
            return False

    def account(self, parser):
        records, models = parser.records, parser.models
        parser.records, parser.models = [], []
        if not records and not models:
            return
        with self.lock:
            counters = self.counters
            for record in records:
                counters['usage_events'] += 1
                for name in USAGE_FIELDS:
                    counters[name] += record[name]
            for model in models:
                if model in counters['models'] or len(counters['models']) < 5:
                    bump(counters['models'], model)
            if counters['total_tokens'] >= self.max_total_tokens:
                self.stop('max_total_tokens')
