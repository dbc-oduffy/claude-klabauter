"""submit_engine_ask.evaluate: a session whose anchor repo and launch repo differ is refused before any emit."""

from __future__ import annotations

import pytest

from coordinator_core import warp_fire
from coordinator_core.bash_guards import _write_bump_applicability as wba
from coordinator_core.hooks import submit_engine_ask as mod


class _Boom:
    def __init__(self):
        self.calls = 0

    def __call__(self, *a, **k):
        self.calls += 1
        raise RuntimeError("must not be reached")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(mod, "_is_first_turn", lambda _p: True)


def _payload(tmp_path, **args):
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": warp_fire.SUBMIT_WORKFLOW_NAME, "args": {"ask": "do x", **args}},
        "session_id": "sess-disagree",
        "transcript_path": str(tmp_path / "t.jsonl"),
        "cwd": str(tmp_path),
    }


def _reason(out):
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "deny", out
    return hso["permissionDecisionReason"]


def test_disagreement_refused_naming_both_paths_without_emit(tmp_path, monkeypatch):
    monkeypatch.setattr(wba, "launch_anchor_disagreement", lambda *a, **k: ("/anchor/clone", "/launch/real"))
    emit, fanout, launch = _Boom(), _Boom(), _Boom()
    out = mod.evaluate(_payload(tmp_path), emit=emit, fanout=fanout, launch=launch)
    reason = _reason(out)
    assert "/anchor/clone" in reason and "/launch/real" in reason and "absolute `repo`" in reason
    assert emit.calls == fanout.calls == launch.calls == 0


def test_absolute_repo_skips_the_disagreement_check(tmp_path, monkeypatch):
    seen = _Boom()
    monkeypatch.setattr(wba, "launch_anchor_disagreement", seen)
    out = mod.evaluate(_payload(tmp_path, repo=str(tmp_path)), emit=_Boom(), fanout=_Boom(), launch=_Boom())
    assert seen.calls == 0
    hso = out.get("hookSpecificOutput") or {}
    assert "different repos" not in (hso.get("permissionDecisionReason") or "")


def test_agreement_is_not_refused_for_disagreement(tmp_path, monkeypatch):
    monkeypatch.setattr(wba, "launch_anchor_disagreement", lambda *a, **k: None)
    out = mod.evaluate(_payload(tmp_path), emit=_Boom(), fanout=_Boom(), launch=_Boom())
    hso = out.get("hookSpecificOutput") or {}
    assert "different repos" not in (hso.get("permissionDecisionReason") or "")
