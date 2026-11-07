"""A `{sizing}` submission of a routed sizing with a linked plan, through the hook's default emit, composes no size agent.

Pins the submission edge (hook -> `dispatch.emit` -> `compose_ask_script`): the sizing_rel reaches compose
on a plain submission, on a refire over a halted `ask-` run, and on an absolute path from a foreign cwd.
"""

from __future__ import annotations

import glob
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.benchmarks import sizing_assemble_process_budget as fixture
from coordinator_core.hooks import submit_engine_ask as hook
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit import halted_run as hr

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_SID = "11111111-2222-3333-4444-555555555555"
_HALTED_ASK = "ask-20261106T181435-d204e6"


def _payload(cwd, args):
    return {
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SID, "cwd": str(cwd),
        "tool_use_id": "t-1", "tool_input": {"name": "coordinator:engine-ask", "args": args},
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A tmp repo distinct from the engine root, holding a routed sizing and its linked plan."""
    root = tmp_path / "repo"
    root.mkdir()
    fixture.build_fixture(root)
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SID)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings-home"))
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return root


@pytest.fixture
def composed(monkeypatch):
    """The `sizing_rel` each `compose_ask_script` call received."""
    seen: list = []
    real = ask_compose.compose_ask_script

    def spy(**kw):
        seen.append(kw.get("sizing_rel"))
        return real(**kw)

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    return seen


def _submit(cwd, args):
    out = hook.evaluate(_payload(cwd, args), launch=lambda *a, **k: Path(cwd) / "launcher.mjs")
    hso = out.get("hookSpecificOutput") or {}
    assert hso.get("permissionDecision") == "allow", hso.get("permissionDecisionReason")


def _scripts(repo):
    pattern = str(repo / "scratch" / "warp" / "**" / "*.mjs")
    return [open(p, encoding="utf-8").read() for p in sorted(glob.glob(pattern, recursive=True))]


def _assert_no_size_stage(repo):
    scripts = _scripts(repo)
    assert scripts, "the default emit wrote no script"
    for text in scripts:
        assert "phase('size')" not in text
        assert f"let _sizingRel = '{fixture.SIZING_REL}';" in text


def test_routed_sizing_submission_reaches_compose_with_its_sizing_rel(repo, composed):
    _submit(repo, {"sizing": fixture.SIZING_REL})
    assert composed == [fixture.SIZING_REL]
    _assert_no_size_stage(repo)


def test_refire_over_a_halted_ask_run_composes_no_size_stage(repo, composed):
    rd = repo / "scratch" / "warp" / _HALTED_ASK
    rd.mkdir(parents=True)
    hr._write(rd, {"status": "halted", "sizing_rel": fixture.SIZING_REL, "reason": "sizing-assemble failed"})
    hr._index_call("add_run", rd, hr.halted_withheld(rd), sizing_rel=fixture.SIZING_REL)
    _submit(repo, {"sizing": fixture.SIZING_REL})
    assert composed == [fixture.SIZING_REL]
    _assert_no_size_stage(repo)


def test_absolute_sizing_path_from_a_foreign_cwd_keeps_the_repo_relative_sizing_rel(repo, composed, tmp_path, monkeypatch):
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    monkeypatch.chdir(foreign)
    _submit(foreign, {"sizing": str(repo / fixture.SIZING_REL)})
    assert composed == [fixture.SIZING_REL]
    _assert_no_size_stage(repo)
