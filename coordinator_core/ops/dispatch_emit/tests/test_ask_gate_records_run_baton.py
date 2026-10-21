"""ask_gate._handler records the baton an M+ verdict minted in the run's record dir, and only then."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_gate
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_BATON_FILE, read_run_baton, record_dir

REL = "state/sizings/2026-10-01-gate.yaml"
RUN = "warp-fixture-run"
ACCEPTED = {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}
BATON = "state/handoffs/baton-fixture.md"


class _FakeDocNew:
    class SizingMintRefused(Exception):
        pass

    @staticmethod
    def mint_baton_from_sizing(sizing_rel, repo_root, baton=None):
        return {"id": "baton-fixture", "path": BATON}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_gate, "_load_doc_new", lambda: _FakeDocNew)
    monkeypatch.setattr(ask_gate, "main_worktree_root", lambda p: p)
    return tmp_path


def _put(repo, tshirt, route, accepted):
    doc = {
        "schema": "sizing-object",
        "name": "gate fixture",
        "intent": "exercise the gate",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": route,
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": accepted},
    }
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_m_plus_verdict_with_run_id_records_the_baton(repo):
    _put(repo, "M", "plan", ACCEPTED)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out["arm"] == "m_plus" and out["baton"]["path"] == BATON
    assert read_run_baton(repo, RUN) == BATON


def test_m_plus_verdict_without_run_id_records_nothing(repo):
    _put(repo, "M", "plan", ACCEPTED)
    out = ask_gate._handler({"sizing_path": REL}, repo_root=repo)
    assert out["arm"] == "m_plus"
    assert not (repo / "state" / "warp").exists() or not list(repo.rglob(RUN_BATON_FILE))


def test_s_verdict_with_run_id_records_no_baton(repo):
    _put(repo, "S", "spec-dispatch", None)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out.get("baton") is None
    assert not (record_dir(repo, RUN) / RUN_BATON_FILE).exists()


def test_halted_verdict_with_run_id_records_no_baton(repo):
    _put(repo, "XL", "pm-decision", {"pm_quote": "y"})
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out["halt"]["kind"]
    assert not (record_dir(repo, RUN) / RUN_BATON_FILE).exists()


def test_xs_verdict_with_baton_records_the_given_baton(repo):
    _put(repo, "XS", "dispatch", ACCEPTED)
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["baton"] = BATON
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN, "baton": BATON, "writes": ["a.py"]}, repo_root=repo)
    assert out["arm"] == "xs" and not out.get("halt")
    assert read_run_baton(repo, RUN) == BATON


def test_xs_verdict_without_baton_records_nothing(repo):
    _put(repo, "XS", "dispatch", ACCEPTED)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN, "writes": ["a.py"]}, repo_root=repo)
    assert out["arm"] == "xs"
    assert not (record_dir(repo, RUN) / RUN_BATON_FILE).exists()


def test_xs_foreign_baton_halts_and_records_nothing(repo):
    _put(repo, "XS", "dispatch", ACCEPTED)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN, "baton": BATON, "writes": ["a.py"]}, repo_root=repo)
    assert out["halt"]["kind"]
    assert not (record_dir(repo, RUN) / RUN_BATON_FILE).exists()


def test_refused_write_leaves_the_verdict_standing(repo, monkeypatch):
    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(ask_gate, "write_run_baton", boom)
    _put(repo, "M", "plan", ACCEPTED)
    out = ask_gate._handler({"sizing_path": REL, "run_id": RUN}, repo_root=repo)
    assert out["arm"] == "m_plus" and out["baton"]["path"] == BATON
