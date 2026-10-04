"""Author-only behaviours refuse or skip quietly on a consumer machine profile."""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core import machine_profile as mp


@pytest.fixture()
def consumer(tmp_path, monkeypatch):
    reg = tmp_path / "reg"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "consumer")
    for var in ("RAG_STATE", "CLAUDE_RAG_STATE_FILE"):
        monkeypatch.delenv(var, raising=False)
    mp.reset_cache()
    yield
    mp.reset_cache()


def test_publishing_refusal_names_feature_and_verb(consumer):
    msg = mp.feature_refusal("publishing")
    assert "off on this machine" in msg and "\n" not in msg
    assert "machine-local set coordinator.feature.publishing on" in msg


def test_memo_refusal_names_feature_and_verb(consumer):
    assert "machine-local set coordinator.feature.cross_repo_memos on" in mp.feature_refusal("cross_repo_memos")


def test_refusal_absent_on_author(monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE", "author")
    mp.reset_cache()
    assert mp.feature_refusal("publishing") is None
    mp.reset_cache()


@pytest.mark.parametrize("feature", ["publishing", "cross_repo_memos"])
def test_explicit_feature_on_lifts_refusal_on_consumer(consumer, monkeypatch, feature):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_" + feature.upper(), "on")
    mp.reset_cache()
    assert mp.feature_refusal(feature) is None


@pytest.mark.parametrize("modname,fn,params", [
    ("percolate_run", "_percolate_run", {}),
    ("percolate_validate", "_percolate_validate_store", {}),
    ("percolate_check_inverse_drift", "_percolate_check_inverse_drift", {}),
    ("percolate_build_token_index", "_percolate_build_token_index", {}),
    ("percolate_ci_smoke_check", "_percolate_run_ci_smoke_check", {}),
    ("percolate_identity_check", "_percolate_run_identity_check", {}),
])
def test_percolate_ops_refuse_on_consumer(consumer, modname, fn, params):
    import importlib

    handler = getattr(importlib.import_module(f"coordinator_core.ops.{modname}"), fn)
    with pytest.raises(ValueError, match="off on this machine"):
        out = handler(params)
        if asyncio.iscoroutine(out):
            asyncio.run(out)


def test_memo_send_refuses_on_consumer(consumer):
    from coordinator_core.ops.fleet.memo_send import _memo_send

    result = _memo_send({"dry_run": True, "topic": "t"}, repo_root=None)
    assert result["exit_code"] == 1


@pytest.mark.parametrize("modname,fn", [
    ("memo_draft", "_memo_draft"),
    ("memo_compose", "_memo_compose"),
])
def test_memo_draft_compose_refuse_on_consumer(consumer, caplog, modname, fn):
    import importlib

    handler = getattr(importlib.import_module(f"coordinator_core.ops.fleet.{modname}"), fn)
    result = handler(
        {"dry_run": True, "topic": "t", "to": "x-em", "title": "T", "summary": "S",
         "body": "b", "kind": "fyi"},
        repo_root=None,
    )
    assert result["exit_code"] == 1
    assert "cross-repo memos is off on this machine" in caplog.text


def test_preflight_scratch_publish_refuses_on_consumer(consumer, capsys):
    from coordinator_core.ops import percolate_preflight_scratch_publish as p

    assert p.main(["--self-test"]) == 2
    assert "off on this machine" in capsys.readouterr().err


def test_setup_rag_decision_skips_on_consumer(consumer, capsys):
    from coordinator_core.ops import setup_rag_decision as srd

    assert srd.main([]) == 0
    assert "consumer profile" in capsys.readouterr().out


def test_memo_surface_readers_skip_on_consumer(consumer, monkeypatch, tmp_path):
    from coordinator_core.ops import check_rag_state as crs
    from coordinator_core.orient_brief import _work

    def boom(*a, **k):
        raise AssertionError("fleet state read on consumer")

    monkeypatch.setattr("coordinator_core.memo_corpus.memo_corpus_root", boom)
    monkeypatch.setattr(crs, "_read_content_root", boom)
    assert not _work._memo_points(tmp_path)
    assert not _work._rag_directive()


def test_check_rag_state_honours_injected_state_on_consumer(consumer, monkeypatch):
    from coordinator_core.ops import check_rag_state as crs

    monkeypatch.setenv("RAG_STATE", "fresh")
    assert crs._rag_configured()


@pytest.mark.parametrize("modname", [
    "workday_start_cross_repo_memo_surface",
    "workday_start_cross_repo_memo_outbox_surface",
])
def test_workday_memo_surfaces_silent_on_consumer(consumer, capsys, modname):
    import importlib

    assert importlib.import_module(f"coordinator_core.ops.{modname}").main([]) == 0
    assert capsys.readouterr().out == ""


def test_memo_features_independent_of_publishing(consumer, monkeypatch):
    monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_PUBLISHING", "on")
    mp.reset_cache()
    assert mp.feature_refusal("cross_repo_memos")
    assert mp.feature_refusal("publishing") is None
