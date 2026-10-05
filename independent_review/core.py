#!/usr/bin/env python3
"""Shared, dependency-free primitives for the PR review engine."""

import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request


class ReviewError(Exception):
    """An intentionally redacted operational failure."""

    def __init__(self, code, diagnostics=None):
        super().__init__(code)
        self.diagnostics = diagnostics or {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ReviewError("redirect_not_allowed")


def request_json(url, token, data=None, method=None, timeout=90):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise ReviewError("https_endpoint_required")
    headers = {"Accept": "application/json", "User-Agent": "review-tool/0.1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None if data is None else json.dumps(data).encode()
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ReviewError("response_too_large")
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ReviewError(f"http_{exc.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise ReviewError("network_or_timeout") from None
    except (ValueError, UnicodeError):
        raise ReviewError("invalid_json_response") from None


def github(repo, path, data=None, method=None):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ReviewError("invalid_repository")
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ReviewError("missing_github_token")
    return request_json(f"https://api.github.com/repos/{repo}" + ("/" + path if path else ""), token, data, method)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def load(path):
    return json.loads(Path(path).read_text())


def safe_path(path):
    return isinstance(path, str) and not path.startswith("/") and all(
        part not in ("", ".", "..", ".git") for part in path.split("/")
    ) and not any(ord(char) < 32 for char in path)


def estimate_tokens(text):
    """Estimate text tokens from UTF-8 size; this is not a provider tokenizer."""
    return (len(text.encode('utf-8')) + 2) // 3


# Room for agy's own message wrapping inside its per-user-input-step limit.
NATIVE_WRAPPER_RESERVE_TOKENS = 4000


def native_step_cap():
    """Read the pinned agy input-step limit; larger native steps are silently cut."""
    native = json.loads(Path(__file__).with_name('agy-release.json').read_text()).get('native_input', {})
    limit = native.get('max_user_input_step_tokens')
    # The cap is only meaningful in the same units as estimate_tokens().
    if type(limit) is not int or limit <= NATIVE_WRAPPER_RESERVE_TOKENS or native.get('bytes_per_token') != 3:
        raise ReviewError('invalid_native_input_cap')
    return limit - NATIVE_WRAPPER_RESERVE_TOKENS


# Accepted effort names per harness; Codex resolves "ultra" client-side.
EFFORTS = {'responses_tools': ('low', 'medium', 'high', 'xhigh'),
           'codex_cli': ('low', 'medium', 'high', 'xhigh', 'max', 'ultra'),
           'antigravity_packet': ('low', 'medium', 'high')}
# Provider context ceilings for configured models; a larger setting is rejected.
MODEL_WINDOWS = {'grok-4.6': 500000, 'grok-4.7': 500000, 'grok-4.3': 1000000, 'gpt-6.1-sol': 1050000}


def configured_effort(backend, env_key, default_key):
    effort = os.environ.get(backend.get(env_key, ''), '') or backend.get(default_key)
    if effort is not None and effort not in EFFORTS.get(backend.get('harness'), ()):
        raise ReviewError('unsupported_reasoning_effort')
    return effort


def runtime_settings(backend):
    effort = configured_effort(backend, 'effort_env', 'default_effort')
    raw_window = os.environ.get(backend.get('context_window_env', ''), '')
    try:
        window = int(raw_window) if raw_window else backend.get('default_context_window_tokens', 1048576)
    except (ValueError, TypeError):
        raise ReviewError('invalid_context_window') from None
    if type(window) is not int or not 32768 <= window <= 2097152:
        raise ReviewError('invalid_context_window')
    model = os.environ.get(backend.get('model_env', ''), '')
    ceiling = MODEL_WINDOWS.get(model) or (1048576 if model.startswith('gemini-3.8-flash') else None)
    if ceiling and window > ceiling:
        raise ReviewError('configured_context_exceeds_model_capacity')
    if model == 'grok-4.3' and effort == 'xhigh':
        raise ReviewError('unsupported_reasoning_effort')
    if backend.get('harness') == 'antigravity_packet' and effort and model:
        if not model.endswith('-' + effort):
            raise ReviewError('native_model_effort_mismatch')
    reserve = backend.get('output_reserve_tokens', 6500)
    if type(reserve) is not int or not 6500 <= reserve < window:
        raise ReviewError('invalid_output_reserve')
    settings = {'effort': effort, 'context_window_tokens': window,
                'output_reserve_tokens': reserve,
                'input_budget_tokens': window - max(16000, window // 10, reserve),
                'token_estimation': 'utf8_bytes_divided_by_3_with_completion_and_window_reserve'}
    if 'verify_effort_env' in backend or 'default_verify_effort' in backend:
        settings['verify_effort'] = configured_effort(backend, 'verify_effort_env', 'default_verify_effort') or effort
    if backend.get('harness') == 'antigravity_packet':
        # The model window is unchanged; one native input step is smaller.
        cap = native_step_cap()
        settings.update(native_step_cap_tokens=cap, input_budget_tokens=min(settings['input_budget_tokens'], cap))
    return settings


def check_context(backend, prompt):
    settings = runtime_settings(backend)
    if estimate_tokens(prompt) > settings['input_budget_tokens']:
        raise ReviewError('prompt_exceeds_configured_context_budget')
    return settings


# Same length as input_nonce(); used only to size a prompt before the real call.
INPUT_NONCE_PLACEHOLDER = "0" * 16


def input_nonce():
    """Return a fresh, unpredictable end-of-input marker for one model call."""
    return secrets.token_hex(8)


def end_of_input(nonce):
    return "\nEND_OF_INPUT_NONCE=" + nonce


def input_end_error(obj, nonce):
    """Name a redacted reason when the model did not echo the final input line."""
    value = obj.get("input_end_nonce")
    if not isinstance(value, str) or not value:
        return "input_end_nonce_missing"
    return None if value == nonce else "input_end_nonce_mismatch"


def failure_description(attempt):
    """Explain known transport outcomes without publishing provider error text."""
    error = attempt.get('error')
    diagnostics = attempt.get('diagnostics') or {}
    if error in ('input_end_nonce_missing', 'input_end_nonce_mismatch'):
        return 'The reviewer did not confirm the end of its input, which may have been truncated; its output is not a complete review.'
    if error == 'lane_diff_omitted':
        return 'Some changed-file diffs did not fit the input limit of this reviewer and were not sent to it; its output is not a complete review.'
    if error == 'provider_stream_error':
        reason = diagnostics.get('incomplete_reason')
        if reason in ('max_output_tokens', 'max_prompt_tokens', 'max_time_limit'):
            limit = {'max_output_tokens': 'output', 'max_prompt_tokens': 'input', 'max_time_limit': 'time'}[reason]
            return f'The upstream ended the response at its {limit} limit before a complete review was available.'
        return 'The upstream reported an error before a complete review was available.'
    if error in ('provider_deadline_exceeded', 'provider_progress_timeout', 'provider_idle_timeout'):
        if diagnostics.get('stage') == 'read' and diagnostics.get('http_status') == 200:
            if type(diagnostics.get('content_events')) is int and diagnostics['content_events'] == 0:
                note = 'The request was accepted, but no final review text arrived before the configured time limit.'
                if type(diagnostics.get('reasoning_events')) is int and diagnostics['reasoning_events'] > 0:
                    note += ' Reasoning updates were received; they do not establish a completed review.'
                return note
            return 'The response did not complete before the configured time limit.'
    return None


def plain(value):
    # Render model content as quoted plain text, without links or mentions.
    return html.escape(str(value)).replace("@", "＠").replace("`", "ˋ").replace("[", "［").replace("]", "］").replace("\n", " ")
