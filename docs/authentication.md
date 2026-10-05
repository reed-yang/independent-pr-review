# Credentials

A consumer stores three encrypted Secrets in its `pr-review` Environment, which must
be restricted to the default branch:

| Secret | Jobs that receive it | Purpose |
| --- | --- | --- |
| `GROK_API_KEY` | Grok generation and verification | Responses API key for `GROK_BASE_URL` |
| `GPT_API_KEY` | GPT generation and verification | Responses API key for `GPT_BASE_URL`, held by the credential proxy |
| `REVIEW_STATE_KEY` | Prepare and publish | HMAC key of the signed PR state, at least 32 characters |

Each provider key must reach the configured model through its base URL. A gateway
key whose group serves only GPT models is sufficient for the GPT lane and gives the
Grok lane no access. Provider errors never switch to another key or route. The Grok
key stays inside the engine process, whose git subprocesses receive an environment
without it; the GPT key is held by a loopback proxy and never reaches Codex (see
[GPT lane key isolation](architecture.md#gpt-lane-key-isolation)).

Store keys with GitHub's encrypted Environment Secrets UI, or feed
`gh secret set NAME --repo OWNER/REPO --env pr-review` from a password manager
through stdin. Never place credentials in command-line arguments or repository
files. Generate the state key directly into the Environment:

```bash
python3 - <<'PY'
import secrets, subprocess
subprocess.run(['gh', 'secret', 'set', 'REVIEW_STATE_KEY', '--repo', 'OWNER/REPO',
                '--env', 'pr-review'], input=secrets.token_hex(32), text=True, check=True)
PY
```

Provider keys can be replaced at any time; they are not part of the configuration
identity. Keep the state key stable while existing PRs are active: rotation requires
a planned state migration, and there is no automatic rotation or multi-key reader.

## Local replay keys

`scripts/replay.py` reads provider keys from the macOS Keychain into its own process
with `--keychain-service SERVICE`. The account is the variable name in lower case
with dashes (`grok-api-key`, `gpt-api-key`); `security add-generic-password -s
SERVICE -a gpt-api-key -w` prompts for the value. Because a command in Codex's macOS
sandbox can read the launch environment of same-user processes, the script refuses
to start when a key variable is exported and the Codex lane is selected.

## Retired agy lane

The `gemini-ai-pro` backend ran the official Antigravity CLI (`agy`) with native
consumer Google OAuth. It is disabled with `disabled_reason`
`retired_until_agy_supports_gemini_4_with_repository_tools`. Its runner, the pinned
installer and `independent_review/agy-release.json`, the `agy_auth` helper, the
Action's `auth-check` phase and the [daily release watch](operations.md#releasing)
remain. Re-enabling it needs a repository-reading agy harness: configuration accepts
only the `responses_tools` and `codex_cli` harnesses and rejects the retained
`antigravity_packet` harness (`unsupported_harness`). The reusable workflow still
declares `AGY_OAUTH_JSON` and `GEMINI_API_KEY` but passes them to no job.

Provisioning, when the lane returns: on a trusted interactive machine, in a reviewed
checkout, install the pinned CLI with
`python3 -m independent_review.install_agy --out "$HOME/.local/share/independent-pr-review/agy"`,
run it with `AGY_CLI_DISABLE_AUTO_UPDATE=true`, finish the Google login, then run
`python3 -m independent_review.agy_auth sync --repo OWNER/REPO`. The helper checks
that the OAuth file is private and that `pr-review` allows only the default branch,
then stores `AGY_OAUTH_JSON` through stdin without printing it. Each hosted call
refreshes a disposable copy with the CLI's own OAuth logic; a rotated or revoked
refresh token fails closed and requires reprovisioning. The `auth-check` phase runs
this refresh on a fresh runner.
