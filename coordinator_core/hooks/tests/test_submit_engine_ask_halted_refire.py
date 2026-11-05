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


def _stalled_lookup_refire(tmp_path, monkeypatch):
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
    from coordinator_core.ops.dispatch_emit import halted_run

    halted = _halted_repo(tmp_path, "alpha")
    assert hrr.read_hint(halted.repo, halted.sizing_rel) is None
    monkeypatch.setattr(halted_run, "halted_run_to_resume", lambda *a, **k: hrr.STALLED)
    seen: list = []

    def emit(params, root):
        seen.append(params)
        return {"path": str(root / "run.js"), "run_id": "r1", "fire_args": {}}

    return hook.evaluate(json.loads(_payload(halted)), emit=emit), seen


def test_a_stalled_halted_lookup_with_no_hint_is_a_deferred_emit_not_a_refusal(tmp_path, monkeypatch):
    env, seen = _stalled_lookup_refire(tmp_path, monkeypatch)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert "Re-submit once" not in json.dumps(env)
    assert seen == []
    assert "deferred emit defer-" in hso["permissionDecisionReason"]


def test_a_stalled_lookup_whose_deferred_script_cannot_be_written_still_never_refuses(tmp_path, monkeypatch):
    from coordinator_core.hooks import submit_engine_ask as hook

    def unwritable(root, params):
        raise OSError("read-only")

    monkeypatch.setattr(hook, "_write_deferred_script", unwritable)
    env, seen = _stalled_lookup_refire(tmp_path, monkeypatch)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert "Re-submit once" not in json.dumps(env)
    assert len(seen) == 1


def test_an_unbuilt_lookup_index_on_the_hook_path_answers_without_building_it(tmp_path):
    """The default hook entry (no `build`) is the emit's lookup; the hook's `build=False` never writes the index."""
    from coordinator_core.ops.dispatch_emit import halted_run

    halted = _halted_repo(tmp_path, "alpha")
    _other_halts(halted.repo, halted_run.INLINE_BUILD_MAX_RUNS + 1)
    sentinel = halted.repo / halted_run.LOOKUP_INDEX_DIR / halted_run.LOOKUP_SENTINEL
    assert not sentinel.exists()
    hit = halted_run.halted_run_to_resume(halted.repo, halted.sizing_rel, build=False)
    assert hit is halted_run.STALLED
    assert not sentinel.exists()
    halted_run.halted_run_to_resume(halted.repo, halted.sizing_rel)
    assert sentinel.exists()


def _other_halts(root: Path, count: int) -> None:
    for n in range(count):
        d = root / RECORD_DIR_ROOT / f"ask-20261028T{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}"
        d.mkdir(parents=True)
        (d / "halted.json").write_text(
            json.dumps({"status": "halted", "halted": "x", "sizing_rel": "state/sizings/2026-10-28-other.yaml"}),
            encoding="utf-8",
        )


def test_a_sizing_with_no_halt_among_many_halts_resumes_nothing_in_one_listing(tmp_path, monkeypatch):
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit import halted_run

    _other_halts(tmp_path, 400)
    halted_run.rebuild_lookup_index(tmp_path)
    reads: list = []
    real = halted_run._read
    monkeypatch.setattr(halted_run, "_read", lambda d: reads.append(d) or real(d))
    started = time.perf_counter()
    route = hook._resume_route(tmp_path, "state/sizings/2026-10-28-none.yaml", {"tool_input": {}}, {}, None, None)
    assert route == (None, None)
    assert reads == []
    assert time.perf_counter() - started < 0.5


def test_a_sizing_with_no_halt_among_many_halts_and_no_index_defers_never_refuses(tmp_path):
    from coordinator_core.hooks import submit_engine_ask as hook

    _other_halts(tmp_path, 400)
    envelope, emitted = hook._resume_route(tmp_path, "state/sizings/2026-10-28-none.yaml", {"tool_input": {}}, {}, None, None)
    assert emitted is None
    assert "Re-submit once" not in json.dumps(envelope)


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


def test_real_launcher_script_carries_every_child_and_maps_each_result(tmp_path, monkeypatch):
    """The written launcher (not a stub) names both children and resolves every settled slot to a run entry,
    so a child absent from a completion result was dropped after the script, not omitted by it."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM, LaunchChild

    ready = _two_children(tmp_path)
    children = [LaunchChild(run_id=r.run_id, sizing_rel=r.slice_sizing, emit_params={"sizing": r.slice_sizing}) for r in ready]
    emitted_params: list = []

    def fake_emit(params, repo_root):
        emitted_params.append(params)
        rid = ready[len(emitted_params) - 1].run_id
        return {"path": f"scratch/warp/{rid}.workflow.mjs", "run_id": rid, "fire_args": {"repoRoot": "/r"}}

    monkeypatch.setattr(hook, "_default_emit", fake_emit)
    script = hook._default_launch(tmp_path, "ask-parent-20261028T000000-aaaaaa", children, _SESSION)
    assert len(emitted_params) == len(ready)
    assert all(p[LAUNCHER_CHILD_EMIT_PARAM] is True for p in emitted_params)
    text = script.read_text(encoding="utf-8")
    assert all(r.run_id in text for r in ready)
    assert "CHILDREN.map((child, i)" in text and "child threw" in text


def _spent_budget_launch(tmp_path, monkeypatch):
    """`_default_launch` with the emit budget spent: every child is handed to the launcher as a deferred-emit script."""
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    def overran(*args, **kwargs):
        raise hook._EmitOverran("spent")

    monkeypatch.setattr(hook, "_emit_within_budget", overran)
    ready = _two_children(tmp_path)[:1]
    children = [LaunchChild(run_id=r.run_id, sizing_rel=r.slice_sizing, emit_params={"sizing": r.slice_sizing}) for r in ready]
    launcher = hook._default_launch(tmp_path, "resume-x-abcdef", children, _SESSION)
    deferred = sorted((tmp_path / "scratch" / "warp").glob("defer-*.workflow.mjs"))
    assert len(deferred) == 1
    return launcher.read_text(encoding="utf-8"), deferred[0].read_text(encoding="utf-8")


def _params_file_text(root) -> str:
    """The payload the deferred script's relay reads: the one `defer-params-*.json` under `scratch/warp/`."""
    files = list((root / "scratch" / "warp").glob("defer-params-*.json"))
    assert len(files) == 1
    return files[0].read_text(encoding="utf-8")


def test_a_launcher_started_deferred_emit_never_nests_workflow_and_the_launcher_starts_what_it_emits(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.fan_launcher import DEFERRED_EMIT_KEY

    launcher, child = _spent_budget_launch(tmp_path, monkeypatch)
    assert "workflow(" not in child
    assert f"{DEFERRED_EMIT_KEY}: {{ path: scriptPath" in child
    assert "launcher_child" in _params_file_text(tmp_path)
    assert f"result.{DEFERRED_EMIT_KEY}" in launcher and launcher.count("await workflow(") == 1


def test_a_standalone_deferred_emit_still_starts_the_run_itself(tmp_path):
    from coordinator_core.hooks import submit_engine_ask as hook
    from coordinator_core.ops.dispatch_emit.fan_launcher import DEFERRED_EMIT_KEY

    script = hook._write_deferred_script(tmp_path, {"ask": "x"}).read_text(encoding="utf-8")
    assert "await workflow({ scriptPath: path })" in script
    assert "launcher_child" in script
    assert DEFERRED_EMIT_KEY not in script


# A run that stopped at plan-blitz leaves run.json bound to its sizing but, historically, no halted.json.


def _unmarked_run(tmp_path, stage="halted"):
    halted = _halted_repo(tmp_path, "alpha")
    record_root = halted.repo / RECORD_DIR_ROOT
    shutil.rmtree(record_root / halted.halted_run)
    run_id = "warp-2026-10-28-alpha-20261028T010101-abcdef"
    rd = record_root / run_id
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"sizing_rel": halted.sizing_rel}), encoding="utf-8")
    (rd / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")
    return halted, run_id


def _refire(halted, monkeypatch):
    from coordinator_core.hooks import submit_engine_ask as hook

    spawned, emitted, launched = [], [], []
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)

    def emit(params, root):
        emitted.append(params)
        return {"path": str(root / "run.js"), "run_id": "r1", "fire_args": {}}

    script = halted.repo / "launcher.js"
    script.write_text("//", encoding="utf-8")

    def launch(root, parent_id, children, session_id):
        launched.append([c.run_id for c in children])
        return script

    env = hook.evaluate(json.loads(_payload(halted)), emit=emit, launch=launch)
    return env, spawned, emitted, launched


def test_sizing_with_an_unmarked_emitted_run_refires_through_the_launcher(tmp_path, monkeypatch):
    halted, run_id = _unmarked_run(tmp_path)
    env, spawned, emitted, launched = _refire(halted, monkeypatch)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert emitted == [] and spawned == [run_id] and launched == [[run_id]]


@pytest.mark.parametrize("stage", ["landed", "minted"])
def test_unmarked_run_outside_the_stopped_stages_takes_the_plain_emit(tmp_path, monkeypatch, stage):
    halted, _ = _unmarked_run(tmp_path, stage=stage)
    env, spawned, emitted, launched = _refire(halted, monkeypatch)
    assert len(emitted) == 1 and launched == [] and spawned == []


def test_newer_minted_only_shell_hides_no_stopped_run(tmp_path, monkeypatch):
    halted, run_id = _unmarked_run(tmp_path)
    shell = halted.repo / RECORD_DIR_ROOT / "warp-2026-10-28-alpha-20261028T020202-fedcba"
    shell.mkdir(parents=True)
    (shell / "run-stage.json").write_text(json.dumps({"stage": "minted"}), encoding="utf-8")
    env, spawned, emitted, launched = _refire(halted, monkeypatch)
    assert emitted == [] and launched == [[run_id]]


def _stored_record_repo(tmp_path, halted_id, *, script_on_disk):
    """A halted run whose resume record matches its fingerprint and names a stored script."""
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    sizing_rel, plan_rel = "state/sizings/2026-10-29-miss.yaml", "docs/plans/2026-10-29-miss.md"
    stored_rel = f"scratch/warp/{halted_id}.workflow.mjs"
    (tmp_path / ".git").mkdir()
    for rel, text in ((sizing_rel, "schema: sizing-object\n"), (plan_rel, "# plan\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    if script_on_disk:
        (tmp_path / stored_rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / stored_rel).write_text("// stored\n", encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / halted_id
    write_halt_marker(rd, halted="ledger-check", sizing_rel=sizing_rel, plan_rel=plan_rel, run_base_sha="a" * 40)
    fp = hrr.fingerprint_for_run(tmp_path, rd, sizing_rel, plan_rel)
    hrr.write(
        rd,
        hrr.build_record(
            composed_script_rel=stored_rel, hoisted_fragments=[], gate_verdict=None,
            landed_state_sha=None, fingerprint=fp, resume_plan_rel=plan_rel,
        ),
    )
    payload = {
        "tool_name": "Workflow", "cwd": str(tmp_path), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": _STUB, "args": {"sizing": sizing_rel}},
    }
    return payload, stored_rel


@pytest.mark.parametrize("script_on_disk", [False, True])
def test_recompose_spawns_only_when_the_stored_script_is_gone(tmp_path, monkeypatch, script_on_disk):
    """Both sides: a stored script missing from disk starts the run through the launcher AND spawns the
    recompose; a present one is served as-is with no recompose."""
    from coordinator_core.hooks import submit_engine_ask as hook

    halted_id = "warp-2026-10-29-miss-20261029T010101-abc123"
    payload, stored_rel = _stored_record_repo(tmp_path, halted_id, script_on_disk=script_on_disk)
    spawned: list = []
    monkeypatch.setattr(hook, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)
    launcher = tmp_path / "launcher.js"
    launcher.write_text("//", encoding="utf-8")
    launched: list = []

    def launch(root, parent_id, children, session_id):
        launched.append([c.run_id for c in children])
        return launcher

    env = hook.evaluate(payload, emit=lambda p, r: pytest.fail("plain emit"), launch=launch)
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    if script_on_disk:
        assert env["hookSpecificOutput"]["updatedInput"]["scriptPath"] == str(tmp_path / stored_rel)
        assert spawned == [] and launched == []
    else:
        assert spawned == [halted_id] and launched == [[halted_id]]


def test_unbound_run_takes_the_plain_emit(tmp_path, monkeypatch):
    halted, run_id = _unmarked_run(tmp_path)
    (halted.repo / RECORD_DIR_ROOT / run_id / "run.json").write_text(json.dumps({"sizing_rel": "other.yaml"}), encoding="utf-8")
    env, spawned, emitted, launched = _refire(halted, monkeypatch)
    assert len(emitted) == 1 and launched == []
