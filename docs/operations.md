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
| `provider_connect_timeout`, `provider_headers_timeout`, `provider_idle_timeout`, `provider_deadline_exceeded` | Inspect redacted stage, timing, byte and event counters in result.json. Connect defaults to 20s, response idle to 120s and total to 600s. Streaming keepalives do not extend the total deadline. No automatic retry spends another full call. |
| `provider_progress_timeout` | No nonempty reasoning or final-text delta arrived within an explicitly configured `progress_timeout_seconds`. Disabled by default: silent internal reasoning can outlast visible summaries. Keepalive comments and status-only events do not renew this optional budget. Compare `last_byte_seconds`, `last_progress_seconds`, `seconds_without_progress` and `keepalive_lines`. This is an incomplete review, not proof of an upstream crash or a clean result. JSON responses that ignore streaming retain the existing socket/total limits because they expose no incremental progress. |
| `provider_stream_error` with `incomplete_reason` | The upstream explicitly ended the response as incomplete. Inspect the allowlisted reason and numeric `provider_usage`, including `reasoning_tokens`; partial text is never accepted as a completed review. `max_output_tokens` semantics differ across providers and must not be assumed to bound internal reasoning. |
| `provider_dns_error`, `provider_tls_error`, `provider_connection_error` | Check the configured gateway/network. Diagnostics never contain raw exceptions, headers, response bodies or credentials. |
| Rejected candidate evidence | Inspect its redacted, bounded quote preview and reason in result.json. Literal contiguous diff-side quotes are accepted without diff markers. Other invalid candidates are excluded and the lane remains partial; valid siblings still receive verification. |
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

## Runner time and billing

Standard GitHub-hosted runner compute in public repositories is free; the included
2,000 minutes on GitHub Free applies to private repository usage. Artifact storage
has separate limits. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
and [per-job rounding](https://docs.github.com/en/billing/reference/actions-runner-pricing).
For private consumers, optimize summed job time, not just end-to-end wall time.

Both reviewers run concurrently on one Ubuntu runner, and verification batches
also run concurrently when both families are needed. No Python dependencies,
consumer environment, product packages or repository tests are installed. Native
agy is downloaded and checksum-verified in its own lane while Grok starts; the
same verified binary serves a subsequent verification call in that job. Native
OAuth/HOME state is never cached. Three credential-separated jobs are retained.

Source collection reads immutable tree sizes before downloading optional source
that cannot fit. On Cortex PR #13 this reduced API reads from 234 to 13 while
preserving byte-for-byte equivalent supplied files and context. Identical successful
snapshots skip source collection and model calls. A failed provider is not called
again for verification during the same run; affected candidates stay uncertain.
Failed attempts retain timing, stage and usage where available. Rejected candidates
never advance the successful baseline or become a clean cached opinion.

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


The default Grok adapter uses native `/responses` with `reasoning.effort`, rather
than relying on gateway conversion of Chat Completions. Custom compatible backends
can explicitly set `api` to `chat_completions` or `responses`; this is part of the
configuration identity, never an automatic fallback. Both use the same configured
base URL/key/model. Responses streams require a completed terminal response with
matching final text. Reports count reasoning events/characters without saving them.
The archived PR13 Chat stream connected in 0.37s, returned HTTP 200 at 5.03s and
received 305 events/101733 bytes, but hit its 600s deadline without recognized final
content. That proves a response-phase failure, not a connection or HTTP auth error;
its old counters cannot distinguish reasoning from protocol mismatch.

The native Responses replay also reached 600s: HTTP 200 at 3.91s, 30 recognized
reasoning events (333 characters), and no content events. The request was accepted
but upstream generation/scheduling did not yield final text within the deadline;
these counters do not reveal internal thinking throughput or the gateway queue.
Large-PR Grok completion therefore remains unqualified. Do not treat either timeout
as a clean review or silently lower effort/coverage to make acceptance pass.

Normal hosted acceptance on Cortex #18 completed with Grok at 28.35s and native
Gemini at 13.54s, producing one English summary. Prepare/review/publish used
13s/34s/7s (54s summed runner time). That packet had one changed file and five
related files, about 15k estimated input tokens; it is not a large-context benchmark.
