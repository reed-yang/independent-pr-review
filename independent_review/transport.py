"""Bound a streaming completion by connection, idle and total time budgets."""

import http.client
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from .core import ReviewError


class Completion:
    def __init__(self):
        self.parts = []
        self.finish = None
        self.model = None
        self.usage = {}
        self.done = False

    def event(self, data):
        if self.done:
            raise ReviewError('provider_data_after_done')
        if data == '[DONE]':
            self.done = True
            return
        try:
            obj = json.loads(data)
            if not isinstance(obj, dict) or obj.get('error'):
                raise ReviewError('provider_stream_error')
            if isinstance(obj.get('model'), str):
                self.model = obj['model'][:200]
            if isinstance(obj.get('usage'), dict):
                self.usage = {k: v for k, v in obj['usage'].items()
                              if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                              and type(v) is int and v >= 0}
            for choice in obj.get('choices', []):
                if choice.get('index', 0) != 0:
                    raise ReviewError('unexpected_provider_choice')
                delta = choice.get('delta', choice.get('message', {}))
                if delta.get('tool_calls') or delta.get('function_call'):
                    raise ReviewError('incomplete_or_unexpected_model_output')
                content = delta.get('content')
                if content is not None:
                    if not isinstance(content, str) or self.finish is not None:
                        raise ReviewError('invalid_provider_response')
                    self.parts.append(content)
                if choice.get('finish_reason') is not None:
                    self.finish = choice['finish_reason']
        except (ValueError, AttributeError, TypeError, KeyError):
            raise ReviewError('invalid_provider_response') from None

    def result(self, model):
        if not self.done or self.finish != 'stop':
            raise ReviewError('incomplete_or_unexpected_model_output')
        text = ''.join(self.parts)
        if not text:
            raise ReviewError('empty_model_output')
        return text, self.model or model, self.usage


def completion(url, token, payload, backend):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ReviewError('https_endpoint_required')
    start = time.monotonic()
    timeout = backend['timeout_seconds']
    deadline = start + timeout
    idle = backend.get('idle_timeout_seconds', 120)
    connect_timeout = backend.get('connect_timeout_seconds', 20)
    metrics = {'transport': 'stream', 'bytes_received': 0, 'events': 0}
    stage = 'connect'
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                       timeout=min(connect_timeout, timeout), context=ssl.create_default_context())

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ReviewError('provider_deadline_exceeded')
        return value

    def read_timeout(sock):
        sock.settimeout(min(idle, remaining()))

    try:
        conn.connect()
        sock = conn.sock
        metrics['connect_seconds'] = round(time.monotonic() - start, 2)
        stage = 'request'
        read_timeout(sock)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        conn.request('POST', path, body=json.dumps(payload).encode(), headers={
            'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
            'Accept': 'text/event-stream', 'User-Agent': 'independent-pr-review'})
        stage = 'headers'
        read_timeout(sock)
        with conn.getresponse() as response:
            metrics['headers_seconds'] = round(time.monotonic() - start, 2)
            metrics['http_status'] = response.status
            if 300 <= response.status < 400:
                raise ReviewError('redirect_not_allowed')
            if response.status != 200:
                raise ReviewError(f'http_{response.status}')
            is_json = response.getheader('Content-Type', '').split(';')[0].strip() == 'application/json'
            stage = 'read'
            decoder = Completion()
            pending, fields = b'', []
            while True:
                read_timeout(sock)
                chunk = response.read1(65536)
                remaining()
                if not chunk:
                    break
                metrics['bytes_received'] += len(chunk)
                metrics.setdefault('first_byte_seconds', round(time.monotonic() - start, 2))
                if metrics['bytes_received'] > 8_000_000:
                    raise ReviewError('response_too_large')
                pending += chunk
                if is_json:
                    continue
                while b'\n' in pending:
                    line, pending = pending.split(b'\n', 1)
                    line = line.rstrip(b'\r')
                    if line.startswith(b'data:'):
                        fields.append(line[5:].removeprefix(b' '))
                    elif not line and fields:
                        before = len(decoder.parts)
                        decoder.event(b'\n'.join(fields).decode('utf-8'))
                        fields = []
                        metrics['events'] += 1
                        if len(decoder.parts) > before and any(decoder.parts[before:]):
                            metrics.setdefault('first_content_seconds', round(time.monotonic() - start, 2))
                if decoder.done:
                    break
            if is_json:
                metrics['transport'] = 'json_response_to_stream_request'
                decoder.event(pending.decode('utf-8'))
                decoder.done = True
            elif not decoder.done:
                raise ReviewError('provider_stream_incomplete')
            text, model, usage = decoder.result(payload['model'])
            metrics['elapsed_seconds'] = round(time.monotonic() - start, 2)
            return text, model, {**usage, 'transport': metrics}
    except (TimeoutError, socket.gaierror, ssl.SSLError, OSError, http.client.HTTPException, UnicodeError, ReviewError) as exc:
        if isinstance(exc, ReviewError):
            code = str(exc)
        elif isinstance(exc, TimeoutError):
            code = 'provider_deadline_exceeded' if time.monotonic() >= deadline else (
                'provider_idle_timeout' if stage == 'read' else 'provider_' + stage + '_timeout')
        elif isinstance(exc, socket.gaierror):
            code = 'provider_dns_error'
        elif isinstance(exc, ssl.SSLError):
            code = 'provider_tls_error'
        elif isinstance(exc, UnicodeError):
            code = 'invalid_provider_response'
        else:
            code = 'provider_connection_error'
        raise ReviewError(code, {**metrics, 'stage': stage,
                                'elapsed_seconds': round(time.monotonic() - start, 2)}) from None
    finally:
        conn.close()
