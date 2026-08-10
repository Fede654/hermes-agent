# BUG: Kimi OAuth (Coding Plan) request routed to `api.moonshot.ai` → 401

**Found:** 2026-07-10, on Coddy (CT 200, `hermes-agent` v0.16.0, branch `feat/integration`).
**Symptom:** a fresh `hermes chat` using the Kimi Coding-Plan OAuth credential sends its
completion to `https://api.moonshot.ai/v1/chat/completions` and gets **HTTP 401 Invalid
Authentication** — even though the OAuth token is valid and unexpired. The long-running
gateway process is unaffected; only newly-spawned agents break.

This is a **routing** bug, not an auth bug. No re-login or token refresh fixes it.

---

## Evidence

Same command (`hermes chat -q … -t … --yolo --cli`), three runs, from `logs/agent.log`:

| time (UTC) | `base_url` in the request | result |
|---|---|---|
| 2026-07-10 09:49 (goal #1/#2) | `https://api.kimi.com/coding/v1` | ✅ works |
| 2026-07-10 10:17 (gateway)     | `https://api.kimi.com/coding/v1` | ✅ works |
| 2026-07-10 12:26 (goal #3)     | `https://api.moonshot.ai/v1`     | ❌ 401 |

`sessions/request_dump_20260710_122600_*.json` confirms
`request.url = https://api.moonshot.ai/v1/chat/completions`.

**The trigger, to the second:** `hermes-home/models_dev_cache.json` was regenerated at
**12:26:05**, three seconds before the failing call at 12:26:08. Moving the cache aside
does not help — it is re-fetched immediately and re-poisons, because the change is
upstream in **models.dev**: around midday 2026-07-10, models.dev began advertising
`kimi-k2.7` served via Moonshot providers, so the `auto` resolver now picks a
`api.moonshot.ai` `base_url` for that model.

The Kimi OAuth token is a **Coding-Plan** credential. It is only valid against
`api.kimi.com/coding` (Anthropic Messages wire), **not** against `api.moonshot.ai`
(OpenAI wire, pay-as-you-go) — see the existing comment at
`agent/auxiliary_client.py:338-341` (`#17076`). Routing the OAuth token to Moonshot is
guaranteed to 401.

## Root cause

The endpoint is chosen from the model's `base_url` (models.dev, via the `auto` path),
and the **presence of a Coding-Plan OAuth credential does not override it**. Both hosts
map to the same provider id, so nothing downstream notices the mismatch:

- `agent/auxiliary_client.py:142-143` — aliases `kimi`, `moonshot` → `kimi-coding`.
- `agent/model_metadata.py:411-413` — `_URL_TO_PROVIDER`: **both** `api.moonshot.ai`
  and `api.kimi.com` → `"kimi-coding"`. So a Moonshot `base_url` still resolves to the
  `kimi-coding` provider and passes every provider-id check while pointing at the wrong
  wire and rejecting the OAuth token.
- The `kimi-coding` plugin (`plugins/model-providers/kimi-coding/__init__.py:61`) has a
  static `base_url="https://api.moonshot.ai/v1"`. The switch to `api.kimi.com/coding`
  only happens when a Coding-Plan OAuth credential is resolved through the credential
  path (`agent/auxiliary_client.py` `_resolve_api_key_provider`, ~1487/1528, keys on
  `base_url_host_matches(base_url, "api.kimi.com")`). The `auto`→models.dev path never
  reaches that rewrite: it takes the model's Moonshot `base_url` as authoritative.

Why the gateway survived: it resolved its client at start-up (2026-07-05, before
models.dev changed) and holds `provider=kimi-coding` / `base_url=api.kimi.com/coding/v1`
in memory. Only freshly-spawned `hermes chat` agents re-resolve against the poisoned
cache.

Upstream `NicoEchaniz/hermes-agent` `main` carries the **same code** — the kimi-coding
plugin `base_url` is identically static and there is no OAuth-vs-models.dev precedence
rule. This is a latent routing bug in both trees that a models.dev-side change exposed.
No upstream commit addresses it (last kimi-coding commits: `ce4e74b` thinking/effort,
`9022804` provider pluggabilization).

## Proposed fix

**Invariant: a Coding-Plan OAuth credential pins the endpoint. It must win over any
`base_url` derived from models.dev.**

Concretely, in the `auto`/model-resolution path (`agent/auxiliary_client.py`,
`resolve_provider_client` → `_resolve_auto`, and the aux path around 1480-1535): when the
resolved provider is `kimi-coding`/`kimi-coding-cn` **and** a Kimi Coding-Plan OAuth
credential is available, force

```
base_url = https://api.kimi.com/coding/v1        # /coding.cn for -cn
```

overriding whatever `base_url` came from the model catalog, before the client is built.
Equivalently: teach `_URL_TO_PROVIDER` / the resolver that `api.moonshot.ai` and
`api.kimi.com/coding` are **distinct auth realms** for the same provider id, and select
the realm from the credential type (OAuth → `/coding`; `sk-` legacy key → Moonshot),
never from the model's advertised host.

Guard rails:
- Do not touch the legacy-key path: a user with a Moonshot `sk-` key must still reach
  `api.moonshot.ai`.
- The `_maybe_wrap_anthropic` / Anthropic-Messages wrapping already keys on
  `api.kimi.com/coding` (see `anthropic_adapter.py:446`), so forcing the `/coding`
  base_url also restores the correct wire format, not just the host.

This is upstreamable to `NicoEchaniz/hermes-agent` — the bug exists there too.

## Immediate workaround (no code change)

Pin the model/provider out of `auto` so the resolver never consults models.dev for
routing:

```
hermes model     # pick the Kimi Coding-Plan OAuth provider + model explicitly
```

or set it non-interactively in `~/.hermes/config.yaml` (`model.provider: kimi-coding`
plus a pinned `model.model`). With the provider pinned, `resolve_provider_client` takes
the `kimi-coding` credential branch (→ `api.kimi.com/coding`) instead of the
`auto`→models.dev branch (→ `api.moonshot.ai`).

## Repro

```
# on Coddy, as hermes, gateway env loaded:
hermes chat -q "di OK" -t terminal --max-turns 1 --yolo --cli
# grep the request url in the run: api.moonshot.ai/v1 → 401
# (was api.kimi.com/coding/v1 before models_dev_cache.json regenerated 12:26:05)
```

---

## Second layer, uncovered while fixing the first (2026-07-10)

Pinning `model.base_url = https://api.kimi.com/coding/v1` fixed the *routing*, and
then a **second, independent** 401 surfaced — a different message:

```
HTTP 401: The API Key appears to be invalid or may have expired.
```

(vs. the routing-layer `Invalid Authentication`.) The two failure modes look alike
and stack, so the routing bug masked a dead credential. Sequence that finally
worked, and the traps in it:

1. **The OAuth access token had expired and the refresh token no longer renewed.**
   The `hermes chat` path does not run `kimi_credential_refresh` — only the gateway
   does, and only in its `curator-review` cycle. A restarted gateway did **not**
   re-mint the token either. A real device-flow re-login was required.

2. **`kimi login` (the standalone kimi CLI) authenticates the wrong store.**
   `hermes auth status kimi` still read `logged out` afterwards, and chats still
   401'd — with a token that was actually **valid** (`curl -H "Authorization:
   Bearer <new access_token>" https://api.kimi.com/coding/v1/models` → `HTTP 200`).

3. **Root cause of layer 2: the credential lives in TWO locations and they are not
   synced.** Per the v0.19 relocation (commit `cb36e0109`), Hermes reads the Kimi
   Coding-Plan OAuth from **`~/.kimi/credentials/kimi-code.json`**, but `kimi login`
   writes to **`~/.kimi-code/credentials/kimi-code.json`**. After the re-login:
   - `~/.kimi-code/credentials/kimi-code.json` — fresh (new token)
   - `~/.kimi/credentials/kimi-code.json` — stale (old, dead token) ← what Hermes uses

   **Fix: copy the OAuth from `~/.kimi-code/credentials/` → `~/.kimi/credentials/`**,
   then restart the gateway. Chat then routes to `api.kimi.com/coding/v1` and auth
   passes.

### What this implies for the fork

The "resolve kimi-code creds across `~/.kimi` and `~/.kimi-code`" logic
(`cb36e0109`) is **read-side only** — it looks in both places but does **not**
propagate a fresh login from one to the other. A post-`kimi login` step (or a
symlink `~/.kimi/credentials/kimi-code.json → ~/.kimi-code/credentials/…`, or making
the resolver prefer the newest-mtime file across both dirs) would make re-login
"just work" instead of requiring a manual copy. **This is the second half of the
fix and belongs upstream too.**

### Operational recovery recipe (no token corruption)

To recover an agent hit by this **without** clobbering a still-good token:

```bash
# 1. only act if the CURRENT credential is actually dead — test it first:
AT=$(python3 -c "import json;print(json.load(open('$HOME/.kimi/credentials/kimi-code.json'))['access_token'])")
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $AT" \
     https://api.kimi.com/coding/v1/models          # 200 = token fine, do NOT re-login

# 2. if 401: back up BOTH copies, re-login, copy fresh → the dir Hermes reads, restart
cp -a ~/.kimi/credentials/kimi-code.json{,.bak-$(date +%s)}
kimi login                                           # device flow → writes ~/.kimi-code
cp ~/.kimi-code/credentials/kimi-code.json ~/.kimi/credentials/kimi-code.json
systemctl restart hermes-gateway.service
```

Pin `model.base_url` (routing fix) is independent and should be applied regardless.

---

## Third layer: rotating refresh tokens with inconsistent persistence (2026-07-10)

After the routing fix (layer 1) and the two-directory copy (layer 2), Coddy still died
roughly every 15 minutes. Root cause is deeper than either, and is the one that actually
matters for **unattended operation**:

**The Kimi Coding-Plan access token lives ~15 min, and the refresh_token ROTATES on every
use.** Each successful refresh returns a *new* refresh_token and invalidates the old one.
That is fine with a single owner — but Coddy had **three independent consumers** all
reading the same on-disk credential and each refreshing on its own:

1. the gateway (`hermes-gateway.service`, in its `kimi_credential_refresh` cycle),
2. ad-hoc `hermes chat` processes (each re-resolves credentials on start),
3. the standalone `kimi -p` / `kimi` CLI (writes `~/.kimi-code`).

Whichever refreshed first rotated the token; the others were left holding an invalidated
grant. Symptom: `resolve_kimi_coding_runtime_credentials(force_refresh=True)` returns
**`"The provided authorization grant is invalid"`** even seconds after a fresh
`kimi login`. (`kimi -p` made it worse: on a stale grant it emitted `auth.login_required`
and *truncated the credentials file to 136 bytes*, wiping the refresh_token entirely.)

### The refresh mechanism (for reference)

hermes's own path, `hermes_cli/auth.py`:

```python
KIMI_CODE_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
KIMI_CODE_OAUTH_HOST = "https://auth.kimi.com"
# POST {HOST}/api/oauth/token  (form-encoded)
#   grant_type=refresh_token, refresh_token=<rt>, client_id=<id>
# -> new access_token + NEW refresh_token; _save_kimi_cli_credentials() persists both
resolve_kimi_coding_runtime_credentials(force_refresh=True)   # read → refresh → persist
```

Note: this endpoint is `auth.kimi.com/api/oauth/token` — **not** `platform.kimi.com` or
`www.kimi.com/code/...` (both 404/302). The credential file hermes actually reads is
`_kimi_cli_credentials_path()` → `~/.kimi/credentials/kimi-code.json`.

### Fix: a single-owner refresher on a timer

Make exactly one process responsible for refreshing, run it well inside the 15-min
window, and never let a goal refresh on its own. Deployed on Coddy as
`/opt/coddy/bin/kimi-refresh.py` + a systemd timer:

```python
# kimi-refresh.py — the ONLY refresher. Timer runs every 5 min.
import sys, time; sys.path.insert(0, "/opt/agent")
from hermes_cli import auth
creds = auth._read_kimi_cli_credentials()
if creds.get("access_token") and (creds.get("expires_at",0) - time.time()) > 480:
    sys.exit(0)                                    # >8 min left, skip
auth.resolve_kimi_coding_runtime_credentials(force_refresh=True,
                                             allow_api_key_fallback=False)
```

```ini
# kimi-refresh.timer
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
```

With the token kept perpetually fresh by one owner, `hermes chat` goals see a valid
token and never trigger a refresh, so the rotation never conflicts. Verified: Coddy ran
goals back-to-back with 0 auth failures and no manual re-login.

### The real upstream fix

Three consumers refreshing the same rotating credential is the bug. Upstream options,
best first:

1. **Serialize refresh through one place** — a file-locked, single-flight refresh in
   `resolve_kimi_coding_runtime_credentials` so concurrent callers wait on one network
   round-trip and all read the persisted result, instead of each rotating independently.
2. **Persist to both `~/.kimi` and `~/.kimi-code` atomically** on every refresh (closes
   layer 2 as a side effect).
3. **Have the gateway own refresh and expose the token** to child `hermes chat`
   invocations (env or socket), so ad-hoc runs never touch the credential file.

Until one of those lands, the single-owner timer above is the correct operational
workaround — and it belongs in the fork's deploy recipe for any 24/7 Kimi-OAuth agent
(Coddy, chiwa, alterbot).

---

## Blast radius: all agents on this fork are exposed (checked 2026-07-10)

The trigger is external (a models.dev catalog change), so **every** Hermes agent
running Kimi in `auto` mode is affected — not just the one that surfaced it. Checked
all three:

| agent | host | gateway at check | disk token | in-memory | cache poisoned? | outcome |
|---|---|---|---|---|---|---|
| Coddy | coddy-pve CT200 | restarted → broke | dead (401) | lost on restart | yes (12:26) | full recovery: re-login + copy + base_url pin |
| chiwa | chiwa-pve CT200 | **active, 0 restarts** | dead (401) | **alive** (last OK 14:30, kimi.com) | **yes (12:38)** | pin applied, gateway left running |
| alterbot | chiwa-pve CT201 | **active, 0 restarts** | dead (401) | **alive** | not yet (Jul 8) | pin applied, gateway left running |

Key observations:

- **A long-running gateway is immune until it re-resolves.** Its in-memory client was
  built before the models.dev change, so it keeps hitting `api.kimi.com/coding`. The
  poisoned cache only bites when a **new** client is constructed — a restart, or an
  `agent_init` on a fresh turn (which is what took Coddy down *without* a restart).
- **The disk token being dead is not visible while the gateway holds a live one in
  memory.** All three had 401-on-`curl` disk tokens; only Coddy (which I restarted)
  actually failed. So: **do not restart a working gateway to "fix" it** — that is what
  converts a latent problem into an outage, and if its refresh-token has also rotated
  out, recovery then needs a full re-login.
- **Safe remediation for a still-working agent = apply the `base_url` pin only, do not
  restart, do not touch the credential.** The pin protects the *next* restart; the
  running gateway keeps working in the meantime. That is what was done for chiwa and
  alterbot: `model.base_url: https://api.kimi.com/coding/v1` inserted into the
  top-level `model:` block (backup `config.yaml.bak-20260710`), gateways untouched.

The real fix (making OAuth pin the endpoint over models.dev, and syncing the two
credential dirs on login) would remove the exposure for all of them at once.
