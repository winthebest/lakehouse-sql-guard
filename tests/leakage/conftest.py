import pytest

from sqlguard.leakage.spec import LeakageSpec

SPEC = {
    "label_table": "labels",
    "entity_keys": ["user_id"],
    "prediction_ts": "label_ts",
    "label_columns": ["churned"],
    "time_columns": {"events": "event_ts", "user_stats": "updated_at", "clicks": "click_ts"},
    "availability_columns": {"events": "ingested_at"},
    "static_tables": ["entities"],
    "outcome_columns": ["user_outcomes.refund_after_churn"],
}

SCHEMA = {
    "labels": {"user_id": "BIGINT", "label_ts": "TIMESTAMP", "churned": "BIGINT"},
    "events": {
        "event_id": "BIGINT",
        "user_id": "BIGINT",
        "event_ts": "TIMESTAMP",
        "ingested_at": "TIMESTAMP",
        "amount": "DOUBLE",
    },
    "user_stats": {
        "user_id": "BIGINT",
        "updated_at": "TIMESTAMP",
        "total_spend": "DOUBLE",
        "n_orders": "BIGINT",
    },
    "clicks": {"user_id": "BIGINT", "click_ts": "TIMESTAMPTZ", "page": "VARCHAR"},
    "entities": {"user_id": "BIGINT", "country": "VARCHAR", "signup_ts": "TIMESTAMP"},
    "user_outcomes": {
        "user_id": "BIGINT",
        "label_ts": "TIMESTAMP",
        "refund_after_churn": "DOUBLE",
        "days_to_churn": "BIGINT",
    },
    "misc": {"user_id": "BIGINT", "x": "DOUBLE"},
}


@pytest.fixture
def spec() -> LeakageSpec:
    return LeakageSpec.from_dict(SPEC)


@pytest.fixture
def rules_of(spec):
    """Return the set of rule ids raised by static analysis of a query."""
    from sqlguard.leakage.validator import static_findings

    def run(sql: str) -> set[str]:
        return {f.rule_id for f in static_findings(sql, spec, SCHEMA)}

    return run
