# Memory-call observability findings — 2026-11-04

Inspected Hermes upstream base `27cbcb122f`, the previously deployed fork
`dc955c0`, provisioning's recipe and the exact Holographic pin
`820b777f48b234577f1c8d74a148e5071e8ab92c` (v0.25.3). This is a code/recipe audit,
not an inspection of the running agents' effective configuration or databases.

## Existing evidence

| Evidence | Where | Coverage and limitation |
|---|---|---|
| Explicit model tool calls | Hermes `state.db`, `messages.tool_calls`, tool result rows | Queries/writes the model requested, arguments and persisted results. Not an independent receipt for a call that crashed before persistence. Direct plugin calls outside the model loop are not automatically transcript tool rows. |
| Automatic recalled memory delivered to a turn | `messages.api_content`, or durable text parts for multimodal turns | The memory-context block sent with that user turn. Exists in both inspected Hermes versions. Shows emitted fact ids/text when the provider includes them; not empty retrievals, every backend attempt, timing or a proof of use. Persistence failures can leave gaps. |
| Fact changes and provenance | Holographic `fact_versions`, source metadata and `fact_store history` | Fact versions, what changed and where it was written. Not a chronological log of all read calls. Forgetting deliberately removes history. |
| Retrieval counts | Holographic `facts.retrieval_count` | Aggregate counts for facts returned by supported recall actions (including direct programmatic calls when counted). Buffered/flushed by the provider; not per-call/session reconstruction or a crash-proof event log. |
| Retrieval receipts and optional candidate stages | Holographic `memory_attempts`, `memory_attempt_refs`, `memory_outcomes` | Available with `experience`; off by default. See below. |
| `MEMORY_OP_MARK` | Hermes shared-metrics subsystem | Opt-in metrics by provider/operation/outcome; no queries, fact contents, fact ids or complete per-session trace. Not a substitute for the audit evidence above. |
| Our audit corrections | `<HERMES_HOME>/memory-audit/audit.db`, JSON reports and bitacora | Plan, per-operation status/result and partial/unknown outcomes. Covers this auditor's work, not every foreground provider invocation. |

No one of these proves **every call was recorded**, nor that a fact exposed to the
model influenced its answer. Exposure, explicit consultation, actual use and task
outcome are different observations. Absence of a record is not proof of no call.

The audit window preparer now extracts automatic memory blocks from the appended
API sidecar without rewriting the original user message. Tool reads and writes
already form part of its evidence. Budget omissions are still reported.

## Holographic already offers an opt-in retrieval journal

The pinned plugin includes [experience documentation](https://github.com/itpartypattaya/hermes-plugin-holographic/blob/820b777f48b234577f1c8d74a148e5071e8ab92c/docs/experience.md).
Provisioning's checked-in recipe does not set either option. Enabling them is a
provisioning decision, not a change to this fork or to the plugin:

```yaml
plugins:
  hermes-memory-store:
    experience: true
    experience_trace: true
```

`experience` records exact emitted fact/version references for supported attempts;
`experience_trace` additionally records bounded candidate/stage ids. The plugin's
`fact_experience explain` reads a receipt without rerunning retrieval; `recent`
lists receipts for the active session/store. Optional outcome reports are explicitly
agent reports, not authenticated human confirmation.

Known coverage limits in this exact version:

- A prefetch that emits no facts records no receipt. Explicit `fact_store search`
  can record an empty result, but historical `as_of` search is not recorded.
- Read-only/subagent contexts do not write these receipts. Collection is best effort:
  a busy database or crash can prevent a receipt; it is not queued for later recovery.
- Default retention is **30 days / 1,000 attempts per store**, with bounded stages
  and outcome revisions. Forgetting a referenced fact/version erases dependent
  receipts; this is intentional, not permanent audit retention.
- Queries and full answers are not copied into the journal. Session identity is
  hashed. Receipts describe emitted versions, not proof of reasoning or correctness.
- Collection adds a `fact_experience` tool and a brief system-prompt instruction for
  writer contexts. Configure it for new/rebuilt sessions through the normal deployment
  procedure; do not hot-swap tools or cached system prompts mid-conversation.

Activation needs no plugin fork, new dependency or extra LLM call. It adds local
SQLite writes/storage. SQLite contention, retention pruning and database scrubbing
can add latency; the plugin documents retries and cleanup costs, so low ordinary
overhead is not a latency guarantee. Measure on the deployed containers if enabled.

## If a complete invocation trace is later needed

A small, opt-in local recorder around the Hermes memory-manager boundary can add
start/end timestamps, profile/session, provider, action, explicit vs automatic
origin, success/error/timeout and emitted fact ids. It should include empty prefetch
and error attempts, define retention, and avoid duplicating fact text by default.
Calls made directly by this auditor also need that recorder's shared API, since
they bypass the manager. Provider-internal candidate scoring still belongs to the
plugin's own trace; a Hermes-boundary logger cannot observe those internal steps.

This is a moderate code/test extension within the Hermes fork, with no extra model
calls and no need to fork Holographic. A durable attempt record before dispatch is
more reliable than an after-only log, but a crash can still leave an unknown result.
We have not added or enabled that broader recorder here: the existing receipts and
transcript exposure evidence should be evaluated first to avoid overlapping logs.
