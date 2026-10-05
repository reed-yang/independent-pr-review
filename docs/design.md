# Design evidence and limits

This implementation adapts observable review behavior from
[pingdotgg/t3code PR #8103](https://github.com/pingdotgg/t3code/pull/8103), inspected
at PR head `07c887d86` and base `09e8de9c`. The research snapshot had 261 commits,
207 files, 83 issue comments, 329 review submissions, 380 inline comments and 155
threads. These are activity counts, not measured bug-detection accuracy.

CodeRabbit's public interaction demonstrated contextual investigation and withdrawal
of a finding after contrary evidence/fixes. Macroscope's checked-in policies scoped
specialist agents by file type and gave them explicit review budgets. Comments also
exposed repeated-SHA skipping, explicit full review, incremental work and budget
skips. Resolved threads and a final skipped/budgeted check do not prove full coverage.

Relevant public references:

- [CodeRabbit finding](https://github.com/pingdotgg/t3code/pull/8103#discussion_r3846889688)
  and [follow-up withdrawal](https://github.com/pingdotgg/t3code/pull/8103#discussion_r3847253472).
- [Macroscope project agents](https://github.com/pingdotgg/t3code/tree/main/.macroscope/check-run-agents).
- [GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows)
  and [workflow configuration reuse](https://docs.github.com/en/actions/concepts/workflows-and-actions/reusing-workflow-configurations).
- [GitHub secure use reference](https://docs.github.com/en/actions/reference/security/secure-use).

Borrowed principles are persistent concise feedback, contextual evidence, an explicit
verification/withdrawal lifecycle, per-lane state, honest coverage, bounded review
cost, and project-owned policies. This repository does not reproduce private SaaS
harnesses, their semantic search/AST infrastructure, undisclosed prompts or accuracy.
It does not copy T3's product-specific review rules or approval policy. Untrusted
fork PRs, tools with write or network access, multi-repository account scheduling or
semantic history indexing need additional isolation or infrastructure.

## Why reviewers read the repository

Up to v0.3, each reviewer received a packet assembled by the engine: changed-file
diffs, whole current files, merge-base text and heuristically related source read
through the GitHub API, projected separately for each provider. A study of v0.3
reviews on Cortex PRs (October 2026) found problems that a larger packet could not fix:

- Two introduced defects were missed although both lanes had the relevant code in
  their packets: a validator that newly accepted an input it should refuse, and a
  parser change that refused input the merge base accepted. Nothing asked the
  reviewers to compare accepted inputs between merge base and head.
- Packet assembly admitted whole files, duplicated added files and tests before
  relevant production code, matched imports without path boundaries, never searched
  for callers, cut the description at 6,000 characters, and in incremental runs
  excluded unchanged PR files that called the changed code. Several runs lacked the
  production code a finding needed, while Grok used at most 35% of its input budget.
- The native agy CLI keeps only a prefix of an input step above 64,000 estimated
  tokens without failing (an earlier 505,018-byte prompt was cut near 192,000
  bytes), so the Gemini lane's input was capped at 60,000. Every wrong verification
  decision in the study came from that lane, including the dismissal of the one
  established defect, and it produced no confirmed finding.
- A correct dismissal of a never-published false positive was turned into
  uncertain, and summaries hid dismissed candidates and reviewer limitations, so
  readers could take thin coverage for a clean result.

v0.4 therefore lets each reviewer choose what to read: it sends a brief and an
immutable snapshot, adds the merge-base-versus-head acceptance comparison, caller
checks and description-claim checks to the prompt, sends the description up to
60,000 characters, lets verification dismiss unpublished findings, rechecks
dismissed findings whose file changed, and shows dismissals, observations,
limitations and per-lane read counts in the summary. The agy lane is retired until
agy supports Gemini 4 with repository tools. The GPT lane uses Codex's read-only
shell, so repository code can run inside its sandbox; this was accepted only
together with the key isolation and sandbox qualification described in
[architecture](architecture.md#gpt-lane-key-isolation). The end-of-input nonce
introduced for the agy truncation is kept for both lanes.

## Model capability evidence

The default efforts and context windows fit Grok 4.7 at `xhigh` and GPT-6.1 Sol at
`ultra`, the models the Cortex deployment selects. These are documented capacities,
not results of a full-window benchmark:

- [Grok 4.7 model specification](https://docs.x.ai/developers/models/grok-4.7):
  500,000 context tokens, reasoning effort `low`, `medium`, `high` or `xhigh`, and
  function calling and structured outputs. The Grok lane sends its function tools
  and the strict JSON schema of the final answer in the same Responses request. A
  larger configured window is rejected (`configured_context_exceeds_model_capacity`).
- [GPT-6.1 Sol model specification](https://developers.openai.com/api/docs/models/gpt-6.1-sol):
  1,050,000 context tokens, 128,000 output tokens, and `reasoning.effort` `low`,
  `medium`, `high`, `xhigh` or `max`. Prompts with more than 272K input tokens are
  priced at 2x input and cache rates and 1.5x output for the full request. The
  Codex 0.159.1 model catalog adds `ultra` for this model, described as maximum
  reasoning with automatic task delegation; Codex resolves it client-side, and the
  requests observed through the loopback proxy carried `reasoning.effort` `xhigh`.
  The engine does not budget the GPT lane's context; Codex manages it, and the proxy
  also forwards Codex's `/responses/compact` requests.

Long reasoning periods are normal at `xhigh`. On one large v0.3 packet (Cortex PR
#13), Grok 4.6 at `xhigh` completed in 1413.96 s with 81,669 reasoning tokens through
the gateway ([hosted run 34954973753](https://github.com/reed-yang/cortex-research/actions/runs/34954973753))
and in 1774.18 s with 101,070 reasoning tokens through native OAuth; the longest gap
without public progress was 1403.86 s despite keepalives. The Grok lane therefore
has a 3,600 s total deadline and no default progress watchdog. These completions do
not guarantee that every call finishes within 3,600 s.
