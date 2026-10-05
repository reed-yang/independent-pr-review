# Independent PR review contributor instructions

Read README.md, docs/ and the active logs/plan.md before changing boundaries.
This repository owns generic review orchestration, lane harnesses and reusable
Actions. Consumer repositories own domain policies, provider settings and Secrets.

## Architecture

- context/snapshot: the PR brief, per-lane plan and an immutable two-commit git
  snapshot (head and merge base) read through git plumbing. Git never runs hooks,
  filters or credential helpers, and only the prepare fetch uses the network.
- prompts/service: one repository-reading prompt and strict schemas; per-lane
  generation, a deterministic cross-family verification plan and normalized
  outcomes. Reject unsupported candidates or decisions individually, keep valid
  siblings, and retain partial status without advancing a successful baseline.
- tool_loop/transport: the Grok lane's read-only tools over snapshot objects and a
  deadline-bounded stateless Responses loop. The engine executes every tool; the
  model executes nothing. Never cache partial lanes.
- codex_runner/credential_proxy/hardening: the pinned official Codex CLI in its
  read-only, no-network sandbox. PR code may execute only there. The provider key
  stays in the non-dumpable engine and its loopback proxy; Codex holds only a
  per-run proxy token that its shell environment policy hides from commands. Ship
  sandbox changes only with a passing `scripts/qualify_codex_sandbox.py`.
- agy_runner/agy_auth: retained for the retired agy lane, whose backend is disabled.
- state: authenticate per-PR state and reserve budget before inference.
  Completion estimates include reasoning; visible-output caps do not bound it.
- delivery: one owned summary with a machine-readable result block and verified
  inline threads; recheck eligibility, immutable identity and reservation before
  writing. Thread resolution failures are recorded, not fatal. No approval or merge.
- cli/action/workflow: prepare and publish hold the GitHub token and state key;
  each generate/verify job holds one lane's provider key and nothing else.
  Reusable workflow calls are pinned; do not execute consumer PR workflows.

Keep comments, documentation and commits in English. Preserve unrelated formatting.
Maintain the implementation plan and meaningful findings/progress in ignored logs.
Run focused provider-free tests and actionlint. Live publication needs an eligible
PR and explicit operator authorization.

Never print, commit, cache or upload provider keys, proxy tokens, native OAuth state,
raw CLI diagnostics or conversation databases. On macOS never export provider keys
to a process that starts Codex. Code-only job handoff artifacts must contain no
credentials and have short retention. Pin official binaries and external Actions.

The source was extracted from owner-authored cortex-research review tooling; no
product history or runtime data is imported. A source license has not been chosen.
