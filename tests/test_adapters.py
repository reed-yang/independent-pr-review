import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from independent_review import config, core, service
from support import ROOT, clean_env


class OutputTests(unittest.TestCase):
    def test_mentions_and_model_links_are_not_rendered_as_active_markup(self):
        escaped = core.plain("@reed [click](https://example.com) <img> `code`\nnext")
        self.assertNotIn("@", escaped)
        self.assertNotIn("[", escaped)
        self.assertNotIn("<img>", escaped)
        self.assertNotIn("`", escaped)
        self.assertNotIn("\n", escaped)

    def test_redirects_cannot_forward_authorization(self):
        with self.assertRaisesRegex(core.ReviewError, "redirect_not_allowed"):
            core.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_unknown_harness_is_not_imported(self):
        with self.assertRaisesRegex(core.ReviewError, "harness_not_implemented"):
            service.harness("antigravity_packet")


class ConfigurationTests(unittest.TestCase):
    """Lane configuration must keep two independent repository-reading families."""

    def load(self, backends=None, review=None, env=None):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "rules.md").write_text("Report P1/P2 defects only.")
            value = {"version": 1, "rules": ["rules.md"], **(review or {})}
            if backends is not None:
                (Path(root) / "backends.json").write_text(json.dumps(backends))
                value["backends"] = "backends.json"
            (Path(root) / "review.json").write_text(json.dumps(value))
            with patch.dict(os.environ, clean_env(**(env or {})), clear=True):
                return config.load(root, "review.json")

    def backends(self):
        return json.loads(Path(config.__file__).with_name("backends.json").read_text())

    def test_default_lanes_are_grok_tools_and_gpt_codex(self):
        loaded = self.load()
        self.assertEqual([(slot["id"], slot["backends"]) for slot in loaded["backends"]["slots"]],
                         [("Grok", ["grok-tools"]), ("GPT", ["gpt-codex"])])
        self.assertEqual(loaded["runtime"]["GPT"]["effort"], "ultra")
        self.assertEqual(loaded["runtime"]["GPT"]["verify_effort"], "xhigh")
        self.assertEqual(loaded["effective"], {name: "" for name in loaded["effective"]})
        self.assertEqual(loaded["engine_version"], "0.4.0")

    def test_disabled_backend_cannot_be_selected(self):
        backends = self.backends()
        backends["slots"][1] = {"id": "Gemini", "opinion_family": "gemini", "backends": ["gemini-ai-pro"]}
        with self.assertRaisesRegex(core.ReviewError, "review_backend_disabled"):
            self.load(backends)

    def test_packet_harness_cannot_run_in_the_repository_pipeline(self):
        backends = self.backends()
        backends["slots"][1] = {"id": "Gemini", "opinion_family": "gemini", "backends": ["gemini-ai-pro"]}
        backends["backends"]["gemini-ai-pro"].pop("disabled_reason")
        with self.assertRaisesRegex(core.ReviewError, "unsupported_harness"):
            self.load(backends)

    def test_fallback_and_family_impersonation_are_rejected(self):
        cases = []
        backends = self.backends()
        backends["slots"][1]["backends"] = ["gpt-codex", "grok-tools"]
        cases.append((backends, "automatic_provider_fallback_not_supported"))
        backends = self.backends()
        backends["slots"][1]["backends"] = ["grok-tools"]
        cases.append((backends, "reviewer_family_mismatch"))
        backends = self.backends()
        backends["slots"][1]["opinion_family"] = "grok"
        cases.append((backends, "two_independent_reviewers_required"))
        backends = self.backends()
        backends["slots"] = backends["slots"][:1]
        cases.append((backends, "two_independent_reviewers_required"))
        for value, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(core.ReviewError, code):
                self.load(value)

    def test_backend_names_budgets_and_timeouts_are_validated(self):
        for change, code in (({"key_env": "lower-case"}, "invalid_backend_environment_name"),
                             ({"max_tool_calls": 0}, "invalid_backend_budget"),
                             ({"reservation_tokens": "many"}, "invalid_backend_budget"),
                             ({"timeout_seconds": 5}, "invalid_provider_timeout"),
                             ({"idle_timeout_seconds": 99999}, "invalid_provider_timeout")):
            backends = self.backends()
            backends["backends"]["grok-tools"].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(core.ReviewError, code):
                self.load(backends)

    def test_generation_policy_and_limits_are_validated(self):
        loaded = self.load(review={"generation": {"GPT": {"min_changed_lines": 400, "labels": ["deep-review"],
                                                          "events": ["ready_for_review"]}}})
        self.assertEqual(loaded["generation"]["GPT"]["min_changed_lines"], 400)
        for review in ({"generation": {"Gemini": {}}}, {"generation": {"GPT": {"min_changed_lines": -1}}},
                       {"generation": {"GPT": {"labels": "deep-review"}}}, {"generation": {"GPT": {"unknown": 1}}},
                       {"generation": {"GPT": {"on_full_review": "yes"}}}, {"generation": []}):
            with self.subTest(review=review), self.assertRaisesRegex(core.ReviewError, "invalid_generation_policy"):
                self.load(review=review)
        for limits in ({"max_runs_per_pr": 0}, {"brief_chars": 3000000}, {"unknown": 1}):
            with self.subTest(limits=limits), self.assertRaisesRegex(core.ReviewError, "invalid_review_limits"):
                self.load(review={"limits": limits})
        with self.assertRaisesRegex(core.ReviewError, "verified_publication_required"):
            self.load(review={"verification": False})

    def test_example_budget_admits_a_full_two_lane_reservation(self):
        loaded = self.load()
        example = json.loads((ROOT / "examples/review.json").read_text())
        backends = loaded["backends"]["backends"]
        needed = sum(backends[name]["reservation_tokens"] + backends[name]["verification_reservation_tokens"]
                     for name in ("grok-tools", "gpt-codex"))
        self.assertGreaterEqual(example["limits"]["max_tokens_per_pr"], needed)
        self.assertGreaterEqual(config.DEFAULTS["max_tokens_per_pr"], needed)

    def test_configuration_identity_changes_with_provider_variables(self):
        first, same = self.load(), self.load()
        second = self.load(env={"GROK_MODEL": "grok-4.7"})
        self.assertEqual(first["config_id"], same["config_id"])
        self.assertNotEqual(first["config_id"], second["config_id"])
        self.assertEqual(second["effective"]["GROK_MODEL"], "grok-4.7")


if __name__ == "__main__":
    unittest.main()
