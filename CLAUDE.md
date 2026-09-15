# Independent PR review contributor instructions

Read README.md, docs/ and the active logs/plan.md before changing boundaries.
This repository owns generic review orchestration, native adapters and reusable
Actions. Consumer repositories own domain policies, provider settings and Secrets.

## Architecture

- context: immutable PR data and bounded related-source collection; never execute
  PR code, hooks, package scripts or tools.
- core/agy_runner: bounded provider calls, English opinions and redacted failures.
  agy remains the official pinned CLI with consumer OAuth and disposable HOME.
- state: authenticate per-PR state and reserve budget before inference.
- service: per-lane baselines, candidate verification and normalized outcomes.
- delivery: one owned summary and verified inline threads; recheck eligibility,
  immutable identity and reservation before writing. No approval or merge action.
- cli/action/workflow: separate reservation, inference and publication credentials.
  Reusable workflow calls are pinned; do not execute consumer PR workflows.

Keep comments, documentation and commits in English. Preserve unrelated formatting.
Maintain the implementation plan and meaningful findings/progress in ignored logs.
Run focused provider-free tests and actionlint. Live publication needs an eligible
PR and explicit operator authorization; the current extraction task authorizes it.

Never print, commit, cache or upload native OAuth state, keys, raw CLI diagnostics
or conversation databases. Code-only job handoff artifacts must contain no
credentials and have short retention. Pin official binaries and external Actions.

The source was extracted from owner-authored cortex-research review tooling; no
product history or runtime data is imported. A source license has not been chosen.
