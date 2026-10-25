"""review.reverify cases that commit a seed repo with real git."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.tests.test_review_reverify import (
    _NOWIN,
    _REAL_POPEN,
    PREFIX,
    SESSION,
    _ledger,
    _manifest_path,
    _row,
    _set_base,
    _sha,
    world,  # noqa: F401  (fixture)
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _commit_seed(root, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", _REAL_POPEN)
    for argv in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
                 ["add", "pkg", "tests"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True, creationflags=_NOWIN)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, creationflags=_NOWIN
    ).stdout.strip()


@pytest.mark.parametrize("anchor", ["abc1234", "z" * 40])
def test_head_without_a_valid_sha_anchor_fails_closed(world, monkeypatch, anchor):
    root, _orig, _ = world
    _commit_seed(root, monkeypatch)
    _set_base(root, anchor)
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    out = rr.review_reverify(
        root, base=anchor, slice_prefix=PREFIX, session_id=SESSION, failing=["tests/test_plain.py::test_p"]
    )
    assert "unreadable" in out["error"] and out["reverted"] == []
    assert (root / "tests/test_plain.py").read_text(encoding="utf-8") == "def test_p():\n    assert nope\n"
    listed = rr.review_reverify(root, base=anchor, slice_prefix=PREFIX, session_id=SESSION)
    assert listed["head_unreadable"] is True


@pytest.mark.parametrize("snapshot_uses_foo, refused", [(True, True), (False, False)])
def test_reverted_caller_is_scanned_at_its_post_revert_bytes(world, monkeypatch, snapshot_uses_foo, refused):
    """tests/test_plain.py is committed without FOO; its snapshot bytes import FOO from a created
    module that the same revert removes. Only the snapshot bytes (post-revert) decide."""
    root, _orig, _ = world
    snap_bytes = (
        b"from pkg.new import FOO\n\n\ndef test_p():\n    assert FOO\n"
        if snapshot_uses_foo
        else b"def test_p():\n    assert True\n"
    )
    snap = _manifest_path(root).parent
    (snap / _sha(snap_bytes)).write_bytes(snap_bytes)
    data = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    data["files"]["tests/test_plain.py"] = _sha(snap_bytes)
    _manifest_path(root).write_text(json.dumps(data), encoding="utf-8")
    base = _commit_seed(root, monkeypatch)
    _set_base(root, base)
    (root / "pkg/new.py").write_text("FOO = 1\n", encoding="utf-8")
    (root / "tests/test_plain.py").write_text("def test_p():\n    assert nope\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {"pkg/new.py": "a1"})
    sidecars = _ledger(
        monkeypatch, _row("F1", "tests/test_plain.py", snap_bytes.decode(), "def test_p():\n    assert nope\n")
    )
    out = rr.review_reverify(
        root, base=base, slice_prefix=PREFIX, session_id=SESSION,
        failing=["tests/test_plain.py::test_p", "pkg/new.py::x"], sidecars=sidecars,
    )
    if refused:
        assert "referencing removed pkg.new.FOO" in out["error"]
        assert (root / "pkg/new.py").exists()
    else:
        assert out["error"] is None
        assert not (root / "pkg/new.py").exists()
