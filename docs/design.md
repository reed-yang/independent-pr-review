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
