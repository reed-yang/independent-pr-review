# Operations

## Commands and normal runs

Events and comments run trusted default-branch workflow code. Commands are exact,
single-line forms; the harness checks current collaborator permission rather than
trusting author-association labels. It ignores bots, issue-only comments and command
suffixes. `/review verify <id>` validates a unique unresolved finding prefix and requests
a full review, prioritizing that finding within the verification cap; it is not a cheaper single-issue run.

`/review` reuses an identical successful snapshot. `/review full` forces new opinions
but respects all budgets. `/review full`, `/review verify` and a `full` dispatch also
make lanes with a [generation policy](../README.md#generation-policy) generate,
unless that policy sets `"on_full_review": false`. `/review pause` prevents future
reservations; a model already running is not interrupted by this command.
`/review resume` unpauses and reviews the latest eligible snapshot. Close/draft
events mark existing state ineligible without provider calls. No command enables
fork review or overrides the configured default branch, caps or trusted policy.
A `dry-run` dispatch prints the changed-file count, statistics, omissions and lane
plan in the prepare log without creating a snapshot, reserving budget, calling a
model or writing to the PR.

The summary is advisory. A provider/verification failure is partial or failed,
never an empty clean result; a lane skipped by its generation policy is not a
failure. The publish job fails on an incomplete review after writing the truthful
summary. A cancelled workflow can retain an in-progress reservation; the next
eligible run remains charged for it. Read its run link. Known transport failures
include a short English explanation in the PR summary; raw provider error text is
never used as comment prose. Reusing a successful snapshot clears transient failure
notes without refunding consumed run budget.

To judge how much a reviewer read, use the summary's "Repository read" column and
each lane's `trace` in `result.json` (tool calls, files read, up to 100 redacted
commands, truncated outputs and `stopped_reason`); `result.md` lists the first five
commands and per-lane usage.

## Failure recovery

| Symptom | Action |
| --- | --- |
| `http_<status>`, no upstream accounts, quota errors on the Grok lane | Fix the selected provider group/account and retry within the budget; no fallback is automatic. |
| `provider_connect_timeout`, `provider_headers_timeout`, `provider_idle_timeout`, `provider_deadline_exceeded` | Inspect redacted stage, timing, byte and event counters in result.json. Each Grok turn has a 20 s connect and 120 s idle limit under the lane's 3,600 s total; keepalives do not extend the total. A tool turn cut off at the 600 s final-answer reserve still gets a final answer turn (`interrupted_turns`). No automatic retry spends another full call. |
| `provider_progress_timeout` | Occurs only when a custom backends file sets `progress_timeout_seconds`: no reasoning or text delta arrived within it. It is disabled by default because silent reasoning can outlast visible progress. This is an incomplete review, not proof of an upstream crash. |
| `provider_stream_error` with `incomplete_reason` | The upstream ended the response as incomplete. Inspect the allowlisted reason and numeric `provider_usage`; partial text is never accepted. `max_output_tokens` semantics differ across providers and must not be assumed to bound internal reasoning. |
| `provider_dns_error`, `provider_tls_error`, `provider_connection_error` | Check the configured gateway/network. Diagnostics never contain raw exceptions, headers, response bodies or credentials. |
| `prompt_exceeds_configured_context_budget` | The Grok prompt (rules, instructions and brief) does not fit the lane's input budget, so no model call was made and the lane does not verify in this run. Lower `brief_chars` or `description_chars`, or shorten the rules. |
| Grok `stopped_reason` other than `final_answer` | Not a failure: a turn, tool call, output, context or time limit was reached and the lane answered from what it had read. Check its limitations. `tool_budget_exhausted` (tool calls after tools were disabled) and `empty_model_output` (no final text) fail the lane. |
| `codex_timeout`, `proxy_budget_exhausted` | The Codex run reached 3,600 s, or the proxy reached its request, token or time limit (`stopped_reason` in diagnostics). Diagnostics keep elapsed time and request, token, event and command counts. |
| `codex_turn_failed`, `codex_process_failed`, `codex_no_final_message` | Codex ended without a final answer. Diagnostics keep counts only, not upstream errors; check the gateway account's access to `GPT_MODEL` and retry within the budget. |
| Codex install or sandbox qualification step failed | The GPT job stopped before any model call, so publication reports `lane_result_missing` or `verification_result_missing`. Read the qualification table in the job log; do not bypass a failed check. `codex_not_found` or `codex_version_mismatch` instead means `CODEX_BIN` is not the pinned package. |
| `lane_result_missing`, `verification_result_missing` | That lane's job wrote no result: setup failure, job timeout, `process_hardening_failed`, `snapshot_identity_mismatch`, `publishing_credentials_in_inference_environment` or `provider_configuration_changed_after_reservation` (a lane Variable changed after prepare). Read the job log and rerun. |
| `verification_skipped_after_provider_failure` | The lane's generation failed with a configuration or authorization error, so it did not verify the other lane's candidates; they stay uncertain and the run is partial. Fix the provider problem and run `/review`. |
| `credential_in_output`, `credential_in_review_output` | A known credential value appeared in model output, a trace or a lane result. The lane failed closed and nothing containing the value was written. Rotate the affected key. |
| `input_end_nonce_missing`, `input_end_nonce_mismatch` | The reviewer did not echo the per-call end-of-input nonce, so it may not have seen its whole input. Evidence-checked results are kept, but the lane or verification batch stays partial and no baseline advances. Repeated failures on small inputs mean the model ignored the output contract. |
| Rejected candidate or verification evidence | Inspect its redacted, bounded quote preview and reason in result.json. Invalid generation candidates are excluded; invalid verification decisions become uncertain. Valid siblings are retained, but the review remains partial and cannot advance a successful baseline. Quotes must be contiguous and exact, preserving line breaks. |
| Missing state/provider Secret in reusable jobs, `backend_not_configured` | Preserve explicit caller secret name mappings and the callee declarations; Environment binding alone can yield empty values. |
| State signature mismatch | Restore the correct state key. Do not silently delete/reset state to bypass budgets. |
| `unsupported_reasoning_effort`, `configured_context_exceeds_model_capacity`, `invalid_context_window` | Use an effort the harness accepts (`low` to `xhigh` for Grok; `low` to `max` or `ultra` for Codex) and no more than the model's window; see [model capability evidence](design.md#model-capability-evidence). |
| `review_backend_disabled`, `unsupported_harness` | A custom backends file selects the retired agy backend or a non-tool harness. See [the retired agy lane](authentication.md#retired-agy-lane). |
| Run or token budget exhausted | A trusted maintainer can review usage and raise the default-branch config cap in a reviewed change. Commands cannot raise it. |
| `skipped_by_policy` | Every lane with changes is below its generation policy, so no reviewer ran for the current head. Use `/review full` or a configured label to review it. |
| Stale head/base at publication | Review the current snapshot; old results cannot be attached to the new SHA. |
| Inline publication failed | Retry `/review`. Successful cached evidence can recover an already posted comment by its owned marker. |
| Publication warning about verified-fixed threads | GitHub refused to resolve an owned thread; the summary names the error type and a later run retries. Check that the caller still grants `pull-requests: write`. |
| Missing or ambiguous anchor | Read the summary/report; the harness intentionally does not guess an inline location. |

Large ledgers fail closed at the comment/state size limit. There is no external
state database, organization-wide budget service, persistent semantic index or
automatic incident routing.

## Runner time and billing

Standard GitHub-hosted runner compute in public repositories is free; the included
2,000 minutes on GitHub Free applies to private repository usage. Artifact storage
has separate limits. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
and [per-job rounding](https://docs.github.com/en/billing/reference/actions-runner-pricing).
For private consumers, optimize summed job time, not just end-to-end wall time.

A review uses six jobs: prepare, one generation and one verification job per lane,
and publish, each on its own runner and rounded separately. Verification starts
after both generation jobs finish, so wall time is about the slower generation plus
the slower verification. Job ceilings are 10 minutes for prepare, 75 for each
generation and verification job (each provider call is limited to 3,600 s), and 8
for publish; successful calls return immediately. Runs that reuse an identical
snapshot or have nothing to generate end in prepare. No Python dependencies,
consumer environment, product packages or repository tests are installed. Each
inference job downloads the handoff artifact and extracts the snapshot. Both GPT
jobs also download the pinned Codex package and run the sandbox qualification, even
when generation is skipped by policy or the verification batch is empty.

`result.md` reports each lane's input, cached input, output, reasoning and total
tokens and request count. Each stateless Grok turn resends the earlier turns, so its
input tokens grow with the number of turns.

## Upgrading from v0.3

Updating only the workflow pin is not enough:

1. In `review.json`, remove `context` and the `packet_chars`, `context_chars` and
   `max_context_files` limits (they fail with `invalid_review_limits`); use
   `brief_chars` and `description_chars` if the defaults do not fit.
2. Raise `max_tokens_per_pr` to at least 17,000,000 or remove it for the 60,000,000
   default. One two-lane run reserves 17M (Grok 4M + 2M, GPT 8M + 3M); the v0.3
   example's 8M makes every run stop with `review_token_budget_exhausted`.
3. Add the `GPT_API_KEY` Secret to the Environment and to the caller's `secrets:`
   mapping, and set `GPT_BASE_URL` and `GPT_MODEL` (optionally `GPT_EFFORT`,
   `GPT_VERIFY_EFFORT`). `AGY_OAUTH_JSON`, `GEMINI_API_KEY` and the `AGY_*`/`GEMINI_*`
   Variables are no longer used.
4. Optionally add a `generation` policy for GPT and the `labeled` event.
5. Run `validate-config` with the new pin before merging, then `/review full` on an
   eligible PR.

## Releasing

1. Run provider-free tests, config validation, `python3 scripts/pin_release.py --check`
   and actionlint. `--check` also requires `independent_review.__version__` and the
   `pyproject.toml` version to match. Review source/artifact inclusion and the Codex
   and agy release hashes. Commit engine/action changes as commit A.
2. Run `python3 scripts/pin_release.py --engine FULL_COMMIT_A_SHA` to set the four
   engine pins of the reusable workflow (prepare, generate, verify and publish) and
   commit them as commit B. The engine commit does not reference itself.
3. Validate the cross-repository workflow with an eligible consumer PR. Publish a
   version tag/release at B containing both pins and actual validation evidence.
4. Consumers update their workflow to B in a reviewed PR. They can roll back by
   restoring the prior immutable SHA; mutable tags are not used for execution.

No workflow watches Codex releases. To move the pin, update the version, tag and
each platform's URL and SHA-256 in `independent_review/codex-release.json` from the
official `openai/codex` release, review its changelog for configuration keys,
`codex exec` flags, JSONL events and sandbox behaviour (the runner uses
`--strict-config`), and let CI run the sandbox qualification. The runner rejects any
other Codex version.

The `Official agy release watch` workflow still runs daily so the retired agy lane
can be re-enabled on a current pin. For a newer version than
`independent_review/agy-release.json`, it downloads both official archives, verifies
their SHA-512, statically checks that the linux binary still sets the qualified
64000-token `MaxTokensPerUserInputStep` (a miss fails with
`native_step_cap_unqualified`), runs the provider-free checks and creates or updates
a same-repository `chore/agy-<version>` PR; a closed PR for that version is respected
until reopened manually. Allow GitHub Actions to create pull requests in the
repository settings. PRs created with `GITHUB_TOKEN` trigger no other workflows, so
dispatch `Harness checks` on the branch when CI on its head is needed. Dependabot
proposes external Action SHA updates weekly; the engine self-pin is excluded.

Engine CI has no provider Secrets. It runs the tests, config validation and pin
check, and the Codex sandbox qualification, on both `ubuntu-latest` and
`ubuntu-26.04` during the runner migration. Consumer runs validate actual gateway
access. Protect release tags and default-branch workflow/config changes according
to the collaboration model of each project. Do not install the review as a required
merge check until its quota, reliability and noise are understood for that project.
