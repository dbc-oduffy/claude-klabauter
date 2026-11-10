"""done_unlanded: report discovery, regex parity, one-spawn classification."""

import os
from pathlib import Path

from coordinator_core.ops.dispatch_emit import done_unlanded as du
from coordinator_core.ops.dispatch_emit.emit import _DISPATCH_REPORT_DIR

FAM = "20261021T182452-4b88be1d"


def _report(root: Path, stem: str, mid: str, text: str, mtime=None) -> Path:
    p = root / _DISPATCH_REPORT_DIR / f"{stem}.spine" / f"{mid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def test_family_done_reports(tmp_path):
    _report(tmp_path, f"{FAM}-t2-p2", "A.C1", "status: DONE\n", 100)
    _report(tmp_path, f"{FAM}-t3", "B.C1", "status: PARTIAL\n", 100)
    _report(tmp_path, f"{FAM}-t2-p2", "D.C1", "status: DONE\n", 100)
    _report(tmp_path, f"{FAM}-t3", "D.C1", "PARTIAL: x\n", 200)
    _report(tmp_path, f"{FAM}-t3", "E.C1", "PARTIAL: x\n", 100)
    _report(tmp_path, f"{FAM}-t2-p2", "E.C1", "DONE: y\n", 200)
    _report(tmp_path, "other-fam-t2", "F.C1", "status: DONE\n")
    bad = _report(tmp_path, f"{FAM}-t2-p2", "G.C1", "status: DONE\n")
    bad.unlink()
    bad.mkdir()
    got = du.family_done_reports(tmp_path, FAM, ["A.C1", "B.C1", "D.C1", "E.C1", "F.C1", "G.C1", "Z"])
    assert sorted(got) == ["A.C1", "E.C1"]
    assert got["A.C1"].run_stem == f"{FAM}-t2-p2"
    assert got["A.C1"].report_rel == f"{_DISPATCH_REPORT_DIR}/{FAM}-t2-p2.spine/A.C1.md"


def test_missing_report_dir(tmp_path):
    assert du.family_done_reports(tmp_path, FAM, ["A.C1"]) == {}


def test_regex_parity_with_terminal_commit():
    from coordinator_core.ops.dispatch_emit.terminal_commit_ledger import _is_delivered_report

    samples = [
        "DONE: x", "DONE_WITH_CONCERNS: x", "status: DONE\n", "status: DONE_WITH_CONCERNS",
        "status: PARTIAL", "PARTIAL: x", "BLOCKED: x", "status: BLOCKED",
        "<exit-status>DONE</exit-status>", "<exit-status>PARTIAL</exit-status>",
        "<exit-status>BLOCKED</exit-status>", "no marker", "DONE: a\nPARTIAL: b",
    ]
    for s in samples:
        assert du._reads_delivered(s) == _is_delivered_report(s), s


def _rep(mid, run="r-t2-p2"):
    return du.DoneReport(mid, f"rep/{mid}.md", run)


def test_classify_empty_makes_no_call():
    calls = []
    assert du.classify_done(Path("."), {}, {}, "inv", run=lambda *a, **k: calls.append(1)) == {}
    assert calls == []


def test_classify_one_spawn_noop_and_land(tmp_path):
    root = tmp_path
    (root / "a.txt").write_text("x")
    (root / "b.txt").write_text("y")
    spawns = []

    class P:
        returncode = 0
        stdout = " M b.txt\n"

    def run(argv, timeout=None):
        spawns.append(argv)
        return P()

    done = {"A": _rep("A"), "B": _rep("B"), "C": _rep("C")}
    declared = {"A": ["a.txt"], "B": ["b.txt"], "C": ["missing.txt"]}
    out = du.classify_done(root, done, declared, "state/inv", run=run)
    assert len(spawns) == 1
    assert out["A"]["class"] == du.DONE_NOOP and out["A"]["route"] == ""
    assert out["B"]["class"] == du.DONE_LAND and out["B"]["dirty"] == ["b.txt"]
    assert out["C"]["class"] == du.DONE_LAND and out["C"]["reason"] == "declared write absent"
    assert out["B"]["route"] == (
        "land it: dispatch.land_run script_path=state/inv/r-t2-p2.spine.workflow.mjs "
        "task_output_path=<that run's task output> incomplete_chunks=[] (uncommitted: b.txt); "
        "once those paths are committed, emit-dispatch-workflow --inventory <record> "
        "--review-only --rows B --run-base <sha> reviews them instead"
    )


def _two_call_run(spawns, log_stdout, log_rc=0):
    class P:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def run(argv, timeout=None):
        spawns.append(argv)
        if "log" in argv:
            return P(log_rc, log_stdout)
        return P(0, " M a.txt\n M b.txt\n")

    return run


def _timed(mid, mtime):
    return du.DoneReport(mid, f"rep/{mid}.md", "r-t2-p2", mtime)


def test_dirty_path_committed_after_report_routes_review_only_in_one_extra_spawn(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    spawns = []
    run = _two_call_run(spawns, "\x01500\na.txt\n\x01450\nb.txt\n")
    done = {"A": _timed("A", 400.0), "B": _timed("B", 400.0)}
    out = du.classify_done(tmp_path, done, {"A": ["a.txt"], "B": ["b.txt"]}, "inv", run=run)
    assert len(spawns) == 2
    for mid in ("A", "B"):
        assert out[mid]["class"] == du.DONE_LAND and out[mid][du.HAND_LANDED] is True
        assert "--review-only --rows " + mid + " --run-base <sha>" in out[mid]["route"]
        assert "land_run" not in out[mid]["route"]


def test_dirty_path_last_committed_before_report_still_lands(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    run = _two_call_run([], "\x01100\na.txt\n")
    done = {"A": _timed("A", 400.0), "B": _timed("B", 400.0)}
    out = du.classify_done(tmp_path, done, {"A": ["a.txt"], "B": ["b.txt"]}, "inv", run=run)
    assert out["A"][du.HAND_LANDED] is False and "dispatch.land_run" in out["A"]["route"]
    assert out["B"][du.HAND_LANDED] is False and "dispatch.land_run" in out["B"]["route"]


def test_commit_lookup_failure_fails_closed(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    run = _two_call_run([], "\x01500\na.txt\n", log_rc=128)
    out = du.classify_done(tmp_path, {"A": _timed("A", 400.0)}, {"A": ["a.txt"]}, "inv", run=run)
    assert out["A"][du.HAND_LANDED] is False and "dispatch.land_run" in out["A"]["route"]


def test_git_failure_fails_closed(tmp_path):
    (tmp_path / "a.txt").write_text("x")

    class P:
        returncode = 128
        stdout = ""

    out = du.classify_done(
        tmp_path, {"A": _rep("A")}, {"A": ["a.txt"]}, "inv", run=lambda *a, **k: P()
    )
    assert out["A"]["class"] == du.DONE_LAND
    assert out["A"]["reason"] == "git status failed; fail closed"
