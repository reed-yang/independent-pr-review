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
        self.terminal = {}
        self.done = False
        self.counts = {'reasoning_events': 0, 'reasoning_chars': 0, 'content_events': 0}

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
            if isinstance(obj.get('type'), str) and obj['type'].startswith('response.'):
                raise ReviewError('provider_protocol_mismatch')
            if not isinstance(obj.get('choices'), list):
                raise ReviewError('invalid_provider_response')
            if isinstance(obj.get('model'), str):
                self.model = obj['model'][:200]
            if isinstance(obj.get('usage'), dict):
                self.usage = {k: v for k, v in obj['usage'].items()
                              if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                              and type(v) is int and v >= 0}
                details = obj['usage'].get('completion_tokens_details')
                if isinstance(details, dict) and type(details.get('reasoning_tokens')) is int and details['reasoning_tokens'] >= 0:
                    self.usage['reasoning_tokens'] = details['reasoning_tokens']
            for choice in obj.get('choices', []):
                if choice.get('index', 0) != 0:
                    raise ReviewError('unexpected_provider_choice')
                delta = choice.get('delta', choice.get('message', {}))
                if delta.get('tool_calls') or delta.get('function_call'):
                    raise ReviewError('incomplete_or_unexpected_model_output')
                reasoning = delta.get('reasoning_content')
                if isinstance(reasoning, str) and reasoning:
                    self.counts['reasoning_events'] += 1
                    self.counts['reasoning_chars'] += len(reasoning)
                content = delta.get('content')
                if content is not None:
                    if not isinstance(content, str) or self.finish is not None:
                        raise ReviewError('invalid_provider_response')
                    self.parts.append(content)
                    self.counts['content_events'] += bool(content)
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


class ResponsesCompletion(Completion):
    def response_metadata(self, response):
        if not isinstance(response, dict):
            raise ReviewError('invalid_provider_response')
        status = response.get('status')
        if status in ('completed', 'incomplete', 'failed', 'cancelled'):
            self.terminal['upstream_status'] = status
        details = response.get('incomplete_details')
        if isinstance(details, dict):
            reason = details.get('reason')
            self.terminal['incomplete_reason'] = reason if reason in (
                'max_output_tokens', 'max_prompt_tokens', 'max_time_limit', 'content_filter') else 'other'
        usage = response.get('usage')
        if isinstance(usage, dict):
            self.usage = {key: value for key, value in usage.items()
                          if key in ('input_tokens', 'output_tokens', 'total_tokens')
                          and type(value) is int and value >= 0}
            details = usage.get('output_tokens_details')
            if isinstance(details, dict) and type(details.get('reasoning_tokens')) is int and details['reasoning_tokens'] >= 0:
                self.usage['reasoning_tokens'] = details['reasoning_tokens']

    def event(self, data):
        if data == '[DONE]':
            if not self.done:
                raise ReviewError('provider_stream_incomplete')
            return
        if self.done:
            raise ReviewError('provider_data_after_done')
        try:
            obj = json.loads(data)
            kind = obj.get('type')
            if kind in ('response.completed', 'response.incomplete', 'response.failed'):
                self.response_metadata(obj.get('response', {}))
            elif obj.get('object') == 'response':
                self.response_metadata(obj)
            if kind in ('error', 'response.failed', 'response.incomplete') or obj.get('error'):
                raise ReviewError('provider_stream_error')
            if kind == 'response.output_text.delta':
                if not isinstance(obj.get('delta'), str):
                    raise ReviewError('invalid_provider_response')
                self.parts.append(obj['delta'])
                self.counts['content_events'] += bool(obj['delta'])
            elif kind in ('response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
                delta = obj.get('delta')
                if isinstance(delta, str) and delta:
                    self.counts['reasoning_events'] += 1
                    self.counts['reasoning_chars'] += len(delta)
            elif kind in ('response.output_item.added', 'response.output_item.done'):
                if obj.get('item', {}).get('type') not in ('message', 'reasoning'):
                    raise ReviewError('incomplete_or_unexpected_model_output')
            elif kind == 'response.completed' or obj.get('object') == 'response':
                response = obj['response'] if kind == 'response.completed' else obj
                if response.get('status') != 'completed' or response.get('error') or response.get('incomplete_details'):
                    raise ReviewError('incomplete_or_unexpected_model_output')
                output = response.get('output')
                if not isinstance(output, list):
                    raise ReviewError('invalid_provider_response')
                parts = []
                for item in output:
                    if item.get('type') == 'reasoning':
                        continue
                    if item.get('type') != 'message' or item.get('role') != 'assistant' or item.get('status', 'completed') != 'completed':
                        raise ReviewError('incomplete_or_unexpected_model_output')
                    for part in item.get('content', []):
                        if part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                            raise ReviewError('incomplete_or_unexpected_model_output')
                        parts.append(part['text'])
                final = ''.join(parts)
                if self.parts and ''.join(self.parts) != final:
                    raise ReviewError('provider_stream_content_mismatch')
                self.parts = parts
                self.model = response.get('model') if isinstance(response.get('model'), str) else None
                self.finish, self.done = 'stop', True
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ReviewError('invalid_provider_response') from None


def completion(url, token, payload, backend):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ReviewError('https_endpoint_required')
    start = time.monotonic()
    timeout = backend['timeout_seconds']
    deadline = start + timeout
    idle = backend.get('idle_timeout_seconds', 120)
    progress_timeout = backend.get('progress_timeout_seconds')
    last_progress = None
    connect_timeout = backend.get('connect_timeout_seconds', 20)
    metrics = {'transport': 'stream', 'api': backend.get('api', 'chat_completions'), 'bytes_received': 0, 'events': 0}
    decoder = ResponsesCompletion() if backend.get('api') == 'responses' else Completion()
    stage = 'connect'
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443,
                                       timeout=min(connect_timeout, timeout), context=ssl.create_default_context())

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise ReviewError('provider_deadline_exceeded')
        return value

    def progress_remaining():
        if last_progress is not None:
            value = progress_timeout - (time.monotonic() - last_progress)
            if value <= 0:
                raise ReviewError('provider_progress_timeout')
            return value
        return timeout

    def read_timeout(sock):
        budget = min(idle, remaining(), progress_remaining())
        sock.settimeout(budget)

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
            observed_progress = time.monotonic()
            metrics['last_progress_seconds'] = round(observed_progress - start, 2)
            if progress_timeout is not None and not is_json:
                last_progress = observed_progress
                metrics['progress_timeout_seconds'] = progress_timeout
                metrics['last_progress_seconds'] = round(last_progress - start, 2)
            stage = 'read'
            pending, fields = b'', []
            while True:
                read_timeout(sock)
                chunk = response.read1(65536)
                remaining()
                progress_remaining()
                if not chunk:
                    break
                metrics['bytes_received'] += len(chunk)
                metrics.setdefault('first_byte_seconds', round(time.monotonic() - start, 2))
                metrics['last_byte_seconds'] = round(time.monotonic() - start, 2)
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
                    elif line.startswith(b':'):
                        metrics['keepalive_lines'] = metrics.get('keepalive_lines', 0) + 1
                    elif not line and fields:
                        before = len(decoder.parts)
                        previous_counts = dict(decoder.counts)
                        decoder.event(b'\n'.join(fields).decode('utf-8'))
                        fields = []
                        metrics['events'] += 1
                        content_progress = len(decoder.parts) > before and any(decoder.parts[before:])
                        if content_progress:
                            metrics.setdefault('first_content_seconds', round(time.monotonic() - start, 2))
                        if decoder.counts != previous_counts or content_progress:
                            progress_at = time.monotonic()
                            metrics['largest_progress_gap_seconds'] = round(max(
                                metrics.get('largest_progress_gap_seconds', 0), progress_at - observed_progress), 2)
                            observed_progress = progress_at
                            metrics['last_progress_seconds'] = round(progress_at - start, 2)
                            if last_progress is not None:
                                last_progress = progress_at
                if decoder.done:
                    break
            if is_json:
                metrics['transport'] = 'json_response_to_stream_request'
                decoder.event(pending.decode('utf-8'))
                decoder.done = True
            elif not decoder.done:
                raise ReviewError('provider_stream_incomplete')
            text, model, usage = decoder.result(payload['model'])
            metrics.update(decoder.counts)
            metrics.update(decoder.terminal)
            metrics['elapsed_seconds'] = round(time.monotonic() - start, 2)
            return text, model, {**usage, 'transport': metrics}
    except (TimeoutError, socket.gaierror, ssl.SSLError, OSError, http.client.HTTPException, UnicodeError, ReviewError) as exc:
        if isinstance(exc, ReviewError):
            code = str(exc)
        elif isinstance(exc, TimeoutError):
            code = 'provider_deadline_exceeded' if time.monotonic() >= deadline else (
                'provider_progress_timeout' if last_progress is not None and time.monotonic() - last_progress >= progress_timeout else
                'provider_idle_timeout' if stage == 'read' else 'provider_' + stage + '_timeout')
        elif isinstance(exc, socket.gaierror):
            code = 'provider_dns_error'
        elif isinstance(exc, ssl.SSLError):
            code = 'provider_tls_error'
        elif isinstance(exc, UnicodeError):
            code = 'invalid_provider_response'
        else:
            code = 'provider_connection_error'
        if 'last_progress_seconds' in metrics:
            metrics['seconds_without_progress'] = round(max(0, time.monotonic() - start - metrics['last_progress_seconds']), 2)
        if decoder.usage:
            metrics['provider_usage'] = decoder.usage
        raise ReviewError(code, {**metrics, **decoder.counts, **decoder.terminal, 'stage': stage,
                                'elapsed_seconds': round(time.monotonic() - start, 2)}) from None
    finally:
        conn.close()
