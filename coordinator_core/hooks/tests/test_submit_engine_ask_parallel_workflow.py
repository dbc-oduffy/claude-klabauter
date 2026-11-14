"""Two Workflow submissions arriving together at `hooks.submit_engine_ask.evaluate` (the warm engine
serves a parallel tool-call pair on two threads of one process) each get their own rewrite: the hook's
read budget is per call, never process-global."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import run_token

pytestmark = pytest.mark.complete

_WAIT_S = 10.0


def _repo(base: Path) -> Path:
    git = base / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return base


def _payload(root: Path, ask: str, session: str) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": ask}},
        "session_id": session,
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


class _ParkedEmit:
    """`dispatch.emit` stand-in that runs `during` once inside the emit, then returns a minted run."""

    def __init__(self, run_id: str, during) -> None:
        self.run_id = run_id
        self.during = during

    def __call__(self, params: dict, root: Path) -> dict:
        self.during()
        script = root / "scratch" / "warp" / f"{self.run_id}.workflow.mjs"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("export const meta = {}\n", encoding="utf-8")
        run_token.mint(root, self.run_id, str(script), session_id=params.get("session_id"))
        return {"ok": True, "path": str(script), "run_id": self.run_id, "fire_args": {"repoRoot": str(root)}}


@pytest.fixture(autouse=True)
def _local_venue(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)


def test_two_parallel_submissions_both_rewrite_and_do_not_share_a_read_budget(tmp_path):
    root_a, root_b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    a_parked, b_spent, a_read = threading.Event(), threading.Event(), threading.Event()
    seen: dict = {}

    def a_during() -> None:
        a_parked.set()
        assert b_spent.wait(_WAIT_S)
        seen["a_wait_ms"] = mod._wait_ms(None)
        a_read.set()

    def b_during() -> None:
        assert a_parked.wait(_WAIT_S)
        mod._charge(time.monotonic() - 1.0)  # B spends its whole read budget
        seen["b_wait_ms"] = mod._wait_ms(None)
        b_spent.set()
        assert a_read.wait(_WAIT_S)

    results: dict = {}

    def submit(tag: str, root: Path, emit) -> None:
        try:
            results[tag] = mod.evaluate(_payload(root, f"ask {tag}", f"sess-{tag}"), emit=emit)
        except BaseException as exc:  # noqa: BLE001 - the assertion below names it
            results[tag] = exc

    threads = [
        threading.Thread(target=submit, args=("a", root_a, _ParkedEmit("ask-20261113T000000-aaaaaa", a_during))),
        threading.Thread(target=submit, args=("b", root_b, _ParkedEmit("ask-20261113T000000-bbbbbb", b_during))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT_S * 2)

    assert seen["b_wait_ms"] <= 0.0
    assert seen["a_wait_ms"] > 0.0, "B's spend leaked into A's read budget"
    for tag in ("a", "b"):
        out = results[tag]
        assert isinstance(out, dict), f"{tag} raised {out!r}"
        hso = out["hookSpecificOutput"]
        assert hso["permissionDecision"] == "allow", hso.get("permissionDecisionReason")
        assert hso["updatedInput"].get("scriptPath"), f"{tag} was not rewritten"


def test_a_finished_submission_leaves_a_parallel_ones_budget_intact(tmp_path):
    root_a, root_b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    a_parked, b_done = threading.Event(), threading.Event()
    seen: dict = {}
    results: dict = {}

    def a_during() -> None:
        a_parked.set()
        assert b_done.wait(_WAIT_S)
        seen["budget_present"] = mod._budget() is not None

    def submit_b() -> None:
        assert a_parked.wait(_WAIT_S)
        results["b"] = mod.evaluate(
            _payload(root_b, "ask b", "sess-b"), emit=_ParkedEmit("ask-20261113T000000-bbbbbb", lambda: None)
        )
        b_done.set()

    def submit_a() -> None:
        results["a"] = mod.evaluate(
            _payload(root_a, "ask a", "sess-a"), emit=_ParkedEmit("ask-20261113T000000-aaaaaa", a_during)
        )

    threads = [threading.Thread(target=submit_a), threading.Thread(target=submit_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_WAIT_S * 2)

    assert seen["budget_present"], "B's exit cleared A's read budget"
    assert all(results[t]["hookSpecificOutput"]["permissionDecision"] == "allow" for t in ("a", "b"))
