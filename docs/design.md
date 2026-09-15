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
It does not copy T3's product-specific review rules or approval policy.

The first release deliberately uses deterministic source retrieval and the already
qualified native agy harness. Organizations needing unrestricted repository tools,
untrusted-fork credentials, multi-repository account scheduling or semantic history
indexing need additional isolation/infrastructure before enabling those capabilities.


## Current model capability evidence

The v0.2 configuration uses Gemini 3.8 Flash Medium through native agy, and Grok
4.6 with xhigh effort. The operator explicitly accepted Grok's 500k ceiling while
requesting Gemini's 1M window. These are supported model capacities, not results of
a full-window benchmark:

- [Gemini 3.8 Flash model specification](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash):
  1,048,576 input tokens, 65,536 output tokens, low/medium/high thinking.
- [agy headless model and effort selection](https://antigravity.google/docs/cli/headless/):
  stable Flash variant slugs and the explicit `--effort` flag. The installed 1.2.2
  CLI's live model list includes `gemini-3.8-flash-medium`.
- [Grok 4.6 model specification](https://docs.x.ai/developers/models/grok-4.6)
  and [reasoning configuration](https://docs.x.ai/developers/model-capabilities/text/reasoning):
  500,000 context tokens and explicit xhigh support. A larger number in client
  configuration cannot increase that provider limit.
