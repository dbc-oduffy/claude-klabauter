"""review.reverify never restores, removes, inverts or probes a path an open kept.json holds."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit.tests._kept_fixture import git, hold_kept
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, _REAL_POPEN, _ledger, _manifest_path, _revert, _row, world,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261025T000000-held01"
NEW = "tests/test_new.py"
NEW_HEAD = b"def test_n():\n    assert True\n"
NEW_BAD = b"def test_n():\n    assert False\n"


def _seed(root, monkeypatch, *, commit_new: bool) -> str:
    """Commit the snapshot files as the base; optionally commit ``NEW`` on top (HEAD tracks it,
    base does not). Writes ``NEW`` as created by review; leaves its worktree bytes at ``NEW_BAD``."""
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    git(root, "init", "-q")
    git(root, "add", "pkg", "tests/test_plain.py", "tests/test_mod.py")
    git(root, "commit", "-q", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    path = _manifest_path(root)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["base"] = base
    path.write_text(json.dumps(data), encoding="utf-8")
    if commit_new:
        (root / NEW).write_bytes(NEW_HEAD)
        git(root, "add", NEW)
        git(root, "commit", "-q", "-m", "new")
    (root / NEW).write_bytes(NEW_BAD)
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {NEW: "a1"})
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    return base


def _run(root, base, failing, **kw):
    return rr.review_reverify(
        root, base=base, slice_prefix=PREFIX, session_id=SESSION, failing=failing, **kw
    )


def test_held_tracked_created_path_keeps_its_bytes_and_names_the_run(world, monkeypatch):
    root, _orig, _ = world
    base = _seed(root, monkeypatch, commit_new=True)
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    hold_kept(root, RUN, [NEW])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    out = _run(root, base, [f"{NEW}::test_n"])
    assert out["error"] is None and out["reverted"] == []
    assert (root / NEW).read_bytes() == NEW_BAD
    assert out["kept"] == [{
        "path": NEW, "tests": [f"{NEW}::test_n"],
        "findings": [{"sidecar": None, "id": None, "reason": f"kept-work-held:{RUN}"}],
    }]


def test_unheld_tracked_created_path_is_still_restored_to_head(world, monkeypatch):
    root, _orig, _ = world
    base = _seed(root, monkeypatch, commit_new=True)
    out = _run(root, base, [f"{NEW}::test_n"])
    assert [(e["path"], e["action"], e["source"]) for e in out["reverted"]] == [(NEW, "restored", "head")]
    assert (root / NEW).read_bytes() == NEW_HEAD


def test_held_untracked_created_path_is_not_removed(world, monkeypatch):
    root, _orig, _ = world
    base = _seed(root, monkeypatch, commit_new=False)
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    hold_kept(root, RUN, [], post=[NEW])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    out = _run(root, base, [f"{NEW}::test_n"])
    assert out["error"] is None and out["reverted"] == []
    assert (root / NEW).read_bytes() == NEW_BAD


def test_held_edited_path_with_an_applied_finding_is_not_inverted(world, monkeypatch):
    root, _orig, _ = world
    (root / "pkg/mod.py").write_bytes(b"VALUE = 99\n")
    sidecars = _ledger(monkeypatch, _row("F1", "pkg/mod.py", "VALUE = 1\n", "VALUE = 99\n"))
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    hold_kept(root, RUN, ["pkg/mod.py"])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    out = _revert(root, ["tests/test_mod.py::test_v"], sidecars=sidecars)
    assert out["error"] is None and out["reverted"] == []
    assert (root / "pkg/mod.py").read_bytes() == b"VALUE = 99\n"
    assert [(k["path"], k["findings"][0]["reason"]) for k in out["kept"]] == [
        ("pkg/mod.py", f"kept-work-held:{RUN}")
    ]


def test_probe_open_skips_a_held_path(world, monkeypatch):
    root, _orig, _ = world
    (root / "tests/test_plain.py").write_bytes(b"def test_p():\n    assert nope\n")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    hold_kept(root, RUN, ["tests/test_plain.py"])
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("spawn")))
    out = _revert(root, ["tests/test_plain.py::test_p"], sidecars=sidecars, probe="open")
    assert out["error"] is None
    assert out["probed"] == [] and out["rerun"] == []
    assert (root / "tests/test_plain.py").read_bytes() == b"def test_p():\n    assert nope\n"


def test_probe_open_still_probes_an_unheld_path(world, monkeypatch):
    root, orig, _ = world
    (root / "tests/test_plain.py").write_bytes(b"def test_p():\n    assert nope\n")
    sidecars = _ledger(monkeypatch, _row("F1", "tests/test_plain.py", "    assert True\n", "    assert nope\n"))
    out = _revert(root, ["tests/test_plain.py::test_p"], sidecars=sidecars, probe="open")
    assert out["rerun"] == ["tests/test_plain.py::test_p"]
    assert (root / "tests/test_plain.py").read_bytes() == orig["tests/test_plain.py"]
