"""A halted-run sizing re-fired after `workflow.revise`, singly and as three concurrent submissions, driven
through the DoE `preuse-workflow-dispatch.py` and the settings-home `hook-run` door from a foreign-repo cwd.

The dispatcher is located through `CLAUDE_PLUGIN_ROOT`; the file skips where it or the door launcher is absent.
The engine is a stamped copy under tmp_path, run cold (`COORDINATOR_WARM=0`), so the warm server is not exercised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil
import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, halted_record_message
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker
from coordinator_core.tests._stamped_engine_fixture import cold_cli_env
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ENGINE_SOURCE = Path(__file__).resolve().parents[3]
_PLUGIN_ROOT_VAR = "CLAUDE_PLUGIN_ROOT"
# Captured at import, before conftest quarantines the home: the launcher binary is copied, never written.
_AMBIENT_SETTINGS_HOME = Path(
    os.environ.get("COORDINATOR_SETTINGS_HOME") or Path.home() / ".coordinator-claude-settings"
)
_PLUGIN_ROOT = os.environ.get(_PLUGIN_ROOT_VAR, "")
_SESSION = "11111111-2222-3333-4444-555555555555"
_STUB = "coordinator:engine-ask"
_REGISTER = {
    "sources": [{"doc": "ask", "ref": "ask"}],
    "rows": [{"id": "R1", "source_text": "refuse met on an unchanged diff", "surface": "internal", "status": "open"}],
}
_ACCEPTED = {"pm_quote": "yes", "on": "2026-10-28", "mode": "pm"}


def _git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


def _plan(repo: Path, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "- id: C1\n  title: TC1\n  disposition: open\n  writes:\n    - some/C1.py\n"
    path.write_text(
        f"---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n", encoding="utf-8", newline="\n"
    )


@dataclass(frozen=True)
class Halted:
    """One foreign repo holding a sizing whose prior run halted on ledger-check and was then revised."""

    repo: Path
    sizing_rel: str
    halted_run: str
    revise_reply: dict


def _halted_repo(root: Path, tag: str) -> Halted:
    repo = root / f"foreign-{tag}"
    repo.mkdir(parents=True)
    sizing_rel = f"state/sizings/2026-10-28-{tag}.yaml"
    plan_rel = f"docs/plans/2026-10-28-{tag}.md"
    halted_run = f"ask-20261028T003011-{tag[:6].ljust(6, '0')}"
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")
    _plan(repo, plan_rel)
    (repo / "some").mkdir()
    (repo / "some" / "C1.py").write_text("x = 1\n", encoding="utf-8")
    doc = {
        "schema": "sizing-object", "name": f"refire {tag}", "intent": "exercise the halted refire",
        "estimate": {"tshirt": "S", "provisional": True}, "route": "spec-dispatch", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized", "plan": plan_rel,
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": f"dlv-{tag}-abc123", "interaction_mode": "pm",
        "exit_criterion": {"statement": "done", "accepted": _ACCEPTED},
        "requirement_register": _REGISTER,
    }
    (repo / sizing_rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / sizing_rel).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    rl.start(repo, halted_run)
    record = repo / RECORD_DIR_ROOT / halted_run
    write_halt_marker(
        record, halted="ledger-check", sizing_rel=sizing_rel, plan_rel=plan_rel, blocking=("R1",),
        blocking_items=[{"id": "R1", "remaining": [], "evidence": "e"}],
    )
    (record / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": ["some/C1.py"]}]}), encoding="utf-8")
    _git(repo, "add", "-A")
    message = halted_record_message(halted_run, {"halted": "ledger-check", "plan": plan_rel})
    _git(repo, "commit", "-q", "-m", message + "\nLands-Chunks: C1\n")
    rl.bind(repo, halted_run, sizing_rel)
    reply = rl._handler({"run_id": halted_run, "verbatim": f"also cover {tag}"}, repo_root=repo)
    return Halted(repo, sizing_rel, halted_run, reply)


@pytest.fixture(scope="module")
def engine_root(tmp_path_factory):
    """A stamped copy of this checkout's engine: the claude-klabauter-shaped repo the door resolves."""
    dest = tmp_path_factory.mktemp("engine")
    skip = shutil.ignore_patterns("tests", "ceremony_tests", "__pycache__", "scratch", ".git", "*.pyc")
    for rel in ("coordinator_core", "coordinator/bin", "coordinator/lib"):
        shutil.copytree(ENGINE_SOURCE / rel, dest / rel, ignore=skip)
    (dest / "coordinator_core" / "_engine_stamp").write_bytes(b"halted-refire-fixture-stamped-engine\n")
    return dest


@pytest.fixture
def door(tmp_path, request):
    script = Path(_PLUGIN_ROOT) / "hooks" / "scripts" / "preuse-workflow-dispatch.py" if _PLUGIN_ROOT else None
    if script is None or not script.is_file():
        pytest.skip(f"{_PLUGIN_ROOT_VAR} is unset or holds no hooks/scripts/preuse-workflow-dispatch.py")
    launcher = next(
        (p for n in ("hook-run.exe", "hook-run") if (p := _AMBIENT_SETTINGS_HOME / "bin" / n).is_file()), None
    )
    if launcher is None:
        pytest.skip(f"no hook-run launcher under {_AMBIENT_SETTINGS_HOME / 'bin'}")
    engine_root = request.getfixturevalue("engine_root")
    home = tmp_path / "settings-home"
    (home / "bin").mkdir(parents=True)
    shutil.copy2(launcher, home / "bin" / launcher.name)
    env = {
        **cold_cli_env(engine_root),
        "COORDINATOR_SETTINGS_HOME": str(home),
        "COORDINATOR_SESSION_ID": _SESSION,
        "COORDINATOR_AGENT_TYPE_HOST": "coordinator",
    }
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID", "DELIVERABLE_ID"):
        env.pop(name, None)
    return script, env


@dataclass
class Run:
    proc: subprocess.Popen
    started: float
    pids: dict
    stdout: str = ""
    stderr: str = ""
    process_s: float = 0.0
    spawns: int = 0


def _payload(halted: Halted) -> str:
    return json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "Workflow", "session_id": _SESSION,
        "transcript_path": str(halted.repo / "transcript.jsonl"), "cwd": str(halted.repo),
        "tool_use_id": "toolu_" + halted.halted_run,
        "tool_input": {"name": _STUB, "args": {"sizing": halted.sizing_rel}},
    })


def _start(door, halted: Halted) -> Run:
    script, env = door
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=str(halted.repo), env=env, **no_console_creationflags(),
    )
    proc.stdin.write(_payload(halted).encode("utf-8"))
    proc.stdin.close()
    return Run(proc, time.perf_counter(), {})


def _sample(runs: list[Run]) -> None:
    """Poll each run's process tree until all exit, keeping the last CPU time seen per pid."""
    while any(r.proc.poll() is None for r in runs):
        for r in runs:
            if r.proc.poll() is not None:
                continue
            try:
                tree = [psutil.Process(r.proc.pid), *psutil.Process(r.proc.pid).children(recursive=True)]
            except psutil.Error:
                continue
            for p in tree:
                try:
                    t = p.cpu_times()
                    r.pids[p.pid] = t.user + t.system
                except psutil.Error:
                    r.pids.setdefault(p.pid, 0.0)
        time.sleep(0.005)


def _collect(runs: list[Run]) -> None:
    for r in runs:
        r.stdout, r.stderr = (b.decode("utf-8", "replace") for b in r.proc.communicate())
        r.process_s = sum(r.pids.values())
        r.spawns = max(len(r.pids) - 1, 0)


def _record(record_property, label: str, run: Run) -> None:
    record_property(f"{label}_process_s", round(run.process_s, 3))
    record_property(f"{label}_spawns", run.spawns)
    record_property(f"{label}_wall_s", round(time.perf_counter() - run.started, 3))


def _assert_rewritten(run: Run) -> str:
    assert run.proc.returncode == 0, run.stderr
    lines = [ln for ln in run.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, (run.stdout, run.stderr)
    hso = json.loads(lines[0])["hookSpecificOutput"]
    assert hso.get("permissionDecision") == "allow", (hso, run.stderr)
    script = hso["updatedInput"]["scriptPath"]
    assert Path(script).is_file(), script
    return script


def _assert_revised(halted: Halted) -> None:
    reply = halted.revise_reply
    assert reply.get("ok") is True and reply.get("run_live") is False, reply
    assert reply["next"]["kind"] == "refire", reply


def test_case_a_one_refire_after_revise_is_rewritten(door, tmp_path, record_property):
    halted = _halted_repo(tmp_path, "alpha")
    _assert_revised(halted)

    run = _start(door, halted)
    _sample([run])
    _collect([run])
    _record(record_property, "case_a", run)

    _assert_rewritten(run)


def test_case_b_three_concurrent_refires_are_each_rewritten(door, tmp_path, record_property):
    sets = [_halted_repo(tmp_path, tag) for tag in ("alpha", "bravo", "charlie")]
    for halted in sets:
        _assert_revised(halted)

    runs = [_start(door, halted) for halted in sets]
    _sample(runs)
    _collect(runs)
    for i, run in enumerate(runs):
        _record(record_property, f"case_b{i}", run)

    scripts = [_assert_rewritten(run) for run in runs]
    assert len(set(scripts)) == 3, scripts


def test_ask_run_halt_takes_the_plain_emit_and_spawns_no_recompose(tmp_path, monkeypatch):
    """The resume record covers a halted `warp-<stem>-*` run; a halted `ask-*` run's refire takes a fresh
    run id, so the hook emits as before and starts no recompose."""
    from coordinator_core.hooks import submit_engine_ask as hook

    halted = _halted_repo(tmp_path, "alpha")
    spawned: list = []
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)
    seen: list = []

    def emit(params, root):
        seen.append(params)
        return {"path": str(root / "run.js"), "run_id": "r1", "fire_args": {}}

    env = hook.evaluate(json.loads(_payload(halted)), emit=emit)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert len(seen) == 1 and spawned == []


# Door-free cases: the dispatch-timeout detail and the asks fan-out launcher.


def test_op_timeout_surfaces_op_and_timeout_in_the_dispatch_error(monkeypatch):
    """A hook op that overruns the dispatch budget raises HookDispatchError naming the op and the hang;
    hook-run renders that string into the unreachable pass body, so it is what the reader sees."""
    from coordinator_core import ipc
    from coordinator_core.hooks import submit_engine_ask as hook

    monkeypatch.setattr(ipc, "DISPATCH_TIMEOUT_SECS", 0.5)
    monkeypatch.setattr(hook, "evaluate", lambda payload: time.sleep(3) or {})
    with pytest.raises(ipc.HookDispatchError) as info:
        ipc.dispatch_from_hook("hooks.submit_engine_ask", {"tool_name": "Workflow", "tool_input": {}})
    text = str(info.value)
    assert "hooks.submit_engine_ask" in text and "hung" in text and "ceiling" in text, text


def test_op_inside_the_budget_returns_the_envelope(monkeypatch):
    from coordinator_core import ipc
    from coordinator_core.hooks import submit_engine_ask as hook

    monkeypatch.setattr(hook, "evaluate", lambda payload: {"hookSpecificOutput": {"permissionDecision": "allow"}})
    result = ipc.dispatch_from_hook("hooks.submit_engine_ask", {"tool_name": "Workflow", "tool_input": {}})
    assert result["hookSpecificOutput"]["permissionDecision"] == "allow"


def _two_children(tmp_path):
    from types import SimpleNamespace

    runs = []
    for tag in ("aaaaaa", "bbbbbb"):
        rel = f"state/sizings/{tag}.yaml"
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(yaml.safe_dump({"schema": "sizing-object"}), encoding="utf-8")
        runs.append(SimpleNamespace(run_id=f"ask-20261028T000000-{tag}", slice_sizing=rel, status="ready"))
    return runs


def _fan_payload():
    return {"session_id": _SESSION, "tool_use_id": "toolu_x", "tool_input": {"name": _STUB, "args": {"asks": []}}}


def test_launcher_rewrite_names_every_child(tmp_path):
    from coordinator_core.hooks import submit_engine_ask as hook

    ready = _two_children(tmp_path)
    script = tmp_path / "launcher.js"
    script.write_text("//", encoding="utf-8")
    seen = {}

    def launch(root, parent_id, children, session_id):
        seen["ids"] = [c.run_id for c in children]
        return script

    out = hook._launch_all(tmp_path, _fan_payload(), {"parent_id": "ask-parent-x"}, ready, None, launch, False)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow" and hso["updatedInput"]["scriptPath"] == str(script)
    assert seen["ids"] == [r.run_id for r in ready]
    assert all(r.run_id in hso["additionalContext"] for r in ready), hso


def test_launcher_failure_is_a_named_deny(tmp_path):
    from coordinator_core.hooks import submit_engine_ask as hook

    def launch(*args):
        raise RuntimeError("no launcher today")

    out = hook._launch_all(tmp_path, _fan_payload(), {"parent_id": "p"}, _two_children(tmp_path), None, launch, False)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny" and "no launcher today" in hso["permissionDecisionReason"], hso


def test_real_launcher_script_carries_every_child_and_maps_each_result(tmp_path):
    """The written launcher (not a stub) names both children and resolves every settled slot to a run entry,
    so a child absent from a completion result was dropped after the script, not omitted by it."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    ready = _two_children(tmp_path)
    children = [LaunchChild(run_id=r.run_id, sizing_rel=r.slice_sizing, emit_params={"sizing": r.slice_sizing}) for r in ready]
    script = hook._default_launch(tmp_path, "ask-parent-20261028T000000-aaaaaa", children, _SESSION)
    text = script.read_text(encoding="utf-8")
    assert all(r.run_id in text for r in ready)
    assert "CHILDREN.map((child, i)" in text and "child threw" in text
