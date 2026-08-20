"""
coordinator_core.ops.tests.test_artifact_emit_scope_touch — `artifact.emit`
declares its `state/cockpit-emission.json` write via `_scope_touch_paths`.

Purpose: pins the C4 fix of docs/plans/2026-08-05-in-process-writers-declare-their-writes.md.
The predecessor handoff misattributed the `state/cockpit-emission.json` write to
`ops/emit/sections/handoffs.py` (correctly ruled unfixable — a `collect(ctx)` returning a
tuple, never IPC-routed). The real write is `ops/emit/envelope.py::emit`'s
`out_path.write_text(...)`, but `artifact_emit._artifact_emit` IS registered as the
`"artifact.emit"` JSON-RPC op (`_registry_map.py`) and transits `coordinator_core.ipc.
dispatch_message`, so it carries the SIBLING plan's cheap `_scope_touch_paths` self-report
seam (docs/plans/2026-08-05-engine-ops-declare-what-they-write.md), not this plan's
expensive one.

Drives the REAL `artifact.emit` handler end-to-end through `dispatch_message` (mirrors
`coordinator_core/tests/test_ipc_scope_touch_self_report.py`'s
`test_real_queue_append_write_lands_in_compute_offer_safe_paths`, F3 precedent: exercise
the real registered handler, not a synthetic one) — `_envelope.resolve_context`/`.emit`
are patched only to avoid the full 21-section envelope build (and its `requires_vendor_pin`
fixture cost), not to bypass the declaration path under test.

Spec backlink: pln-in-process-engine-writers-decl-33016a § C4
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import coordinator_core.ipc as ipc
from coordinator_core.ipc import dispatch_message, _SCOPE_TOUCH_PATHS_KEY
import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (artifact.emit)
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.session.safe_commit_offer import compute_offer

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _run(coro):
    return asyncio.run(coro)


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _dispatch(method, params, origin_worktree=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}
    if origin_worktree is not None:
        msg["_origin_worktree"] = str(origin_worktree)
    return _run(dispatch_message(msg))


def _fake_ctx(repo_root: Path) -> MagicMock:
    ctx = MagicMock(spec=EmitContext)
    ctx.repo_root = repo_root
    ctx.central_state_root = repo_root / "state"
    ctx.repo_name = "fixture/artifact-emit-test"
    ctx.full_enrichment = True
    return ctx


def _fake_emit_writing_real_file(ctx, out=None):
    """Stand-in for `envelope.emit`: performs the SAME single write it does
    (`out_path.write_text(...)`) so the declared path exists on disk for
    `_record_self_reported_touches`'s containment/isfile check, without
    running the full 21-section envelope build.
    """
    out_path = Path(out) if out is not None else (ctx.central_state_root / "cockpit-emission.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"ok": True}) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "out": str(out_path),
        "schema_version": "test",
        "emitted_at": "2026-08-05T00:00:00Z",
        "section_count": 0,
        "malformed_counts": {},
        "malformed_total": 0,
    }


# ---------------------------------------------------------------------------
# PRIMARY: the emitted cockpit-emission.json lands in compute_offer's safe_paths
# after a real artifact.emit dispatch through ipc.dispatch_message.
# ---------------------------------------------------------------------------


def test_artifact_emit_declared_path_lands_in_compute_offer_safe_paths(tmp_path, monkeypatch):
    assert "artifact.emit" in ipc._REGISTRY, "import guard: artifact.emit not registered"
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-emit-1")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)

    fake_ctx = _fake_ctx(repo)
    with patch(
        "coordinator_core.ops.artifact_emit._envelope.resolve_context",
        return_value=fake_ctx,
    ), patch(
        "coordinator_core.ops.artifact_emit._envelope.emit",
        side_effect=_fake_emit_writing_real_file,
    ):
        d = _dispatch("artifact.emit", {}, origin_worktree=repo)

    assert "error" not in d, d
    assert _SCOPE_TOUCH_PATHS_KEY not in d["result"]
    out_path = Path(d["result"]["out"])
    assert out_path.is_file()

    # `.as_posix()`, not `str()`: `compute_offer` speaks the repo-relative
    # POSIX form by construction — `safe_commit_offer` normalizes `\` to `/`
    # and does all its path work in `posixpath`, matching what git itself
    # emits. `str()` on a WindowsPath yields backslashes, so this compared
    # the host's separator convention against the contract's and failed on
    # Windows regardless of whether the declared path actually landed.
    rel = out_path.relative_to(repo).as_posix()
    offer = compute_offer("sid-emit-1", cwd=str(repo))
    assert rel in offer["safe_paths"], (rel, offer)
    assert rel not in offer["orphans"]


# ---------------------------------------------------------------------------
# The reserved key never reaches the wire envelope.
# ---------------------------------------------------------------------------


def test_reserved_key_never_reaches_wire_envelope(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-emit-2")

    fake_ctx = _fake_ctx(repo)
    with patch(
        "coordinator_core.ops.artifact_emit._envelope.resolve_context",
        return_value=fake_ctx,
    ), patch(
        "coordinator_core.ops.artifact_emit._envelope.emit",
        side_effect=_fake_emit_writing_real_file,
    ):
        d = _dispatch("artifact.emit", {}, origin_worktree=repo)

    assert "error" not in d, d
    assert _SCOPE_TOUCH_PATHS_KEY not in d["result"]


# ---------------------------------------------------------------------------
# Custom `out` param: the declared path tracks the ACTUAL out path, not a
# hardcoded default (AC4-adjacent — never a constant).
# ---------------------------------------------------------------------------


def test_artifact_emit_custom_out_path_is_the_one_declared(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sid-emit-3")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    custom_out = repo / "state" / "custom-emission.json"

    fake_ctx = _fake_ctx(repo)
    with patch(
        "coordinator_core.ops.artifact_emit._envelope.resolve_context",
        return_value=fake_ctx,
    ), patch(
        "coordinator_core.ops.artifact_emit._envelope.emit",
        side_effect=_fake_emit_writing_real_file,
    ):
        d = _dispatch("artifact.emit", {"out": str(custom_out)}, origin_worktree=repo)

    assert "error" not in d, d
    assert d["result"]["out"] == str(custom_out)

    offer = compute_offer("sid-emit-3", cwd=str(repo))
    assert "state/custom-emission.json" in offer["safe_paths"]
