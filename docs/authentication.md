# Native agy OAuth and credentials

The default Gemini lane runs the official Antigravity CLI (`agy`) 1.2.2 using its
native consumer Google OAuth route. The binary and archive SHA-512 are pinned in
`independent_review/agy-release.json`. The harness does not implement a substitute
OAuth client, scrape browser cookies, or exchange consumer tokens through a proxy.

## Provision from a trusted interactive machine

In a reviewed checkout of this repository:

```bash
python3 -m independent_review.install_agy --out "$HOME/.local/share/independent-pr-review/agy"
AGY_CLI_DISABLE_AUTO_UPDATE=true "$HOME/.local/share/independent-pr-review/agy"
```

Choose the personal Google account and finish browser consent, then exit the CLI.
Its native OAuth document is normally at
`~/.gemini/antigravity-cli/antigravity-oauth-token`. Keep the source file private
(mode `0600`). After creating the consumer's default-branch-only `pr-review`
Environment, sync it without displaying it:

```bash
python3 -m independent_review.agy_auth sync --repo OWNER/REPO
```

The helper validates the native consumer document and protected Environment before
sending it to `gh secret set` over stdin. It does not print token contents or persist
another plaintext copy. Store API keys using GitHub's encrypted Environment Secrets
UI or a local password manager/secret tool feeding `gh secret set` via stdin.
Never place credentials in command-line arguments or repository files.

Generate a separate state key directly into the consumer's Environment:

```bash
python3 - <<'PY'
import secrets, subprocess
subprocess.run(['gh', 'secret', 'set', 'REVIEW_STATE_KEY', '--repo', 'OWNER/REPO',
                '--env', 'pr-review'], input=secrets.token_hex(32), text=True, check=True)
PY
```

## What each hosted job proves

Each call creates a disposable private HOME and copies the OAuth document there.
Only the copy's access token and expiry are deliberately made stale. The official
CLI renews it using its own OAuth logic. Success requires a fresh access token,
future expiry and unchanged refresh token. Rotation or revocation fails closed and
requires reprovisioning from the interactive machine; "persistent OAuth" does not
mean permanent authorization.

The CLI agent excludes default components, tools, MCP, plugins, skills and ambient
customizations. Its child environment excludes GitHub/API keys and auth-route
redirects. The runner validates agent/model selection before submitting input,
allows one response turn, rejects tool events/denials/truncation, enforces output
and wall-clock bounds, and removes its private state. It does not enable additional
G1 credits. Google/account eligibility and quotas remain external dependencies.

For a smoke check, call the pinned composite Action with `phase: auth-check`, on a
fresh hosted runner in the protected Environment, passing only `AGY_OAUTH_JSON`
and `GEMINI_MODEL`. Serialize it with `agy-subscription-account` like the review job.

## Optional Gemini gateway

A consumer can copy `independent_review/backends.json` into a trusted config file,
select `gemini-gateway` for the Gemini slot, and set `backends` to that path. This
route requires `GEMINI_API_KEY`, `GEMINI_BASE_URL` and `GEMINI_MODEL` (leave `AGY_MODEL`
unset). It is an explicit alternative; provider errors never silently change the
billing/authentication route. A GPT-only proxy key does not imply Grok or Gemini
upstream access. Provider keys stay in the consumer repository's Environment.
