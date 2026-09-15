# Operations

## Commands and normal runs

Events and comments run trusted default-branch workflow code. Commands are exact,
single-line forms; the harness checks current collaborator permission rather than
trusting author-association labels. It ignores bots, issue-only comments and command
suffixes. `/review verify <id>` validates a unique unresolved finding prefix and requests
a full review, prioritizing that finding within the verification cap; it is not a cheaper single-issue run.

`/review` reuses an identical successful snapshot. `/review full` forces new opinions
but respects all budgets. `/review pause` prevents future reservations; a model
already running is not interrupted by this command. `/review resume` unpauses and
reviews the latest eligible snapshot. Close/draft events mark existing state
ineligible without provider calls. No command enables fork review or overrides the
configured default branch, caps or trusted policy.

The summary is advisory. A provider/verification failure is partial or failed,
never an empty clean result. Workflow publication fails on incomplete review after
writing the truthful summary. A cancelled workflow can retain an in-progress
reservation; the next eligible run remains charged for it. Read its run link.

## Failure recovery

| Symptom | Action |
| --- | --- |
| `http_503`, no upstream accounts, quota errors | Fix the selected provider group/account and retry within the budget; no fallback is automatic. |
| `agy_authentication_required`, refresh rotation | Log in interactively and resync encrypted OAuth; do not paste tokens in comments. |
| Missing state/provider Secret in reusable jobs | Preserve explicit caller secret name mappings and the callee declarations; Environment binding alone can yield empty values. |
| State signature mismatch | Restore the correct state key. Do not silently delete/reset state to bypass budgets. |
| Context/effort configuration rejected | Use the actual model limit and supported effort. Grok 4.6 is 500k/xhigh; Gemini 3.8 Flash supports 1M and low/medium/high. |
| Run or token budget exhausted | A trusted maintainer can review usage and raise the default-branch config cap in a reviewed change. Commands cannot raise it. |
| Stale head/base at publication | Review the current snapshot; old results cannot be attached to the new SHA. |
| Inline publication failed | Retry `/review`. Successful cached evidence can recover an already posted comment by its owned marker. |
| Missing or ambiguous anchor | Read the summary/report; the harness intentionally does not guess an inline location. |

Keep the state key stable while existing PRs are active. Rotation requires a planned
state migration; this initial version has no automatic rotation/multi-key reader.
Large ledgers fail closed at the comment/state size limit. There is no external
state database, organization-wide budget service, persistent semantic index,
automatic incident routing, or automatic account refresh-token propagation.

## Releasing

1. Run provider-free tests, config validation and actionlint. Review source/artifact
   inclusion and native release hashes. Commit engine/action changes as commit A.
2. Run `python3 scripts/pin_release.py --engine FULL_COMMIT_A_SHA` and commit the
   reusable workflow pin as commit B. The engine commit does not reference itself.
3. Validate the cross-repository workflow with an eligible consumer PR. Publish a
   version tag/release at B containing both pins and actual validation evidence.
4. Consumers update their workflow to B in a reviewed PR. They can roll back by
   restoring the prior immutable SHA; mutable tags are not used for execution.

Engine CI has no provider Secrets. Consumer runs validate actual gateway/OAuth
access. Protect release tags and default-branch workflow/config changes according
to the collaboration model of each project. Do not install the review as a required
merge check until its quota, reliability and noise are understood for that project.
