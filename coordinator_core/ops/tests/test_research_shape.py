"""research.shape: the value_class x appetite table, the scouts floor, preflight and ordering."""

from __future__ import annotations

import pytest

from coordinator_core.ops.research_shape import shape

TABLE = {
    ("scouts", "small"): "scouts",
    ("scouts", "medium"): "scouts",
    ("scouts", "large"): "corpus",
    ("corpus", "small"): "scouts",
    ("corpus", "medium"): "corpus",
    ("corpus", "large"): "deep",
    ("deep", "small"): "corpus",
    ("deep", "medium"): "deep",
    ("deep", "large"): "deep",
}


@pytest.mark.parametrize(("vc", "ap"), list(TABLE))
def test_tier_for_every_pair(vc, ap):
    assert shape({"value_class": vc, "appetite": ap})["tier"] == TABLE[(vc, ap)]


def test_absent_appetite_reads_medium():
    assert shape({"value_class": "corpus"})["tier"] == "corpus"


@pytest.mark.parametrize("src", ["repo", "structured", "notebooklm"])
def test_floor_fires_at_scouts(src):
    r = shape({"value_class": "scouts", "appetite": "small", "sources": [src]})
    assert r["tier"] == "corpus"
    assert "floor" in r["reason"]


def test_floor_never_fires_for_web():
    r = shape({"value_class": "scouts", "appetite": "small", "sources": ["web"]})
    assert r["tier"] == "scouts" and r["pipelines"] == ["scouts"]
    assert "floor" not in r["reason"]


@pytest.mark.parametrize("vc", ["scouts", "corpus", "deep"])
@pytest.mark.parametrize("ap", ["small", "medium", "large"])
def test_preflight_first_iff_notebooklm(vc, ap):
    with_nlm = shape({"value_class": vc, "appetite": ap, "sources": ["web", "notebooklm"]})
    assert with_nlm["pipelines"][0] == "nlm-preflight"
    without = shape({"value_class": vc, "appetite": ap, "sources": ["web", "repo"]})
    assert "nlm-preflight" not in without["pipelines"]


def test_corpus_keeps_sources_order_and_defaults_to_web():
    r = shape({"value_class": "corpus", "sources": ["structured", "repo", "web"]})
    assert r["pipelines"] == ["structured", "repo", "web"]
    assert shape({"value_class": "corpus"})["pipelines"] == ["web"]


def test_deep_is_unblock():
    assert shape({"value_class": "deep", "appetite": "medium"})["pipelines"] == ["unblock"]


def test_deep_runs_a_named_source_with_no_specialist_ahead_of_the_team():
    r = shape({"value_class": "corpus", "appetite": "large", "sources": ["notebooklm", "web", "structured"]})
    assert r["tier"] == "deep"
    assert r["pipelines"] == ["nlm-preflight", "notebooklm", "structured", "unblock"]


def test_unknown_value_class_refused():
    with pytest.raises(ValueError):
        shape({"value_class": "bogus"})
    with pytest.raises(ValueError):
        shape({})


def test_deepest_flags_the_repo_pipeline_only():
    assert shape({"value_class": "corpus", "sources": ["web", "repo"], "depth": "deepest"})["flags"] == {
        "repo": {"deepest": "true"}
    }
    assert shape({"value_class": "corpus", "sources": ["web"], "depth": "deepest"})["flags"] == {}
    assert shape({"value_class": "corpus", "sources": ["repo"], "depth": "deeper"})["flags"] == {}
