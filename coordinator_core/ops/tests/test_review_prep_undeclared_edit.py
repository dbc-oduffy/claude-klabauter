"""review.prep with run_id: an edit under no declared row reaches review via the stage dirt snapshot.

Reproduces run 4d5478: a pure-shrink edit to a file under no declared row or prefix must reach the
frozen diff and be marked undeclared, while a file already dirty at stage and unchanged stays out.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit import stage_dirt_snapshot as snap
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
RUN = "ask-20261031T000000-4d5478"
SHRUNK = "docs/wiki/fu7-extraction.md"
PEER = "peer/dirty.txt"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    for rel, text in (
        (SHRUNK, "extraction line\n" * 130),
        (PEER, "peer v1\n"),
        ("docs/plans/p.md", "---\nplan_id: pln-test-1\n---\n\n# p\n"),
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    (tmp_path / PEER).write_text("peer v2, dirty before stage\n", encoding="utf-8")
    aged = time.time() - 3600
    os.utime(tmp_path / PEER, (aged, aged))
    record = record_dir(tmp_path, RUN)
    record.mkdir(parents=True, exist_ok=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": ["a/one.py"]}), encoding="utf-8")
    return tmp_path


def _prep(repo: Path) -> dict:
    return rp._handler(
        {
            "run_id": RUN,
            "base": _git(repo, "rev-parse", "HEAD").strip(),
            "worktree": True,
            "plan_path": "docs/plans/p.md",
            "session_id": SESSION,
        },
        repo,
    )


@pytest.fixture(autouse=True)
def _own_session_claims(monkeypatch):
    """The run's own session holds a write claim on every path: the claim witness the partition requires."""
    from coordinator_core.session import claim_index, liveness

    class _Found(dict):
        recorded_kind = None

    def lookup(paths, **kw):
        found = _Found({p: [SESSION] for p in paths})
        found.recorded_kind = {p: {SESSION: "write"} for p in paths}
        return found

    monkeypatch.setattr(claim_index, "lookup", lookup)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid == SESSION)


def _run_work(repo: Path) -> None:
    (repo / "a").mkdir(exist_ok=True)
    (repo / "a" / "one.py").write_text("x = 1\n", encoding="utf-8")
    (repo / SHRUNK).write_text("extraction line\n" * 8, encoding="utf-8")


def test_stage_snapshot_records_the_file_clock_marker_write_once(repo):
    written = snap.write_snapshot(repo, record_dir(repo, RUN))
    assert written is not None and written.name == "stage-dirt.json"
    marker = snap.read_snapshot(record_dir(repo, RUN))
    assert isinstance(marker, int) and marker > 0
    assert snap.write_snapshot(repo, record_dir(repo, RUN)) is None
    assert snap.read_snapshot(record_dir(repo, RUN)) == marker


def test_pure_shrink_under_no_declared_row_reaches_the_review_diff(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    _run_work(repo)
    reply = _prep(repo)
    assert reply["error"] is None
    assert reply["undeclared_edits"] == [SHRUNK]
    assert SHRUNK in reply["reviewed_sha256"]
    diff = Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert f"diff --git a/{SHRUNK} b/{SHRUNK}" in diff
    assert "a/one.py" in diff
    sidecar = (repo / reply["sidecar_path"]).read_text(encoding="utf-8")
    assert "undeclared_edits:" in sidecar and SHRUNK in sidecar


def test_pre_stage_dirty_unchanged_peer_file_stays_out(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    _run_work(repo)
    reply = _prep(repo)
    assert PEER not in reply["reviewed_sha256"]
    assert PEER not in Path(reply["whole_diff_path"]).read_text(encoding="utf-8")
    assert PEER not in reply["undeclared_edits"]


def test_pre_stage_dirty_file_edited_after_stage_is_picked_up(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    _run_work(repo)
    (repo / PEER).write_text("peer v3, edited after stage\n", encoding="utf-8")
    assert _prep(repo)["undeclared_edits"] == sorted([PEER, SHRUNK])


def test_no_snapshot_adds_nothing(repo):
    _run_work(repo)
    reply = _prep(repo)
    assert reply["error"] is None
    assert reply["undeclared_edits"] == []
    assert SHRUNK not in reply["reviewed_sha256"]


def test_stage_spawns_no_process(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("stage spawned a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    assert snap.write_snapshot(repo, record_dir(repo, RUN)) is not None


def test_touched_with_identical_bytes_is_not_undeclared(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    _run_work(repo)
    touched = repo / "docs" / "plans" / "p.md"
    touched.write_bytes(touched.read_bytes())
    st = touched.stat()
    os.utime(touched, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert "docs/plans/p.md" in snap._stat_dirty(repo)[0]
    assert snap.undeclared_edits(repo, record_dir(repo, RUN), []) == [SHRUNK]
    assert _prep(repo)["undeclared_edits"] == [SHRUNK]


def test_stage_reads_neither_index_nor_tree_bytes(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("stage read the index or the tree")

    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_identity", boom)
    monkeypatch.setattr("coordinator_core.git.git_index.parse_index_stat", boom)
    assert snap.write_snapshot(repo, record_dir(repo, RUN)) is not None


def test_deleted_after_stage_is_undeclared(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    (repo / SHRUNK).unlink()
    assert snap.undeclared_edits(repo, record_dir(repo, RUN), []) == [SHRUNK]


def test_pre_stage_dirty_reverted_to_index_is_not_undeclared(repo):
    snap.write_snapshot(repo, record_dir(repo, RUN))
    (repo / PEER).write_text("peer v1\n", encoding="utf-8")
    assert snap.undeclared_edits(repo, record_dir(repo, RUN), []) == []


def test_scan_set_identity_across_every_dirt_kind(repo):
    for rel in ("m/pre_dirty.py", "m/post_edit.py", "m/touched_clean.py", "m/deleted_post.py", "m/declared.py", "n/keep.py"):
        (repo / rel).parent.mkdir(exist_ok=True)
        (repo / rel).write_text(f"{rel} v1\n", encoding="utf-8")
    (repo / "gone").mkdir()
    (repo / "gone" / "x.py").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")
    aged = time.time() - 3600
    (repo / "m" / "pre_dirty.py").write_text("dirty before stage\n", encoding="utf-8")
    os.utime(repo / "m" / "pre_dirty.py", (aged, aged))
    snap.write_snapshot(repo, record_dir(repo, RUN))
    (repo / "m" / "post_edit.py").write_text("edited after stage\n", encoding="utf-8")
    (repo / "m" / "declared.py").write_text("edited and declared\n", encoding="utf-8")
    touched = repo / "m" / "touched_clean.py"
    touched.write_bytes(touched.read_bytes())
    (repo / "m" / "deleted_post.py").unlink()
    (repo / "gone" / "x.py").unlink()
    (repo / "gone").rmdir()
    got = snap.undeclared_edits(repo, record_dir(repo, RUN), ["m/declared.py"])
    assert got == ["gone/x.py", "m/deleted_post.py", "m/post_edit.py"]
    assert snap.undeclared_edits(repo, record_dir(repo, RUN), []) == sorted(got + ["m/declared.py"])
    dirty = snap._stat_dirty(repo)[0]
    assert dirty["gone/x.py"] is None and "n/keep.py" not in dirty and "m/pre_dirty.py" in dirty


def test_declared_path_is_not_marked_undeclared(repo):
    record = record_dir(repo, RUN)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": [SHRUNK]}), encoding="utf-8")
    snap.write_snapshot(repo, record)
    _run_work(repo)
    reply = _prep(repo)
    assert reply["undeclared_edits"] == []
    assert SHRUNK in reply["reviewed_sha256"]
