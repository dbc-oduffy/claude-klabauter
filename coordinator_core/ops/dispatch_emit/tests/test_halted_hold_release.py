"""halted_hold_release: release reasons, register lines and the plan-origin landed marker."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release as hr

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

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


def test_superseded_marker_releases_while_halted_with_open_sizing_holds(tmp_path):
    root = _root(tmp_path)
    c = _rid("ask", 10, "bbbbbb")
    sup, held = _rid("ask", 300, "aaaaaa"), _rid("ask", 290, "cccccc")
    _run(root, sup, marker="superseded")
    _run(root, held, marker="halted", sizing="state/sizings/other.yaml", plan="docs/plans/z.md")
    assert hr.release_reason(root, sup, c, None) == hr.RELEASE_MARKER_SUPERSEDED
    assert hr.RELEASE_MARKER_SUPERSEDED == "marker-superseded"
    assert hr.release_reason(root, held, c, None) is None


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


def _released(root: Path, run_id: str, marker: str, released) -> None:
    _run(root, run_id, marker=marker)
    path = root / "state/warp" / run_id / "halted.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["released"] = released
    path.write_text(json.dumps(data), encoding="utf-8")


def test_recorded_criterion_met_release_frees_halted_only(tmp_path):
    root = _root(tmp_path)
    record = {"reason": hr.RELEASE_CRITERION_MET, "sha": "a" * 40}
    h, r = _rid("ask", 300, "aaaaaa"), _rid("ask", 200, "bbbbbb")
    _released(root, h, "halted", record)
    _released(root, r, "resumed", record)
    assert hr.release_reason(root, h, None, None) == hr.RELEASE_CRITERION_MET
    assert hr.release_reason(root, r, None, None) is None


def test_recorded_owner_not_live_release_frees_halted_only(tmp_path):
    root = _root(tmp_path)
    record = {"reason": hr.RELEASE_OWNER_NOT_LIVE, "sha": "a" * 40}
    h, r = _rid("ask", 300, "aaaaaa"), _rid("ask", 200, "bbbbbb")
    _released(root, h, "halted", record)
    _released(root, r, "resumed", record)
    assert hr.RELEASE_OWNER_NOT_LIVE == "owner-not-live"
    assert hr._LEGACY_RELEASE_PHANTOM == "phantom-no-own-hunk-owner-gone"
    assert hr.release_reason(root, h, None, None) == hr.RELEASE_OWNER_NOT_LIVE
    assert hr.release_reason(root, r, None, None) is None


def test_malformed_released_record_holds(tmp_path):
    root = _root(tmp_path)
    for i, bad in enumerate(("criterion-met-at-head", {"reason": "other", "sha": "a"}, ["x"], {})):
        h = _rid("ask", 300 + i, f"a0000{i}")
        _released(root, h, "halted", bad)
        assert hr.release_reason(root, h, None, None) is None


def test_release_calls_register_lines(tmp_path):
    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h, marker="halted")
    assert hr.release_calls(root, h) == (
        f"sizing.supersede {{sizing_path: {SIZING}, note: <why the run is no longer owed>}}",
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


def _git(root: Path, *args: str, stdin: str = "") -> str:
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, input=stdin,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def _kept_repo(tmp_path: Path, head_text: str, kept_text: str):
    """Real repo: base commit of `a.py`, then HEAD holds `head_text`; a halted run withheld `a.py` keeping `kept_text`."""
    base_text = "def f():\n    return 1\n"
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "a.py", base_text)
    _git(tmp_path, "add", "a.py")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    _write(tmp_path / "a.py", head_text)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-am", "head")
    _write(tmp_path / "a.py", head_text + "# uncommitted\n")  # dirty, so a clean-tree release cannot free the run
    kept_blob = _git(tmp_path, "hash-object", "-w", "--stdin", stdin=kept_text)
    _sizing(tmp_path, SIZING)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(tmp_path, h, marker="halted", plan="")
    _run(tmp_path, c, sizing="state/sizings/other.yaml")
    rec = tmp_path / "state/warp" / h
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    halted.update({"run_base_sha": base, "run_artifacts": {"withheld": {"a.py": "halt-record-only"}}})
    _write(rec / "halted.json", json.dumps(halted))
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {"a.py": kept_blob}}}, "plan_gate": None}))
    return h, c


KEPT = "def f():\n    return 1\n\n\ndef g(a, b):\n    return a + b\n"


def test_kept_blob_equal_to_head_releases(tmp_path):
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD


def test_kept_crlf_blob_whose_lf_form_is_at_head_releases(tmp_path):
    """ue-addon cd5bb6: under core.autocrlf the keep is a raw CRLF blob while HEAD stores the LF form."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    _git(tmp_path, "config", "core.autocrlf", "true")
    crlf = KEPT.replace("\n", "\r\n").encode()
    kept_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=tmp_path, input=crlf,
        capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    assert kept_blob != _git(tmp_path, "rev-parse", "HEAD:a.py")
    rec = tmp_path / "state/warp" / h
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {"a.py": kept_blob}}}, "plan_gate": None}))
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    halted.pop("run_base_sha")  # no _hunks_present fallback: only blob equality can release
    _write(rec / "halted.json", json.dumps(halted))
    assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD


def test_kept_crlf_blob_with_different_content_holds(tmp_path):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    h, c = _kept_repo(tmp_path, "def f():\n    return 1\n", KEPT)
    _git(tmp_path, "config", "core.autocrlf", "true")
    kept_blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=tmp_path, input=KEPT.replace("\n", "\r\n").encode(),
        capture_output=True, check=True, **no_console_creationflags(),
    ).stdout.decode().strip()
    _write(tmp_path / "state/warp" / h / "kept.json",
           json.dumps({"chunks": {"X1": {"paths": {"a.py": kept_blob}}}, "plan_gate": None}))
    assert hr.release_reason(tmp_path, h, c, None) is None


def test_kept_hunks_present_after_reflow_release(tmp_path):
    reflowed = "def f():\n    return 1\n\n\ndef g(\n    a, b\n):\n    return a + b\n"
    h, c = _kept_repo(tmp_path, reflowed, KEPT)
    assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD


def test_unlanded_kept_hunks_still_hold(tmp_path):
    h, c = _kept_repo(tmp_path, "def f():\n    return 1\n", KEPT)
    assert hr.release_reason(tmp_path, h, c, None) is None


def test_withheld_path_without_kept_blob_holds(tmp_path):
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    rec = tmp_path / "state/warp" / h
    _write(rec / "kept.json", json.dumps({"chunks": {}, "plan_gate": None}))
    assert hr.release_reason(tmp_path, h, c, None) is None


def test_withhold_with_no_own_hunk_releases(tmp_path):
    """The 4b2001 shape: the kept blob is the run base, so the run holds no hunk of its own in the path."""
    base_text = "def f():\n    return 1\n"
    h, c = _kept_repo(tmp_path, KEPT, base_text)
    assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD


def _two_path_run(tmp_path: Path, b_head_text: str):
    """Halted run withholding `a.py` (kept == HEAD) and `b.py` (kept `KEPT`, HEAD `b_head_text`)."""
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    base_b = "x = 1\n"
    _write(tmp_path / "b.py", base_b)
    _git(tmp_path, "add", "b.py")
    _git(tmp_path, "commit", "-q", "-m", "b")
    _write(tmp_path / "b.py", b_head_text)
    _git(tmp_path, "commit", "-q", "-am", "b head")
    kept_b = _git(tmp_path, "hash-object", "-w", "--stdin", stdin=KEPT)
    kept_a = _git(tmp_path, "hash-object", "-w", "--stdin", stdin=KEPT)
    rec = tmp_path / "state/warp" / h
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    halted["run_artifacts"] = {"withheld": {"a.py": "halt-record-only", "b.py": "halt-record-only"}}
    _write(rec / "halted.json", json.dumps(halted))
    _write(rec / "kept.json", json.dumps({"chunks": {"X1": {"paths": {"a.py": kept_a, "b.py": kept_b}}}, "plan_gate": None}))
    for p in ("a.py", "b.py"):  # dirty withheld paths: a clean-tree release cannot free the run
        _write(tmp_path / p, "# uncommitted\n")
    return h, c, rec, halted


def test_kept_at_head_paths_releases_per_path(tmp_path):
    h, c, rec, halted = _two_path_run(tmp_path, "x = 2\n")
    assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py"}
    assert hr.release_reason(tmp_path, h, c, None) is None


def test_kept_at_head_paths_all_at_head_releases_the_run(tmp_path):
    h, c, rec, halted = _two_path_run(tmp_path, KEPT)
    assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py", "b.py"}
    assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD


def test_kept_at_head_paths_nothing_withheld_spawns_nothing(tmp_path, monkeypatch):
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    rec = tmp_path / "state/warp" / h
    halted = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    halted["run_artifacts"] = {}
    _write(rec / "halted.json", json.dumps(halted))
    monkeypatch.setattr(hr, "_blob_batch", lambda *a: (_ for _ in ()).throw(AssertionError("spawned")))
    assert hr.kept_at_head_paths(tmp_path, rec, halted) == set()


def _withhold(root: Path, run_id: str, *paths: str) -> None:
    rec = root / "state/warp" / run_id
    body = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    body["run_artifacts"] = {"withheld": {p: "kept" for p in paths}}
    _write(rec / "halted.json", json.dumps(body))
    for p in paths:  # untracked on disk = dirty, so a clean-tree release does not free the run
        _write(root / p.removeprefix("./"), "# uncommitted\n")


def test_held_paths_is_the_normalized_withheld_set_while_the_run_holds(tmp_path):
    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted", writes=("a.py", "b.py"))
    _run(root, c, sizing="state/sizings/other.yaml")
    _withhold(root, h, "./DELIVERY.md", "a.py")
    held = hr.held_paths(root, h, c, "docs/plans/q.md")
    assert held == frozenset({"DELIVERY.md", "a.py"})
    assert "b.py" not in held


def test_held_paths_empty_when_released_and_when_nothing_withheld(tmp_path):
    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted")
    _run(root, c)
    _withhold(root, h, "a.py")
    assert hr.release_reason(root, h, c, None) == hr.RELEASE_SUCCESSOR
    assert hr.held_paths(root, h, c, None) == frozenset()
    other = _rid("ask", 10, "cccccc")
    _run(root, other, sizing="state/sizings/other.yaml")
    _write(root / "state/warp" / h / "halted.json", json.dumps({"status": "halted", "sizing_rel": SIZING}))
    assert hr.held_paths(root, h, other, None) == frozenset()
    assert hr.held_paths(root, "missing-20260101T000000-abcdef", other, None) == frozenset()


def test_held_paths_fails_closed_when_release_or_normalizer_errors(tmp_path, monkeypatch):
    from coordinator_core.ops import warp_run_deliverable as wrd

    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted")
    _run(root, c, sizing="state/sizings/other.yaml")
    _withhold(root, h, "./a.py")

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(hr, "release_reason", boom)
    assert hr.held_paths(root, h, c, None) == frozenset({"a.py"})
    monkeypatch.setattr(wrd, "halted_withheld_paths", boom)
    assert hr.held_paths(root, h, c, None) == frozenset({"a.py"})


def _corrupt(root: Path, run_id: str) -> None:
    _write(root / "state/warp" / run_id / "halted.json", "{not json")


def test_corrupt_marker_held_paths_raises_not_empty(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import UnreadableHaltRecord

    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h)
    _corrupt(root, h)
    assert hr.release_reason(root, h, c, None) is None
    with pytest.raises(UnreadableHaltRecord) as err:
        hr.held_paths(root, h, c, None)
    assert err.value.reason == f"unreadable-halt-record:{h}"


def test_corrupt_marker_kept_at_head_frees_nothing(tmp_path):
    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h)
    _corrupt(root, h)
    rec = root / "state/warp" / h
    assert hr.kept_at_head_paths(root, rec, {}) == set()
    assert hr._kept_at_head(root, rec, {}) is False


def test_corrupt_marker_halted_run_landed_refuses_with_reason(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run_landed

    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h)
    _corrupt(root, h)
    out = halted_run_landed._handler({"run_id": h, "sha": "abc1234"}, root)
    assert out["ok"] is False
    assert f"unreadable-halt-record:{h}" in out["error"]
    assert "never ran" not in out["error"]
    refusal, covers, _voided = halted_run_landed._carries_withheld(root, h, ["abc1234"])
    assert refusal and f"unreadable-halt-record:{h}" in refusal and covers == {}


def test_corrupt_marker_halted_record_commit_holds_with_reason(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc

    root = _root(tmp_path)
    h = _rid("ask", 300, "aaaaaa")
    _run(root, h)
    _corrupt(root, h)
    out = hrc._land(root, root, {"run_id": h}, h, f"state/warp/{h}", {"halted": "stop_rule"})
    assert out["committed"] is False
    assert out["refused"] == f"unreadable-halt-record:{h}"


def _count(monkeypatch, name: str) -> list:
    calls: list = []
    real = getattr(hr, name)

    def wrapper(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(hr, name, wrapper)
    return calls


def test_scope_evaluates_release_reason_body_once_per_key(tmp_path, monkeypatch):
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    body = _count(monkeypatch, "_release_reason")
    kept = _count(monkeypatch, "_blob_batch")
    with hr.release_scope():
        results = [hr.release_reason(tmp_path, h, c, None) for _ in range(3)]
        results.append(hr.release_reason(tmp_path, h, c, "docs/plans/other.md"))
    assert results == [hr.RELEASE_KEPT_AT_HEAD] * 4
    assert len(body) == 2
    assert len(kept) == 1  # the plan-independent kept-at-head leg ran once for both keys


def test_scope_memoises_none_from_the_except_arm(tmp_path, monkeypatch):
    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h, marker="halted")
    monkeypatch.setattr(hr, "_halted_sizing", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with hr.release_scope():
        assert hr.release_reason(root, h, c, None) is None
        monkeypatch.undo()
        _sizing(root, SIZING, "superseded")
        assert hr.release_reason(root, h, c, None) is None  # the failed evaluation is not retried into a release
    assert hr.release_reason(root, h, c, None) == hr.RELEASE_SUPERSEDED


def test_scope_corrupt_marker_still_holds(tmp_path):
    from coordinator_core.ops.dispatch_emit.halted_run import UnreadableHaltRecord

    root = _root(tmp_path)
    h, c = _rid("ask", 300, "aaaaaa"), _rid("ask", 10, "bbbbbb")
    _run(root, h)
    _corrupt(root, h)
    with hr.release_scope():
        assert hr.release_reason(root, h, c, None) is None
        with pytest.raises(UnreadableHaltRecord):
            hr.held_paths(root, h, c, None)


def test_prefetch_is_one_batch_for_every_run_and_kept_at_head_uses_it(tmp_path, monkeypatch):
    h, c, rec, halted = _two_path_run(tmp_path, KEPT)
    batches = _count(monkeypatch, "_blob_batch")
    with hr.release_scope():
        hr.prefetch_kept_at_head(tmp_path, [h, "ask-20260101T000000-ffffff"])
        assert len(batches) == 1
        assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py", "b.py"}
        assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD
    assert len(batches) == 1


def test_failed_prefetch_leaves_every_run_held(tmp_path, monkeypatch):
    h, c, rec, halted = _two_path_run(tmp_path, KEPT)
    real = hr._blob_batch
    monkeypatch.setattr(hr, "_blob_batch", lambda *a: None)
    with hr.release_scope():
        hr.prefetch_kept_at_head(tmp_path, [h])
        monkeypatch.setattr(hr, "_blob_batch", real)
        assert hr.kept_at_head_paths(tmp_path, rec, halted) == set()
        assert hr.release_reason(tmp_path, h, c, None) is None


def test_spec_absent_from_prefetch_falls_back_to_own_batch(tmp_path, monkeypatch):
    h, c, rec, halted = _two_path_run(tmp_path, KEPT)
    batches = _count(monkeypatch, "_blob_batch")
    with hr.release_scope():
        hr.prefetch_kept_at_head(tmp_path, [])  # nothing prefetched: not "prefetched"
        assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py", "b.py"}
    assert len(batches) == 1


def test_outside_scope_nothing_is_cached(tmp_path, monkeypatch):
    h, c = _kept_repo(tmp_path, KEPT, KEPT)
    body = _count(monkeypatch, "_release_reason")
    assert hr._SCOPE.get() is None
    hr.release_reason(tmp_path, h, c, None)
    hr.release_reason(tmp_path, h, c, None)
    hr.prefetch_kept_at_head(tmp_path, [h])  # no scope: no-op
    assert len(body) == 2 and hr._SCOPE.get() is None
    with hr.release_scope():
        hr.release_reason(tmp_path, h, c, None)
    assert hr._SCOPE.get() is None
    with hr.release_scope() as outer, hr.release_scope() as inner:
        assert inner is outer


def test_scope_computes_kept_at_head_paths_once_per_record(tmp_path, monkeypatch):
    h, c, rec, halted = _two_path_run(tmp_path, KEPT)
    plans = _count(monkeypatch, "_kept_plan")
    with hr.release_scope():
        first = hr.kept_at_head_paths(tmp_path, rec, halted)
        first.add("mutated")  # a caller's mutation must not poison the memo
        assert hr.kept_at_head_paths(tmp_path, rec, halted) == {"a.py", "b.py"}
        assert hr.release_reason(tmp_path, h, c, None) == hr.RELEASE_KEPT_AT_HEAD
    assert len(plans) == 1


def test_hunks_present_diffs_each_pair_once_per_scope(monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_hunk_claims

    calls = []
    real = run_hunk_claims.changed_opcodes
    monkeypatch.setattr(run_hunk_claims, "changed_opcodes", lambda a, b: calls.append(1) or real(a, b))
    with hr.release_scope():
        assert hr._hunks_present("a\nb\n", "a\nc\n", "a\nc\n")
        assert hr._hunks_present("a\nb\n", "a\nc\n", "a\nc\n")
    assert len(calls) == 1


def test_hunks_present_still_refuses_missing_and_unbounded_middle():
    """Fail-closed: a hunk absent from HEAD, or a middle past the diff bound, stays held."""
    assert not hr._hunks_present("a\nb\n", "a\nc\n", "a\nb\n")
    base = "".join(f"x{i}\n" for i in range(600))
    kept = "".join(f"y{i}\n" for i in range(600))
    assert not hr._hunks_present(base, kept, base)


def _green(root: Path, run_id: str, *, incomplete=None, sidecar=True) -> None:
    rel = f"docs/reviews/{run_id}.md"
    if sidecar:
        _write(root / rel, "review\n")
    outcome = {"inline_review": {"integration": {"sidecar": rel}}}
    if incomplete:
        outcome["incomplete_chunks"] = incomplete
    _write(root / "state/warp" / run_id / "run-outcome.json", json.dumps(outcome))


def _index(root: Path, run_id: str, *paths: str) -> None:
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    idx.add_run(root, run_id, paths)


def _sub(base: Path, name: str) -> Path:
    (base / name).mkdir()
    return base / name


def _auto_land_fixture(root: Path, *, peer_marker="halted"):
    me, peer = _rid("ask", 10, "bbbbbb"), _rid("ask", 300, "aaaaaa")
    _run(root, me, sizing="state/sizings/other.yaml", writes=("a.py", "b.py"))
    _green(root, me)
    _run(root, peer, marker=peer_marker, plan="docs/plans/z.md")
    _index(root, peer, "a.py", "residue.py")
    return me, peer


def test_halted_only_withholders_all_halted_and_green(tmp_path):
    root = _root(tmp_path)
    me, peer = _auto_land_fixture(root)
    assert hr.halted_only_withholders(root, me) == [peer]


def test_halted_only_withholders_none_for_live_unreadable_or_red(tmp_path):
    root = _root(tmp_path)
    me, peer = _auto_land_fixture(root)
    live = _rid("ask", 200, "cccccc")
    _run(root, live, writes=("b.py",))
    _index(root, live, "b.py")
    assert hr.halted_only_withholders(root, me) is None  # live holder (no halt marker)
    root2 = _root(_sub(tmp_path, "two"))
    me2, peer2 = _auto_land_fixture(root2)
    _corrupt(root2, peer2)
    assert hr.halted_only_withholders(root2, me2) is None  # unreadable claim
    root3 = _root(_sub(tmp_path, "three"))
    me3, _ = _auto_land_fixture(root3)
    _green(root3, me3, incomplete=["X1"])
    assert hr.halted_only_withholders(root3, me3) is None  # red outcome
    _green(root3, me3)
    (root3 / f"docs/reviews/{me3}.md").unlink()
    assert hr.halted_only_withholders(root3, me3) is None  # no review record


def test_halted_only_withholders_none_when_nothing_withholds_or_released(tmp_path):
    root = _root(tmp_path)
    me, peer = _auto_land_fixture(root)
    _sizing(root, "state/sizings/other.yaml")
    _run(root, peer, marker="landed", plan="docs/plans/z.md")
    assert hr.halted_only_withholders(root, me) is None
    other = _rid("ask", 5, "dddddd")
    _run(root, other, sizing="state/sizings/other.yaml", writes=("zzz.py",))
    _green(root, other)
    assert hr.halted_only_withholders(root, other) is None


def test_residue_paths_excludes_own_paths_and_other_states(tmp_path):
    root = _root(tmp_path)
    me, peer = _auto_land_fixture(root)
    assert hr.residue_paths(root, me) == frozenset({"residue.py"})
    assert hr.residue_paths(root, peer) == frozenset()
    _run(root, peer, marker="landed", plan="docs/plans/z.md")
    assert hr.residue_paths(root, me) == frozenset()


def test_a_per_path_release_drops_that_pair_and_that_path_from_held_paths(tmp_path):
    root = _root(tmp_path)
    holder = _rid("ask", 60, "aaaaaa")
    _run(root, holder, marker="halted", plan="docs/plans/other.md")
    marker_file = root / "state/warp" / holder / "halted.json"
    marker = json.loads(marker_file.read_text(encoding="utf-8"))
    marker["run_artifacts"] = {"withheld": {"a.py": "peer-held", "b.py": "peer-held"}}
    marker[hr.RELEASED_PATHS_FIELD] = {"a.py": {"reason": "mine at HEAD", "at": "2026-10-27T00:00:00Z"}}
    for p in ("a.py", "b.py"):  # untracked on disk = dirty, so a clean-tree release does not free the run
        _write(root / p, "# uncommitted\n")
    marker_file.write_text(json.dumps(marker), encoding="utf-8")

    assert hr.released_path_reasons(root, holder) == {"a.py": "mine at HEAD"}
    assert hr.without_released_paths(root, [("a.py", holder), ("b.py", holder)]) == [("b.py", holder)]
    assert "a.py" not in hr.held_paths(root, holder, "ask-20261027T000000-bbbbbb")
    assert "b.py" in hr.held_paths(root, holder, "ask-20261027T000000-bbbbbb")


def test_a_run_with_no_release_record_releases_nothing(tmp_path):
    root = _root(tmp_path)
    holder = _rid("ask", 60, "aaaaaa")
    _run(root, holder, marker="halted", plan="docs/plans/other.md")
    assert hr.released_path_reasons(root, holder) == {}
    assert hr.without_released_paths(root, [("a.py", holder)]) == [("a.py", holder)]
