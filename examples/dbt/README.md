# dbt example: leakage hidden in an intermediate model

A small churn project that shows `sqlguard leakage dbt` finding a leak that lives one model
upstream of the training set.

```
raw.churn_labels  (label spine)   raw.orders (temporal)   raw.customers (static)      Iceberg
        |                               |                        |
        |                         stg_orders                stg_customers             views
        |                         /          \                   |
        |       int_customer_features   int_customer_recent_orders                    views
        |            (lifetime aggregate)     (90 days before the cutoff)
        |                 |                          |
   training_churn_leaky                    training_churn_clean                       tables
```

- `training_churn_leaky` joins `int_customer_features`, which counts every order of a customer.
  For a prediction made on 1 March it also counts the orders of April to July. The guard reports
  the finding against `models/intermediate/int_customer_features.sql`, not against the training
  model that merely joins it.
- `training_churn_clean` joins `int_customer_recent_orders`, whose bounds
  (`ordered_at < cutoff_ts and loaded_at < cutoff_ts`, 90 days back) sit in the join.

The sources are Iceberg tables read through dbt-duckdb's `iceberg` plugin. Orders land in weekly
commits, so replay can rebuild what each prediction date could have seen.

## Run it

```bash
uv sync --extra dbt                          # dbt-core<2 and dbt-duckdb, for this example only
export ICEBERG_WAREHOUSE=$PWD/examples/dbt/warehouse
uv run python examples/dbt/make-warehouse.py "$ICEBERG_WAREHOUSE"

cd examples/dbt
uv run dbt compile --profiles-dir . --target-path build_x \
    --select "+config.meta.leakage.role:training_set"

uv run sqlguard leakage dbt --manifest build_x/manifest.json --suggest-fix   # exit 1
uv run sqlguard leakage dbt --manifest build_x/manifest.json --select '*clean'   # exit 0
```

Replay the Iceberg sources point in time (leaky: the lifetime columns differ from what was
knowable; clean: 130 rows over 4 prediction dates match):

```bash
export PYICEBERG_CATALOG__JAFFLE__TYPE=sql
export PYICEBERG_CATALOG__JAFFLE__URI=sqlite:///$ICEBERG_WAREHOUSE/catalog.db
export PYICEBERG_CATALOG__JAFFLE__WAREHOUSE=file://$ICEBERG_WAREHOUSE
uv run sqlguard leakage dbt --manifest build_x/manifest.json \
    --replay --catalog jaffle --catalog-namespace raw
```

## What the project declares

All of it is `meta.leakage` in the yml files; nothing is inferred from column names.

| where | meta |
|---|---|
| `models/staging/sources.yml` `churn_labels` | `role: label_spine`, `entity_keys`, `prediction_ts`, `label_columns` |
| `models/staging/sources.yml` `orders` | `time_column: ordered_at`, `availability_column: loaded_at` |
| `models/staging/sources.yml` `customers` | `role: static` |
| `models/marts/schema.yml` | `role: training_set` on both training models |

## Attribution

This project is derived from [dbt-labs/jaffle_shop_duckdb](https://github.com/dbt-labs/jaffle_shop_duckdb)
(Apache License 2.0, copyright dbt Labs). From that project it keeps the staging pattern, the
`customers`/`orders` entities and the DuckDB profile layout; the Iceberg sources, the churn
labels, the intermediate models, the training models and the warehouse builder are new. The
license is in [LICENSE](LICENSE).
