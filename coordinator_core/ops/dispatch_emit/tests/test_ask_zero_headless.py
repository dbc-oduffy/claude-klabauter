"""Zero-headless pin: the ask emit path, both verbs, and the retired --fire spawn nothing."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, cli
from coordinator_core.ops.dispatch_emit.ask_gate import gate
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.win_portability import no_console_creationflags

REL = "state/sizings/2026-10-01-zero.yaml"
_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_ROUTE = {"XS": "dispatch", "S": "spec-dispatch", "M": "plan"}
_ACCEPTED = {"pm_quote": "yes", "on": "2026-10-01", "mode": "pm"}


def _boom(*_a, **_k):
    raise AssertionError("headless spawn reached")


_REAL_POPEN = subprocess.Popen


def _popen_but_git(args, *a, **k):
    # The accepted-M gate mints a baton, which reads the branch via git; any other child is headless.
    argv = args if isinstance(args, (list, tuple)) else [args]
    if str(argv[0]) != "git":
        _boom()
    return _REAL_POPEN(args, *a, **k)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(subprocess, "Popen", _popen_but_git)
    monkeypatch.setattr("coordinator_core.ops.workflow_fire.fire.fire_workflow", _boom)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _put(repo, tshirt):
    doc = {
        "schema": "sizing-object",
        "name": "zero fixture",
        "intent": "exercise the pin",
        "estimate": {"tshirt": tshirt, "provisional": True},
        "route": _ROUTE[tshirt],
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-zero-abc123",
        "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": _ACCEPTED},
    }
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _emit(repo, capsys, *argv):
    rc = cli.main([*argv, "--repo-root", str(repo)])
    return rc, capsys.readouterr().out


def test_raw_ask_emits_without_spawning_or_naming_fire(repo, capsys):
    rc, out = _emit(repo, capsys, "--ask", "add a thing")
    assert rc == 0
    assert "--fire" not in out


@pytest.mark.parametrize("tshirt", ["XS", "S", "M"])
def test_ask_sizing_emits_without_spawning_or_naming_fire(repo, capsys, tshirt):
    _put(repo, tshirt)
    if tshirt == "M":  # C5: emit runs the gate, which mints the M baton and reads the branch
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    rc, out = _emit(repo, capsys, "--ask", "--sizing", REL)
    assert rc == 0
    assert "--fire" not in out


@pytest.mark.parametrize("tshirt", ["XS", "S", "M"])
def test_gate_spawns_nothing(repo, tshirt):
    _put(repo, tshirt)
    if tshirt == "M":
        subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, **no_console_creationflags())
    assert gate(repo, REL, writes=["a.py"]).halt is None


def test_stage_spawns_nothing(repo):
    _put(repo, "XS")
    m = stage(repo, run_id="zero1", sizing_rel=REL, writes=["pkg/a.py"])
    assert [r.id for r in m.rows] == ["X1"]


@pytest.mark.parametrize(
    "argv",
    [["--sizing", REL, "--fire"], ["--ask", "x", "--fire"], ["--ask", "--sizing", REL, "--fire"]],
)
def test_fire_with_an_ask_verb_is_refused_before_any_emit(repo, capsys, argv, monkeypatch):
    monkeypatch.setattr(cli, "_dispatch_emit", _boom)
    rc, out = _emit(repo, capsys, *argv)
    assert rc == cli.EXIT_USAGE
    assert out == ""


def test_raw_ask_creates_a_missing_out_directory(repo, capsys):
    out = repo / "state" / "scratch" / "warp" / "fresh" / "ask.workflow.mjs"
    rc, _ = _emit(repo, capsys, "--ask", "add a thing", "--out", str(out))
    assert rc == 0
    assert out.is_file()
