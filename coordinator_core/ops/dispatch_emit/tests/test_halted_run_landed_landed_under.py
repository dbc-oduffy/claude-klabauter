"""dispatch.record_halted_run_landed ``landed_under``: a never-ran run is credited by the run that landed its work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op

NEVER = "warp-2026-11-04-research-20261104T000000-aaaaaa"
OTHER = "warp-2026-11-04-refire-20261104T010000-bbbbbb"
SHA = "a" * 40


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture(autouse=True)
def _git_root(tmp_path):
    (tmp_path / ".git").mkdir()


def _marker(root: Path, rid: str, status: str, sha: str = SHA) -> None:
    d = root / "state" / "warp" / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "halted.json").write_text(json.dumps({"status": status, "sha": sha}), encoding="utf-8")


def _tree(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in (root / "state").rglob("*") if p.is_file()}


def test_never_ran_run_is_credited_by_the_landed_run(tmp_path):
    _marker(tmp_path, OTHER, "landed")
    before = _tree(tmp_path)
    out = op._handler({"run_id": NEVER, "sha": SHA[:12], "landed_under": OTHER}, tmp_path)
    assert out["ok"] and out["sha"] == SHA and out["landed_under"] == OTHER, out
    assert out["close"]["status"] == "not-applicable"
    assert _tree(tmp_path) == before


def test_sibling_status_landed_run_also_credits(tmp_path):
    st = tmp_path / c.status_rel(OTHER)
    st.parent.mkdir(parents=True, exist_ok=True)
    st.write_text(json.dumps({"parent_id": "P", "status": "landed", "sha": SHA}), encoding="utf-8")
    out = op._handler({"run_id": NEVER, "sha": SHA, "landed_under": OTHER}, tmp_path)
    assert out["ok"] and out["sha"] == SHA, out


def test_without_landed_under_the_never_ran_refusal_stands(tmp_path):
    _marker(tmp_path, OTHER, "landed")
    out = op._handler({"run_id": NEVER, "sha": SHA}, tmp_path)
    assert not out["ok"] and "never ran" in out["error"]


@pytest.mark.parametrize("status,sha", [("halted", SHA), ("landed", "b" * 40)])
def test_named_run_not_landed_at_the_sha_refuses(tmp_path, status, sha):
    _marker(tmp_path, OTHER, status, sha)
    out = op._handler({"run_id": NEVER, "sha": SHA, "landed_under": OTHER}, tmp_path)
    assert not out["ok"] and "never ran" in out["error"]


def test_unknown_landed_under_run_refuses(tmp_path):
    out = op._handler({"run_id": NEVER, "sha": SHA, "landed_under": "no-such-run"}, tmp_path)
    assert not out["ok"] and "never ran" in out["error"]


def test_non_string_landed_under_refuses(tmp_path):
    out = op._handler({"run_id": NEVER, "sha": SHA, "landed_under": 5}, tmp_path)
    assert not out["ok"] and "landed_under" in out["error"]
