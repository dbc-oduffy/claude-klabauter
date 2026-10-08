"""Tests for the baton.awaiting_gate_recheck op."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops import baton_awaiting_gate_recheck as op


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(op, "main_worktree_root", lambda r: Path(r))
    monkeypatch.setattr(op, "_landed", lambda shas, wt: {s for s in shas if s.startswith("abc")})


def _h(root: Path, name: str, extra: str, state: str = "awaiting_gate", created: str = "2026-10-01") -> None:
    p = root / "state" / "handoffs" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nkind: roadmap-seed\ntitle: {name}\nhandoff_id: hnd-{name}-aaaaaa\n"
        f"deployment_state: {state}\ncreated: {created}\n{extra}---\nbody\n",
        encoding="utf-8",
    )


def test_rows_report_holder_age_condition_and_draft(tmp_path):
    _h(tmp_path, "w", "blocking_notes: Gated on Example-Game-Repo's women's pipeline\n")
    _h(tmp_path, "s", "gate_holder: some-em\ngate_dependency: landed in abc1234 upstream\n", created="2026-10-05")
    _h(tmp_path, "r", "", state="ready_to_fire")
    r = op._handler({"today": "2026-10-08"}, tmp_path)
    rows = {x["handoff_id"]: x for x in r["rows"]}
    assert r["count"] == 2 and "hnd-r-aaaaaa" not in rows
    w, s = rows["hnd-w-aaaaaa"], rows["hnd-s-aaaaaa"]
    assert (w["holder"], w["holder_source"], w["parked_days"], w["machine_condition_met"]) == ("Example-Game-Repo", "inferred", 7, None)
    assert (s["holder"], s["holder_source"], s["parked_days"], s["machine_condition_met"]) == ("some-em", "field", 3, True)
    assert s["memo_draft"].startswith("cross-repo-memo draft gate-recheck-") and "--to some-em" in s["memo_draft"]
    assert r["rows"][0]["handoff_id"] == "hnd-w-aaaaaa"


def test_blocked_by_shipped_or_not_and_holder_filter(tmp_path):
    _h(tmp_path, "up", "", state="shipped")
    _h(tmp_path, "g1", "blocked_by: [hnd-up-aaaaaa]\ngate_holder: a\n")
    _h(tmp_path, "g2", "blocked_by: [hnd-g1-aaaaaa]\ngate_holder: b\n")
    rows = {x["handoff_id"]: x for x in op._handler({}, tmp_path)["rows"]}
    assert rows["hnd-g1-aaaaaa"]["machine_condition_met"] is True
    assert rows["hnd-g2-aaaaaa"]["machine_condition_met"] is False
    only = op._handler({"holder": "B"}, tmp_path)
    assert [x["handoff_id"] for x in only["rows"]] == ["hnd-g2-aaaaaa"]


def test_unknown_holder_leaves_a_placeholder_and_nothing_is_written(tmp_path):
    _h(tmp_path, "n", "gate_dependency: someone later\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    row = op._handler({}, tmp_path)["rows"][0]
    assert row["holder"] is None and "<gate-holder-em>" in row["memo_draft"]
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_landed_resolves_abbreviated_loose_objects_without_a_spawn(tmp_path, monkeypatch):
    monkeypatch.undo()
    obj = tmp_path / ".git" / "objects" / "ab"
    obj.mkdir(parents=True)
    (obj / ("c" * 38)).write_bytes(b"x")
    assert op._landed(["abccc", "ab" + "c" * 20, "ab123456"], tmp_path) == {"abccc", "ab" + "c" * 20}
