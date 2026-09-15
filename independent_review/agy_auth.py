"""Provision native consumer OAuth privately, or check a fresh hosted session."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess

from .agy_runner import AgyError, oauth_document, run
from .core import ReviewError, parse_findings


def sync(source, repo):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise AgyError("invalid_repository")
    if source.stat().st_mode & 0o077:
        raise AgyError("oauth_source_requires_private_permissions")
    raw = source.read_text()
    oauth_document(raw)
    metadata = subprocess.run(['gh', 'api', f'repos/{repo}', '--jq', '.default_branch'],
                              capture_output=True, text=True, timeout=30)
    if metadata.returncode or not metadata.stdout.strip():
        raise AgyError('default_branch_unavailable')
    default_branch = metadata.stdout.strip()
    # Require the same branch boundary as the deployed inference workflow.
    response = subprocess.run(["gh", "api", f"repos/{repo}/environments/pr-review"],
                              capture_output=True, text=True, timeout=30)
    if response.returncode:
        raise AgyError("protected_environment_not_found")
    environment = json.loads(response.stdout)
    policy = environment.get("deployment_branch_policy") or {}
    branches = subprocess.run(["gh", "api", f"repos/{repo}/environments/pr-review/deployment-branch-policies"],
                              capture_output=True, text=True, timeout=30)
    if branches.returncode:
        raise AgyError("protected_environment_branch_policy_unavailable")
    policies = json.loads(branches.stdout).get("branch_policies", [])
    if not policy.get("custom_branch_policies") or len(policies) != 1 or (
        policies[0].get("name") != default_branch or policies[0].get("type") != "branch"
    ):
        raise AgyError("protected_environment_must_allow_only_default_branch")
    result = subprocess.run(["gh", "secret", "set", "AGY_OAUTH_JSON", "--repo", repo,
                             "--env", "pr-review"], input=raw, text=True,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise AgyError("encrypted_secret_update_failed")
    print("Updated AGY_OAUTH_JSON in the default-branch-only pr-review environment.")


def check():
    backend = json.loads(Path(__file__).with_name("backends.json").read_text())["backends"]["gemini-ai-pro"]
    raw, model, usage = run(backend, 'Native OAuth smoke test. Return only JSON: '
                            '{"summary":"OAuth smoke passed","limitations":[],"findings":[]}.')
    result = parse_findings(raw, {"files": []})
    if result["summary"] != "OAuth smoke passed" or result["findings"]:
        raise AgyError("agy_smoke_contract_failed")
    print(json.dumps({"status": "completed", "harness": "antigravity_packet",
                      "model": model, "native_refresh": "verified",
                      "refresh_token": "unchanged", "usage": usage}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser("sync")
    provision.add_argument("--repo", required=True)
    provision.add_argument("--source", type=Path, default=Path.home() / ".gemini/antigravity-cli/antigravity-oauth-token")
    commands.add_parser("check")
    args = parser.parse_args()
    try:
        if args.command == "sync":
            sync(args.source, args.repo)
        else:
            check()
    except (AgyError, ReviewError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("agy_auth_setup_failed")
        raise SystemExit(1) from None
