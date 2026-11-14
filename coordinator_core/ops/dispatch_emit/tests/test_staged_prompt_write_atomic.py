"""dispatch.emit's ask branch writes every prompts/<name>.md complete-or-absent: a reader never sees a partial stage brief."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import atomic_replace, warp_fire
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import PROMPTS_DIR
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.session_baton import store

pytestmark = pytest.mark.complete

SID = "sid-atomic"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "refs").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "hub" / SID).mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "hub" / s)
    assert PT.append_turn("split these two jobs please", session_id=SID)["ok"]
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


def _emit(root: Path) -> dict:
    seen: list[dict] = []

    def capture(params, _root):
        seen.append(params)
        return {"ok": True, "path": "scratch/x.mjs", "run_id": "ask-20261106T000000-aaaaaa", "fire_args": {}}

    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": "build it", "source": "relayed"}},
        "session_id": "sess-1",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }
    mod.evaluate(payload, emit=capture)
    params = {**seen[0], "target_root": root.as_posix(), "session_id": SID}
    return _dispatch_emit(params, repo_root=root)


def _prompts_dir(result: dict, root: Path) -> Path:
    return next((root / "state" / "warp").glob("*")) / PROMPTS_DIR


def test_staged_prompt_is_absent_until_whole_and_never_partial(repo, monkeypatch):
    real_replace = atomic_replace._replace_with_retry
    at_swap: list[tuple[bool, bytes]] = []

    def spy(src, dst):
        dst_p = Path(dst)
        if dst_p.parent.name == PROMPTS_DIR:
            at_swap.append((dst_p.exists(), Path(src).read_bytes()))
        return real_replace(src, dst)

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", spy)
    result = _emit(repo)
    assert result.get("ok", True) is not False, result
    staged = sorted(_prompts_dir(result, repo).glob("*.md"))
    assert staged, "the default emit staged no prompt"
    assert len(at_swap) >= len(staged)
    for existed, body in at_swap:
        assert not existed, "a staged prompt was visible before its complete bytes were swapped in"
    for path in staged:
        assert path.read_bytes() in {body for _, body in at_swap}
    assert not [p for p in _prompts_dir(result, repo).iterdir() if p.suffix != ".md"]


def test_failed_write_leaves_no_partial_prompt(repo, monkeypatch):
    real = atomic_replace._replace_with_retry

    def boom(src, dst):
        if Path(dst).parent.name == PROMPTS_DIR:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(atomic_replace, "_replace_with_retry", boom)
    try:
        _emit(repo)
    except OSError:
        pass
    leftovers = [p for d in (repo / "state" / "warp").glob("*/" + PROMPTS_DIR) for p in d.iterdir()]
    assert leftovers == [], f"a failed staged write left {leftovers}"
