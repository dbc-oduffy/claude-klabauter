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
    # The refusal log is the deny's own durable record, not a write the refusal must precede.
    log_dir = Path(mod.REFUSAL_LOG_REL).parts[0]
    return sorted(
        str(p) for r in roots for p in r.rglob("*") if ".git" not in p.parts and log_dir not in p.relative_to(r).parts
    )


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
    assert str(other) in reason and "a session rooted in" not in reason and "cross_repo_approved" in reason
    assert "repo?" in reason, "the usage line carries the `repo` key the refusal suggests"
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
    assert "a session rooted in" not in str(hso.get("permissionDecisionReason", ""))
    assert emit.calls and emit.calls[0][1].resolve() == other.resolve()


def test_an_explicit_repo_naming_a_registered_sibling_is_admitted(repos, monkeypatch):
    own, other = repos
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_registered_repo",
        lambda target, env=None: Path(target).resolve() == other.resolve(),
    )
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other)}), emit=emit)
    assert emit.calls and emit.calls[0][1].resolve() == other.resolve()


def _registry_says_registered(monkeypatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_registered_repo",
        lambda target, env=None: True,
    )


def test_naming_the_sessions_own_registered_repo_grants_no_approval(repos, monkeypatch):
    own, _ = repos
    _registry_says_registered(monkeypatch)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(own)}), emit=emit)
    assert emit.calls and "cross_repo_approved" not in emit.calls[0][0]


def test_a_sizing_naming_the_sessions_own_registered_repo_grants_no_approval(repos, monkeypatch):
    own, _ = repos
    (own / "state" / "sizings").mkdir(parents=True)
    (own / SIZING_REL).write_text("title: x\n", encoding="utf-8")
    _registry_says_registered(monkeypatch)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"sizing": SIZING_REL, "repo": str(own)}), emit=emit)
    assert emit.calls and "cross_repo_approved" not in emit.calls[0][0]


def test_the_explicit_flag_still_approves_the_sessions_own_repo(repos, monkeypatch):
    own, _ = repos
    _registry_says_registered(monkeypatch)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(own), "cross_repo_approved": True}), emit=emit)
    assert emit.calls and emit.calls[0][0].get("cross_repo_approved") is True


def test_a_registered_sibling_still_carries_the_approval(repos, monkeypatch):
    own, other = repos
    _registry_says_registered(monkeypatch)
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other)}), emit=emit)
    assert emit.calls and emit.calls[0][0].get("cross_repo_approved") is True


def test_a_mint_makes_the_sessions_next_nag_name_the_fire_not_a_submit(repos, monkeypatch, tmp_path):
    own, _ = repos
    state = tmp_path / "nag-state"
    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: state)
    envelope = mod._premint_deferred_envelope(own, {"ask": "a", "session_id": "sess-mint"}, "ask-run-1")
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "allow"
    for host in (None, "codex"):
        if host:
            mod._premint_deferred_envelope(own, {"ask": "a", "session_id": "sess-mint"}, "ask-run-2")
        text = warp_fire.nag_text("sess-mint", host=host)
        assert "workflow.fire" in text and "do not call engine_ask.submit again" in text
        assert warp_fire.fire_command(host=host) not in text
    assert warp_fire.nag_text("sess-mint") == ""
    assert warp_fire.nag_text("sess-mint", repeat=True) == "[engine-ask nag #4] call `" + warp_fire.fire_command() + "`, once."


def test_another_sessions_nag_and_a_stale_mint_still_name_the_submit(repos, monkeypatch, tmp_path):
    own, _ = repos
    state = tmp_path / "nag-state"
    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: state)
    mod._premint_deferred_envelope(own, {"ask": "a", "session_id": "sess-mint"}, "ask-run-1")
    assert warp_fire.nag_text("sess-other") == warp_fire.fire_step()
    monkeypatch.setattr(warp_fire, "_MINTED_TTL_S", -1)
    assert warp_fire.fire_command() in warp_fire.nag_text("sess-mint")


def test_a_mint_note_with_no_readable_path_still_names_the_fire(monkeypatch, tmp_path):
    state = tmp_path / "nag-state"
    monkeypatch.setattr(warp_fire, "_counter_dir", lambda: state)
    warp_fire.note_minted("sess-blank", "", state)
    text = warp_fire.nag_text("sess-blank")
    assert "workflow.fire" in text and warp_fire.fire_command() not in text
    assert not (state / "sess-blank.minted").exists()


def test_an_unregistered_explicit_repo_is_still_refused(repos, monkeypatch):
    own, other = repos
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_registered_repo",
        lambda target, env=None: False,
    )
    _assert_refused_before_any_write(own, other, {"ask": "a", "repo": str(other)}, _Recorder(), _Recorder())


def test_a_registered_repo_reached_without_repo_arg_is_still_refused(repos, monkeypatch):
    own, other = repos
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_registered_repo",
        lambda target, env=None: True,
    )
    monkeypatch.setattr(mod, "_context_home", lambda root, paths: other)
    (other / "note.md").write_text("n\n", encoding="utf-8")
    _assert_refused_before_any_write(
        own, other, {"ask": "a", "context": [str(other / "note.md")]}, _Recorder(), _Recorder()
    )


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


def _origin(root: Path, url: str) -> None:
    (root / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")


def _registry_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability._all_registered_repo_roots", lambda env=None: []
    )
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability.target_is_registered_repo", lambda t, env=None: False
    )


@pytest.mark.parametrize("args_of", [
    lambda other: {"sizing": str(other / SIZING_REL), "repo": str(other)},
    lambda other: {"ask": "a", "repo": str(other)},
])
def test_a_second_clone_of_the_sessions_repo_is_admitted_on_origin_not_path(repos, monkeypatch, args_of):
    # The 2026-11-22 fake blocker: a same-origin clone on another branch, in another dir, read as a foreign repo.
    own, other = repos
    _registry_empty(monkeypatch)
    _origin(own, "https://github.com/acme/thing.git")
    _origin(other, "git@github.com:Acme/thing")
    emit = _Recorder()
    out = mod.evaluate(_payload(own, args_of(other)), emit=emit)
    assert "is neither this session's repo" not in str((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason", ""))
    assert emit.calls and emit.calls[0][1].resolve() == other.resolve()


def test_a_clone_of_a_registered_repo_is_admitted(repos, tmp_path, monkeypatch):
    own, other = repos
    reg = _repo(tmp_path, "registered")
    _origin(reg, "https://github.com/acme/reg.git")
    _origin(other, "https://github.com/acme/reg")
    monkeypatch.setattr(
        "coordinator_core.bash_guards._write_bump_applicability._all_registered_repo_roots", lambda env=None: [str(reg)]
    )
    emit = _Recorder()
    mod.evaluate(_payload(own, {"ask": "a", "repo": str(other)}), emit=emit)
    assert emit.calls and emit.calls[0][1].resolve() == other.resolve()


def test_a_different_origin_is_still_refused(repos, monkeypatch):
    own, other = repos
    _registry_empty(monkeypatch)
    _origin(own, "https://github.com/acme/thing.git")
    _origin(other, "https://github.com/acme/other.git")
    _assert_refused_before_any_write(own, other, {"ask": "a", "repo": str(other)}, _Recorder(), _Recorder())
