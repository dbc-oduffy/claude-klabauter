"""A refire carries a halted run's recorded post-checkpoint hunks as the kept-review chunk, or refuses naming drifted ones."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import kept_work, resume_snapshot
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import read_stage_reply

PLAN_REL = "docs/plans/p.md"
RUN = "r2"
KEPT_RUN = "r1"
HUNKS = ("src/a.py", "src/b.py", "src/c.py")


def _plan(tmp_path: Path, writes: str = "docs/x/C1.md") -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    rows = f"- id: C1\n  title: tC1\n  change_kind: doc-edit\n  surface: {writes}\n  writes:\n    - {writes}\n"
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n", encoding="utf-8", newline="\n"
    )


def _record(tmp_path: Path, run: str, paths=HUNKS) -> dict:
    blobs = {}
    for rel in paths:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"bytes {rel}", encoding="utf-8", newline="\n")
        blobs[rel] = kept_work.fingerprint(tmp_path, rel)
    kept_work.write(tmp_path / "state/warp" / run, {}, None, post_checkpoint=blobs)
    return blobs


def _stage(tmp_path: Path, run: str = RUN, kept_run: str | None = None) -> dict:
    resume_snapshot.write(tmp_path, run, PLAN_REL, [], ["C1"], kept_run=kept_run)
    return read_stage_reply(
        tmp_path, _handler({"run_id": run, "plan_path": PLAN_REL, "only_rows": ["C1"]}, repo_root=tmp_path)
    )


def _request(tmp_path: Path, run: str = RUN):
    text = (tmp_path / "scratch/warp" / run / "commit-request.txt").read_text(encoding="utf-8")
    return parse_marker(text)


def _kept_review(req):
    return [c for c in req.chunks if c.id == "kept-review"]


def test_a_same_id_refire_carries_matching_hunks_as_kept_review(tmp_path):
    _plan(tmp_path)
    _record(tmp_path, RUN)
    reply = _stage(tmp_path)
    assert "error" not in reply, reply
    (chunk,) = _kept_review(_request(tmp_path))
    assert chunk.title == "kept post-checkpoint hunks"
    assert chunk.report in (None, "")
    assert tuple(chunk.paths) == HUNKS
    assert set(HUNKS) <= set(reply["review_declared_paths"])


def test_one_edited_file_refuses_naming_it_and_its_blob(tmp_path):
    _plan(tmp_path)
    blobs = _record(tmp_path, RUN)
    (tmp_path / HUNKS[1]).write_text("edited", encoding="utf-8")
    reply = _stage(tmp_path)
    assert reply["error"].startswith("kept-hunks-drifted:")
    assert HUNKS[1] in reply["error"] and blobs[HUNKS[1]] in reply["error"]
    assert HUNKS[0] not in reply["error"]


def test_a_recorded_path_absent_from_the_tree_refuses(tmp_path):
    _plan(tmp_path)
    _record(tmp_path, RUN)
    (tmp_path / HUNKS[2]).unlink()
    assert HUNKS[2] in _stage(tmp_path)["error"]


def test_a_fresh_id_refire_reads_the_kept_run_record(tmp_path):
    _plan(tmp_path)
    _record(tmp_path, KEPT_RUN)
    reply = _stage(tmp_path, kept_run=KEPT_RUN)
    assert "error" not in reply, reply
    (chunk,) = _kept_review(_request(tmp_path))
    assert tuple(chunk.paths) == HUNKS


def test_a_path_a_staged_row_declares_is_not_duplicated(tmp_path):
    _plan(tmp_path, writes=HUNKS[0])
    _record(tmp_path, RUN)
    (tmp_path / HUNKS[0]).write_text("row will rewrite", encoding="utf-8")
    reply = _stage(tmp_path)
    assert "error" not in reply, reply
    req = _request(tmp_path)
    (chunk,) = _kept_review(req)
    assert tuple(chunk.paths) == HUNKS[1:]
    assert sum(HUNKS[0] in c.paths for c in req.chunks) == 1


def test_no_recorded_post_checkpoint_leaves_the_request_unchanged(tmp_path):
    _plan(tmp_path)
    assert "error" not in _stage(tmp_path)
    assert _kept_review(_request(tmp_path)) == []
