"""Background engine convergence: a clean behind clone is fast-forwarded and its
stamp changes; a dirty clone is refused with the reason recorded; scheduling
returns without running git."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.warm import engine_converge, push_cadence
from coordinator_core.warm.skew import ENGINE_STAMP_FILENAME, compute_client_token

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO_LIB = Path(__file__).resolve().parents[3] / "coordinator" / "lib"


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.strip()


def _stamp(root: Path) -> Path:
    return root / "coordinator_core" / ENGINE_STAMP_FILENAME


def _commit_stamp(root: Path, sha: str) -> None:
    _stamp(root).parent.mkdir(parents=True, exist_ok=True)
    _stamp(root).write_text(f"sha:{sha}\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"stamp {sha}")


@pytest.fixture
def clone_behind(tmp_path):
    """(clone, peer): clone sits one publish behind origin."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "candidate", str(origin))
    peer = tmp_path / "peer"
    _git(tmp_path, "clone", "-q", str(origin), str(peer))
    _git(peer, "checkout", "-q", "-b", "candidate")
    (peer / "seed.txt").write_text("seed", encoding="utf-8")
    _commit_stamp(peer, "aaaaaaa")
    _git(peer, "push", "-q", "-u", "origin", "candidate")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "-b", "candidate", str(origin), str(clone))
    _commit_stamp(peer, "bbbbbbb")
    _git(peer, "push", "-q", "origin", "candidate")
    return clone, peer


def _ff(clone: Path):
    if str(_REPO_LIB) not in sys.path:
        sys.path.insert(0, str(_REPO_LIB))
    from percolate.dest_refresh import fast_forward_consumer

    return fast_forward_consumer(clone)


def test_behind_clean_clone_is_fast_forwarded_and_stamp_changes(clone_behind, tmp_path):
    clone, peer = clone_behind
    before = compute_client_token(clone)
    record = tmp_path / "rec" / "engine-converge.json"

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=record
    )

    assert payload["outcome"] == "fast_forwarded"
    assert _git(clone, "rev-list", "--count", "HEAD..origin/candidate") == "0"
    assert _git(clone, "rev-parse", "HEAD") == _git(peer, "rev-parse", "HEAD")
    assert compute_client_token(clone) != before
    on_disk = json.loads(record.read_text(encoding="utf-8"))
    assert on_disk["outcome"] == "fast_forwarded" and on_disk["behind"] == 0


def test_dirty_clone_is_refused_and_head_does_not_move(clone_behind, tmp_path):
    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")
    record = tmp_path / "engine-converge.json"

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=record
    )

    assert payload["outcome"] == "refused_dirty"
    assert payload["ok"] is False and payload["dirty"] is True
    assert "modified tracked" in payload["reason"]
    assert _git(clone, "rev-parse", "HEAD") == head
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:handedit\n"
    assert json.loads(record.read_text(encoding="utf-8"))["outcome"] == "refused_dirty"


def _default_ff(clone: Path):
    if str(_REPO_LIB) not in sys.path:
        sys.path.insert(0, str(_REPO_LIB))
    return engine_converge._default_fast_forward(clone)


def test_a_dirty_engine_stamp_alone_does_not_block_the_default_convergence(clone_behind, tmp_path):
    clone, peer = clone_behind
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "fast_forwarded"
    assert _git(clone, "rev-parse", "HEAD") == _git(peer, "rev-parse", "HEAD")
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:bbbbbbb\n"


def test_other_dirt_beside_the_stamp_still_refuses_the_default_convergence(clone_behind, tmp_path):
    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:handedit\n", encoding="utf-8")
    (clone / "seed.txt").write_text("edited", encoding="utf-8")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "refused_dirty"
    assert _stamp(clone).read_text(encoding="utf-8") == "sha:handedit\n"
    assert _git(clone, "rev-parse", "HEAD") == head


def test_a_held_publish_marker_leaves_the_dirty_stamp_untouched(clone_behind, tmp_path):
    from coordinator_core.warm import breadcrumb

    clone, _peer = clone_behind
    head = _git(clone, "rev-parse", "HEAD")
    _stamp(clone).write_text("sha:live-token\n", encoding="utf-8")
    live = _stamp(clone).read_bytes()

    with breadcrumb.hold_publish_marker(clone, holder="t"):
        payload = engine_converge.run_convergence_pass(
            clone=clone, fast_forward=_default_ff, record_to=tmp_path / "r.json"
        )

    assert payload["ok"] is False and payload["fast_forwarded"] is False
    assert _stamp(clone).read_bytes() == live
    assert _git(clone, "rev-parse", "HEAD") == head


def test_diverged_clone_is_refused(clone_behind, tmp_path):
    clone, _peer = clone_behind
    (clone / "local.txt").write_text("x", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "local")
    head = _git(clone, "rev-parse", "HEAD")

    payload = engine_converge.run_convergence_pass(
        clone=clone, fast_forward=_ff, record_to=tmp_path / "r.json"
    )

    assert payload["outcome"] == "refused_diverged"
    assert _git(clone, "rev-parse", "HEAD") == head


@pytest.fixture(autouse=True)
def _reset():
    engine_converge.reset_converge_for_test()
    push_cadence.reset_cadence_for_test()
    yield
    engine_converge.reset_converge_for_test()
