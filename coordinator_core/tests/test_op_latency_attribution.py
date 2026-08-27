"""
coordinator_core.tests.test_op_latency_attribution — warm-served rows
attribute to the CALLER's repo, not the server's cwd (C7,
2026-08-20-a-refusal-cannot-exit-zero).

Purpose: ``op_latency._write_entry`` falls back to ``Path.cwd()`` when the
envelope carries no ``_origin_worktree`` — true for every op NOT in
``WORKTREE_SCOPED_OPS`` (see ``invoke.__main__.main``'s injection gate),
since that field is only ever stamped for worktree-scoped ops. In a warm
pool worker, ``Path.cwd()`` is the SERVER's cwd (the persistent klabauter
clone), not the caller's — so warm-served rows for those ops were silently
attributed to the wrong repo's sink while the identical op, served cold,
landed correctly. This corrupts the very denominator the warmth sweeps
measure. Fixed by having ``invoke.__main__.main`` stamp the caller's actual
process cwd into a telemetry-only ``_caller_cwd`` envelope field
unconditionally (regardless of op scope), and having ``ipc.dispatch_message``
prefer ``ipc.resolve_caller_cwd`` over letting ``op_latency._write_entry``
fall back to its own process's cwd.

This test exercises ``ipc.dispatch_message`` directly (not the CLI or the
warm server) with a "none"-scoped test op, simulating the warm-worker
condition by chdir'ing the TEST process itself to a directory that is
neither the caller's nor associated with the caller's repo, while the
envelope carries ``_caller_cwd`` pointing at a distinct "caller" directory.
A correct fix resolves the sink from the caller's directory; the pre-fix
behaviour resolves it from the test process's own (post-chdir) cwd instead.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C7.md
               docs/wiki/machine-load-norm.md
"""

from __future__ import annotations

import asyncio
import json

import coordinator_core.ipc as ipc
from coordinator_core.ipc import _REGISTRY, dispatch_message


def _run(coro):
    return asyncio.run(coro)


class _RegistryScope:
    def __init__(self, handlers: dict) -> None:
        self._handlers = handlers
        self._saved: dict = {}

    def __enter__(self):
        for name in self._handlers:
            self._saved[name] = _REGISTRY.get(name)
        _REGISTRY.update(self._handlers)
        return self

    def __exit__(self, *_):
        for name, old in self._saved.items():
            if old is None:
                _REGISTRY.pop(name, None)
            else:
                _REGISTRY[name] = old


def _sync_handler(params: dict, ctx=None, repo_root=None) -> dict:
    return {"echo": params}


_TEST_HANDLERS = {"test.attribution_sync": _sync_handler}


def _sink_for(fake_common_dir):
    return fake_common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def test_none_scoped_op_served_warm_attributes_to_caller_not_server(tmp_path, monkeypatch):
    caller_dir = tmp_path / "caller-repo"
    caller_dir.mkdir()
    caller_common_dir = caller_dir / ".git"
    caller_common_dir.mkdir()

    server_dir = tmp_path / "server-clone"
    server_dir.mkdir()
    server_common_dir = server_dir / ".git"
    server_common_dir.mkdir()

    def _fake_git_common_dir(repo_root):
        from pathlib import Path
        repo_root = Path(repo_root).resolve()
        if repo_root == caller_dir.resolve():
            return caller_common_dir
        if repo_root == server_dir.resolve():
            return server_common_dir
        raise RuntimeError(f"unexpected repo_root in test: {repo_root}")

    monkeypatch.setattr("coordinator_core.lifecycle.git_common_dir", _fake_git_common_dir)
    # Simulate a warm pool worker: the SERVING process's cwd is the server's
    # clone, not the caller's — this is what `Path.cwd()` would resolve to
    # inside `_write_entry` absent the C7 fix.
    monkeypatch.chdir(server_dir)

    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "test.attribution_sync",
        "params": {},
        # No _origin_worktree — this is a "none"-scoped op, exactly the
        # population WORKTREE_SCOPED_OPS excludes from that field.
        "_caller_cwd": str(caller_dir),
    }

    with _RegistryScope(_TEST_HANDLERS):
        d = _run(dispatch_message(msg))
    assert "error" not in d

    caller_sink = _sink_for(caller_common_dir)
    server_sink = _sink_for(server_common_dir)

    assert caller_sink.exists(), (
        "warm-served row was not attributed to the caller's repo — "
        "op_latency fell back to the serving process's own cwd"
    )
    assert not server_sink.exists(), (
        "warm-served row was attributed to the SERVER's cwd instead of "
        "the caller's — this is the exact defect C7 fixes"
    )

    lines = [json.loads(l) for l in caller_sink.read_text(encoding="utf-8").splitlines()]
    # started + complete + the chokepoint process_time row. The count is
    # bookkeeping; the invariant under test is that EVERY row a dispatch emits
    # lands on the caller's repo, so the process-time row is asserted here
    # alongside the other two rather than excluded from the check.
    assert {line["kind"] for line in lines} == {"started", "complete", "process_time"}
    assert len(lines) == 3
    for line in lines:
        assert line["repo_key"] == str(caller_common_dir)


def test_resolve_caller_cwd_absent_field_returns_none():
    assert ipc.resolve_caller_cwd({}) is None
    assert ipc.resolve_caller_cwd({"_caller_cwd": ""}) is None
    assert ipc.resolve_caller_cwd({"_caller_cwd": 123}) is None


def test_resolve_caller_cwd_present_field_returns_path():
    from pathlib import Path

    assert ipc.resolve_caller_cwd({"_caller_cwd": "/some/dir"}) == Path("/some/dir")
