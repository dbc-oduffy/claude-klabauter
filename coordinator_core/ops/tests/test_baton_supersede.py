"""Tests for the baton.supersede op."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import baton_supersede as op


def _baton(root: Path, stub_id: str, where: str = "state/handoffs") -> Path:
    p = root / where / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "---\nkind: roadmap-baton\ntitle: t\nstub_id: " + stub_id
        + "\nstatus: open\ndeployment_state: ready_to_fire\nbaton_role: work\n---\nbody\n",
        encoding="utf-8",
    )
    return p


def _plain_rmw(path, mutate, *, repo_root):
    old = path.read_text(encoding="utf-8")
    new = mutate(old)
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")
    return new


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    monkeypatch.setattr(op, "locked_rmw", _plain_rmw)


def test_writes_the_field_and_is_idempotent(tmp_path):
    a = _baton(tmp_path, "a-1")
    _baton(tmp_path, "b-1")
    r = op._handler({"baton": "a-1", "superseded_by": "b-1"}, tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is True
    assert "superseded_by: b-1" in a.read_text(encoding="utf-8")
    r = op._handler({"baton": "a-1", "superseded_by": "b-1"}, tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is False


def test_target_may_be_archived(tmp_path):
    a = _baton(tmp_path, "a-1")
    _baton(tmp_path, "old-1", "archive/handoffs/2026-09")
    assert op._handler({"baton": "a-1", "superseded_by": "old-1"}, tmp_path)["applied"]
    assert "superseded_by: old-1" in a.read_text(encoding="utf-8")


def test_refuses_unknown_target_without_writing(tmp_path):
    a = _baton(tmp_path, "a-1")
    before = a.read_text(encoding="utf-8")
    r = op._handler({"baton": "a-1", "superseded_by": "nope-1"}, tmp_path)
    assert r["exit_code"] == 1 and "names no baton" in r["error"]
    assert a.read_text(encoding="utf-8") == before


def test_refuses_self_supersede(tmp_path):
    a = _baton(tmp_path, "a-1")
    before = a.read_text(encoding="utf-8")
    r = op._handler({"baton": "a-1", "superseded_by": "a-1"}, tmp_path)
    assert r["exit_code"] == 1 and "itself" in r["error"]
    assert a.read_text(encoding="utf-8") == before


def test_refuses_unknown_source(tmp_path):
    _baton(tmp_path, "b-1")
    r = op._handler({"baton": "zzz", "superseded_by": "b-1"}, tmp_path)
    assert r["exit_code"] == 1 and "not found" in r["error"]
