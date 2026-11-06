"""An engine-ask `repo` that is a registry key (`repos.<key>`) resolves to the registered checkout; an unknown
key or a relative path that does not exist is refused naming the value, never walked up to the session anchor."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import warp_fire
from coordinator_core.hooks import submit_engine_ask as mod


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


def _payload(own: Path, args: dict) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": args},
        "session_id": "sess-registry-key",
        "transcript_path": str(own / "transcript.jsonl"),
        "cwd": str(own),
        "env": {"CLAUDE_PROJECT_DIR": str(own)},
    }


def _reason(out: dict) -> str:
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", out
    return hso["permissionDecisionReason"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(mod, "_is_first_turn", lambda _p: True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    own = _repo(tmp_path, "own")
    sib = _repo(tmp_path, "sib")
    from coordinator_core import machine_resolver
    from coordinator_core.bash_guards import _write_bump_applicability as wb

    table = {"repos.sib": str(sib), "engine.working_repos.wsib": str(sib)}
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key: table.get(key))
    registered = {str(sib.resolve())}
    monkeypatch.setattr(wb, "target_is_registered_repo", lambda t, env=None: str(Path(t).resolve()) in registered)
    return own, sib


@pytest.mark.parametrize("key", ["repos.sib", "engine.working_repos.wsib"])
def test_a_registry_key_repo_runs_in_the_registered_checkout_without_approval(world, key):
    own, sib = world
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "do it", "repo": key}), emit=emit, fanout=_Recorder())
    assert len(emit.calls) == 1 and emit.calls[0][1].resolve() == sib.resolve()


def test_a_registry_key_in_a_single_item_asks_resolves_the_same_way(world):
    own, sib = world
    emit = _Recorder()
    mod.evaluate(_payload(own, {"asks": [{"ask": "do it", "repo": "repos.sib"}]}), emit=emit, fanout=_Recorder())
    assert emit.calls and emit.calls[0][1].resolve() == sib.resolve()


def test_a_registry_key_on_an_asks_entry_resolves_to_its_checkout(world):
    own, sib = world
    fan = _Recorder()
    mod.evaluate(
            _payload(own, {"asks": [{"ask": "a", "repo": "repos.sib"}, {"ask": "b", "repo": "repos.sib"}]}),
            emit=_Recorder(), fanout=fan,
        )
    assert fan.calls and fan.calls[0][1].resolve() == sib.resolve()


def test_an_unknown_registry_key_is_refused_naming_the_key(world):
    own, _sib = world
    emit = _Recorder()
    reason = _reason(mod.evaluate(_payload(own, {"ask": "x", "repo": "repos.nope"}), emit=emit, fanout=_Recorder()))
    assert "repos.nope" in reason and emit.calls == []


def test_a_relative_repo_that_does_not_exist_is_refused_not_walked_up_to_the_anchor(world):
    own, _sib = world
    emit = _Recorder()
    reason = _reason(mod.evaluate(_payload(own, {"ask": "x", "repo": "ghost/sub"}), emit=emit, fanout=_Recorder()))
    assert "ghost/sub" in reason and emit.calls == []


def test_a_relative_repo_that_exists_still_resolves_under_the_anchor(world):
    own, _sib = world
    inner = _repo(own, "inner")
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "x", "repo": "inner", "cross_repo_approved": True}), emit=emit, fanout=_Recorder())
    assert emit.calls and emit.calls[0][1].resolve() == inner.resolve()
