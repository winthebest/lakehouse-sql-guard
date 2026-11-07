"""The Kaggle harness in development mode: INFO counts, the
registration and cases it follows, and what a dev run refuses or skips."""

import json
from pathlib import Path

import pytest
import yaml

from bench.kaggle import evaluate as ev


def cli(findings, replay=None):
    entry = {"file": "q.sql", "verdict": "leaky", "score": 0.9, "leak_types": []}
    return json.dumps([{**entry, "findings": findings, **({"replay": replay} if replay else {})}])


def test_demoted_findings_are_counted_apart_from_scored_ones_and_notes():
    findings = [
        {"rule_id": "L1", "evidence": {}},
        {"rule_id": "L6", "evidence": {"non_feature_only": True, "outputs": ["churned"]}},
        {"rule_id": "L7", "evidence": {"non_feature_only": True}},
        {"rule_id": "SPEC", "evidence": {}},
        {"rule_id": "L3"},
    ]
    result = ev.parse_cli_output(cli(findings), "", 1)
    assert (result.findings, result.info_non_feature) == (2, 2)
    assert {"findings", "info_non_feature"} <= set(ev.CSV_COLUMNS)


def test_the_replay_reason_is_kept_for_every_verdict():
    reason = "integer prediction time without a declared unit"
    result = ev.parse_cli_output(cli([], {"status": "unverifiable", "reason": reason}), "", 0)
    assert result.replay_reason == reason and result.replay_status == "unverifiable"
    assert "replay_reason" in ev.CSV_COLUMNS


def test_a_dev_run_does_not_need_the_registered_source_tree(monkeypatch):
    prereg = yaml.safe_load(ev.PREREG_PATH.read_text(encoding="utf-8"))
    prereg["checker"]["src_tree"] = "0" * 40
    asked = []

    def fake_git(*args):
        asked.append(args)
        return ""

    monkeypatch.setattr(ev, "git", fake_git)
    assert ev.preflight(prereg, dev=True) == []
    assert not any("rev-parse" in a for a in asked)


def test_a_run_that_is_not_dev_still_checks_the_registered_tree(monkeypatch):
    prereg = yaml.safe_load(ev.PREREG_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(ev, "git", lambda *args: "deadbeef" if args[0] == "rev-parse" else "")
    problems = ev.preflight(prereg)
    assert any("is not the registered" in p for p in problems)


def test_the_cases_digest_follows_the_cases_dir_that_was_given(tmp_path, monkeypatch):
    prereg = yaml.safe_load(ev.PREREG_PATH.read_text(encoding="utf-8"))
    monkeypatch.setattr(ev, "git", lambda *args: "")
    (tmp_path / "kl00").mkdir()
    (tmp_path / "kl00" / "case.yaml").write_text("x: 1\n")
    problems = ev.preflight(prereg, dev=True, cases_dir=tmp_path)
    assert any("cases digest" in p for p in problems)


def test_main_accepts_the_new_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(ev, "run", lambda args: seen.update(vars(args)) or 0)
    ev.main(["--dev", "--cases-dir", "c", "--prereg", "p.yaml", "--specs-dir", "s", "--tag", "x"])
    assert seen["dev"] and seen["cases_dir"] == "c" and seen["prereg"] == "p.yaml"
    assert (
        seen["specs_dir"] == "s" and seen["tag"] == "x" and seen["rules_tag"] == "rules-frozen-v3"
    )
    seen.clear()
    ev.main([])
    assert (
        seen["dev"] is False
        and seen["specs_dir"] is None
        and seen["cases_dir"] == str(ev.CASES_DIR)
    )


@pytest.mark.parametrize("flag", ["--cases-dir"])
def test_check_cases_takes_a_cases_dir(flag):
    from bench.kaggle import check_cases

    assert Path(check_cases.__file__).is_file()
    source = Path(check_cases.__file__).read_text(encoding="utf-8")
    assert flag in source
