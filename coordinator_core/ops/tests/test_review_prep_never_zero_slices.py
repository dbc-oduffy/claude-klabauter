"""review.prep never returns zero slices for a run whose write set has a real delta (default range mode)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-own"
PEER = "ask-peer"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "seed.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("t = 1\n", encoding="utf-8")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _stage(repo: Path, declared: list) -> None:
    d = record_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _handler(repo: Path, **kw) -> dict:
    head = _git(repo, "rev-parse", "HEAD").strip()
    params = {
        "base": head, "head": head, "slice_prefix": "prep-z", "plan_path": "docs/plans/p.md",
        "session_id": "sess-own", "run_id": RUN, **kw,
    }
    return rp._handler(params, repo)


def _sliced(reply: dict) -> list:
    assert reply["error"] is None
    assert reply["slices"], reply
    return [f for s in reply["slices"] for f in s["files"]]


def _hold(monkeypatch, held: dict) -> None:
    from coordinator_core.ops.dispatch_emit import land_run

    monkeypatch.setattr(land_run, "foreign_halted_withheld", lambda root, rid, plan, paths: dict(held))


def test_a_tracked_edit_left_in_the_worktree_is_sliced(repo, monkeypatch):
    (repo / "tracked.py").write_text("t = 2\n", encoding="utf-8")
    _stage(repo, ["tracked.py"])
    _hold(monkeypatch, {})
    reply = _handler(repo)
    assert _sliced(reply) == ["tracked.py"]
    assert reply["excluded"] == []


def test_b_an_untracked_new_file_is_sliced(repo, monkeypatch):
    (repo / "new_mod.py").write_text("n = 1\n", encoding="utf-8")
    _stage(repo, ["new_mod.py"])
    _hold(monkeypatch, {})
    reply = _handler(repo)
    assert _sliced(reply) == ["new_mod.py"]
    assert reply["excluded"] == []


def test_c_a_file_a_peer_also_holds_with_uncommitted_hunks_is_sliced(repo, monkeypatch):
    # Pins only that the peer-committed delta yields a slice; the own uncommitted hunk is not asserted.
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("t = 1\npeer = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "peer work\n\nSession-Id: sess-peer")
    peer_sha = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("t = 1\npeer = 1\nown = 1\n", encoding="utf-8")
    _stage(repo, ["tracked.py"])
    _hold(monkeypatch, {})
    reply = _handler(repo, base=base, head=peer_sha)
    assert _sliced(reply) == ["tracked.py"]
    assert reply["excluded"] == []
    assert peer_sha in (repo / reply["sidecar_path"]).read_text(encoding="utf-8")


def test_d_an_uncommitted_hunk_on_top_of_a_committed_checkpoint_is_in_the_slice(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("t = 1\ncheckpoint = 1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-q", "-m", "checkpoint\n\nSession-Id: sess-own")
    head = _git(repo, "rev-parse", "HEAD").strip()
    (repo / "tracked.py").write_text("t = 1\ncheckpoint = 1\nuncommitted_hunk = 1\n", encoding="utf-8")
    _stage(repo, ["tracked.py"])
    _hold(monkeypatch, {"tracked.py": PEER})
    reply = _handler(repo, base=base, head=head)
    assert _sliced(reply) == ["tracked.py"]
    diff = "".join(Path(s["diff_path"]).read_text(encoding="utf-8") for s in reply["slices"])
    assert "+uncommitted_hunk = 1" in diff
    assert "+checkpoint = 1" in diff


def test_e_the_only_write_withheld_by_a_halted_peer_is_sliced(repo, monkeypatch):
    (repo / "new_held.py").write_text("h = 1\n", encoding="utf-8")
    _stage(repo, ["new_held.py"])
    _hold(monkeypatch, {"new_held.py": PEER})
    reply = _handler(repo)
    assert _sliced(reply) == ["new_held.py"]
    assert reply["peer_held"] == ["new_held.py"]
    assert reply["excluded"] == []


TEST_FILES = ["coordinator_core/tests/test_spawn.py", "coordinator_core/distill/tests/test_guard.py"]


def _write_tests(repo: Path, body: str) -> None:
    for rel in TEST_FILES:
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")


def test_test_files_only_committed_range_is_sliced(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _write_tests(repo, "def test_x():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tests\n\nSession-Id: sess-own")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _stage(repo, TEST_FILES)
    _hold(monkeypatch, {})
    reply = _handler(repo, base=base, head=head)
    assert sorted(_sliced(reply)) == sorted(TEST_FILES)
    assert reply["excluded"] == []


def test_test_files_only_with_uncommitted_hunk_on_checkpoint_is_sliced(repo, monkeypatch):
    base = _git(repo, "rev-parse", "HEAD").strip()
    _write_tests(repo, "def test_x():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tests\n\nSession-Id: sess-own")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _write_tests(repo, "def test_x():\n    assert True\n\n\ndef test_y():\n    assert True\n")
    _stage(repo, TEST_FILES)
    _hold(monkeypatch, {})
    reply = _handler(repo, base=base, head=head)
    assert sorted(_sliced(reply)) == sorted(TEST_FILES)
    diff = "".join(Path(s["diff_path"]).read_text(encoding="utf-8") for s in reply["slices"])
    assert "+def test_y" in diff
    assert reply["excluded"] == []


def test_mode_only_range_delta_is_never_a_silent_zero_slice_reply(repo, monkeypatch):
    # A POSIX exec-bit flip freezes a non-empty diff with no ---/+++ lines; zero slices there must carry an error.
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-index", "--chmod=+x", "tracked.py")
    _git(repo, "commit", "-q", "-m", "chmod\n\nSession-Id: sess-own")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _stage(repo, ["tracked.py"])
    _hold(monkeypatch, {})
    reply = _handler(repo, base=base, head=head)
    assert Path(reply["whole_diff_path"]).stat().st_size > 0
    assert reply["slices"] == [] and reply["error"]
    assert "non-empty frozen diff" in reply["error"]
