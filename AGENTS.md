# AGENTS.md

## Project Scope

This repository is an AstrBot platform adapter for Rocket.Chat.
Work should preserve three core guarantees:

- Normal unencrypted rooms must keep working even if E2EE is misconfigured or temporarily broken.
- Rocket.Chat behavior should be aligned to official server/client behavior, not third-party guesses.
- New features should prefer modular changes over growing `rocketchat_adapter.py` into a catch-all file.

Detailed implementation notes live in:

- [docs/DEVELOPMENT_KNOWLEDGE_BASE.md](/D:/Workspace/astrbot_plugin_rocket_chat_adapter/docs/DEVELOPMENT_KNOWLEDGE_BASE.md)

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
