# Checking a dbt project

`sqlguard leakage dbt` checks every model with `meta.leakage.role: training_set` in a compiled
dbt project. It reads `manifest.json` only (schema v12, dbt-core 1.8 to 1.12; anything else exits
2). A worked example is in [examples/dbt](../examples/dbt).

## How it works

Leaks usually sit in an intermediate model (a lifetime aggregate in `int_*`), not in the model
that joins it. The guard replaces each upstream model with a CTE holding its compiled SQL, down to
sources, seeds, snapshots and models you declare as label spine, static or temporal, and checks
the resulting single query with the normal rules. Ephemeral models, which dbt pastes into their
parent, are checked too, including what they read. Findings name the model that holds the leak
(`original_file_path`); when that cannot be told apart, the candidates are listed. `--suggest-fix`
prints a diff of the inlined query and never writes a file.

## Declare time in meta

```yaml
sources:
  - name: raw
    tables:
      - name: churn_labels
        config: {meta: {leakage: {role: label_spine, entity_keys: [customer_id],
                                  prediction_ts: cutoff_ts, label_columns: [churned]}}}
      - name: orders
        config: {meta: {leakage: {time_column: ordered_at, availability_column: loaded_at,
                                  max_ingest_delay: 2 days}}}
      - name: customers
        config: {meta: {leakage: {role: static}}}
models:
  - name: training_churn
    config: {meta: {leakage: {role: training_set}}}
```

`entity_keys`, `prediction_ts`, `label_columns`, `boundary` and `label_known_after` may be set on
the training model (it wins) or on the label spine. `outcome_columns: [col]` on a table marks facts
known only after the outcome. A table that declares nothing is unclassified and the model is
unverifiable. Unknown roles are an error.

## Compile, then check

```bash
dbt compile --target-path build_ci --select "+config.meta.leakage.role:training_set"
sqlguard leakage dbt --manifest build_ci/manifest.json [--select 'training_*'] [--suggest-fix] [--json]
```

Compile with your real warehouse profile: macros that introspect the warehouse (`run_query`) do
not compile on DuckDB. Do not select with `state:modified+`: dbt ignores meta-only edits, and a
changed `time_column` must be re-checked. Ancestors need `compiled_code` (`dbt parse` is not
enough).

Column names and types come from the yml `columns:` (give time columns a `data_type`) or from
`--catalog-json` (`dbt docs generate`); without them the model is unverifiable.

In CI, see [examples/github-action-dbt.yml](../examples/github-action-dbt.yml). The
`sqlguard-leakage-dbt` pre-commit hook is in `.pre-commit-hooks.yaml`.

## `--spec`, replay

`--spec leakage.yml` (a leakage spec file) is merged over the meta and wins, with a warning for
each value it changes and for each key that can relax a rule (`boundary` other than strict,
`max_ingest_delay`, `label_known_after`, availability columns, static tables, the label table,
`relations`). `--replay --catalog NAME` replays the physical tables (sources); materialized models
have no Iceberg history, so only declare label spine, static or temporal models that are Iceberg
tables if you want them replayed. dbt's database and schema become the relations map
(`db.raw.orders` -> `raw.orders`); set `iceberg_table` in a source's meta, or `relations` in
`--spec`, when the catalog table has another name. Static analysis and replay both apply it.

## What is not checked

A model that declares `time_column`, `role: static` or `role: label_spine` is read as a physical
table and trusted as declared: its own SQL is not inlined or checked, and a leak inside it is not
found. Every run lists these models (`trusted without inlining: ...` in the text,
`trusted_models` in the JSON). Replay of such a model, if it is an Iceberg table, checks its
snapshot history "as built": a model that was rebuilt or backfilled has snapshots from build
time, which may already contain facts from after the prediction time.

## Stale manifests

The guard reads the manifest; it does not compile. The `sqlguard-leakage-dbt` hook therefore
checks the SQL of the last `dbt compile`; add a `dbt compile` step before it (a `repo: local`
hook). When a checked model or yml file is newer than the manifest, a warning says so (only when
the project is found from the working directory or next to the manifest).

## Exit codes

0 clean; 1 leaky or unverifiable (`--allow-unverifiable` accepts unverifiable); 2 unusable input:
wrong manifest version, missing `compiled_code` (the message says `dbt compile --select
+<model>`), no `training_set`, or a training set that reads no table with a `time_column`.
