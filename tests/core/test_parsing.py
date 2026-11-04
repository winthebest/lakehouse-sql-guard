import pytest

from sqlguard.core.findings import Decision, Finding, Severity
from sqlguard.core.parsing import SqlParseError, parse, parse_single


def test_multi_statement_payload_is_split():
    kinds = [s.kind for s in parse("SELECT 1; COMMIT; DROP TABLE orders")]
    assert kinds == ["Select", "Commit", "Drop"]


def test_comments_and_case_do_not_hide_statements():
    kinds = [s.kind for s in parse("select 1 /* ; */; -- note\n dElEtE FROM orders")]
    assert kinds == ["Select", "Delete"]


def test_delete_where_detection():
    assert parse("DELETE FROM orders")[0].ast.args.get("where") is None
    assert parse("DELETE FROM orders WHERE id = 2")[0].ast.args.get("where") is not None


def test_spark_call_falls_back_to_command():
    stmt = parse("CALL system.expire_snapshots(table => 'db.t')", dialect="spark")[0]
    assert stmt.kind == "Command"


@pytest.mark.parametrize("bad", ["", "   ", "SELEC FROM"])
def test_invalid_sql_raises(bad):
    with pytest.raises(SqlParseError):
        parse(bad)


def test_parse_single_rejects_multiple():
    with pytest.raises(SqlParseError):
        parse_single("SELECT 1; SELECT 2")


def test_decision_strictest_and_fail_closed():
    assert Decision.strictest([Decision.ALLOW, Decision.ASK]) is Decision.ASK
    assert Decision.strictest([]) is Decision.DENY


def test_finding_to_dict():
    data = Finding("L2", Severity.HIGH, "msg", evidence={"x": 1}).to_dict()
    assert data["severity"] == "high" and data["evidence"] == {"x": 1}
