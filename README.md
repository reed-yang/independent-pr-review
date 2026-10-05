# Independent PR review

Portable GitHub Actions review with independent Grok and GPT opinions,
cross-family verification of every candidate, and persistent English PR feedback.

A consumer repository keeps a small workflow and its project rules. This repository
owns the engine, reusable workflow, pinned official Codex CLI, and provider-free
tests. Reviews run on GitHub-hosted Ubuntu runners.

Reviewers do not receive a pre-built packet of source text. Each receives a brief
(PR metadata, the description as the author's claims, the changed-file list and
bounded patches) and reads the repository itself from an immutable snapshot of the
PR head and merge base: Grok through read-only tools that the engine implements,
GPT through the official Codex CLI in its read-only, no-network sandbox. Each
family then verifies the other family's candidates. See
[architecture and trust boundaries](docs/architecture.md).

## What appears on the PR

- One updated English summary: status, reviewed head SHA and run link; counts of
  verified open, uncertain, verified fixed and dismissed findings; a reviewer table
  with model, effort, status, scope and how much of the repository each lane read;
  candidates dismissed by verification with the verifier's reason; reviewer
  observations and limitations; budget use; and a machine-readable
  `<!-- independent-pr-review-result:v1 {...} -->` block for agents.
- P1/P2 inline comments only after a fresh cross-family verification call confirmed
  them and a unique changed-line anchor exists. Ambiguous anchors remain in the summary.
- Stable finding IDs and rechecks of unresolved issues. Only this harness's own
  threads are resolved, after verification cites a fix in current source. A thread
  that GitHub fails to resolve is shown as a warning and retried by a later run.
- `/review`, `/review full`, `/review pause`, `/review resume`, and
  `/review verify <finding-id>` for users with current write/maintain/admin access.

The harness never approves, requests changes, merges or edits code. Repository code
can run only inside the GPT lane's read-only, no-network Codex sandbox; the engine
itself runs no tests or PR code. A completed run means the bounded review completed,
not that the PR is safe to merge. Skipped lanes, failed providers and reviewer
limitations remain visible. Each lane generates and verifies in its own jobs, which
hold only that lane's provider key. See
[runner time and billing](docs/operations.md#runner-time-and-billing).

## Connect a repository

Upgrading from v0.3: follow [these steps](docs/operations.md#upgrading-from-v03);
changing only the workflow pin stops every review.

1. Copy [examples/auto-review.yml](examples/auto-review.yml) to
   `.github/workflows/auto-review.yml`, replacing `RELEASE_COMMIT_SHA` with a reviewed
   immutable release SHA. The [release notes](https://github.com/reed-yang/independent-pr-review/releases)
   give the reusable workflow and composite Action pins separately.
2. Copy [examples/review.json](examples/review.json) to `.github/review.json` and
   [examples/rules.md](examples/rules.md) to `.github/review-rules.md`. Change the
   config's `rules` entry to `.github/review-rules.md`. Scope additional policies
   using `{ "id": "api", "paths": ["src/api/*"], "file": ".github/api-review.md" }`.
   Config and rules are loaded from the trusted default-branch workflow revision.
3. Create a `pr-review` Environment restricted to your default branch. Add its
   encrypted Secrets: `GROK_API_KEY`, `GPT_API_KEY`, and a unique random
   `REVIEW_STATE_KEY` of at least 32 characters. Keep keys out of YAML, git, comments,
   and artifacts. See [credentials](docs/authentication.md).
4. Set repository Variables `GROK_BASE_URL`, `GROK_MODEL`, `GPT_BASE_URL` and
   `GPT_MODEL`. Each base URL is an HTTPS endpoint of the Responses API; the engine
   appends `/responses`. Optional Variables override the defaults: `GROK_EFFORT`
   (`xhigh`), `GROK_CONTEXT_WINDOW` (`500000`), `GPT_EFFORT` (`ultra`),
   `GPT_VERIFY_EFFORT` (`xhigh`) and `GPT_CONTEXT_WINDOW` (`1050000`). The Cortex
   deployment uses `grok-4.7` at `xhigh` and `gpt-6.1-sol` at `ultra`. Your gateway
   and key must actually serve the selected models; see
   [model capability evidence](docs/design.md#model-capability-evidence).
5. Set `AUTO_REVIEW_ENABLED=true` and `AUTO_REVIEW_PUBLISH=true`. The latter enables
   the durable summary that records budget reservations before inference. Use a
   manual `dry-run` first; it builds the brief and lane plan without a snapshot,
   model calls or PR writes.
6. Open an eligible PR or dispatch `review` from the default branch. Fork PRs,
   drafts, closed PRs and non-default targets cannot invoke the providers.

Reusable jobs load Secrets directly from the **consumer Environment**. No provider
credential belongs in this engine repository. Keep the example's explicit secret
name mappings: Environment binding alone does not reliably expose Secrets in a
reusable workflow. No blanket `secrets: inherit` or repository-level copies are
needed. The caller must grant `contents: read` and `pull-requests: write`; the
generation and verification jobs run with no permissions and receive no GitHub token.

### Generation policy

By default both lanes generate opinions whenever the PR has changes their previous
successful review does not cover. An optional `generation` entry in `review.json`
limits generation per lane id:

```json
"generation": {
  "GPT": {"min_changed_lines": 400, "min_changed_files": 10,
          "labels": ["deep-review"], "events": ["ready_for_review"]}
}
```

The values are only an example. A lane with a policy generates when any of these
holds: the run comes from `/review full`, `/review verify <id>` or a `full` dispatch
(unless `"on_full_review": false`); the `pull_request_target` action is listed in
`events`; the PR has a listed label; or the whole PR reaches a configured threshold
(added plus deleted lines, or changed files). Otherwise the lane is reported as
skipped with `below_generation_threshold`, still verifies the other lane's
candidates, and keeps its previous baseline. If every lane with changes is skipped,
the summary says `skipped_by_policy` instead of presenting a clean review. Add `labeled` to the caller's
`pull_request_target` types if adding a label should start a run.

[Architecture and trust boundaries](docs/architecture.md) ·
[Operations and troubleshooting](docs/operations.md) ·
[Design evidence and limitations](docs/design.md)

## Local development

Python 3.11+ and the standard library are sufficient; no provider credentials are
required for tests:

```bash
python3 -m unittest discover -s tests -v
python3 -m independent_review.cli validate-config --config examples/review.json --out /tmp/review-check
```

`scripts/replay.py` runs one historical PR from a local clone through generation,
verification and combination without GitHub or publication; `--fake` uses a
scripted provider-free runner. `scripts/qualify_codex_sandbox.py` checks the pinned
Codex sandbox on the current host without a provider. The installed
`independent-review` command exposes the same phases as the Action. Action execution
runs the package directly from the pinned Action directory, without installing or
importing code from the PR. See [release procedure](docs/operations.md#releasing).

Source was extracted from owner-authored Cortex repository tooling. Product code,
private context, repository history and credentials are not included. No source
license has been selected yet; public visibility alone is not a general reuse grant.
