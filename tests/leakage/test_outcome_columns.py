"""An outcome column is the target wherever it sits: in a table that holds only outcomes the whole
table is post-outcome data, in the label table or a temporal table only the listed columns are.
A feature computed from a column known only after the outcome (tenure up to the churn date) is
a leak of the target. Each clean query is paired with one that reads a listed column."""

from sqlguard.leakage.analysis import analyze
from sqlguard.leakage.spec import LeakageSpec
from sqlguard.leakage.validator import analysis_findings, validate

from .conftest import SCHEMA, SPEC

SCHEMA_WITH_OUTCOMES = {
    **SCHEMA,
    "labels": {**SCHEMA["labels"], "segment": "VARCHAR", "refund_at": "TIMESTAMP"},
}
KEYS = "l.user_id, l.label_ts, l.churned"


def spec_with(**overrides) -> LeakageSpec:
    return LeakageSpec.from_dict({**SPEC, **overrides})


def lineage(sql: str, schema=SCHEMA_WITH_OUTCOMES, **overrides) -> dict[str, list[str]]:
    """feature -> the source columns the lineage check names for it"""
    found = analysis_findings(analyze(sql, spec_with(**overrides), schema))
    return {f.location: f.evidence["sources"] for f in found if f.rule_id == "L6" and f.evidence}


# --- outcome columns of the label table --------------------------------------------------------

LABEL_TABLE_OUTCOME = {"outcome_columns": ["labels.refund_at"]}


def test_a_plain_column_of_the_label_table_is_not_post_outcome_data():
    sql = f"SELECT {KEYS}, l.segment FROM labels l"
    assert lineage(sql, **LABEL_TABLE_OUTCOME) == {}


def test_the_listed_outcome_column_of_the_label_table_is_flagged():
    sql = f"SELECT {KEYS}, l.segment, l.refund_at IS NOT NULL AS refunded FROM labels l"
    assert lineage(sql, **LABEL_TABLE_OUTCOME) == {"refunded": ["labels.refund_at"]}
    sql = f"SELECT {KEYS}, DATEDIFF('day', l.label_ts, l.refund_at) AS days_to_refund FROM labels l"
    assert lineage(sql, **LABEL_TABLE_OUTCOME) == {"days_to_refund": ["labels.refund_at"]}


def test_a_feature_mixing_a_plain_and_an_outcome_column_is_flagged_for_the_outcome_only():
    sql = f"SELECT {KEYS}, l.segment || CAST(l.refund_at AS VARCHAR) AS mixed FROM labels l"
    assert lineage(sql, **LABEL_TABLE_OUTCOME) == {"mixed": ["labels.refund_at"]}


def test_the_label_table_outcome_does_not_clear_a_dirty_query():
    sql = f"SELECT {KEYS}, l.segment, e.amount FROM labels l JOIN events e ON e.user_id = l.user_id"
    report = validate(sql, spec_with(**LABEL_TABLE_OUTCOME), schema=SCHEMA_WITH_OUTCOMES)
    assert "L2" in report.leak_types


# --- outcome columns of a temporal table -------------------------------------------------------

TEMPORAL_OUTCOME = {"outcome_columns": ["user_stats.n_orders"]}
STATS = """SELECT {keys}, s.{column} AS feat FROM labels l
JOIN user_stats s ON s.user_id = l.user_id AND s.updated_at < l.label_ts"""


def test_only_the_listed_column_of_a_temporal_table_is_flagged():
    clean = STATS.format(keys=KEYS, column="total_spend")
    assert lineage(clean, **TEMPORAL_OUTCOME) == {}
    leaky = STATS.format(keys=KEYS, column="n_orders")
    assert lineage(leaky, **TEMPORAL_OUTCOME) == {"feat": ["user_stats.n_orders"]}


# --- a table that holds only outcomes ---------------------------------------------------------


def test_a_table_of_outcomes_only_is_post_outcome_data_whatever_column_is_read():
    # `days_to_churn` is not listed; the table is the spec's outcome table, so it all is
    sql = f"""SELECT {KEYS}, o.days_to_churn FROM labels l
JOIN user_outcomes o ON o.user_id = l.user_id AND o.label_ts = l.label_ts"""
    assert lineage(sql) == {"days_to_churn": ["user_outcomes.days_to_churn"]}
    listed = lineage(sql.replace("o.days_to_churn", "o.refund_after_churn"))
    assert listed == {"refund_after_churn": ["user_outcomes.refund_after_churn"]}


# --- a feature computed from the time of the outcome ------------------------------------------

CUSTOMERS = {
    **SCHEMA,
    "customers": {
        "customer_id": "BIGINT",
        "signup_ts": "TIMESTAMP",
        "churn_ts": "TIMESTAMP",
        "obs_ts": "TIMESTAMP",
        "is_churned": "BOOLEAN",
    },
    "invoices": {"customer_id": "BIGINT", "invoice_ts": "TIMESTAMP", "amount": "DOUBLE"},
}
CUSTOMER_SPEC = {
    "label_table": "customers",
    "entity_keys": ["customer_id"],
    "prediction_ts": "churn_ts",
    "label_columns": ["is_churned"],
    "time_columns": {"invoices": "invoice_ts"},
    "outcome_columns": ["customers.churn_ts"],
}
CUSTOMER_KEYS = "c.customer_id, c.churn_ts, c.is_churned"


def customers(sql: str, **overrides):
    spec = LeakageSpec.from_dict({**CUSTOMER_SPEC, **overrides})
    return analysis_findings(analyze(sql, spec, CUSTOMERS))


def test_tenure_up_to_a_cutoff_that_is_not_the_outcome_time_is_clean():
    sql = (
        f"SELECT {CUSTOMER_KEYS}, DATEDIFF('day', c.signup_ts, c.obs_ts) AS tenure FROM customers c"
    )
    assert [f for f in customers(sql) if f.rule_id == "L6"] == []


def test_tenure_computed_from_the_outcome_time_is_flagged_with_the_reason():
    sql = f"""SELECT {CUSTOMER_KEYS},
  DATEDIFF('day', c.signup_ts, COALESCE(c.churn_ts, c.obs_ts)) AS tenure FROM customers c"""
    (finding,) = [f for f in customers(sql) if f.rule_id == "L6"]
    assert finding.location == "tenure"
    assert finding.evidence["sources"] == ["customers.churn_ts"]
    assert (
        "customers.churn_ts" in finding.message
        and "only known after the outcome" in finding.message
    )


def test_passing_the_outcome_time_through_as_the_prediction_time_is_not_a_leak():
    sql = f"SELECT {CUSTOMER_KEYS} FROM customers c"
    assert [f for f in customers(sql) if f.rule_id == "L6"] == []


def test_comparing_a_history_table_with_the_outcome_time_is_a_bound_not_a_feature_source():
    sql = f"""SELECT {CUSTOMER_KEYS}, SUM(i.amount) AS spend FROM customers c
JOIN invoices i ON i.customer_id = c.customer_id AND i.invoice_ts < c.churn_ts
GROUP BY c.customer_id, c.churn_ts, c.is_churned"""
    assert [f for f in customers(sql) if f.rule_id == "L6"] == []


def test_a_label_derived_feature_keeps_the_label_wording():
    sql = f"SELECT {CUSTOMER_KEYS}, CAST(c.is_churned AS INT) * 2 AS feat FROM customers c"
    found = [f for f in customers(sql) if f.rule_id == "L6" and "sources" in f.evidence]
    assert [f.message for f in found] == [
        "feature 'feat' is derived from the target or post-outcome data"
    ]
