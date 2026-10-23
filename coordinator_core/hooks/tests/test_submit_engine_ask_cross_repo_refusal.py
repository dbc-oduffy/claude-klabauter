"""R1: an engine-ask whose run root is another checkout is refused at submission, before any write."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod

SIZING_REL = "state/sizings/2026-10-23-x.yaml"


def _repo(base: Path, name: str) -> Path:
    root = base / name
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "refs").mkdir()
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


class _Recorder:
    def __init__(self):
        self.calls: list = []

    def __call__(self, params, root):
        self.calls.append((params, root))
        raise RuntimeError("recorder reached")


def _listing(*roots: Path) -> list[str]:
    return sorted(str(p) for r in roots for p in r.rglob("*") if ".git" not in p.parts)


def _payload(own: Path, args, *, anchored: bool = True) -> dict:
    payload = {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-xrepo",
        "transcript_path": str(own / "transcript.jsonl"),
        "cwd": str(own),
    }
    if anchored:
        payload["env"] = {"CLAUDE_PROJECT_DIR": str(own)}
    return payload


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _p: True)


@pytest.fixture
def repos(tmp_path):
    own = _repo(tmp_path, "own")
    other = _repo(tmp_path, "other")
    (other / "state" / "sizings").mkdir(parents=True)
    (other / SIZING_REL).write_text("title: x\n", encoding="utf-8")
    return own, other


def _denied(out: dict) -> str:
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", out
    return hso["permissionDecisionReason"]


def _assert_refused_before_any_write(own, other, args, emit, fanout, **kw):
    before = _listing(own, other)
    reason = _denied(mod.evaluate(_payload(own, args, **kw), emit=emit, fanout=fanout))
    assert str(other) in reason and "Fire it from a session rooted in" in reason
    assert emit.calls == [] and fanout.calls == []
    assert _listing(own, other) == before


def test_ask_with_a_foreign_repo_is_refused(repos):
    own, other = repos
    _assert_refused_before_any_write(own, other, {"ask": "a", "repo": str(other)}, _Recorder(), _Recorder())


def test_approval_clears_the_refusal(repos):
    own, other = repos
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other), "cross_repo_approved": True}), emit=emit)
    assert emit.calls and emit.calls[0][0].get("cross_repo_approved") is True


def test_a_remote_venue_clears_the_refusal(repos, monkeypatch):
    own, other = repos
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other)}), emit=emit)
    assert emit.calls and emit.calls[0][0].get("cross_repo_approved") is True


def test_asks_with_a_foreign_repo_is_refused(repos):
    own, other = repos
    args = {"asks": [{"ask": "a", "label": "a"}, {"ask": "b", "label": "b"}], "repo": str(other)}
    _assert_refused_before_any_write(own, other, args, _Recorder(), _Recorder())


def test_sizing_without_research_and_a_foreign_repo_is_refused(repos):
    own, other = repos
    _assert_refused_before_any_write(own, other, {"sizing": SIZING_REL, "repo": str(other)}, _Recorder(), _Recorder())


def test_a_context_rerooted_ask_is_refused(repos, monkeypatch):
    own, other = repos
    monkeypatch.setattr(mod, "_context_home", lambda root, paths: other)
    (other / "note.md").write_text("n\n", encoding="utf-8")
    args = {"ask": "a", "context": [str(other / "note.md")]}
    _assert_refused_before_any_write(own, other, args, _Recorder(), _Recorder())


def test_a_research_sizing_in_a_foreign_repo_is_admitted(repos):
    own, other = repos
    (other / SIZING_REL).write_text("title: x\nresearch:\n  question: q\n", encoding="utf-8")
    emit = _Recorder()
    out = mod.evaluate(_payload(own, {"sizing": SIZING_REL, "repo": str(other)}), emit=emit)
    hso = out.get("hookSpecificOutput") or {}
    assert "Fire it from a session rooted in" not in str(hso.get("permissionDecisionReason", ""))
    assert emit.calls and emit.calls[0][1].resolve() == other.resolve()


def test_the_sessions_own_repo_is_admitted(repos):
    own, _ = repos
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(own)}), emit=emit)
    assert emit.calls


def test_an_unanchored_session_is_refused(repos):
    own, other = repos
    _assert_refused_before_any_write(own, other, {"ask": "a", "repo": str(other)}, _Recorder(), _Recorder(), anchored=False)


def test_a_stood_down_environment_is_refused(repos, monkeypatch):
    own, other = repos
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_stand_down.environment_stands_the_bump_down",
        lambda env: "stand-down",
    )
    _assert_refused_before_any_write(own, other, {"ask": "a", "repo": str(other)}, _Recorder(), _Recorder())
