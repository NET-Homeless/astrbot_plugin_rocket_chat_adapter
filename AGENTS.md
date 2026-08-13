# AGENTS.md

## Project Scope

This repository is an AstrBot platform adapter for Rocket.Chat.
Work should preserve three core guarantees:

- Normal unencrypted rooms must keep working even if E2EE is misconfigured or temporarily broken.
- Rocket.Chat behavior should be aligned to official server/client behavior, not third-party guesses.
- New features should prefer modular changes over growing `rocketchat_adapter.py` into a catch-all file.

Detailed implementation notes live in:

- [docs/DEVELOPMENT_KNOWLEDGE_BASE.md](docs/DEVELOPMENT_KNOWLEDGE_BASE.md)

## Source Of Truth

When behavior is unclear, verify in this order:

1. Rocket.Chat official web client source in `Rocket.Chat`
2. Rocket.Chat official developer docs
3. AstrBot official repo / local framework code
4. Only then use third-party articles or model summaries

Do not treat the Electron shell repo as the primary source for chat behavior unless the issue is desktop-shell-specific.

## File Ownership

- `main.py`: plugin registration only
- `rocketchat_adapter.py`: platform lifecycle, config, caches, REST helpers, bridge orchestration
- `rocketchat_realtime.py`: DDP/WebSocket handshake, subscriptions, result dispatch, dynamic room subscribe
- `rocketchat_inbound.py`: inbound normalization, quote parsing, wake/mention detection, AstrBot event assembly
- `rocketchat_sender.py`: outbound text/quote/typing/send_by_session routing
- `rocketchat_event.py`: AstrBot `MessageChain` splitting and reply flow
- `rocketchat_e2ee.py`: E2EE protocol, room keys, encrypted message/media payloads
- `rocketchat_media.py`: media download/upload bridge and fallback behavior

When adding logic:

- Platform lifecycle, caches, and shared REST helpers go in `rocketchat_adapter.py`
- DDP handshake/subscription/result handling goes in `rocketchat_realtime.py`
- Inbound parsing and AstrBot event construction go in `rocketchat_inbound.py`
- Outbound text/quote/typing/send-by-session routing goes in `rocketchat_sender.py`
- E2EE details go in `rocketchat_e2ee.py`
- Message component sequencing goes in `rocketchat_event.py`
- Media download/upload/fallback stays in `rocketchat_media.py`

## Non-Negotiable Behavior

### E2EE

- E2EE only applies to Rocket.Chat direct messages (`d`) and private groups (`p`)
- Public channels (`c`) must continue to use the normal plaintext path
- E2EE failures must stay local to the affected encrypted room or message
- Room-key recovery must retry, but retry failures must not break plaintext rooms

### Media

- Remote image/audio/video should prefer download-to-local-then-upload
- Encrypted-room remote media download failures must degrade to an encrypted text link, not silent drop
- Keep `File` remote URL behavior simple unless there is a clear requirement to change it

### Typing

- Typing behavior should mirror official Rocket.Chat `user-activity` semantics
- Use bot `username`, not display name or userId
- `stream-notify-room` payload must include the fourth `extras` argument
- Typing is not one-shot: renew while the bot is still generating a reply

## Configuration Validation

- `_validate_config()` runs inside `__init__` and raises `ValueError` on invalid config.
- This matches the AstrBot framework convention: Slack, Mattermost, and LINE built-in adapters all validate config the same way.
- `PlatformManager.initialize()` catches the exception, logs it, and skips the adapter — other platforms are unaffected.
- When adding new config items, validate them in `_validate_config()` rather than failing at runtime.

## REST Authentication

- `_get_auth_headers()` returns `X-Auth-Token` / `X-User-Id` only when both credentials are present; returns `{}` otherwise. All REST calls must use this method — never construct auth headers inline.
- `_is_own_server_url()` compares `scheme + netloc` (not string prefix) to decide whether a URL belongs to the configured server. Always use it before attaching credentials to a URL.
- Do not attach auth query parameters to URLs that do not belong to the bot's own server.

## AstrBot-Specific Rules

- `enable_e2ee` must remain a boolean config item
- Do not add local-only UI hacks for password fields unless AstrBot officially supports that config type
- `RocketChatMessageEvent.send()` must still call `await super().send(message)`
- AstrBot admin identity for Rocket.Chat should use `userId`, not username

## Documentation Discipline

When behavior changes, sync these files if relevant:

- `AGENTS.md`
- `README.md`
- `metadata.yaml`
- `docs/DEVELOPMENT_KNOWLEDGE_BASE.md`

Do not leave README support claims behind the real implementation.

## Verification Minimum

Before claiming a change is done:

- Run `python -m py_compile rocketchat_adapter.py rocketchat_realtime.py rocketchat_inbound.py rocketchat_sender.py rocketchat_e2ee.py rocketchat_event.py rocketchat_media.py main.py`
- If touching DDP behavior, verify method payload shape and result/error logging
- If touching E2EE, check that plaintext rooms are unaffected by failure paths

## Pre-commit Quality Gate

This repository uses lefthook to enforce code quality before every commit.

After cloning or pulling changes:

```bash
# Install lefthook (one-time)
brew install lefthook        # macOS
# or
curl -sSfL https://raw.githubusercontent.com/evilmartians/lefthook/master/install.sh | sh

# Activate hooks in this repo
lefthook install

# Ensure dev tools are available
pip install -r requirements-dev.txt
```

On `git commit`, lefthook will automatically run:
- `ruff check --fix` (lint + auto-fix, re-stages fixed files)
- `ruff format` (formatting, re-stages)
- `pyright` (type checking on staged files)
- `unittest` (full mock test suite)

If any step fails, the commit is blocked.

Local commit hooks are convenience checks and can be bypassed with Git client flags. The non-bypassable gate is GitHub branch protection requiring the `quality` status check from `.github/workflows/ci.yml` before merging to `main`.

CI (`.github/workflows/ci.yml`) runs lint, format check, pyright, py_compile, and the full test suite on Pull Requests and pushes to `main` / `master`. Release automation (`.github/workflows/release.yml`) is for publishing and should not be treated as the only quality gate.

Dependabot PRs are auto-merged by `.github/workflows/dependabot-auto-merge.yml` once the `quality` check passes: pip patch/minor updates and all GitHub Actions updates merge automatically; pip major updates stay open for manual review.
