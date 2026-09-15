# Independent PR review

Portable GitHub Actions review with independent Grok and Gemini opinions, native
agy OAuth, evidence verification, and persistent English PR feedback.

A consumer repository keeps a small workflow and its project rules. This repository
owns the engine, reusable workflow, pinned official CLI, and provider-free tests.
The default setup uses GitHub-hosted Ubuntu runners; a permanently running Mac mini
is not required. The mini can provision or renew the account's interactive login.

## What appears on the PR

- One updated English summary: reviewed SHA, actual reviewer status, verified open
  findings, uncertainty, budget and run link.
- P1/P2 inline comments only after a fresh cross-family verification pass and a
  demonstrated changed-line anchor. Ambiguous anchors remain in the summary.
- Stable finding IDs and rechecks of unresolved issues. Only this harness's own
  threads are resolved, after current source demonstrates a fix.
- `/review`, `/review full`, `/review pause`, `/review resume`, and
  `/review verify <finding-id>` for users with current write/maintain/admin access.

The harness never approves, requests changes, merges, edits code, or executes PR
code. A completed run means the bounded review completed, not that the PR is safe
to merge. Missing context and incomplete provider runs remain visible.

Both models share one runner and run concurrently. Grok uses streaming with bounded
deadlines; native CLI installation overlaps its request. Literal diff quotes and
per-candidate rejection keep valid findings without presenting a partial review as
clean. See [runtime and billing](docs/operations.md#runner-time-and-billing).

## Connect a repository

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
   encrypted Secrets: `GROK_API_KEY`, `AGY_OAUTH_JSON`, and a unique random
   `REVIEW_STATE_KEY` of at least 32 characters. Keep keys out of YAML, git, comments,
   and artifacts. See [OAuth and credentials](docs/authentication.md).
4. Set repository Variables `GROK_BASE_URL`, `GROK_MODEL`, and `AGY_MODEL`.
   The qualified Cortex deployment uses `grok-4.6` and `gemini-3.8-flash-medium`.
   Set `GROK_EFFORT=xhigh`, `GROK_CONTEXT_WINDOW=500000`,
   `GEMINI_EFFORT=medium`, and `GEMINI_CONTEXT_WINDOW=1048576`.
   Gemini's medium is its next-to-highest level (low/medium/high). Grok 4.6
   supports xhigh but has a 500k ceiling; it cannot be configured to 1M.
   Your gateway/account must actually support your selected models.
5. Set `AUTO_REVIEW_ENABLED=true` and `AUTO_REVIEW_PUBLISH=true`. The latter enables
   the durable summary that records budget reservations before inference. Use a
   manual `dry-run` first; it collects context without model calls or PR writes.
6. Open an eligible PR or dispatch `review` from the default branch. Fork PRs,
   drafts, closed PRs and non-default targets cannot invoke the providers.

Reusable jobs load Secrets directly from the **consumer Environment**. No provider
credential belongs in this engine repository. Keep the example's explicit secret
name mappings: Environment binding alone does not reliably expose Secrets in a
reusable workflow. No blanket `secrets: inherit` or repository-level copies are needed. The caller must grant `contents: read` and `pull-requests: write`; the
inference job narrows permissions and does not receive a GitHub token.

[Architecture and trust boundaries](docs/architecture.md) ·
[Operations and troubleshooting](docs/operations.md) ·
[Design evidence and limitations](docs/design.md)

## Context and effort

The model window and input retrieval budget are separate. The collector can now
assemble up to 4,000,000 serialized characters, including up to 2,500,000 related
source characters, 100 related files and 300 API reads. Each reviewer receives its
own bounded projection: 500,000 tokens for Grok and 1,048,576 for Gemini. Whole
patch hunks and requested verification evidence are preserved; missing context is
reported explicitly. A smaller Grok window does not cap Gemini's input.

Input tokens are estimated from UTF-8 bytes divided by three, with ten percent of
the configured window reserved for overhead/output (at least 16k). This is not an
exact provider tokenizer and does not certify maximum-window accuracy. Provider
context/truncation failures remain failures. No fabricated context-window parameter
is sent to the API: model selection determines provider capacity, while the engine
budgets what it sends. Effort is sent explicitly in each provider's supported form.

The default per-PR accounting ceiling is 8,000,000 tokens so a large-context review
and its verification can run. The independent hard limit remains eight reservations;
small PRs use their actual context and never receive padding to fill a window.

## Local development

Python 3.11+ and the standard library are sufficient; no provider credentials are
required for tests:

```bash
python3 -m unittest discover -s tests -v
python3 -m independent_review.cli validate-config --config examples/review.json --out /tmp/review-check
```

The installed `independent-review` command exposes the same phases. Action execution
runs the package directly from the pinned Action directory, without installing or
importing code from the PR. See [release procedure](docs/operations.md#releasing).

Source was extracted from owner-authored Cortex repository tooling. Product code,
private context, repository history and credentials are not included. No source
license has been selected yet; public visibility alone is not a general reuse grant.
