"""`land_credited_profile`: the scale check, the flatness test, and the (a) and (h) legs on a tmp_path repo."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.benchmarks import land_credited_profile as lcp

pytestmark = pytest.mark.spawns_process

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, creationflags=NO_WINDOW)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        _git(root, "config", key, value)
    old = time.time() - 3600
    for name in ("a.py", "b.py"):
        (root / name).write_text(f"VALUE = '{name}'\n", encoding="utf-8")
        os.utime(root / name, (old, old))
    _git(root, "add", "a.py", "b.py")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _context(root: Path, declared: list) -> lcp.SpikeContext:
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(declared)),), plan_path="docs/plans/p.md")
    (root / "script.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    return lcp.SpikeContext(root, "ask-20260101T000000-abcdef", {"script_path": "script.mjs"})


def test_replay_clone_makes_no_git_clone(monkeypatch, tmp_path):
    from coordinator_core.session import core as session_core

    spawned: list = []
    entered: list = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a) or pytest.fail("replay_clone spawned a process"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("replay_clone spawned a process"))
    monkeypatch.setattr(lcp.base_profile, "real_record_params", lambda root, run_id: {"p": 1})
    monkeypatch.setattr(lcp.base_profile, "enter_record_fixture", lambda stack, root, params, **kw: entered.append(kw) or tmp_path)
    monkeypatch.setattr(lcp.base_profile, "seed_real_record", lambda *a: None)
    monkeypatch.setattr(lcp.base_profile, "scale_halted_runs", lambda *a: None)
    monkeypatch.setattr(lcp.base_profile, "seed_parse_verdicts", lambda *a: None)
    monkeypatch.setattr(lcp, "seed_live_halted_markers", lambda *a: None)
    monkeypatch.setattr(lcp, "halted_seen", lambda clone: 0)
    monkeypatch.setattr(session_core, "init", lambda *a, **k: None)

    with lcp.replay_clone("ask-20260101T000000-abcdef", tmp_path, 2) as (clone, params, _before, _after):
        assert clone == tmp_path and params == {"p": 1}
    assert len(entered) == 1 and spawned == []


def test_flat_within_ten_percent():
    assert lcp.flat_within(100.0, 110.0)
    assert not lcp.flat_within(100.0, 110.1)


def test_median_is_the_middle_sample():
    assert lcp.median([300.0, 100.0, 200.0]) == 200.0
    assert lcp.median([]) == 0.0


def test_scale_check_passes_exactly_k_times_a_nonzero_count():
    lcp.assert_holds_scaled(9, 18, 2)
    lcp.assert_holds_scaled(9, 9, 1)


@pytest.mark.parametrize("before,after,scale", [(0, 0, 2), (9, 9, 2), (9, 17, 2), (9, 27, 2)])
def test_scale_check_fails_loud_when_vacuous_or_not_k_times(before, after, scale):
    with pytest.raises(lcp.VacuousScale):
        lcp.assert_holds_scaled(before, after, scale)


def test_credited_differing_names_only_paths_that_differ_from_head(tmp_path):
    root = _repo(tmp_path)
    (root / "a.py").write_text("VALUE = 'a changed, and longer than before'\n", encoding="utf-8")
    ctx = _context(root, ["a.py", "b.py", "c.py"])
    (root / "c.py").write_text("NEW = 1\n", encoding="utf-8")
    detail = lcp.leg_a_credited_differing(ctx)
    assert detail["gap"] is False
    assert ctx.differing == ["a.py", "c.py"]
    assert ctx.plan_rel == "docs/plans/p.md"


def test_commit_leg_moves_the_ref_with_exactly_the_credited_paths(tmp_path):
    root = _repo(tmp_path)
    ctx = _context(root, ["a.py", "b.py"])
    (root / "a.py").write_text("VALUE = 'a changed, and longer than before'\n", encoding="utf-8")
    (root / "peer.py").write_text("PEER = 1\n", encoding="utf-8")
    lcp.leg_a_credited_differing(ctx)
    before = lcp._head(root)
    detail = lcp.leg_h_commit(ctx)
    assert detail["moved"] is True and detail["committed"] == 1
    assert lcp._head(root) != before
    shown = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(root), capture_output=True, check=True, creationflags=NO_WINDOW)
    assert shown.stdout.decode().split() == ["a.py"]


def test_dirty_step_refuses_a_clone_with_no_credited_path(tmp_path):
    root = _repo(tmp_path)
    ctx = _context(root, ["absent.py"])
    lcp.leg_a_credited_differing(ctx)
    with pytest.raises(FileNotFoundError):
        lcp.dirty_credited_paths(ctx, 0)


def test_op_absent_until_registered(tmp_path):
    out = lcp.run_op("dispatch.not_registered_anywhere", "ask-x", tmp_path, 1, 1)
    assert out == {"op": "dispatch.not_registered_anywhere", "absent": True}


def test_render_refuses_to_call_a_refused_reply_under_the_bar():
    refused = {"op": lcp.DEFAULT_OP, "absent": False, "process_ms": [100.0], "spawns": 0, "halted_in_window": 9,
               "outcome": "commit-refused", "landed": False}
    landed = {**refused, "outcome": "abc123", "landed": True}
    assert "NOT A LANDING" in lcp.render({"run_id": "r", "halted_scale": 1, "op": refused})
    text = lcp.render({"run_id": "r", "halted_scale": 1, "op": landed})
    assert "UNDER 500ms" in text and "NOT A LANDING" not in text


def test_render_prints_every_requirement_row_and_the_absent_op():
    rows = [lcp.LegRow(req, name, [10.0, 20.0, 30.0], 0, {"k": 1}) for req, name, _ in lcp.LEGS]
    report = {"run_id": "r", "halted_scale": 2, "spike": {"rows": rows, "halted_before": 9, "halted_after": 18},
              "op": {"op": lcp.DEFAULT_OP, "absent": True}}
    text = lcp.render(report)
    assert "holds read saw 18 in-window halted runs (9 unscaled)" in text
    assert {f"({req})" for req in "abcdefgh"} <= {tok for line in text.splitlines() for tok in line.split()[:1]}
    assert "absent (not registered)" in text
    json.dumps({"rows": [r.median_ms for r in rows]})


def test_seed_live_halted_markers_copies_only_what_the_clone_lacks(tmp_path):
    source, clone = tmp_path / "src", tmp_path / "clone"
    run_a, run_b = "ask-20990101T000000-aaaaaa", "ask-20990101T000000-bbbbbb"
    for root in (source, clone):
        (root / "state" / "warp" / run_a).mkdir(parents=True)
    for run in (run_a, run_b):
        (source / "state" / "warp" / run).mkdir(parents=True, exist_ok=True)
        (source / "state" / "warp" / run / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    (clone / "state" / "warp" / run_a / "halted.json").write_text(json.dumps({"status": "halted", "kept": True}), encoding="utf-8")
    assert lcp.seed_live_halted_markers(source, clone) == 1
    assert json.loads((clone / "state" / "warp" / run_a / "halted.json").read_text(encoding="utf-8")).get("kept") is True
    assert (clone / "state" / "warp" / run_b / "halted.json").is_file()
