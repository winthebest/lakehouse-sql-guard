# dbt example on a lakehouse: late rows leak through an intermediate model

A small churn project whose sources are Iceberg tables in a REST catalog, loaded night by night.
It is the project of the [end-to-end demo](../../docs/mcp.md#end-to-end-demo)
(`docker/demo-e2e.sh`); see [docs/dbt.md](../../docs/dbt.md#on-a-lakehouse-with-nightly-commits).

```
raw.churn_labels (label spine)   raw.orders (temporal)   raw.customers (static)      Iceberg, Lakekeeper
        |                              |                       |
        |                   int_customer_orders_7d       stg_customers               views
        |                (7 days before each cutoff)           |
        +-------------------------- training_churn -----------+                      table
```

- `int_customer_orders_7d` counts the orders of the 7 days before each prediction date, bounded on
  `ordered_at` only. The nightly job brings one row in five one to three days late, so for a
  prediction made on 11 September it also counts orders placed before then that only landed in
  the following nights. The
  guard reports it (rule L4, and a replay mismatch) against this file, not against
  `training_churn`, which only joins it.
- `fix/int_customer_orders_7d.sql` is the same model with the one join condition the guard
  suggests, `and orders.loaded_at < labels.cutoff_ts`.

`load-lakehouse.py` writes the sources the way a nightly job would, from a fixed seed: one Iceberg
commit per night (29 of them), each holding the orders placed the day before and the late rows.
The commits are written seconds apart, so each records the night it stands for in the snapshot
property `sqlguard.logical-ts`; replay pins to that instead of the commit time. A real nightly job
does not need the property.

## Run it

The demo does all of this in Docker. By hand, against any Iceberg REST catalog:

```bash
export PYICEBERG_CATALOG__LAKEHOUSE__TYPE=rest
export PYICEBERG_CATALOG__LAKEHOUSE__URI=http://localhost:8181/catalog     # your catalog
export PYICEBERG_CATALOG__LAKEHOUSE__WAREHOUSE=demo
export ICEBERG_REST_URI=$PYICEBERG_CATALOG__LAKEHOUSE__URI ICEBERG_REST_WAREHOUSE=demo
uv sync --extra dbt
uv run python examples/dbt-lakehouse/load-lakehouse.py --catalog lakehouse

cd examples/dbt-lakehouse
uv run dbt build --profiles-dir . --target-path build_ci
uv run sqlguard leakage dbt --manifest build_ci/manifest.json --replay \
    --catalog lakehouse --catalog-namespace raw --suggest-fix          # exit 1: leaky
cp fix/int_customer_orders_7d.sql models/intermediate/                  # apply the suggested change
uv run dbt build --profiles-dir . --target-path build_ci
uv run sqlguard leakage dbt --manifest build_ci/manifest.json --replay \
    --catalog lakehouse --catalog-namespace raw                         # exit 0: clean
```

The storage endpoint a catalog hands out may only resolve inside its own network (Lakekeeper in
the demo's Compose stack uses `http://silo:9000`); run these in the stack's `runner` container, as
the demo does.

## What the project declares

All of it is `meta.leakage` in the yml files; nothing is inferred from column names.

| where | meta |
|---|---|
| `models/staging/sources.yml` `churn_labels` | `role: label_spine`, `entity_keys`, `prediction_ts`, `label_columns` |
| `models/staging/sources.yml` `orders` | `time_column: ordered_at`, `availability_column: loaded_at` |
| `models/staging/sources.yml` `customers` | `role: static` |
| `models/marts/schema.yml` | `role: training_set` on `training_churn` |

## Attribution

Same shape as [examples/dbt](../dbt), which is derived from
[dbt-labs/jaffle_shop_duckdb](https://github.com/dbt-labs/jaffle_shop_duckdb) (Apache License 2.0,
copyright dbt Labs; license in [../dbt/LICENSE](../dbt/LICENSE)). The models, the loader and the
data in this project are new.
