"""Run the official agy CLI with no agent tools and bounded, redacted output."""

import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time


class AgyError(Exception):
    """A redacted CLI or credential-lifecycle failure."""


AGENT = "independent-packet-review"
DENIED_ACTIONS = ("read_file", "write_file", "read_url", "execute_url", "command", "mcp")


def oauth_document(raw):
    try:
        document = json.loads(raw)
        token = document["token"]
        if document["auth_method"] != "consumer" or not isinstance(token.get("refresh_token"), str) or not token["refresh_token"]:
            raise ValueError()
        if token.get("token_type") != "Bearer":
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AgyError("agy_invalid_consumer_oauth") from None
    return document


def sensitive_values(document):
    token = document.get("token", {})
    return [value for value in [document.get("id_token"), token.get("access_token"),
                               token.get("refresh_token")] if isinstance(value, str) and value]


def mask_credentials(document):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for value in sensitive_values(document):
            escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print("::add-mask::" + escaped, flush=True)


def refreshed_credentials(original, updated):
    mask_credentials(updated)
    if original["token"]["refresh_token"] != updated["token"]["refresh_token"]:
        raise AgyError("agy_refresh_token_rotated_reprovision_required")
    try:
        expiry = datetime.fromisoformat(updated["token"]["expiry"].replace("Z", "+00:00"))
        valid = expiry > datetime.now(timezone.utc)
    except (KeyError, TypeError, ValueError):
        valid = False
    access = updated["token"].get("access_token")
    if not valid or not isinstance(access, str) or not access or access == "expired-for-native-refresh":
        raise AgyError("agy_native_refresh_not_verified")


def child_environment():
    # Only native process essentials reach agy; gateway/GitHub keys do not.
    names = ("HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "TMPDIR",
             "SYSTEMROOT", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["AGY_CLI_DISABLE_AUTO_UPDATE"] = "true"
    return env


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin can return EPERM for a process group whose last member exited.
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            raise AgyError("agy_process_cleanup_failed") from None


def parse_event(line):
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        raise AgyError("agy_invalid_event") from None
    if not isinstance(event, dict):
        raise AgyError("agy_invalid_event")
    return event


def result_payload(result, stderr):
    if not isinstance(result, dict) or result.get("status") != "SUCCESS":
        raise AgyError("agy_incomplete_result")
    if result.get("num_turns") != 1 or result.get("error") or result.get("denied_actions"):
        raise AgyError("agy_incomplete_result")
    if re.search(rb"timed?\s*out|timeout|truncat|partial (?:output|response)", stderr, re.I):
        raise AgyError("agy_incomplete_result")
    payload = result.get("structured_output")
    if payload is None:
        try:
            response = result["response"].strip()
            if response.startswith("```json\n") and response.endswith("```"):
                response = response[8:-3].strip()
            payload = json.loads(response)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise AgyError("agy_invalid_review_json") from None
    if not isinstance(payload, dict):
        raise AgyError("agy_invalid_review_json")
    usage = result.get("usage", {})
    if not isinstance(usage, dict):
        raise AgyError("agy_invalid_usage")
    usage = {name: value for name, value in usage.items()
             if name in ("input_tokens", "output_tokens", "thinking_tokens", "cache_read_tokens", "total_tokens")
             and type(value) is int and value >= 0}
    return json.dumps(payload, ensure_ascii=False), usage


def stream_review(command, prompt, cwd, env, timeout, model):
    """Verify native session selection before supplying the single review turn."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    pending = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False).encode() + b"\n"
    stdout = b""
    stderr = bytearray()
    total = 0
    initialized = False
    result = None
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for stream in (process.stdout, process.stderr, process.stdin):
                os.set_blocking(stream.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgyError("agy_timeout")
                for key, _ in selector.select(min(remaining, 0.5)):
                    stream = key.fileobj
                    if stream is process.stdin:
                        try:
                            written = os.write(stream.fileno(), pending[:65536])
                        except BrokenPipeError:
                            raise AgyError("agy_input_closed") from None
                        pending = pending[written:]
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        continue
                    total += len(chunk)
                    if total > 4_000_000:
                        raise AgyError("agy_output_too_large")
                    if stream is process.stderr:
                        stderr.extend(chunk)
                        continue
                    stdout += chunk
                    while b"\n" in stdout:
                        line, stdout = stdout.split(b"\n", 1)
                        if not line.strip():
                            continue
                        event = parse_event(line)
                        kind = event.get("event")
                        if kind == "init":
                            initialization = event.get("init")
                            # 1.2.2 reports the global registry here, even for a
                            # custom agent that excludes all default tools.
                            if initialized or not isinstance(initialization, dict) or (
                                initialization.get("agent") != AGENT or
                                initialization.get("model") != model or
                                initialization.get("permission_mode") != "request-review"
                            ):
                                raise AgyError("agy_session_selection_failed")
                            initialized = True
                            selector.register(process.stdin, selectors.EVENT_WRITE)
                        elif kind == "result":
                            candidate = event.get("result")
                            if not isinstance(candidate, dict):
                                raise AgyError("agy_invalid_event")
                            error = str(candidate.get("error", ""))
                            if re.search(r"not (?:signed|logged) in|authenticat", error, re.I):
                                raise AgyError("agy_authentication_required")
                            if not initialized or result is not None:
                                raise AgyError("agy_unexpected_result")
                            result = candidate
                        elif kind == "step_update":
                            step = event.get("step_update", {})
                            if not initialized or not isinstance(step, dict) or step.get("step_type") not in ("user_input", "agent_response", "checkpoint"):
                                raise AgyError("agy_unexpected_agent_action")
                        else:
                            raise AgyError("agy_unexpected_event")
            if stdout.strip():
                raise AgyError("agy_incomplete_event")
        remaining = max(0.01, deadline - time.monotonic())
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise AgyError("agy_timeout") from None
        if code:
            if re.search(rb"not (?:signed|logged) in|authentication required|cannot complete interactive login", stderr, re.I):
                raise AgyError("agy_authentication_required")
            raise AgyError("agy_process_failed")
        return result_payload(result, bytes(stderr))
    finally:
        kill_group(process)
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def run(backend, prompt):
    from .core import check_context
    settings = check_context(backend, prompt)
    binary = os.environ.get(backend["binary_env"], "")
    model = os.environ.get(backend["model_env"], "")
    state = os.environ.get(backend["state_env"], "")
    oauth = os.environ.get(backend["oauth_env"], "")
    if not binary or not state or not model or not oauth:
        raise AgyError("agy_not_configured")
    original = oauth_document(oauth)
    mask_credentials(original)
    if not Path(binary).is_absolute() or not Path(binary).is_file():
        raise AgyError("agy_binary_not_found")
    if not re.fullmatch(r"gemini-[a-zA-Z0-9._-]+", model):
        raise AgyError("agy_invalid_model")
    root = Path(state)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "review.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgyError("agy_account_busy") from None
        with tempfile.TemporaryDirectory(prefix="packet-", dir=root) as temporary:
            private_home = Path(temporary)
            workspace = private_home / "workspace"
            workspace.mkdir()
            profile = private_home / ".gemini/antigravity-cli"
            profile.mkdir(parents=True, mode=0o700)
            token_path = profile / "antigravity-oauth-token"
            restored = json.loads(oauth)
            # Every invocation proves native renewal rather than relying on a
            # cached access token. Never mutate the operator's login file.
            restored["token"]["access_token"] = "expired-for-native-refresh"
            restored["token"]["expiry"] = "2000-01-01T00:00:00Z"
            token_path.write_text(json.dumps(restored))
            token_path.chmod(0o600)
            (profile / "settings.json").write_text(json.dumps({
                "permissions": {"deny": [action + "(*)" for action in DENIED_ACTIONS]},
                "useG1Credits": False,
            }))
            agent = private_home / ".gemini/config/agents" / AGENT / "agent.md"
            agent.parent.mkdir(parents=True)
            agent.write_text(Path(__file__).with_name("agy-agent.md").read_text())
            command = [binary, "--input-format", "stream-json", "--output-format", "stream-json",
                       "--model", model, "--agent", AGENT,
                       "--add-dir", str(workspace), "--disable-slash-commands",
                       "--print-timeout", str(backend["timeout_seconds"]) + "s"]
            if settings["effort"]:
                command.extend(["--effort", settings["effort"]])
            env = child_environment()
            env["HOME"] = str(private_home)
            try:
                raw, usage = stream_review(command, prompt, workspace, env, backend["timeout_seconds"], model)
            finally:
                updated = oauth_document(token_path.read_text())
                mask_credentials(updated)
            refreshed_credentials(original, updated)
            if any(value in raw for value in sensitive_values(original) + sensitive_values(updated)):
                raise AgyError("agy_credential_in_output")
    # This is the pinned selection, not a claim of independently attested identity.
    return raw, model, usage
