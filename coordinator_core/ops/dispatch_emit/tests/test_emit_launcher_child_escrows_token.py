"""dispatch.emit --ask: a launcher child escrows its run token and leaves it out of the reply; a standalone ask does not."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import op, run_token
from coordinator_core.ops.dispatch_emit.tests.test_sizing_fire_wiring import env  # noqa: F401


def _escrow_files(root):
    return list(root.rglob("escrow/*.json"))


def test_launcher_child_escrows_the_minted_token_and_replies_without_it(env):  # noqa: F811
    reply = op._dispatch_emit({"ask": "add a widget", "launcher_child": True}, repo_root=env.root)
    assert "runToken" not in reply["fire_args"]
    token = run_token.escrowed(env.root, reply["run_id"])
    assert token is not None and run_token.TOKEN_RE.match(token)
    assert run_token.admits(env.root, reply["run_id"], token, f"scratch/warp/{reply['run_id']}.workflow.mjs")


def test_escrowed_token_appears_nowhere_in_the_worktree(env):  # noqa: F811
    reply = op._dispatch_emit({"ask": "add a widget", "launcher_child": True}, repo_root=env.root)
    token = run_token.escrowed(env.root, reply["run_id"]).encode()
    assert token not in repr(reply).encode()
    for path in env.root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            assert token not in path.read_bytes(), path


def test_standalone_ask_replies_with_the_token_and_writes_no_escrow(env):  # noqa: F811
    reply = op._dispatch_emit({"ask": "add a widget"}, repo_root=env.root)
    assert run_token.TOKEN_RE.match(reply["fire_args"]["runToken"])
    assert _escrow_files(env.root) == []


def test_failed_escrow_keeps_the_token_in_the_reply(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(run_token, "escrow", lambda *a, **k: False)
    reply = op._dispatch_emit({"ask": "add a widget", "launcher_child": True}, repo_root=env.root)
    assert run_token.TOKEN_RE.match(reply["fire_args"]["runToken"])
