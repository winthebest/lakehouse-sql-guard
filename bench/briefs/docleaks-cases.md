# Brief: rebuild documented temporal leaks as small SQL cases

You are an independent author, and you are an AI agent. A first agent has read human-written sources
(post-mortems, feature-store documentation, articles) and listed, for each source, a concrete way
that information from the future got into a training set. You turn one catalog entry at a time into
a **case**: a small synthetic data set, a declaration of its time columns, and pairs of SQL queries,
each pair holding a leaky query and a clean twin. The cases are used to measure how well automatic
checkers find such leaks. Your cases are reported as "AI rebuilt from human-written sources"; the
catalog author, the readers of the results and the judges are AI agents too.

## Rules of engagement

- You have no web access and no other source than this brief, the catalog entry you were given, the
  passage quoted in it, and the public description of the time declaration format in `docs/spec.md`
  of your clone. The clone holds nothing else on purpose.
- Do not browse GitHub or the web. If you recognise material from a public repository, say so in your
  notes and do not use it.
- You cannot run a checker. You may run your own `generate.py`, and DuckDB on the CSV files it writes.
- Do not invent a leak. A case must follow from what the quoted passage says. If the passage does not
  support three leaky queries and three clean twins, write what it does support, and say in `case.yaml`
  what is missing: a smaller honest case is worth more than a padded one.
- Write all files in English.

## 1. What you receive

One catalog entry: an id (`dl01`, ...), the source kind, its URL (for your notes only), the quoted
passage, the leak type the catalog author assigned (L2, L4 or L7, see section 4), and the tables
and time columns the passage mentions.

## 2. What you hand back: one directory per case

```
<id>/case.yaml       metadata, the queries and their labels (section 5)
<id>/spec.yaml       the time declaration, as described in docs/spec.md
<id>/schema.yaml     the tables of the data set (section 3)
<id>/generate.py     writes the CSV files of the data set; deterministic
<id>/leaky_1.sql ... leaky_N.sql
<id>/clean_1.sql ... clean_N.sql   (clean_k is the twin of leaky_k)
<id>/notes.md        one line per query: why its label is certain, in terms of section 4
```

Quotas for a case: at least **3 leaky** queries and **3 clean twins**, as pairs. Across all your cases,
at least 10 cases with at least 30 leaky and 30 clean queries in total are wanted, and at least 2 cases
for each of L2, L4 and L7 where the sources allow; fewer is acceptable if the sources do not allow more.
Say so, with the number you reached.

## 3. The data set

- `generate.py OUT_DIR` writes one CSV per table, with a header row, using only the standard library
  and a fixed seed (the same output every run). Keep it small: a few hundred to a few thousand rows
  per table, enough that a leaky query and its clean twin return different feature values for some rows.
- `schema.yaml` lists, per table, `columns` (name: DuckDB type), `event_time` (the column that says
  when the row's fact happened), optionally `time_unit` (`s` or `ms` for an integer time column) and
  `availability_time` (the column that says when the row was loaded, or `null`), and a `spine` block:
  the table whose rows are predicted, its `entity_keys` (may be empty for one series), the
  `prediction_time` column and the `label_columns`.
- The data set follows the passage: use its table names and column names where it gives them. Give the
  data the time structure the leak needs (late loads, snapshots, a clock that moves).

## 4. The leak types you may be given

- **L2, the latest row without a bound.** A temporal table is joined, or a "latest row" is selected,
  with no upper time bound relative to the prediction time, so the row chosen may come after it (a
  point-in-time join done wrongly; a "current" value read where the value at the time was needed).
- **L4, late-arriving data.** The only time bound is on event time while the table has a load time, so
  rows that describe the past but were loaded later are counted. The SQL looks time-correct.
- **L7, the wall clock.** A feature depends on the time at which the query is run, so it changes with
  the day the query is run. A filter that only selects which rows to train on is not a feature.

A query is **leaky** if for at least one row a feature depends on something that was not known at that
row's prediction time. It is **clean** if no row's features do. Judge the query, not its intent. The
boundary is strict unless your `spec.yaml` says `boundary: inclusive` for a reason the passage gives.

## 5. case.yaml

```yaml
catalog_id: dl01
title: short title of the source
source_urls: [https://example.org/post-mortem]
source_kind: [postmortem]            # postmortem | feature-store-docs | blog | article
leak_type: L4
description: >
  What the passage says happened, in your words, with the mechanism.
fidelity:
  level: faithful | adapted | thin   # how closely the data set and queries follow the passage
  note: >
    What you had to assume or simplify, and what the passage does not say.
check:
  spine_key: [row_id]                # columns that identify one spine row
queries:
  - {file: leaky_1.sql, label: leaky, leak_type: L4, pair: 1}
  - {file: clean_1.sql, label: clean, pair: 1}
```

## 6. Queries

- One `SELECT` per file (CTEs allowed), DuckDB dialect, reading only the tables of the case.
- It outputs the spine key, the entity keys, the prediction time and the label columns, one row per
  spine row, plus its features, under their own names.
- A clean twin differs from its leaky query by the smallest change that makes it correct, and it is a
  realistic query, not a query with the feature removed.
- The text of a query (comments and names included) must not give its label away: do not use the words
  "leak", "clean", "future" or "label" in a query file.

## 7. How your cases are checked

A script generates your data twice (the output must be identical), runs every query, checks that each
query returns exactly one row per spine row, that a leaky query and its clean twin differ on some rows,
and that every clean query gives the same features on the full data as on the data cut down to what was
knowable at each sampled row's prediction time, while every leaky query changes for at least one row. It
reports what does not hold to you for repair of the data, the queries or the labels, before any detector
result is seen. Labels are never changed by anyone but you, and only before a result has been seen.
