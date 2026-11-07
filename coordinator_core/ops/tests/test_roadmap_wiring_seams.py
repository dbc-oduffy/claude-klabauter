"""Tests for the roadmap.wiring_seams op."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import roadmap_wiring_seams as op


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))


def _plan(root: Path, name: str, parts: str) -> None:
    p = root / "docs" / "plans" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: {name}\nstatus: approved\nplan_id: pln-{name}\nwiring:\n{parts}---\nbody\n", encoding="utf-8")


def _baton(root: Path, name: str, blocked_by: str = "[]", roadmap: str = "rm-1") -> None:
    p = root / "state" / "handoffs" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nkind: roadmap-seed\ntitle: {name}\nhandoff_id: hnd-{name}\nroadmap_id: {roadmap}\n"
        f"deployment_state: ready_to_fire\ngoverning_plan: docs/plans/{name}.md\nblocked_by: {blocked_by}\n---\nbody\n",
        encoding="utf-8",
    )


def _part(pid: str, **seams: list[str]) -> str:
    lines = [f"  - id: {pid}\n    status: not_built\n"]
    lines += [f"    {k}: [{', '.join(repr(v) for v in vs)}]\n" for k, vs in seams.items()]
    return "".join(lines)


def _roadmap(tmp_path: Path, consumer_entry: str, producer_parts: str, blocked_by: str) -> dict:
    _plan(tmp_path, "up", producer_parts)
    _plan(tmp_path, "down", _part("c", consumes_external=[consumer_entry]))
    _baton(tmp_path, "up")
    _baton(tmp_path, "down", blocked_by=blocked_by)
    return op._handler({}, tmp_path)


def test_clean_roadmap_has_no_findings(tmp_path):
    r = _roadmap(tmp_path, "pln-up:art", _part("p", produces=["art"]), "[hnd-up]")
    assert (r["ok"], r["count"], r["findings"], r["unchecked"]) == (True, 0, [], [])


def test_unproduced_artifact_is_refused(tmp_path):
    r = _roadmap(tmp_path, "pln-up:art", _part("p", produces=["other"]), "[hnd-up]")
    assert [f["code"] for f in r["findings"]] == ["external-unproduced"]
    assert r["ok"] is False and r["findings"][0]["part"] == "c"


def test_plan_without_wiring_parts_is_unproduced(tmp_path):
    r = _roadmap(tmp_path, "pln-up:art", "  - id: p\n    status: not_built\n", "[hnd-up]")
    assert [f["code"] for f in r["findings"]] == ["external-unproduced"]


def test_baton_not_upstream_is_refused(tmp_path):
    r = _roadmap(tmp_path, "pln-up:art", _part("p", produces=["art"]), "[]")
    assert [f["code"] for f in r["findings"]] == ["external-not-upstream"]


def test_upstream_is_transitive_through_a_middle_baton(tmp_path):
    _plan(tmp_path, "up", _part("p", produces=["art"]))
    _plan(tmp_path, "mid", _part("m"))
    _plan(tmp_path, "down", _part("c", consumes_external=["pln-up:art"]))
    _baton(tmp_path, "up")
    _baton(tmp_path, "mid", blocked_by="[hnd-up]")
    _baton(tmp_path, "down", blocked_by="[hnd-mid]")
    assert op._handler({}, tmp_path)["findings"] == []


def test_both_defects_report_both_codes(tmp_path):
    r = _roadmap(tmp_path, "pln-up:art", _part("p", produces=["other"]), "[]")
    assert sorted(f["code"] for f in r["findings"]) == ["external-not-upstream", "external-unproduced"]


def test_bare_plan_id_checks_upstream_only(tmp_path):
    assert _roadmap(tmp_path, "pln-up", _part("p"), "[hnd-up]")["findings"] == []


def test_repo_path_sha_is_reported_not_checked(tmp_path):
    r = _roadmap(tmp_path, "example-game-repo:scripts/x.py@d9eb50041e", _part("p"), "[]")
    assert r["findings"] == []
    assert r["unchecked"][0]["reason"] == "repo-path-sha" and r["unchecked"][0]["entry"].startswith("example-game-repo:")


def test_unknown_plan_id_is_unchecked_not_refused(tmp_path):
    r = _roadmap(tmp_path, "pln-ghost:art", _part("p"), "[]")
    assert r["findings"] == []
    assert r["unchecked"][0]["reason"] == "plan-not-in-roadmap"


def test_roadmap_id_filter_drops_other_roadmaps(tmp_path):
    _roadmap(tmp_path, "pln-up:art", _part("p", produces=["other"]), "[hnd-up]")
    assert op._handler({"roadmap_id": "rm-2"}, tmp_path)["count"] == 0
    assert op._handler({"roadmap_id": "rm-1"}, tmp_path)["count"] == 1


def test_missing_repo_root_is_an_error():
    assert op._handler({}, None)["exit_code"] == 1


def test_op_spawns_no_process(tmp_path, monkeypatch):
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    r = _roadmap(tmp_path, "pln-up:art", _part("p", produces=["art"]), "[hnd-up]")
    assert r["ok"] is True
