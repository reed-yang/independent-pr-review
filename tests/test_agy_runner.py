import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from independent_review import agy_runner


class AgyRunnerTests(unittest.TestCase):
    def result(self, **overrides):
        return {"status": "SUCCESS", "num_turns": 1,
                "structured_output": {"summary": "Reviewed", "limitations": [], "findings": []},
                "usage": {"total_tokens": 42}, **overrides}

    def run_fake(self, body, timeout=3):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake.py"
            script.write_text("import json, sys, time\n" + body)
            return agy_runner.stream_review([sys.executable, "-u", str(script)],
                                             "private PR packet", directory,
                                             agy_runner.child_environment(), timeout, "gemini-test")

    def test_child_does_not_inherit_api_keys_or_auth_route_overrides(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "grok-secret", "GH_TOKEN": "github-secret",
                                     "GEMINI_API_KEY": "gemini-secret", "AGY_ADC_AUTH": "true",
                                     "GOOGLE_GEMINI_BASE_URL": "https://other.example",
                                     "JETSKI_APP_DATA_DIR": "/untrusted"}):
            environment = agy_runner.child_environment()
        self.assertNotIn("secret", json.dumps(environment))
        self.assertNotIn("AGY_ADC_AUTH", environment)
        self.assertNotIn("GOOGLE_GEMINI_BASE_URL", environment)
        self.assertNotIn("JETSKI_APP_DATA_DIR", environment)
        self.assertEqual(environment["AGY_CLI_DISABLE_AUTO_UPDATE"], "true")

    def test_complete_single_turn_is_accepted(self):
        body = 'print(json.dumps({"event":"init", "init":{"tools":[],"agent":"independent-packet-review","model":"gemini-test","permission_mode":"request-review"}}), flush=True)\n'
        body += 'message=json.loads(sys.stdin.readline())\n'
        body += 'assert message["message"]["content"] == "private PR packet"\n'
        body += 'print(json.dumps({"event":"result", "result":' + repr(self.result()) + '}), flush=True)\n'
        raw, usage = self.run_fake(body)
        self.assertEqual(json.loads(raw)["findings"], [])
        self.assertEqual(usage["total_tokens"], 42)

    def test_wrong_agent_is_rejected_before_packet_is_sent(self):
        body = 'print(json.dumps({"event":"init", "init":{"tools":[],"agent":"default","model":"gemini-test","permission_mode":"request-review"}}), flush=True)\n'
        body += 'assert not sys.stdin.readline()\n'
        with self.assertRaisesRegex(agy_runner.AgyError, "session_selection_failed"):
            self.run_fake(body)

    def test_zero_exit_partial_response_is_rejected(self):
        body = 'print(json.dumps({"event":"init", "init":{"tools":[],"agent":"independent-packet-review","model":"gemini-test","permission_mode":"request-review"}}), flush=True)\n'
        body += 'sys.stdin.readline()\n'
        body += 'print("warning: timeout reached; partial response", file=sys.stderr)\n'
        body += 'print(json.dumps({"event":"result", "result":' + repr(self.result()) + '}))\n'
        with self.assertRaisesRegex(agy_runner.AgyError, "incomplete_result"):
            self.run_fake(body)

    def test_hung_process_is_terminated_at_outer_deadline(self):
        with self.assertRaisesRegex(agy_runner.AgyError, "agy_timeout"):
            self.run_fake('time.sleep(30)\n', timeout=0.1)

    def test_oversized_output_is_bounded(self):
        with self.assertRaisesRegex(agy_runner.AgyError, "output_too_large"):
            self.run_fake('sys.stderr.write("x" * 4_100_000)\n')

    def test_authentication_error_does_not_expose_cli_output(self):
        body = 'print("Authentication required secret-token-value", file=sys.stderr)\n'
        body += 'sys.exit(1)\n'
        with self.assertRaises(agy_runner.AgyError) as failure:
            self.run_fake(body)
        self.assertEqual(str(failure.exception), "agy_authentication_required")

    def test_unexpected_tool_action_fails_even_when_tools_were_empty(self):
        body = 'print(json.dumps({"event":"init", "init":{"tools":[],"agent":"independent-packet-review","model":"gemini-test","permission_mode":"request-review"}}), flush=True)\n'
        body += 'sys.stdin.readline()\n'
        body += 'print(json.dumps({"event":"step_update", "step_update":{"step_type":"tool_call"}}))\n'
        with self.assertRaisesRegex(agy_runner.AgyError, "unexpected_agent_action"):
            self.run_fake(body)

    def test_authentication_failure_before_init_is_redacted(self):
        body = 'print(json.dumps({"event":"result", "result":{"status":"ERROR", "error":"authentication failed private-token"}}))\n'
        with self.assertRaises(agy_runner.AgyError) as failure:
            self.run_fake(body)
        self.assertEqual(str(failure.exception), "agy_authentication_required")

    def test_nonterminal_denied_and_multiple_turn_results_are_rejected(self):
        for result in [self.result(status="WAITING"), self.result(num_turns=2),
                       self.result(denied_actions=["read_file"]), self.result(error="private diagnostic")]:
            with self.subTest(result=result):
                with self.assertRaisesRegex(agy_runner.AgyError, "incomplete_result"):
                    agy_runner.result_payload(result, b"")

    def test_malformed_stream_envelopes_are_redacted(self):
        for event in [{"event": "init", "init": None}, {"event": "result", "result": "private"},
                      {"event": "step_update", "step_update": None}, ["invalid"]]:
            with self.subTest(event=event):
                with self.assertRaises(agy_runner.AgyError):
                    self.run_fake('print(' + repr(json.dumps(event)) + ')\n')

    def test_oauth_route_and_refresh_rotation_fail_closed(self):
        original = {"auth_method": "consumer", "token": {
            "token_type": "Bearer", "refresh_token": "original-refresh", "access_token": "new-access",
            "expiry": "2999-01-01T00:00:00Z"}}
        self.assertEqual(agy_runner.oauth_document(json.dumps(original)), original)
        for bad in [[], {"token": []}, {**original, "auth_method": "enterprise"}]:
            with self.assertRaisesRegex(agy_runner.AgyError, "invalid_consumer_oauth"):
                agy_runner.oauth_document(json.dumps(bad))
        agy_runner.refreshed_credentials(original, original)
        updated = json.loads(json.dumps(original))
        updated["token"]["refresh_token"] = "rotated-refresh"
        with self.assertRaisesRegex(agy_runner.AgyError, "rotated_reprovision_required"):
            agy_runner.refreshed_credentials(original, updated)
        updated["token"] = {**original["token"], "expiry": "2000-01-01T00:00:00Z"}
        with self.assertRaisesRegex(agy_runner.AgyError, "native_refresh_not_verified"):
            agy_runner.refreshed_credentials(original, updated)

    def test_native_home_is_private_and_disposed_without_ambient_customizations(self):
        credential = {"auth_method": "consumer", "token": {
            "token_type": "Bearer", "refresh_token": "original-refresh",
            "access_token": "original-access", "expiry": "2999-01-01T00:00:00Z"}}
        backend = {"binary_env": "AGY_BIN", "state_env": "AGY_WORK_ROOT", "model_env": "GEMINI_MODEL",
                   "oauth_env": "AGY_OAUTH_JSON", "timeout_seconds": 3}
        seen = []

        def native(command, prompt, cwd, env, timeout, model):
            home = Path(env["HOME"])
            seen.append(home)
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)
            self.assertNotIn("AGY_OAUTH_JSON", env)
            profile = home / ".gemini/antigravity-cli"
            token = profile / "antigravity-oauth-token"
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            state = json.loads(token.read_text())
            self.assertEqual(state["token"]["expiry"], "2000-01-01T00:00:00Z")
            settings = json.loads((profile / "settings.json").read_text())
            self.assertFalse(settings["useG1Credits"])
            self.assertEqual(set(settings["permissions"]["deny"]), {
                "read_file(*)", "write_file(*)", "command(*)", "read_url(*)", "execute_url(*)", "mcp(*)"})
            agent = home / ".gemini/config/agents/independent-packet-review/agent.md"
            self.assertIn("excludeDefaultComponents: true", agent.read_text())
            self.assertIn("inheritCustomizations: false", agent.read_text())
            self.assertNotIn("--json-schema", command)
            state["token"].update(access_token="refreshed-access", expiry="2999-01-01T00:00:00Z")
            token.write_text(json.dumps(state))
            return '{"summary":"Reviewed","limitations":[],"findings":[]}', {}

        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"AGY_BIN": sys.executable, "AGY_WORK_ROOT": root,
                                         "GEMINI_MODEL": "gemini-test", "AGY_OAUTH_JSON": json.dumps(credential)}):
                with patch.object(agy_runner, "stream_review", side_effect=native):
                    agy_runner.run(backend, "packet")
                    agy_runner.run(backend, "packet")
            self.assertNotEqual(seen[0], seen[1])
            self.assertTrue(all(not path.exists() for path in seen))
            self.assertEqual([p.name for p in Path(root).iterdir()], ["review.lock"])

    def test_schema_mode_can_be_replaced_by_strict_json_fence_parsing(self):
        result = self.result()
        del result["structured_output"]
        result["response"] = '```json\n{"summary":"Reviewed","limitations":[],"findings":[]}\n```'
        raw, _ = agy_runner.result_payload(result, b"")
        self.assertEqual(json.loads(raw)["summary"], "Reviewed")
        result["response"] += '\n{"summary":"another response"}'
        with self.assertRaisesRegex(agy_runner.AgyError, "invalid_review_json"):
            agy_runner.result_payload(result, b"")

    def test_non_string_cli_responses_are_redacted_failures(self):
        for response in [None, [], {}, 7]:
            result = self.result()
            del result["structured_output"]
            result["response"] = response
            with self.subTest(response=response):
                with self.assertRaisesRegex(agy_runner.AgyError, "invalid_review_json"):
                    agy_runner.result_payload(result, b"")


if __name__ == "__main__":
    unittest.main()
