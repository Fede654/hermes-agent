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
- **Canonical entry documents.** `AGENTS.md` holds repository instructions;
  `CLAUDE.md` is a relative symlink to that file.

## External memory provider

[Fede's Hermes Memory Kit](https://github.com/Fede654/hermes-memory-kit)
is configured as a standalone memory-provider plugin in each profile's
Hermes home. It is not copied into this source tree.

The provider supplies automatic recall through `prefetch`, exposes no model
memory tools and does not automatically persist turns. Durable writes use
HMK's `memoryctl` commands. A configured embedding backend is required;
HMK defaults to NVIDIA and supports a local `model2vec` configuration.
Plugin discovery and live recall must be validated for each deployment.

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
