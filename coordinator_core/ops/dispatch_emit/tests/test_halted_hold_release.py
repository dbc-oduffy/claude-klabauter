"""halted_hold_release: release reasons, register lines and the plan-origin landed marker."""

from __future__ import annotations

import json
import time
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr

PLAN = "docs/plans/p.md"
SIZING = "state/sizings/s.yaml"
GONE = "state/sizings/gone.yaml"


def _rid(kind: str, age: int, tail: str) -> str:
    return f"{kind}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _sizing(root: Path, rel: str, status: str = "open", deliverable: str = "") -> None:
    body = f"status: {status}\n" + (f"deliverable_id: {deliverable}\n" if deliverable else "")
    _write(root / rel, body)


def _run(root: Path, run_id: str, *, sizing=SIZING, marker=None, plan=PLAN, writes=("a.py",)) -> None:
    rec = root / "state/warp" / run_id
    _write(rec / "run.json", json.dumps({"sizing_rel": sizing}))
    if marker:
        body = {"status": marker, "sizing_rel": sizing}
        if plan:
            body["plan"] = plan
        _write(rec / "halted.json", json.dumps(body))
    _write(rec / "manifest.json", json.dumps({"rows": [{"writes": list(writes)}]}))


def _root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    _sizing(tmp_path, SIZING)
    return tmp_path


def test_superseded_sizing_releases_halted(tmp_path):
    root = _root(tmp_path)
    _sizing(root, SIZING, "superseded")
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h, marker="halted")
    assert hr.release_reason(root, h, _rid("ask", 10, "bbbbbb"), None) == hr.RELEASE_SUPERSEDED


def test_successor_same_sizing_releases(tmp_path):
    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted")
    _run(root, c)
    assert hr.release_reason(root, h, c, None) == hr.RELEASE_SUCCESSOR


def test_successor_same_deliverable_releases(tmp_path):
    root = _root(tmp_path)
    _sizing(root, SIZING, deliverable="dlv-x")
    _sizing(root, "state/sizings/t.yaml", deliverable="dlv-x")
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted")
    _run(root, c, sizing="state/sizings/t.yaml")
    assert hr.release_reason(root, h, c, None) == hr.RELEASE_SUCCESSOR


def test_plan_landing_releases_and_other_plan_holds(tmp_path):
    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("warp", 10, "bbbbbb")
    _run(root, h, marker="halted")
    _run(root, c, sizing="state/sizings/other.yaml")
    assert hr.release_reason(root, h, c, "./" + PLAN) == hr.RELEASE_PLAN_LANDING
    assert hr.release_reason(root, h, c, "docs/plans/q.md") is None
    assert hr.release_reason(root, h, c, None) is None


def test_none_cases_landed_markerless_unrelated_unreadable(tmp_path):
    root = _root(tmp_path)
    c = _rid("ask", 10, "bbbbbb")
    _run(root, c, sizing="state/sizings/other.yaml")
    bare, unrelated, unreadable = (_rid("ask", 400 - i, f"00000{i}") for i in range(3))
    _run(root, bare)
    _run(root, unrelated, marker="halted", sizing="state/sizings/u.yaml", plan="docs/plans/z.md")
    _run(root, unreadable, marker="halted", sizing=GONE, plan="docs/plans/z.md")
    for rid in (bare, unrelated, unreadable):
        assert hr.release_reason(root, rid, c, PLAN) is None, rid
    assert hr.release_reason(root, "missing-20260101T000000-abcdef", c, PLAN) is None


def test_recorded_landed_marker_releases_and_shipped_sizing_frees_halted_and_resumed(tmp_path):
    root = _root(tmp_path)
    c = _rid("ask", 10, "bbbbbb")
    _run(root, c, sizing="state/sizings/other.yaml")
    landed, halted, resumed = (_rid("ask", 400 - i, f"00001{i}") for i in range(3))
    _run(root, landed, marker="landed", sizing="state/sizings/u.yaml", plan="docs/plans/z.md")
    assert hr.release_reason(root, landed, c, PLAN) == hr.RELEASE_RECORDED_LANDED
    _sizing(root, "state/sizings/ship.yaml", "shipped")
    _run(root, halted, marker="halted", sizing="state/sizings/ship.yaml", plan="docs/plans/z.md")
    _run(root, resumed, marker="resumed", sizing="state/sizings/ship.yaml", plan="docs/plans/z.md")
    assert hr.release_reason(root, halted, c, None) == hr.RELEASE_SIZING_SHIPPED
    assert hr.release_reason(root, resumed, c, None) == hr.RELEASE_SIZING_SHIPPED
    _sizing(root, "state/sizings/ship.yaml", "open")
    assert hr.release_reason(root, halted, c, None) is None


def test_resumed_released_by_superseded_sizing(tmp_path):
    root = _root(tmp_path)
    _sizing(root, SIZING, "superseded")
    r = _rid("ask", 300, "aaaaaa")
    _run(root, r, marker="resumed")
    assert hr.release_reason(root, r, None, None) == hr.RELEASE_SUPERSEDED


def test_resumed_released_by_later_successor_marker_landed(tmp_path):
    root = _root(tmp_path)
    r, s = _rid("ask", 300, "aaaaaa"), _rid("ask", 100, "bbbbbb")
    _run(root, r, marker="resumed")
    _run(root, s, marker="landed")
    assert hr.release_reason(root, r, None, None) == hr.RELEASE_SUCCESSOR_LANDED


def test_resumed_released_by_successor_spent_receipt(tmp_path):
    root = _root(tmp_path)
    r, s = _rid("ask", 300, "aaaaaa"), _rid("ask", 100, "bbbbbb")
    _run(root, r, marker="resumed")
    _run(root, s)
    _write(root / ".git/coordinator-warp-run-tokens/spent" / f"{s}.json", json.dumps({"landed": {"sha": "f" * 40}}))
    assert hr.release_reason(root, r, None, None) == hr.RELEASE_SUCCESSOR_LANDED


def test_resumed_holds_when_successor_not_landed_even_sharing_sizing_and_plan(tmp_path):
    root = _root(tmp_path)
    r, s, older = _rid("ask", 300, "aaaaaa"), _rid("ask", 100, "bbbbbb"), _rid("ask", 500, "cccccc")
    _run(root, r, marker="resumed")
    _run(root, s)
    _run(root, older, marker="landed")
    _write(root / ".git/coordinator-warp-run-tokens/spent" / f"{s}.json", json.dumps({"note": "no landed"}))
    assert hr.release_reason(root, r, s, PLAN) is None


def test_release_calls_register_lines(tmp_path):
    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h, marker="halted")
    assert hr.release_calls(root, h) == (
        f"sizing.supersede {{sizing: {SIZING}}}",
        f"dispatch.record_halted_run_landed {{run_id: {h}, sha: <landing sha>}}",
    )
    lone = hr.release_calls(root, "nope-20260101T000000-abcdef")
    assert lone == ("dispatch.record_halted_run_landed {run_id: nope-20260101T000000-abcdef, sha: <landing sha>}",)


def test_mark_plan_origins_landed_flips_only_the_covered_match(tmp_path):
    root = _root(tmp_path)
    match, other_plan, old, partial, landing = (
        _rid("ask", 300, "aaaaaa"), _rid("ask", 290, "bbbbbb"), _rid("ask", 3 * 86400, "cccccc"),
        _rid("ask", 280, "dddddd"), _rid("warp", 10, "eeeeee"),
    )
    _run(root, match, marker="halted")
    _run(root, other_plan, marker="halted", plan="docs/plans/q.md")
    _run(root, old, marker="halted")
    _run(root, partial, marker="halted", writes=("a.py", "b.py"))
    out = hr.mark_plan_origins_landed(root, PLAN, "9" * 40, ["a.py"], exclude_run=landing)
    assert out == [{"run_id": match, "recorded": f"state/warp/{match}/halted.json"}]
    status = lambda rid: json.loads((root / "state/warp" / rid / "halted.json").read_text(encoding="utf-8"))
    assert status(match)["status"] == "landed" and status(match)["sha"] == "9" * 40
    for rid in (other_plan, old, partial):
        assert status(rid)["status"] == "halted", rid


def test_mark_plan_origins_landed_skips_excluded_run(tmp_path):
    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h, marker="halted")
    assert hr.mark_plan_origins_landed(root, PLAN, "9" * 40, ["a.py"], exclude_run=h) == []
