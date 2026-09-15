import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from independent_review import core as review


def packet():
    return {
        "repository": "example/cortex-research", "pr_number": 12,
        "head_sha": "a" * 40, "base_sha": "b" * 40, "packet_id": "packet-1",
        "coverage": "bounded_changed_files_only", "omitted": [],
        "files": [{"path": "a.py", "status": "modified", "patch": "+return value[0]",
                   "head_text": "def first(value):\n    return value[0]\n"}],
    }


def answer():
    return {"summary": "One finding", "limitations": ["Caller not supplied"], "findings": [
        {"path": "a.py", "line": 2, "severity": "P2", "title": "Empty value",
         "body": "An empty list fails at index zero.", "evidence": "return value[0]"}
    ]}


def configuration():
    config = review.load(Path(review.__file__).with_name("backends.json"))
    # Injected runners test coordinator behavior, never Gemini entitlement.
    config["slots"][1]["backends"] = ["gemini-ai-pro"]
    config["backends"]["gemini-ai-pro"].pop("disabled_reason", None)
    return config


class OutputTests(unittest.TestCase):
    def test_evidence_missing_from_packet_is_rejected(self):
        obj = answer()
        obj["findings"][0]["evidence"] = "invented_code()"
        with self.assertRaisesRegex(review.ReviewError, "evidence_not_in_packet"):
            review.parse_findings(json.dumps(obj), packet())

    def test_unknown_file_is_rejected(self):
        obj = answer()
        obj["findings"][0]["path"] = "../../oauth.json"
        with self.assertRaises(review.ReviewError):
            review.parse_findings(json.dumps(obj), packet())

    def test_unverified_line_loses_anchor_not_finding(self):
        obj = answer()
        obj["findings"][0]["line"] = 900
        result = review.parse_findings(json.dumps(obj), packet())
        self.assertIsNone(result["findings"][0]["line"])

    def test_line_without_the_claimed_evidence_loses_anchor(self):
        obj = answer()
        obj["findings"][0]["line"] = 1
        result = review.parse_findings(json.dumps(obj), packet())
        self.assertIsNone(result["findings"][0]["line"])

    def test_no_reviewable_text_does_not_spend_model_calls(self):
        empty = dict(packet(), files=[])
        with self.assertRaisesRegex(review.ReviewError, "no_reviewable_text"):
            review.run_reviews(empty, configuration())

    def test_rejects_boolean_line_and_invalid_schema(self):
        obj = answer()
        obj["findings"][0]["line"] = True
        for text in ["[]", "Not JSON", json.dumps(obj)]:
            with self.assertRaises(review.ReviewError):
                review.parse_findings(text, packet())

    def test_two_independent_reviews_keep_unique_findings(self):
        def grok(*args):
            return json.dumps(answer()), "grok-test", {}
        def gemini(*args):
            return json.dumps({"summary": "No finding", "limitations": [], "findings": []}), "gemini-test", {}
        result = review.run_reviews(packet(), configuration(), {
            "compatible_packet": grok, "antigravity_packet": gemini,
        })
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["reviews"][0]["findings"]), 1)
        self.assertEqual(len(result["reviews"][1]["findings"]), 0)

    def test_failed_backend_is_partial_not_clean(self):
        def fail(*args):
            raise review.ReviewError("http_403")
        result = review.run_reviews(packet(), configuration(), {
            "compatible_packet": lambda *args: (json.dumps(answer()), "grok", {}),
            "antigravity_packet": fail,
        })
        self.assertEqual(result["status"], "partial")

    def test_mentions_and_model_links_are_not_rendered_as_active_markup(self):
        escaped = review.plain("@reed [click](https://example.com) <img> `code`\nnext")
        self.assertNotIn("@", escaped)
        self.assertNotIn("[", escaped)
        self.assertNotIn("<img>", escaped)
        self.assertNotIn("`", escaped)


class RoutingTests(unittest.TestCase):
    def test_disabled_backend_blocks_invocation(self):
        config = review.load(Path(review.__file__).with_name("backends.json"))
        config["backends"]["gemini-ai-pro"]["disabled_reason"] = "operator_disabled"
        slot = dict(config["slots"][1], backends=["gemini-ai-pro"])
        def unexpected(*args):
            self.fail("Unqualified subscription adapter must not run")
        result = review.run_slot(slot, config["backends"], packet(),
                                 {"antigravity_packet": unexpected})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"][0]["error"], "operator_disabled")

    def test_transport_failure_uses_configured_alternative(self):
        config = configuration()
        config["backends"]["grok-backup"] = dict(config["backends"]["grok-gateway"])
        config["backends"]["grok-backup"]["key_env"] = "BACKUP"
        slot = dict(config["slots"][0], backends=["grok-gateway", "grok-backup"])
        calls = []
        def run(backend, prompt):
            calls.append(backend["key_env"])
            if len(calls) == 1:
                raise review.ReviewError("http_503")
            return json.dumps(answer()), "grok-backup-model", {}
        result = review.run_slot(slot, config["backends"], packet(), {"compatible_packet": run})
        self.assertEqual(result["backend"], "grok-backup")
        self.assertEqual(len(result["attempts"]), 2)

    def test_quota_exhaustion_does_not_automatically_switch(self):
        config = configuration()
        config["backends"]["backup"] = dict(config["backends"]["grok-gateway"])
        slot = dict(config["slots"][0], backends=["grok-gateway", "backup"])
        calls = []
        def run(*args):
            calls.append(True)
            raise review.ReviewError("http_429")
        result = review.run_slot(slot, config["backends"], packet(), {"compatible_packet": run})
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "failed")

    def test_fallback_cannot_impersonate_an_independent_model_family(self):
        config = configuration()
        slot = dict(config["slots"][0], backends=["gemini-ai-pro"])
        with self.assertRaisesRegex(review.ReviewError, "preserve_opinion_family"):
            review.run_slot(slot, config["backends"], packet())


class AuthenticationTests(unittest.TestCase):
    def test_readiness_reports_names_without_secret_values(self):
        with patch.dict(os.environ, {"GROK_API_KEY": "secret-value", "GROK_MODEL": "test",
                                     "GROK_BASE_URL": "https://example.invalid/v1"}, clear=True):
            result = review.configuration_status(review.load(Path(review.__file__).with_name("backends.json")))
        self.assertEqual(result[0]["status"], "configured")
        self.assertEqual(result[1]["status"], "unconfigured")
        self.assertEqual(result[1]["missing"], ["AGY_BIN", "AGY_WORK_ROOT", "AGY_OAUTH_JSON", "GEMINI_MODEL"])
        self.assertNotIn("secret-value", json.dumps(result))

    def test_missing_review_configuration_is_explicit(self):
        with patch.dict(os.environ, {}, clear=True):
            result = review.configuration_status(configuration())
        self.assertEqual(result[0]["missing"], ["GROK_API_KEY", "GROK_BASE_URL", "GROK_MODEL"])
        self.assertEqual(result[1]["missing"], ["AGY_BIN", "AGY_WORK_ROOT", "AGY_OAUTH_JSON", "GEMINI_MODEL"])

    def test_provider_truncation_is_not_clean_review(self):
        backend = configuration()["backends"]["grok-gateway"]
        with patch.dict(os.environ, {"GROK_MODEL": "test", "GROK_BASE_URL": "https://gateway.example/v1", "GROK_API_KEY": "test"}):
            with patch("independent_review.core.request_json", return_value={"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}):
                with self.assertRaisesRegex(review.ReviewError, "incomplete"):
                    review.run_compatible(backend, "test")


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.config = review.load(Path(review.__file__).with_name("backends.json"))
        self.backend = self.config["backends"]["gemini-gateway"]
        self.env = patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-secret",
                             "GEMINI_BASE_URL": "https://gateway.example/v1beta",
                             "GEMINI_MODEL": "gemini-test", "GROK_API_KEY": "grok-secret",
                             "GROK_BASE_URL": "https://gateway.example/v1", "GROK_MODEL": "grok-test"}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def response(self):
        return {"modelVersion": "gemini-test-version", "usageMetadata": {"totalTokenCount": 123},
                "candidates": [{"finishReason": "STOP", "content": {"parts": [
                    {"thought": True, "text": "Internal reasoning"}, {"text": json.dumps(answer())}]}}]}

    def test_native_response_keeps_only_answer_and_provider_metadata(self):
        with patch("independent_review.core.request_json", return_value=self.response()) as request:
            text, model, usage = review.run_gemini(self.backend, "review packet")
        self.assertEqual(json.loads(text), answer())
        self.assertEqual(model, "gemini-test-version")
        self.assertEqual(usage["totalTokenCount"], 123)
        self.assertEqual(request.call_args.args[0], "https://gateway.example/v1beta/models/gemini-test:generateContent")
        self.assertEqual(request.call_args.args[1], "gemini-secret")
        payload = request.call_args.args[2]
        self.assertNotIn("gemini-secret", json.dumps(payload))
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")

    def test_incomplete_blocked_or_tool_output_is_rejected(self):
        for reason in ["MAX_TOKENS", "SAFETY", "RECITATION"]:
            with self.subTest(reason=reason):
                response = self.response()
                response["candidates"][0]["finishReason"] = reason
                with patch("independent_review.core.request_json", return_value=response):
                    with self.assertRaisesRegex(review.ReviewError, "incomplete"):
                        review.run_gemini(self.backend, "test")
        response = self.response()
        response["candidates"][0]["content"]["parts"].append({"functionCall": {"name": "read_file"}})
        with patch("independent_review.core.request_json", return_value=response):
            with self.assertRaisesRegex(review.ReviewError, "unexpected_model_output"):
                review.run_gemini(self.backend, "test")

    def test_empty_and_malformed_responses_are_redacted_failures(self):
        for response in [{}, {"candidates": []}, {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}]:
            with self.subTest(response=response), patch("independent_review.core.request_json", return_value=response):
                with self.assertRaises(review.ReviewError):
                    review.run_gemini(self.backend, "test")

    def test_model_cannot_inject_an_endpoint_path(self):
        with patch.dict(os.environ, {"GEMINI_MODEL": "gemini-test/../../other"}), patch("independent_review.core.request_json") as request:
            with self.assertRaisesRegex(review.ReviewError, "invalid_gemini_model"):
                review.run_gemini(self.backend, "test")
            request.assert_not_called()

    def test_optional_gateway_config_uses_independent_keys_and_protocols(self):
        self.config["slots"][1]["backends"] = ["gemini-gateway"]
        def reply(url, key, data, **kwargs):
            if "/chat/completions" in url:
                self.assertEqual(key, "grok-secret")
                return {"model": "grok-test", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer())}}]}
            self.assertEqual(key, "gemini-secret")
            return self.response()
        with patch("independent_review.core.request_json", side_effect=reply):
            result = review.run_reviews(packet(), self.config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual([r["opinion_family"] for r in result["reviews"]], ["grok", "gemini"])
        self.assertNotIn("secret", json.dumps(result))

    def test_redirects_cannot_forward_authorization(self):
        with self.assertRaisesRegex(review.ReviewError, "redirect_not_allowed"):
            review.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


