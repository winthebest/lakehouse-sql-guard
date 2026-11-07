"""Replay compares the output columns that are features.

A column the spec declares a label or run metadata is not a feature, so a difference confined to it
between the pinned and the unpinned run says nothing about leakage into features. A difference in
any feature column, or in which rows exist, still counts: the declaration cannot hide those.
"""

import pytest

from sqlguard.leakage.replay import Replayer, ReplayStatus

from .test_replay_time_types import build_catalog, spec_for

# `next_churned` looks at the next row of the user: absent from the pinned run, present in the full
# one, which is what a label made with LEAD does
BASE = """SELECT l.user_id, l.label_ts, COUNT(e.event_id) AS n,
       LEAD(l.churned) OVER (PARTITION BY l.user_id ORDER BY l.label_ts) AS next_churned
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND {bound}
GROUP BY l.user_id, l.label_ts, l.churned"""
CLEAN, LEAKY = "e.event_ts < l.label_ts", "e.event_ts <= l.label_ts + INTERVAL 1 DAY"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    return build_catalog(tmp_path_factory.mktemp("roles"), "timestamp")


def replay(catalog, sql: str, **extra):
    return Replayer(catalog, spec_for("timestamp", **extra)).run(sql)


def test_a_difference_in_a_declared_label_column_is_not_leakage(catalog):
    sql = BASE.format(bound=CLEAN)
    undeclared = replay(catalog, sql)
    assert undeclared.status is ReplayStatus.MISMATCH
    assert set(undeclared.mismatched_columns) == {"next_churned"}
    declared = replay(catalog, sql, output_roles={"label": ["next_churned"]})
    assert declared.status is ReplayStatus.PASS and declared.rows_mismatched == 0
    assert declared.rows_compared == undeclared.rows_compared > 0


def test_a_difference_in_a_declared_metadata_column_is_not_leakage(catalog):
    sql = BASE.format(bound=CLEAN)
    result = replay(catalog, sql, output_roles={"metadata": ["next_churned"]})
    assert result.status is ReplayStatus.PASS


def test_a_difference_outside_the_declared_feature_list_is_not_leakage(catalog):
    sql = BASE.format(bound=CLEAN)
    result = replay(catalog, sql, output_roles={"features": ["n"]})
    assert result.status is ReplayStatus.PASS


def test_a_difference_in_a_feature_column_still_counts_beside_a_declared_label(catalog):
    sql = BASE.format(bound=LEAKY)
    result = replay(catalog, sql, output_roles={"label": ["next_churned"]})
    assert result.status is ReplayStatus.MISMATCH
    assert set(result.mismatched_columns) == {"n"}


MIXED = """SELECT l.user_id, l.label_ts, COUNT(e.event_id) AS n,
       LEAD(l.churned) OVER w AS next_churned,
       COALESCE(LEAD(l.churned) OVER w, 0) + COUNT(e.event_id) AS mixed
FROM labels l LEFT JOIN events e ON e.user_id = l.user_id AND e.event_ts < l.label_ts
GROUP BY l.user_id, l.label_ts, l.churned
WINDOW w AS (PARTITION BY l.user_id ORDER BY l.label_ts)"""


def test_a_column_that_mixes_the_label_into_a_feature_is_still_compared(catalog):
    result = replay(catalog, MIXED, output_roles={"label": ["next_churned"]})
    assert result.status is ReplayStatus.MISMATCH
    assert "mixed" in result.mismatched_columns


def test_which_rows_exist_still_counts_when_every_other_column_is_declared_away(catalog):
    inner = BASE.format(bound=CLEAN)
    sql = f"SELECT * FROM ({inner}) WHERE next_churned IS NOT NULL"
    result = replay(catalog, sql, output_roles={"label": ["next_churned"]})
    assert result.status is ReplayStatus.MISMATCH
    assert "<row>" in "".join(result.mismatched_columns) or result.rows_mismatched > 0


def test_the_label_columns_of_the_spec_are_still_compared_without_a_declaration(catalog):
    sql = """SELECT l.user_id, l.label_ts, l.churned,
       LEAD(l.churned) OVER (PARTITION BY l.user_id ORDER BY l.label_ts) AS next_churned
FROM labels l"""
    result = replay(catalog, sql)
    assert result.status is ReplayStatus.MISMATCH


def test_a_features_list_that_names_a_column_the_query_lacks_hides_nothing(catalog):
    # the lookahead is `next_churned`; the list spells it `next_chrned` (and so omits it)
    sql = BASE.format(bound=CLEAN)
    typo = replay(catalog, sql, output_roles={"features": ["n", "next_chrned"]})
    assert typo.status is ReplayStatus.MISMATCH
    assert "next_churned" in typo.mismatched_columns
    exact = replay(catalog, sql, output_roles={"features": ["n"]})
    assert exact.status is ReplayStatus.PASS
