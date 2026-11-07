"""The registered protocol of the documented-leak set (H3): the Kaggle catalog is out, the source
kinds are named, and the goals are the ones the sample sizes can prove."""

from pathlib import Path

import pytest
import yaml

from bench.docleaks import exclusions
from bench.leakage import freeze

PREREG = Path("bench/docleaks/search-prereg.yaml")


@pytest.fixture(scope="module")
def prereg():
    return yaml.safe_load(PREREG.read_text(encoding="utf-8"))


def test_the_kaggle_catalog_is_excluded_whole_and_pinned(prereg):
    assert prereg["excluded"]["catalog"] == exclusions.CATALOG
    assert prereg["excluded"]["rows"] == exclusions.row_count() == 32
    assert prereg["excluded"]["sha256"] == exclusions.digest()


def test_the_source_kinds_are_named_and_kaggle_is_not_one_of_them(prereg):
    kinds = prereg["source_kinds"]
    assert {"postmortem", "feature_store_docs"} <= set(kinds)
    assert not [k for k in kinds if "kaggle" in k]
    for entry in prereg["searches"]:
        assert entry["kind"] in kinds, entry["id"]
        assert "kaggle" not in entry["query"].lower()
    assert len({e["id"] for e in prereg["searches"]}) == len(prereg["searches"])
    assert {e["kind"] for e in prereg["searches"]} == set(kinds)


def test_every_url_the_catalog_lists_is_excluded_and_so_is_all_of_kaggle():
    urls = exclusions.catalog_urls()
    assert len(urls) > 30
    assert any(not u.startswith("kaggle.com") for u in urls), "the catalog also cites other hosts"
    assert exclusions.is_excluded("https://www.kaggle.com/competitions/anything/discussion/1")
    assert exclusions.is_excluded(
        "https://www.kaggle.com/competitions/ga-customer-revenue-prediction/discussion/67207/"
    )
    assert exclusions.is_excluded(
        "https://cybertraining-dsc.github.io/report/fa20-523-337/project/project#section"
    )
    assert exclusions.is_excluded("WSDM - KKBox's Churn Prediction Challenge")


def test_other_hosts_and_titles_are_not_excluded():
    assert not exclusions.is_excluded(
        "https://docs.feast.dev/getting-started/concepts/point-in-time-joins"
    )
    assert not exclusions.is_excluded("A post-mortem of a recommendation model")
    assert not exclusions.is_excluded("https://example.org/kaggle-like-post")


def test_every_search_is_new_text_and_the_kept_sources_have_a_quote_rule(prereg):
    queries = [e["query"] for e in prereg["searches"]]
    assert len(set(queries)) == len(queries) >= 10
    assert any("at most 120 words" in c for c in prereg["keep_criteria"])
    assert "catalog" in prereg and "quote" in prereg["catalog"]["columns"]
    assert prereg["catalog"]["committed_before"] == "agent B starts a case"


def test_the_stop_rule_is_ten_cases_of_three_pairs_or_eighty_sources(prereg):
    assert "10 kept sources" in prereg["stop"]["keep"]
    assert "3 leaky queries" in prereg["stop"]["keep"] and "3 clean twins" in prereg["stop"]["keep"]
    assert "80 sources" in prereg["stop"]["limit"]
    assert "L2, L4 and L7" in " ".join(prereg["stop"]["keep"].split())


def test_the_goals_follow_from_the_sample_size_and_the_exact_bound(prereg):
    recall, fpr = prereg["goals"]["recall_static"], prereg["goals"]["fpr_static"]
    assert recall["n"] == fpr["n"] == 30
    assert (
        recall["misses_allowed"] == freeze.max_errors_allowed("recall", 30, recall["at_least"]) == 3
    )
    assert (
        fpr["false_positives_allowed"] == freeze.max_errors_allowed("fpr", 30, fpr["at_most"]) == 2
    )
    assert (recall["bound"], fpr["bound"]) == ("lower", "upper")


def test_the_case_brief_and_the_case_format_agree(prereg):
    assert "bench/briefs/docleaks-cases.md" in prereg["cases"]["builder"]
    assert Path("bench/briefs/docleaks-cases.md").is_file()
    for name in (
        "case.yaml",
        "spec.yaml",
        "schema.yaml",
        "generate.py",
        "leaky_N.sql",
        "clean_N.sql",
    ):
        assert name in prereg["cases"]["format"]
