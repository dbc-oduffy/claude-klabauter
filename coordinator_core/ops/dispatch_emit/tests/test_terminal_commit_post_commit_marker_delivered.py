"""``_partial_chunks`` lands a PARTIAL report undone only by post-commit bug_close markers."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import bug_close, terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

ID = "0123456789ab"
MARK = bug_close.marker_line(ID)
F8207B = (
    "PARTIAL: X1\nNot done: close bug 0123456789ab with the landing sha\n"
    "<exit-status>PARTIAL</exit-status>\n"
)


def _run(tmp_path, report_text):
    (tmp_path / "r.md").write_text(report_text, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="X1", title="t", paths=("a.py",), report="r.md"),))
    withheld: dict = {}
    out = terminal_commit._partial_chunks(tmp_path, request, {"X1"}, withheld)
    return [c.id for c in out], withheld


def test_partial_with_a_marker_is_delivered_and_never_withheld(tmp_path):
    delivered, withheld = _run(tmp_path, f"PARTIAL: X1\n{MARK}\n<exit-status>PARTIAL</exit-status>\n")
    assert delivered == ["X1"] and withheld == {}


def test_partial_with_status_field_and_a_marker_is_delivered(tmp_path):
    delivered, withheld = _run(tmp_path, f"status: PARTIAL\n- {MARK}\n")
    assert delivered == ["X1"] and withheld == {}


def test_partial_with_only_the_prose_stays_stranded_and_withheld(tmp_path):
    delivered, withheld = _run(tmp_path, F8207B)
    assert delivered == [] and list(withheld) == ["X1"]


def test_partial_marker_with_a_gate_blocker_keeps_todays_handling(tmp_path):
    delivered, withheld = _run(
        tmp_path, f"PARTIAL: X1\n{MARK}\ngate-blocker: outside-footprint: x\n"
    )
    assert delivered == [] and list(withheld) == ["X1"]


def test_blocked_with_a_marker_stays_stranded(tmp_path):
    delivered, withheld = _run(tmp_path, f"BLOCKED on: X1\n{MARK}\n<exit-status>BLOCKED</exit-status>\n")
    assert delivered == [] and withheld == {}


def test_done_still_lands(tmp_path):
    delivered, _ = _run(tmp_path, "DONE: X1\n<exit-status>DONE</exit-status>\n")
    assert delivered == ["X1"]
