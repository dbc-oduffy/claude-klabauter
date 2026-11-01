"""A run's own re-verify edit refreshes the commit expectation; a foreign edit after it still refuses."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401
    _fire,
    _git,
    _record_at,
    _sha,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SIDECAR = ".coordinator-local/subagent-share/reverify.md"


def _setup(repo, monkeypatch, *, edit_at: float, sidecar_at: float, hashes: dict | None = None) -> dict:
    """The run wrote ``A``, then the re-verify step rewrote the file to ``B`` (mtime ``edit_at``)
    and finished (sidecar mtime ``sidecar_at``); the engine's touch record holds only the ``A`` write."""
    monkeypatch.setattr(review_edits, "review_writes", lambda root, sid, since: {})
    target = repo / "a.py"
    target.write_text("B\n", encoding="utf-8", newline="")
    sidecar = repo / SIDECAR
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("done\n", encoding="utf-8")
    os.utime(target, (edit_at, edit_at))
    os.utime(sidecar, (sidecar_at, sidecar_at))
    _record_at(repo, "a.py", _sha("A\n"), edit_at - 100)
    block = {"edited": ["a.py"], "sidecar_path": SIDECAR}
    if hashes is not None:
        block["edited_sha256"] = hashes
    return reviewed(reverify=block)


def test_a_reverify_edit_lands_at_its_own_bytes(repo, monkeypatch):
    now = time.time()
    review = _setup(repo, monkeypatch, edit_at=now - 50, sidecar_at=now - 40)
    out, seen = _fire(repo, monkeypatch, review)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_a_foreign_edit_after_the_reverify_step_still_refuses(repo, monkeypatch):
    now = time.time()
    review = _setup(repo, monkeypatch, edit_at=now, sidecar_at=now - 40)
    out, seen = _fire(repo, monkeypatch, review)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}
    assert _git(["log", "--oneline"], repo).count("\n") == 0


def test_a_reverify_record_without_a_resolvable_finish_keeps_the_old_expectation(repo, monkeypatch):
    now = time.time()
    review = _setup(repo, monkeypatch, edit_at=now - 50, sidecar_at=now - 40)
    (repo / SIDECAR).unlink()
    out, _ = _fire(repo, monkeypatch, review)
    assert out["refused"] == "blob-drift"


def test_a_recorded_hash_lands_the_matching_bytes_without_the_mtime_test(repo, monkeypatch):
    now = time.time()
    review = _setup(repo, monkeypatch, edit_at=now, sidecar_at=now - 40, hashes={"a.py": _sha("B\n")})
    out, seen = _fire(repo, monkeypatch, review)
    assert out["committed"] is True, out
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("B\n")}


def test_a_recorded_hash_refuses_a_foreign_edit_that_predates_the_finish(repo, monkeypatch):
    now = time.time()
    review = _setup(repo, monkeypatch, edit_at=now - 50, sidecar_at=now - 40, hashes={"a.py": _sha("C\n")})
    out, seen = _fire(repo, monkeypatch, review)
    assert out["refused"] == "blob-drift"
    assert seen[0]["expected_content_sha256"] == {"a.py": _sha("A\n")}
