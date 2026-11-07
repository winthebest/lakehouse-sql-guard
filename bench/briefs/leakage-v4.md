# Brief: write a labelled set of SQL feature queries for temporal-leakage testing

You are an independent author, and you are an AI agent. You will write SQL queries that build
training sets for a prediction model, and you will label each one: does it leak information from the
future, or not? Your queries will later be used to measure how well automatic checkers find such
leaks. Your set is reported as AI-written; the other authors, the readers of the results and the
judges are AI agents too. The value of your work depends on two things: your labels are right, and
your queries are varied and realistic.

## Rules of engagement

- Work only from this brief and the files in the clone you were given. The clone holds nothing
  else on purpose.
- Do not browse GitHub, and do not search the web for this task, for tools that detect leakage in
  SQL, or for benchmarks of such tools. You may read general SQL and DuckDB documentation. If you
  recognise material from a public repository, say so in your notes and do not use it.
- You cannot run a checker and you do not need one. Label by reasoning from the world described
  below. To check syntax you may create empty DuckDB tables from the DDL in section 2 and run your
  queries against them (zero rows is fine).
- Do not try to fool or to please a particular checker. You do not know how any of them work.
- Write all files in English.

## 1. The task

Your assignment message names **one of two worlds**: W1 (a subscription company and a churn model) or
W2 (a learning app and a model of whether an answer is correct). Read the whole brief, but write
queries only for your world. Everything below that is per world is marked.

Write **template files** (format in section 5). Quotas, **for your world**:

- at least **124 clean** templates (the share of clean queries a checker wrongly flags can be
  bounded at 0.05 only with a set this large);
- at least **10 leaky templates for each of L1 to L7** (so at least 70 leaky ones);
- at least 30% of all templates must have a dbt-style shape (defined in section 6).

More is welcome; quality and variety matter more than volume. Each template is labelled once, with
exactly one of `clean`, `L1`, ..., `L7`. A leaky template has one dominant cause: if two causes
apply, pick the one that is the root cause and keep the other out of the query.

## 2. The worlds

In both worlds a model predicts, for a row at a **prediction time** `t`, a label. All timestamps are
naive UTC. The data below is the state of the warehouse **today**, after everything has been loaded.
A training query runs on that data, but it must only use what was already known at each row's
prediction time `t`.

### 2.1 World W1: subscriptions and churn

A company sells subscriptions. A model predicts, for a user at `t`, whether the user churns in the
following 14 days.

```sql
CREATE TABLE entities (            -- static; one row per user
  user_id BIGINT, country VARCHAR, signup_ts TIMESTAMP);

CREATE TABLE events (              -- one row per purchase
  event_id BIGINT, user_id BIGINT,
  event_ts TIMESTAMP,              -- when the purchase happened
  ingested_at TIMESTAMP,           -- when the row was loaded into the warehouse
  amount DOUBLE);

CREATE TABLE user_stats (          -- a full recompute every midnight, appended
  user_id BIGINT,
  updated_at TIMESTAMP,            -- the midnight of the recompute
  total_spend DOUBLE, n_orders BIGINT);   -- lifetime values as of that midnight

CREATE TABLE plan_history (        -- slowly changing dimension, type 2
  user_id BIGINT, plan VARCHAR,    -- free | basic | pro
  monthly_fee DOUBLE,
  valid_from TIMESTAMP,            -- the plan took effect
  valid_to TIMESTAMP,              -- the next plan took effect; NULL while it is still open
  ingested_at TIMESTAMP);          -- when this version's row was first loaded

CREATE TABLE labels (              -- the prediction spine: one row per (user, prediction time)
  user_id BIGINT,
  label_ts TIMESTAMP,              -- the prediction time t
  churned BIGINT);                 -- 1 if the user churned within 14 days after label_ts

CREATE TABLE user_outcomes (       -- facts observed after a user churned; churners only
  user_id BIGINT, label_ts TIMESTAMP,
  refund_after_churn DOUBLE, days_to_churn BIGINT);
```

How the data arrives:

- **events**: a purchase on day `d` is normally loaded at midnight at the end of day `d` (the
  midnight that starts day `d+1`). About 10% arrive late: 1 to 7 extra days. A row is never loaded
  more than 8 days after its `event_ts`, and never before it.
- **user_stats**: every midnight the table gets one new row per user (lifetime spend and order count
  over events loaded so far). A row stamped `m` becomes known just after `m`: at `t = m` it is not
  known yet (the boundary is strict, section 3).
- **plan_history**: a change of plan is loaded the night after it takes effect, or up to 7 nights
  later (never before the version it replaces). When a version is loaded, the previous version of
  that user is closed in the same load: its `valid_to` becomes visible then, not before. So today's
  `valid_to` of a version tells you when the next plan began, and that was **not known** until
  the next version was loaded. A version still open today has `valid_to IS NULL`. The oldest
  versions began a few days before the first load. A row is loaded less than 8 days after its
  `valid_from`.
- **timestamps**: `events.ingested_at`, `plan_history.ingested_at` and `user_stats.updated_at` are
  always exact midnights. `events.event_ts`, `plan_history.valid_from` and `labels.label_ts` carry a
  time of day, except that about a third of the `label_ts` values are exact midnights, so ties
  between a prediction time and a load or stats stamp happen.
- **labels**: prediction times lie in a window of a few weeks, some exactly at midnight, some at
  other times of day. A label (`churned`) is final 14 days after its `label_ts`; before that it
  cannot be known. The spine and the outcomes were loaded long after the fact.
- **user_outcomes**: facts that only exist after a churn; never known at `t` for that row.

Sizes: about 300 users; `events` and `user_stats` hold low tens of thousands of rows each, `labels`
a few hundred rows over about 17 days of prediction times. Your queries should keep most spine rows
(at least dozens of rows, spread over several prediction times); a query that returns a handful of
rows cannot be judged.

### 2.1.1 Spec of W1

This is the declaration the W1 queries are judged against. It says which table is the spine, which
column of each table is its time column, which tables have an ingestion time, and how long data may
take to arrive.

```yaml
# Spec for the synthetic churn world of the held-out v3 templates: which table is the label
# spine, which columns carry time, and how long data takes to arrive. Qualified relation names
# (dbt-style `database.schema.model`) are mapped to the warehouse tables by `relations`.
label_table: labels
entity_keys: [user_id]
prediction_ts: label_ts
label_columns: [churned]
time_columns:
  events: event_ts
  user_stats: updated_at
  plan_history: valid_from
availability_columns:
  events: ingested_at
  plan_history: ingested_at
boundary: strict
# A row is loaded less than this long after it happened (a guarantee of the data generator).
max_ingest_delay:
  events: 8 days
  plan_history: 8 days
label_known_after: 14 days
static_tables: [entities]
outcome_columns:
  - user_outcomes.refund_after_churn
  - user_outcomes.days_to_churn
# A query may name a table by its bare catalog name (`events`) or by any name listed here.
relations:
  analytics.staging.stg_events: events
  analytics.staging.stg_user_stats: user_stats
  analytics.staging.stg_entities: entities
  analytics.staging.stg_user_outcomes: user_outcomes
  analytics.snapshots.plan_history_snapshot: plan_history
  analytics.marts.fct_training_spine: labels
  staging.stg_events: events
  staging.stg_user_stats: user_stats
  staging.stg_entities: entities
  staging.stg_user_outcomes: user_outcomes
  snapshots.plan_history_snapshot: plan_history
  marts.fct_training_spine: labels
dialect: duckdb
```

Qualified names: in a query you may write any table either by its bare name (`events`) or by any of
the qualified names under `relations`, as a dbt project would (`analytics.staging.stg_events`,
`staging.stg_events`). They are the same table. Use only names from the list. (If you write your
queries against empty DuckDB tables to check syntax, create schemas and views for the qualified
names, or substitute the bare names for the check only.)

### 2.2 World W2: a learning app

Users answer items (exercises). A model predicts, for an answer given at `t`, whether it is correct.
The table of answers is at once the **prediction spine** (one row per answer), the **history** of
every user (earlier answers are the main source of features) and the holder of a fact that only
exists after the answer. Times in this world are **integers**, in different units per table.

```sql
CREATE TABLE items (                -- static; one row per item
  item_id BIGINT, topic VARCHAR, level BIGINT);

CREATE TABLE interactions (         -- one row per answer: the spine AND the history
  interaction_id BIGINT, user_id BIGINT, item_id BIGINT,
  ts_ms BIGINT,                     -- when the answer was given: the prediction time t, epoch MILLISECONDS
  correct BIGINT,                   -- 1 if the answer was correct: the label
  answer_shown_at BIGINT,           -- when the correct answer was shown to the user, epoch milliseconds
  loaded_ms BIGINT);                -- when this row was loaded into the warehouse, epoch milliseconds

CREATE TABLE item_stats (           -- recomputed every midnight per item, appended
  item_id BIGINT,
  updated_ms BIGINT,                -- the midnight of the recompute, epoch milliseconds
  loaded_ms BIGINT,                 -- when this row was loaded, epoch milliseconds
  difficulty DOUBLE,                -- 1 - (correct answers + 1) / (answers + 2) over the answers loaded so far
  n_attempts BIGINT);               -- answers loaded so far

CREATE TABLE market_prices (        -- one series, no entity key: a price every 15 minutes
  ts_s BIGINT,                      -- epoch SECONDS
  price DOUBLE);
```

How the data arrives:

- **interactions**: an answer on day `d` is normally loaded at midnight at the end of day `d` (the
  midnight that starts day `d+1`). About 10% arrive late: 1 or 2 extra nights. A row is loaded less
  than 3 days after its `ts_ms`, and never before it. `loaded_ms` is always an exact midnight;
  `ts_ms` carries any millisecond of the day. `correct` is final the moment the row is stamped.
  `answer_shown_at` is 30 seconds to 10 minutes after `ts_ms`: it is a fact about the outcome of the
  answer, and it is not known before that moment.
- **item_stats**: every midnight, one new row per item that has been answered (answers loaded so far).
  A row stamped `updated_ms = m` is normally loaded at `m`; about 10% are loaded 1 or 2 nights later.
  A row is loaded less than 3 days after its `updated_ms`. At `t = m` it is not known yet (the boundary
  is strict, section 3).
- **market_prices**: a price row every 900 seconds. All the rows of a day are loaded at the midnight
  that ends that day, so a row is loaded less than 2 days after its `ts_s`. The series has no entity
  key: every user sees the same prices.
- **items**: static.
- **units**: `interactions.ts_ms`, `interactions.answer_shown_at`, `interactions.loaded_ms`,
  `item_stats.updated_ms` and `item_stats.loaded_ms` count **milliseconds** since 1970-01-01;
  `market_prices.ts_s` counts **seconds**. Convert yourself where you mix them.

Sizes: about 120 users and 30 items; `interactions` holds a few thousand rows over 30 days,
`item_stats` under a thousand, `market_prices` 2,880. Your queries should keep most spine rows (at
least dozens of rows, spread over several prediction times); a query that returns a handful of rows
cannot be judged.

#### 2.2.1 Spec of W2

```yaml
# Spec for the synthetic learning-app world of the second held-out set: the table of answers is
# both the prediction spine and the event history of every user, its times are integer epoch
# milliseconds, and one table is a single series without an entity key. Qualified relation names
# (dbt-style `database.schema.model`) are mapped to the warehouse tables by `relations`.
label_table: interactions
entity_keys: [user_id]
prediction_ts: ts_ms
label_columns: [correct]
time_columns:
  interactions: ts_ms
  item_stats: updated_ms
  market_prices: ts_s
# Integer time columns count epoch time in these units.
time_units:
  interactions: milliseconds
  item_stats: milliseconds
  market_prices: seconds
availability_columns:
  interactions: loaded_ms
  item_stats: loaded_ms
boundary: strict
# A row is loaded less than this long after the time it is stamped with (a guarantee of the data
# generator). A day of market prices is loaded at the midnight that ends the day.
max_ingest_delay:
  interactions: 3 days
  item_stats: 3 days
  market_prices: 2 days
# A label (`correct`) is final the moment its row is stamped.
label_known_after: 0
static_tables: [items]
# Known only after the answer was given, from the same row's table.
outcome_columns:
  - interactions.answer_shown_at
# A query may name a table by its bare catalog name (`interactions`) or by any name listed here.
relations:
  analytics.staging.stg_interactions: interactions
  analytics.staging.stg_item_stats: item_stats
  analytics.staging.stg_market_prices: market_prices
  analytics.staging.stg_items: items
  analytics.marts.fct_training_spine: interactions
  staging.stg_interactions: interactions
  staging.stg_item_stats: item_stats
  staging.stg_market_prices: market_prices
  staging.stg_items: items
  marts.fct_training_spine: interactions
dialect: duckdb
```

Qualified names work as in W1: use any name under `relations`, or the bare table name.

## 3. What "known at prediction time" means

### 3.1 The churn world (W1)

For a row with prediction time `t`, a fact is **known** when:

- it sits in a table with an ingestion time (`events`, `plan_history`) and `ingested_at < t`;
- it sits in `user_stats` and `updated_at < t`;
- it comes from `entities` (static; always known);
- it is a label of an earlier spine row whose `label_ts` is at least 14 days before `t`
  (strictly: `label_ts + 14 days < t`); a younger label is not final yet;
- it never comes from `user_outcomes`, and never from the row's own `churned`.
- the spine itself is known only up to `t`: spine rows with a later `label_ts` do not exist yet, so a
  value read from them as a feature (`MAX(label_ts)` in the select list, the next prediction time)
  depends on the future (label L1). A time-split cutoff is different: a `WHERE` that compares the
  spine's own `label_ts` with `(SELECT MAX(label_ts) FROM labels)` (or `MIN`, a quantile, a median,
  with an interval added or subtracted outside the subquery) only chooses which rows to train on, and
  is accepted. Anything else built on such a subquery (compared with `event_ts` or `ingested_at`,
  used as a feature, `=`) is judged like any other read of the spine. So is a cutoff read from a CTE,
  even one that only copies the spine: read it from the spine itself.

The boundary is **strict**: a fact stamped exactly `t` is not known at `t`.

An event-time bound alone (`event_ts < t`) is **not enough** for a table with an ingestion time:
late rows describing the past were not loaded yet. Such a query is safe only if it also bounds the
ingestion time (`ingested_at < t`), or if its event-time bound is earlier than `t` by at least the
declared maximum delay (`event_ts < t - INTERVAL 8 DAY` for `events` and `plan_history`).

#### The SCD2 table `plan_history` (W1)

The plan known at `t` is the version with the newest `valid_from` among the rows with
`ingested_at < t`. `valid_to` of a version is known at `t` only if the **next version was loaded
before `t`**. Today's table cannot tell you that: a version that the next one replaced shows its
final `valid_to` whether or not that replacement had been loaded at `t`.

So any use of `valid_to` that can see a version closed by a load at or after `t` is **leaky, label
L4** (late-arriving data). This includes the usual as-of lookup

```sql
valid_from < t AND (valid_to IS NULL OR valid_to >= t)          -- also COALESCE(valid_to, <far future>) > t
```

**even together with `ingested_at < t`**. Example: the next plan took effect before `t` but was loaded
at or after `t`; today the old version has `valid_to < t` and drops out of the lookup, while the new
version fails `ingested_at < t`. The feature is empty where it should hold the old plan. Using the
final `valid_to` of a version as a value (tenure, days to the next change) is leaky for the same
reason.

Clean forms:

- the newest version with `ingested_at < t`, ranked by `valid_from` (for example
  `row_number() OVER (PARTITION BY ... ORDER BY valid_from DESC) = 1`), without `valid_to`;
- the version in effect at `t - 8 days`: `valid_from < t - INTERVAL 8 DAY` and
  `(valid_to IS NULL OR valid_to >= t - INTERVAL 8 DAY)`.

`valid_from < t` alone, with no `ingested_at` bound, is also L4: the version in effect may not have
been loaded yet. Any of these is a good leaky or clean template; just keep to the rule above when you
label.


### 3.2 The learning-app world (W2)

For a row with prediction time `t` (the `ts_ms` of the row), a fact is **known** when:

- it sits in `interactions` in a row with `ts_ms < t` **and** `loaded_ms < t`; such a row's `correct`
  is known (a label is final when its row is stamped); its `answer_shown_at` is known only if it is
  itself `< t`;
- it sits in `item_stats` and `loaded_ms < t`;
- it sits in `market_prices` and the midnight that ends the day of its `ts_s` is before `t`;
- it comes from `items` (static; always known);
- it is a value of the row itself other than its label, its `answer_shown_at` and its `loaded_ms`
  (its `user_id`, `item_id` and `ts_ms` are the prediction's inputs);
- the spine itself is known only up to `t`: answers with a later `ts_ms` do not exist yet. A time-split
  cutoff is different: a `WHERE` that compares the spine's own `ts_ms` with
  `(SELECT MAX(ts_ms) FROM interactions)` (or `MIN`, a quantile, a median, with an interval-sized
  constant added or subtracted outside the subquery) only chooses which rows to train on, and is
  accepted. Anything else built on such a subquery is judged like any other read of the spine. A cutoff
  read from a CTE is judged like any other read of that CTE.

The boundary is **strict**: a fact stamped exactly `t` is not known at `t`.

An event-time bound alone is **not enough** for a table with a load time: late rows describing the past
were not loaded yet. A query is safe only if it also bounds the load time (`loaded_ms < t`), or if its
event-time bound is earlier than `t` by at least the maximum delay: 3 days for `interactions` and
`item_stats`, 2 days for `market_prices` (which has no load-time column).

Because `interactions` is both spine and history, one table plays two roles in a query: the row being
predicted, and the earlier answers of the same user or item. The earlier answers obey the rules above;
the row being predicted is the prediction.

## 4. The taxonomy (labels)

A query is **leaky** if, for at least one training row, a feature value depends on something that
was not known at that row's prediction time `t`. Its label is the type of that dependence:

- **L1, reads the future.** A feature uses facts at or after `t` by their own time: a window that
  extends past `t`, the next event or the next stats row, bounds written the wrong way round.
- **L2, picks the latest row without a bound.** A temporal table is joined, or a "latest row" is
  selected, with no upper time bound relative to `t`, so the row chosen may come after `t`.
- **L3, aggregates without an upper bound.** A temporal table is aggregated with no upper bound
  relative to `t`.
- **L4, late-arriving data.** The only time bound is on event time, while the table has a load time
  and the bound is not at least the maximum delay before `t`. The SQL looks time-correct; the data was
  not all there yet. (In W1 this also covers the use of `valid_to` described in section 3.1.)
- **L5, boundary error.** The bound is off at the edge: the fact stamped `t` is counted although it is
  not yet known, a shift forward of less than a day, comparing a time-zone aware value with a naive one,
  a date cast that pulls in the rest of the day, a unit conversion that is off by a factor.
- **L6, uses the label or the outcome.** A feature depends on the label, on an outcome fact (W1:
  `user_outcomes`; W2: `answer_shown_at`), or on labels that are not final yet.
- **L7, reads the wall clock.** A feature depends on the time at which the query is run. Its value
  changes with the day the query is run. A filter that only chooses which spine rows to train on is not
  a feature: it is clean.

A query is **clean** if no row's features depend on anything unknown at its `t`. Clean queries include
correct point-in-time joins, bounds on load time, event-time bounds at least the maximum delay before
`t`, labels that are final, static tables, constants, and wall-clock use that only selects rows.

Labelling discipline:

- Judge the query, not its intent. Read every join, subquery, CTE and window frame.
- A query is leaky if **any** of its renderings (section 5) is leaky; make every rendering of a
  template have the same label. Parameters change values (windows, columns, join kinds), never the
  label.
- Avoid ambiguity. If you cannot decide the label with certainty from section 3, rewrite the query
  until you can, or drop it.
- Do not build a leaky template whose only leak is something a clean template of yours also does.
  Tricky clean twins of leaky shapes are encouraged; just keep both labels certain.

## 5. Template format

One file per template, named `<id>.sql`. The id is lowercase letters, digits and underscores, starts
with `hv4w1_` (W1) or `hv4w2_` (W2), and is unique: for example `hv4w1_l4_scd2_event_time_lookup`,
`hv4w2_clean_loaded_bounded_count`.

```
-- leak: clean | L1 | L2 | L3 | L4 | L5 | L6 | L7
-- output_roles: label=<columns>; metadata=<columns>     (optional)
-- param <name>: value | value | ...      (zero or more lines)
<one SQL SELECT, DuckDB dialect, with {name} placeholders>
```

- The header lines come first. After the first non-header line, no more `-- leak`, `-- output_roles` or
  `-- param` lines. Ordinary `--` comments in the body are fine and encouraged: say why the label is
  what it is.
- Every combination of parameter values becomes one rendering (a case). Keep it to at most about
  16 combinations per template and make the values meaningfully different (bounds, window sizes,
  column choices, join kinds), not cosmetic.
- Placeholders use Python `str.format`: write a literal brace as `{{` or `}}`.
- Each query is a single `SELECT` (CTEs allowed). It must output, besides its features, the key columns
  of the spine under their own names, one row per spine row (or per (user, prediction time)): W1
  `user_id`, `label_ts`, `churned`; W2 `user_id`, `ts_ms`, `correct`.
- No `INSERT`, DDL, or multiple statements. No reading of files or of anything outside the tables of
  your world.

### Output roles

A query may output a column that is not a feature: the label itself, a label that the query derives
(for example a value taken from a later row), or run metadata (a load or build timestamp). Declare
such columns the way a data engineer would in a schema file, in the optional header

```
-- output_roles: label=next_return; metadata=_loaded_at
```

`label` and `metadata` columns are not features. A role names columns of the query's output, one role
once, a column under one role only. Leakage is about the **feature** columns: the label of your
template (`clean` or `L1`..`L7`) is decided by them. A declaration is a claim that is checked against
your notes: declare a column as `label` or `metadata` only if it truly is one, and never to excuse a
feature. A query whose feature reuses a label or a later value is leaky whether or not a column
is declared. Use the header where it is natural; most templates will not need it.

## 6. dbt-style shapes (at least 30% of templates)

A template is dbt-style if any of these holds:

- it names a relation by a qualified name from `relations` (two or three parts);
- (W1 only) it reads `plan_history` (the SCD2 table), by either name;
- it chains at least three CTEs (staging, intermediate, mart layers), the way models are layered.

Make these realistic: `stg_` style renaming CTEs, surrogate keys, deduplication, incremental-style
filters, spine-driven joins, `ref`-like layering. Both clean and leaky templates should be dbt-style, in
every leak type where it makes sense.

## 7. Variety

- Different leak types differ in the mechanism, not only in the numbers. Within a type, vary the
  mechanism.
- Cover every table of your world.
- Choose your own families of queries and vary them on purpose: different structure, different layering
  (a flat query, a chain of CTEs, a model read twice), different SQL features of DuckDB. For every
  family you choose, write both clean and leaky versions, so that the family does not tell the label.
- Include a stratum of leaks that are hard to see in the data: a leak that only a few rows of the
  warehouse show (a narrow gap between the bound and the real cut-off, a rare late row, one user, one
  prediction time). Label it leaky from section 3, not from whether the data happens to show it.
- Clean templates should not all be small variants of one shape; include look-alikes of leaky shapes
  whose label is clean for a definite reason.
- Your set will be compared structurally with other query sets. Near-copies of one another (the same
  query with renamed aliases or other constants) count as one template: do not pad.

## 8. How your labels are checked

After you hand back, your labels are audited mechanically; you run nothing yourself. For every
rendering of a clean or L1 to L5 template, the query is run on the final tables and again on the
tables as they were knowable at each prediction time, built from the data generator's own load times
(section 3: what is loaded and stamped before `t`, the spine up to `t`, labels hidden unless final, no
outcomes). If a feature differs for a row at `t`, the query used something unknown at `t`. A clean
template must never differ; a leaky template must differ in at least one rendering. Templates the audit
cannot decide (L6, L7, anything that reads the wall clock, anything that does not run or returns too
few rows) are listed as such and are not checked this way. A time-split cutoff is checked: its value is
computed once from the final spine and used in both runs, so the rest of the query is audited as
written.

A disagreement is returned to you with the row where it shows. Labels are never changed by anyone but
you, and only before any detector result has been seen.

## 9. What to hand back

1. The `.sql` files, in one directory.
2. `notes.md`: one line per template, `<id>: <label>: <the reason the label is certain, in terms of
   section 3>`. For a template with an `output_roles` header, also the reason each declared column is
   a label or metadata. This file is how your labels are audited.
3. A count table: templates per label and the number of dbt-style ones.

Queries that fail to parse or to run on the schema will be returned to you for **syntax repair only**;
labels and shapes cannot change after a result has been seen.
