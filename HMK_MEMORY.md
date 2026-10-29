# Default HMK memory

The Unix installer on `feat/integration` provisions HMK automatically. For an
existing installation or another profile, run:

```bash
hermes memory bootstrap
hermes -p work memory bootstrap
```

Each command provisions the selected profile only. Choosing another memory
provider explicitly — including `builtin` — prevents automatic replacement.
The Windows installer continues to follow upstream; the HMK automatic-install
path is currently validated on Linux, with Unix file locking in the toolkit.

`fork-memory.json` records an exact commit of Fede654/hermes-memory-kit and its
`hermes_plugin/` package. Hermes' native installer scans and publishes that
package, and PM prepares the declared Python dependencies. No HMK code is
vendored into Hermes core. Installation downloads a public Model2Vec multilingual
model once; subsequent retrieval runs on CPU without a memory-service API key.

Before selecting HMK, an installation probe in PM's selected interpreter checks
real embeddings and SQLite integrity. If provisioning or verification fails,
the installer fails without disabling native memory. Fix the reported error
and rerun `hermes memory bootstrap`. Existing native MEMORY.md/USER.md files
are imported once and retained. Repeating bootstrap preserves the database.

After success, new sessions use:

```yaml
memory:
  provider: hmk-memory
  memory_enabled: false
  user_profile_enabled: false
```

The SQLite database is `<HERMES_HOME>/hmk-memory/library.db`, private to that
profile. Process-wide legacy HMK path variables do not redirect it. Existing
standalone HMK libraries are not imported automatically: retain them and migrate
them deliberately rather than guessing which profile owns them.

The agent gets `remember`, `recall` and `librarian` tools through Hermes' existing
memory-provider interface. `librarian` can expand, update and delete records.
Automatic recall stays outside the frozen system prompt, preserving the cache.
If local embeddings fail later, writes remain durable and recall uses lexical
search; pending embeddings are retried on a successful write or bootstrap.

The default model is `minishlab/potion-multilingual-128M`. Non-secret overrides
belong under `memory.hmk` in config.yaml, for example:

```yaml
memory:
  hmk:
    embedding_provider: model2vec
    embedding_model: minishlab/potion-multilingual-128M
    distill_enabled: true
    distill_timeout: 120
    limit: 8
    budget_tokens: 1500
    threshold: 0.30
```

Session-end and pre-compression extraction use the auxiliary model configured
in Hermes and can incur normal model charges. Set `distill_enabled: false`
for deliberate memory writes only. The integration does not install a gateway
service or restart running sessions.

To return to native memory, select `memory.provider: builtin` and set both
native flags to true. Native files contain their retained originals; later
HMK-only memories stay in SQLite and are not copied back automatically.

## Validation contract

Hermes tests cover delayed selection on failure, idempotent provisioning and
preservation of explicit choices. HMK's runtime tests load the actual plugin
through Hermes discovery and exercise A→B→A isolation, CRUD, reopening the DB,
real local semantic retrieval, native-summary import, backend outages and
session/pre-compression extraction. The extractor response is supplied locally
in tests; they never call a paid model.
