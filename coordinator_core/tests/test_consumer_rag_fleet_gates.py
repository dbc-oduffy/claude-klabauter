"""Consumer-profile gates: orient-assemble fleet-state readers, check_rag_state, setup_rag_decision."""

from __future__ import annotations

import pytest

from coordinator_core import machine_profile as mp
from coordinator_core.ops import check_rag_state as crs
from coordinator_core.ops import setup_rag_decision as srd
from coordinator_core.orient_brief import _work

_ENV = "MACHINE_LOCAL_COORDINATOR_MACHINE_PROFILE"


@pytest.fixture(params=["consumer", "author"])
def profile(request, monkeypatch):
    monkeypatch.setenv(_ENV, request.param)
    mp.reset_cache()
    yield request.param
    mp.reset_cache()


def test_memo_and_rag_readers_gated_by_profile(profile, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("coordinator_core.memo_corpus.memo_corpus_root", lambda root: calls.append("inbox") or str(tmp_path / "none"))
    monkeypatch.setattr(crs, "_read_content_root", lambda h: calls.append("rag") or "")
    _work._memo_points(tmp_path)
    _work._rag_directive()
    assert (calls == []) == (profile == "consumer")


@pytest.mark.parametrize("profile_name,override,expect_read", [
    ("consumer", "on", True),
    ("author", "off", False),
    ("consumer", None, False),
    ("author", None, True),
])
def test_memo_reader_follows_feature_toggle(profile_name, override, expect_read, monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV, profile_name)
    if override:
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_FEATURE_CROSS_REPO_MEMOS", override)
    else:
        monkeypatch.delenv("MACHINE_LOCAL_COORDINATOR_FEATURE_CROSS_REPO_MEMOS", raising=False)
    mp.reset_cache()
    calls = []
    monkeypatch.setattr("coordinator_core.memo_corpus.memo_corpus_root", lambda root: calls.append("inbox") or str(tmp_path / "none"))
    _work._memo_points(tmp_path)
    mp.reset_cache()
    assert bool(calls) == expect_read


def test_check_rag_state_reads_nothing_on_consumer(monkeypatch):
    monkeypatch.setenv(_ENV, "consumer")
    mp.reset_cache()
    for k in ("RAG_STATE", "CLAUDE_RAG_STATE_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(crs, "_rag_configured", lambda: False)
    monkeypatch.setattr(crs, "_read_content_root", lambda h: pytest.fail("pointer read"))
    assert crs.check_rag_state() == ("", 1)
    assert crs.main([]) == 1
    mp.reset_cache()


def test_check_rag_state_reads_pointer_when_configured(monkeypatch):
    monkeypatch.setenv(_ENV, "consumer")
    mp.reset_cache()
    seen = []
    monkeypatch.setattr(crs, "_rag_configured", lambda: True)
    monkeypatch.setattr(crs, "_read_content_root", lambda h: seen.append(h) or "")
    crs.check_rag_state()
    assert seen
    mp.reset_cache()


def test_setup_rag_decision_profiles(profile, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# t\n", encoding="utf-8")
    assert srd.main([]) == 0
    written = "RAG Index" in claude_md.read_text(encoding="utf-8")
    assert written == (profile == "author")
