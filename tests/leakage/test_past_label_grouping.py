"""A label summed per (entity, time) and read back from earlier rows is history, also when the
rows come through static joins, and when the sum is also the declared label output itself.

Every pair keeps the leaky twin: the same shape with the current row in the frame, a grouping that
does not name one row per (entity, time), a label that reaches a feature, or no declared delay.
"""

import pytest

from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import static_findings

SPEC = {
    "label_table": "sales",
    "entity_keys": ["book_id"],
    "prediction_ts": "sale_date",
    "label_columns": ["quantity"],
    "time_columns": {"sales": "sale_date"},
    "static_tables": ["books"],
    "label_known_after": 0,
    "output_roles": {"label": ["daily_quantity"]},
}
SCHEMA = {
    "sales": {
        "book_id": "VARCHAR",
        "sale_date": "DATE",
        "quantity": "INTEGER",
        "region": "VARCHAR",
    },
    "books": {"book_id": "VARCHAR", "genre": "VARCHAR"},
    "events": {"book_id": "VARCHAR", "event_ts": "TIMESTAMP", "v": "DOUBLE"},
}


def rules(sql: str, **overrides) -> set[str]:
    spec = LeakageSpec.from_dict({**SPEC, **overrides})
    return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}


FACT = """WITH fact AS (
  SELECT s.book_id, s.sale_date, s.quantity, b.genre
  FROM sales s LEFT JOIN books b ON s.book_id = b.book_id
),
daily AS (
  SELECT book_id, sale_date, SUM(quantity) AS daily_quantity FROM fact GROUP BY book_id, sale_date
),
feat AS (
  SELECT book_id, sale_date, daily_quantity,
         {feature} AS f
  FROM daily
)
SELECT * FROM feat"""
LAG = "LAG(daily_quantity, 1) OVER (PARTITION BY book_id ORDER BY sale_date)"


def fact(feature: str) -> str:
    return FACT.format(feature=feature)


def test_a_lag_of_the_per_day_sum_read_through_a_static_join_is_history():
    assert rules(fact(LAG)) == set()


def test_the_declared_label_output_is_not_a_use_of_the_label():
    assert rules(fact("1")) == set()  # daily_quantity itself reaches the output as the label


@pytest.mark.parametrize(
    "feature",
    [
        # the current day is in the frame
        "AVG(daily_quantity) OVER (PARTITION BY book_id ORDER BY sale_date "
        "ROWS BETWEEN 6 PRECEDING AND CURRENT ROW)",
        "daily_quantity - " + LAG,
        "LEAD(daily_quantity, 1) OVER (PARTITION BY book_id ORDER BY sale_date)",
        "SUM(daily_quantity) OVER (PARTITION BY book_id)",
        # the sum reaches a feature
        "daily_quantity * 2",
        "CASE WHEN daily_quantity > 3 THEN 1 ELSE 0 END",
        # not one row per (entity, time)
        "LAG(daily_quantity, 1) OVER (PARTITION BY book_id ORDER BY book_id)",
        "LAG(daily_quantity, 1) OVER (ORDER BY sale_date)",
    ],
)
def test_the_twins_with_the_current_row_or_the_label_in_a_feature_stay_flagged(feature):
    assert rules(fact(feature)), feature


def test_a_second_output_that_is_not_declared_a_label_stays_flagged():
    sql = fact(LAG).replace(
        "daily_quantity,\n         ", "daily_quantity, daily_quantity AS copy,\n         "
    )
    assert rules(sql), "a copy of the label under another name is a feature"
    assert rules(sql, output_roles={"label": ["daily_quantity", "copy"]}) == set()


def test_without_a_declared_label_the_per_day_sum_is_a_use():
    assert rules(fact(LAG), output_roles={"metadata": ["f"]}, label_columns=["quantity"])


def test_without_label_known_after_the_lag_is_not_history():
    spec = {k: v for k, v in SPEC.items() if k != "label_known_after"}
    got = {f.rule_id for f in static_findings(fact(LAG), LeakageSpec.from_dict(spec), SCHEMA)}
    assert got


def test_grouping_that_leaves_out_the_time_is_not_one_row_per_day():
    sql = (
        fact(LAG)
        .replace("GROUP BY book_id, sale_date", "GROUP BY book_id")
        .replace(
            "SELECT book_id, sale_date, SUM(quantity)",
            "SELECT book_id, MAX(sale_date) AS sale_date, SUM(quantity)",
        )
    )
    assert rules(sql)


def test_a_join_to_a_table_that_is_not_static_is_not_a_plain_source():
    sql = (
        fact(LAG)
        .replace(
            "FROM sales s LEFT JOIN books b ON s.book_id = b.book_id",
            "FROM sales s LEFT JOIN events b ON s.book_id = b.book_id",
        )
        .replace("b.genre", "b.v AS genre")
    )
    assert rules(sql, time_columns={"sales": "sale_date", "events": "event_ts"})
