# Fork workflow

This repository is a direct fork of NousResearch/hermes-agent with a small
set of profile-aware extensions. Development tracks Nous through `main`;
the validated runtime lives on `feat/integration`.

`origin` is `Fede654/hermes-agent`. `upstream` fetches NousResearch and has
push disabled.

## Branch roles

| Reference | Role | Rule |
|-----------|------|------|
| `upstream/main` | Read-only Nous development reference | Fetch and inspect; never push upstream. |
| `main` | Exact mirror of Nous development at the last sync | No fork patches, merges, or local documentation. |
| Official Nous release tag | Reproducible runtime base | Record the exact tag and commit. |
| `feat/integration` | Default branch and single deploy target | Published release plus the maintained delta. |
| `integration/sync-<date>` | Temporary release candidate | Build and test in isolation; PRs target `feat/integration`. |
| `feat/*`, `fix/*` | Active development | Keep changes focused and independently testable. |
| `canonical-<date>` tags | Optional validated runtime snapshots | Immutable release pins. |

This document belongs to `feat/integration`; `main` keeps the unmodified
upstream tree.

## Maintained extensions

The runtime base is NousResearch `v0.22.5`, commit
`bf0203a794a07aac3ff764972ab9087baa4e4f4e`.

- **Outbound send audit.** `agent/send_audit.py` records `send_message`
  attempts from `hermes send`, cron and dashboard messaging in each
  profile's `<HERMES_HOME>/audit.db`. It captures the resolved destination,
  outcome, message ID and up to 2,000 characters of content. Ordinary
  gateway replies that do not call `send_message` are outside its scope.
  Audit failures must not prevent delivery.
- **Session-scoped untargeted sends.** `send_message` without an explicit
  target resolves to the chat of the current gateway session when it is on the
  same platform (`_session_chat_for` in `tools/send_message_tool.py`). The home
  channel remains the fallback for cron, CLI and cross-platform sends. Without
  this, a send issued while serving one user can surface in the home chat.
- **Telegram observed group context.** The channel prompt for observed
  (unmentioned) group messages tells the agent to treat them as first-class
  recent memory, and observed group audio is transcribed
  (`_transcribe_observed_audio`) so the transcript, not just the file path,
  is kept. Transcription is best-effort and never blocks ingestion. Bot
  self-message and bot-to-bot loop guards are upstream (`_is_own_message`,
  `_bot_sender_suppressed`); do not re-add them.
- **Canonical entry documents.** `AGENTS.md` holds repository instructions;
  `CLAUDE.md` is a relative symlink to that file.

## External memory provider

[Hermes Memory Kit](https://github.com/Fede654/hermes-memory-kit) is the default
memory backend for this distribution. The Unix installer fetches only its
`hermes_plugin/` runtime package at the exact commit in `fork-memory.json`,
prepares dependencies through Hermes PM and validates SQLite plus local
multilingual CPU embeddings before selecting it.

`remember` and `recall` provide deliberate writes and retrieval; `librarian`
supports inspection, expansion, updates and deletion. Automatic recall uses
Hermes' existing prefetch channel. Session-end and pre-compression extraction
use the configured auxiliary model. Each profile owns its database at
`<HERMES_HOME>/hmk-memory/library.db`; no process-wide HMK paths are used.
Native summaries are imported once and retained on disk.

Run `hermes memory bootstrap` to provision the current profile explicitly.
An explicit choice of another provider is preserved by installer reruns.
Configuration changes affect new sessions. See [HMK installation](HMK_MEMORY.md).

## Deploying an agent

`feat/integration` is the only deploy target. An agent is created with
[`agent-provision`](https://github.com/Fede654/agent-provision), which pins this
branch by **commit SHA** (or a `canonical-<date>` tag) and runs the Hermes
installer in the container; the container envelope (network zone, storage,
privileges) is defined by `pve-infra`. Never deploy from `main` (a mirror of Nous
development, not a release) and never hand-patch a deployed checkout: a change
goes through a PR to `feat/integration`, a new `canonical-<date>` tag, and a
re-provision. Live patches that were found only in running containers are
recorded in the commit that ports them.

## WhatsApp restricted-number configuration

To accept one phone number and stay silent for all other contacts, configure
the native policy in each profile's `config.yaml`:

```yaml
whatsapp:
  dm_policy: allowlist
  allow_from: ["<authorized phone number>"]
  group_policy: disabled
  unauthorized_dm_behavior: ignore
```

Use bot mode for a separate WhatsApp bot number. Keep owner-message forwarding
disabled unless needed; forwarding must obey the same destination allowlist.
Existing `WHATSAPP_*` environment settings can affect the effective policy:
remove wildcard `WHATSAPP_ALLOWED_USERS=*` and `WHATSAPP_ALLOW_ALL_USERS=true`
settings and verify the adapter and bridge resolve to the same allowlist.
Never put real phone numbers or credentials in this repository.

## Update workflow

1. Fetch Nous's development branch and published releases. Fast-forward
   `main` to the exact fetched upstream commit. An upstream history rewrite
   requires an explicit reference-replacement decision.
2. Select a published release tag and record its commit. Build the maintained
   delta on that tag in an isolated `integration/sync-<date>` worktree.
3. Retain the current integration's ancestry when preparing a promotion and
   compare the final tree with the chosen release to keep the delta explicit.
4. Run `scripts/run_tests.sh` for audit, messaging, multiplex-profile isolation,
   relay-target authorization and WhatsApp allowlist behavior. Run
   `python scripts/check --base <release-tag>` for the full lint gate.
5. Review and validate the exact candidate SHA, then promote through a PR to
   `feat/integration` without force-updating the default branch.
6. Deploy only under separate authorization, using current PM tools and each
   profile's own configuration. Validate HMK discovery/recall and live delivery.
7. Remove integrated temporary branches only after verifying their exact tips
   are ancestors of the integration and reachable from a remote ref. Leave
   active Orca workspaces intact.
