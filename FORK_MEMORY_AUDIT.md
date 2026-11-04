# Knowledge audit service

The fork provides an opt-in idle auditor. `agent-provision-nous` owns provider
installation/version pins, deployed doctrine, workspace layout, service/timer
installation and the deployment pin. No agents or provisioning files are changed
by developing or enabling the fork's CLI.

## Knowledge contract

The auditor reads the **installed** doctrine (by default `AGENTS.md`,
`doctrina/memoria.md`, `doctrina/conocimiento.md` and `SOUL.md`) and evaluates:

- **Library:** canonical documents, sources and research. Source pointers must
  exist; original documents are never rewritten or relocated by the auditor.
- **Wiki:** current concepts, synthesis and links into the library. Report stale
  synthesis, broken links and contradictory current versions.
- **Episodic memory:** dated events, confirmed decisions, reasons and source
  pointers. Compare actual agent writes with conversation evidence and current
  facts. Change an outdated fact instead of adding a competing current fact.
- **Skills:** reusable procedures, never project canon, dated state or research.
  Phase 1a reports issues; it cannot create/edit skills or move content.

No correction is a valid and expected outcome. The reviewer has **no tools**;
its JSON response is a proposed plan. Library/wiki/skills/native-memory changes
remain findings. Native MEMORY.md/USER.md and doctrine/SOUL are never rewritten.
This deliberately leaves native-rule restoration as a report for the foreground
agent/operator rather than mixing file writes with an external fact transaction.
Temporary failures remain dated symptoms, not durable incapacity rules. Past
events are preserved even when their consequences have since changed.

## Operator configuration

Example `config.yaml` (paths and doctrine are deployment decisions):

```yaml
audit:
  enabled: true
  mode: dry_run
  workspace: /home/hermes/workspace
  idle_hours: 6
  window_days: 2
  max_messages: 120
  overlap: 10
  max_writes: 8
  max_input_tokens: 64000
  timeout: 600
  provider: ""                 # empty: standard auxiliary router, main provider/model
  model: ""
  reasoning_effort: high         # medium or high
  platforms: [telegram]
  doctrine_files: [AGENTS.md, doctrina/memoria.md, doctrina/conocimiento.md, SOUL.md]
  wiki_path: wiki
  library_path: biblioteca
  journal_path: bitacora.md
```

`audit.enabled` also disables the old background memory/skill reviewer. Keep
`auxiliary.background_review.enabled: false` in provisioning as an explicit
policy. Per-turn nudge counters are not reused. Existing foreground memory
management and provider lifecycle remain unchanged.

Run through the installed Hermes entry point, with the normal owning profile:

```bash
hermes memory audit tick
hermes memory audit run SESSION_ID
hermes memory audit status
hermes memory audit show RUN_ID
```

Provisioning schedules `hermes memory audit tick` **hourly or more often**, using
an external timer or a Hermes `no_agent` cron script. The command checks once
and exits; it does not install services or spawn a gateway. Each invocation serves
one explicitly selected profile; normal Hermes profile selection applies. A busy
or unchanged session needs no model call. `run` obeys the same idle/activity gates
as `tick`; it does not force an audit during work. Disabled audits are a no-op.

`python scripts/prepare_memory_audit.py SESSION_ID --watermark N` remains a
read-only diagnostic command that prints an evidence window or `null`.

## Execution and persistence

1. Read durable `state.db` history, including compacted/archived raw messages but
   excluding synthetic compression summaries. Use at most 120 textual user/
   assistant messages from the latest two days, with bounded overlap after the
   last successfully audited id. Tool bodies are summarized except memory/skill
   operations, which remain complete. Resolve tool names through call ids too.
2. Check profile-wide foreground leases, compression locks and open/deferred
   delegations: shared knowledge must not be audited while another topic writes.
   Serialize auditors per profile with a durable, expiring claim.
3. In a disposable worker, collect bounded doctrine, native memory, relevant wiki
   pages/library source text, indexes and link existence, skill contents/sizes,
   ledger and recent bitacora, plus current provider facts. Resolve file paths
   within the configured roots; do not follow links into outside private files.
4. Build a fresh, isolated, tool-free request through the standard auxiliary
   router (`task=memory_audit`). Nothing mutates the foreground context or its
   cached system prompt. UTF-8 bytes conservatively bound the input token budget;
   optional context/ordinary rows can be omitted with explicit coverage gaps.
   Doctrine, provider facts and memory operations are never silently clipped.
   If protected evidence alone exceeds the budget, fail before a model call.
5. Poll activity while the worker runs; kill the worker on new user activity,
   a writer lease or timeout. Recheck source generation and file fingerprints
   before accepting the plan. A changed source invalidates it. No correction can
   occur in the worker. Record known usage in `session_model_usage`, including
   invalid responses; interrupted dispatched calls have explicitly unknown cost
   when the provider never returned usage.
6. Validate the complete plan: bounded writes, allowed operations (`add/change`),
   existing evidence ids, exact expected old fact text and unique target ids.
   An incomplete snapshot cannot justify an `add`. Budget-omitted conversation
   text cannot justify a correction. Skills/wiki/library/native writes are never
   part of the executable plan.
7. Dry-run publishes only a local report and bitacora entry. Apply checks the
   provider's transaction capability and revision, rechecks activity under the
   short state.db writer fence, then commits exactly one idempotent batch. A new
   turn cannot be admitted halfway through that commit.
8. Persist successful watermarks only after reporting. A second check of the same
   window makes no model call or corrections. Truncated history is explicitly a
   coverage gap; discarded messages are never claimed to have been reviewed.

State and full JSON reports live under `<HERMES_HOME>/memory-audit/`, with a
private directory/database. Bitacora includes window, findings, proposals, actual
fact ids/before/after from the receipt and pending issues. Reports are local and
have no Telegram or other messaging delivery path. The run id marks journal
entries so receipt recovery cannot append the same successful report twice.
A crash during apply leaves a `committing` record; the next invocation queries
its durable provider receipt before advancing or retrying anything.

## External provider contract

The generic `MemoryProvider` ABC has additive optional methods:

```python
audit_snapshot(*, session_id, query)
# {"facts": [{"fact_id": 109, "content": "..."}], "revision": "opaque", "complete": True, "atomic_apply": True}

apply_audit_batch(*, batch_id, expected_revision, operations)
# {"batch_id": batch_id, "writes": [{"fact_id": 109, "before": "...", "after": "..."}]}

audit_batch_status(batch_id)
# committed receipt, or None if the batch never committed
```

Set `atomic_apply: true` only when both batch methods are implemented; hosted
proxies cannot establish that by method introspection alone.
The snapshot is read-only, scoped to the routed identity and initialized with
`agent_context=subagent`; no extraction or recall bookkeeping. `complete: true`
means the snapshot can rule out existing duplicates in that memory scope. Every
relevant concurrent write must invalidate its revision. Apply validates the entire
batch and revision **before** mutation, commits all or none, stores its receipt in
the same transaction and returns that same receipt for a repeated batch id. It
must be local and short: no model/network/deferred writes under the source fence.
Do not implement it by looping independent tool calls. The status method must
remain read-only and reliable after process crashes.

Legacy providers exposing `fact_store` can be audited through bounded read-only
`list/search` calls. The engine labels those snapshots incomplete and does not
claim a missing fact merely because a bounded search missed it. Other providers
need their own `audit_snapshot` implementation. No provider-specific SQL, plugin
imports, memory database format or version pin lives in this audit engine.

**The deployed Holographic v0.25.3 pin (`820b777f`) has no atomic batch/receipt
methods.** It supports the read-only dry-run path. `apply` refuses it before a model
request. Enabling atomic writes requires implementing the contract in the external
provider plugin; changing configuration or issuing sequential `fact_store` calls
cannot supply that guarantee. The fork's transaction and recovery paths are tested
through real discovery of an isolated contract provider, not by modifying Holographic.

## Validation

Behaviour tests cover real SessionDB/provider discovery, dry-run non-mutation and
usage, idempotence, cancellation on new messages, worker termination, concurrent
claims, source changes, explicit profile binding, atomic batch failure and crash
receipt recovery, budget refusal before model dispatch, layer/source/skill evidence
and rejecting unsafe/unverified model operations. They do not prove the chosen
LLM's judgement. The pre-cleanup Eyja/Filiberto exports are still needed for the
handoff's semantic acceptance evaluation before enabling apply in deployment.
