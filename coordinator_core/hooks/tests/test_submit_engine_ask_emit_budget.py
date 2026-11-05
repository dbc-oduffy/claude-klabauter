"""The submit hook's whole emit stays under the 500ms process-time bar on the real `dispatch.emit`."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import run_token

pytestmark = [pytest.mark.cadence]

BAR_S = 0.5
repo = real.repo


def _token_dir(repo_root):
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.engine_record_home import engine_record_dir

    return engine_record_dir(repo_root) / run_token._DIR


def test_listing_scope_lists_each_session_dir_once_and_never_the_parent(tmp_path, monkeypatch):
    from coordinator_core import launch_records, warp_live

    for session in ("sess", "other"):
        Path(launch_records.record_path(str(tmp_path), session, "t1")).write_text("{}", encoding="utf-8")
    calls = []
    real_listdir = warp_live.os.listdir
    monkeypatch.setattr(warp_live.os, "listdir", lambda p: calls.append(p) or real_listdir(p))
    dirs = [str(tmp_path)]
    with warp_live.listing_scope():
        for session in ("sess", "other", "sess"):
            warp_live.LaunchSnapshot(session, dirs)
    assert sorted(calls) == sorted(launch_records.session_dir(str(tmp_path), s) for s in ("sess", "other"))
    assert str(tmp_path) not in calls
    warp_live.LaunchSnapshot("sess", dirs)
    warp_live.LaunchSnapshot("sess", dirs)
    assert len(calls) == 4  # outside a scope every snapshot lists afresh


def test_worker_threads_join_the_open_sizing_memo_scope_and_only_that_scope():
    from coordinator_core.ops.dispatch_emit import sizing_fire

    seen = lambda params, root: sizing_fire._SIZING_MEMO.get() is not None  # noqa: E731
    with sizing_fire.sizing_memo():
        assert mod._emit_within_budget(seen, {}, Path("."), 5.0) is True
    assert mod._emit_within_budget(seen, {}, Path("."), 5.0) is False


def test_two_ask_submission_parses_each_slice_sizing_once_per_rewrite(repo, monkeypatch):
    """Both child emits and the hook's own reads share one parse of a slice sizing (18 parses before the shared scope)."""
    import types

    import yaml

    from coordinator_core.ops.dispatch_emit import sizing_fire
    from coordinator_core.hooks.tests import test_submit_engine_ask_two_ask_process_budget as two

    two._seed_turn(repo, monkeypatch)
    mod.evaluate(real._payload(repo, {"asks": two._two_asks()}))  # warm the imports
    parses: list = []

    def counted(text):
        parses.append(text)
        return yaml.safe_load(text)

    monkeypatch.setattr(sizing_fire, "yaml", types.SimpleNamespace(safe_load=counted, YAMLError=yaml.YAMLError))
    env = mod.evaluate(real._payload(repo, {"asks": two._two_asks()}))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert len(parses) <= 4, len(parses)


def _overrun(repo, monkeypatch, mint: bool):
    import threading

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "1.0")
    release = threading.Event()

    def emit(params, root):
        if mint:  # token first, naming the submitting session, then the run dir: the real emit's order
            run_token.mint(root, "ask-20261029T200000-e3414a", "scratch/warp/ask-20261029T200000-e3414a.workflow.mjs",
                           session_id=params.get("session_id") or None)
            d = root / RECORD_DIR_ROOT / "ask-20261029T200000-e3414a"
            d.mkdir(parents=True)
            (d / RUN_STAGE_FILE).write_text('{"stage": "minted"}', encoding="utf-8")
        release.wait(30)
        return {}

    try:
        return mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed"}), emit=emit)
    finally:
        release.set()


def test_overrun_with_no_run_minted_is_taken_as_a_deferred_emit(repo, monkeypatch):
    env = _overrun(repo, monkeypatch, mint=False)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    script = Path(hso["updatedInput"]["scriptPath"])
    text = script.read_text(encoding="utf-8")
    params_file = Path(json.loads(re.search(r"^const PARAMS_FILE = (.*);$", text, re.M).group(1)))
    assert script.parent == repo / "scratch" / "warp" and "dispatch.emit" in text
    assert "build the thing" in params_file.read_text(encoding="utf-8")
    assert script.stem.split(".")[0] in hso["additionalContext"] and "Do not re-submit" in hso["additionalContext"]
    assert "nothing was taken" not in str(env)


def test_overrun_after_a_run_was_minted_starts_it_and_names_it(repo, monkeypatch):
    # A minted run is started, never denied: a deny here orphaned the run it had just minted.
    env = _overrun(repo, monkeypatch, mint=True)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    assert "ask-20261029T200000-e3414a" in json.dumps(env)
    assert "Do not re-submit" in json.dumps(env)


def _stall_record_write(monkeypatch):
    """Stall the run record's temp-file creation until released; `done` is set when the stalled write returns."""
    import tempfile
    import threading

    from coordinator_core.ops.dispatch_emit import run_token

    release, entered, done = threading.Event(), threading.Event(), threading.Event()
    real_mkstemp = tempfile.mkstemp

    def stalled(*args, **kwargs):
        if str(kwargs.get("prefix", "")).startswith(".mint."):
            entered.set()
            release.wait(30)
        return real_mkstemp(*args, **kwargs)

    real_publish = run_token._publish_record

    def publish(path, text):
        try:
            real_publish(path, text)
        finally:
            done.set()

    monkeypatch.setattr(run_token.tempfile, "mkstemp", stalled)
    monkeypatch.setattr(run_token, "_publish_record", publish)
    return release, entered, done


def test_stalled_record_write_refuses_naming_the_disk_and_leaves_no_late_run(repo, monkeypatch):
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "2.5")
    release, entered, done = _stall_record_write(monkeypatch)
    try:
        env = mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed"}))
        assert entered.is_set()
    finally:
        release.set()
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    reason = hso["permissionDecisionReason"]
    assert "disk is stalled" in reason and "re-submitting is safe" in reason
    assert done.wait(10)  # the stalled write has now returned
    token_dir = _token_dir(repo)
    assert not list(token_dir.glob("*.json")) and not list(token_dir.glob(".mint.*"))
    assert not list((repo / "scratch" / "warp").glob("defer-*"))  # no deferred emit onto the stalled disk


def test_default_ask_answers_with_a_run_id_before_any_bulk_io_on_a_slow_disk(repo):
    import re

    from coordinator_core.benchmarks.submit_slow_disk_profile import Charger

    charger = Charger(repo, 2.0)
    with charger.active():
        start = time.process_time()
        env = mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed"}))
        spent_ms = (time.process_time() - start) * 1000.0
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", env
    found = re.search(r"\bask-\d{8}T\d{6}-[0-9a-f]{6}\b", hso["additionalContext"])
    assert found, hso["additionalContext"]
    assert charger.waited_ms + spent_ms < 0.8 * mod._caller_budget_s() * 1000.0, charger.report()
    assert not [s for s in charger.report() if s["bulk"]], charger.report()
    assert (_token_dir(repo) / f"{found.group(0)}.json").is_file()
    assert not (repo / "scratch" / "warp" / found.group(0)).exists()  # the prompts are the run's own emit, later


def test_unwritable_deferred_script_refuses_and_drops_the_minted_marker(repo, monkeypatch):
    def fail(root, params):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_deferred_script", fail)
    env = mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed"}))
    assert env["hookSpecificOutput"]["permissionDecision"] == "deny", env
    token_dir = _token_dir(repo)
    assert not list(token_dir.glob("ask-*.json"))


def test_deferred_emit_adopts_the_minted_run_id_and_refuses_another_shape(repo):
    rid = "ask-20261031T120000-abcdef"
    params = {"ask": "build the thing", "ask_source": "relayed", "session_id": "", "preminted_run_id": rid}
    result = mod._default_emit(params, repo)
    assert result["run_id"] == rid and Path(result["path"]).name == f"{rid}.workflow.mjs"
    with pytest.raises(ValueError, match="preminted_run_id"):
        mod._default_emit({**params, "preminted_run_id": "ask-../escape"}, repo)


def test_ask_with_a_brief_file_keeps_its_synchronous_emit(repo):
    (repo / "brief.md").write_text("BRIEF-FILE", encoding="utf-8")
    env = mod.evaluate(real._payload(repo, {"ask": "build the thing", "source": "relayed", "brief_file": "brief.md"}))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert not list((repo / "scratch" / "warp").glob("defer-*"))


def test_non_stalled_emit_mints_its_record(repo, monkeypatch):
    real._put_sizing(repo)
    env = mod.evaluate(real._payload(repo, {"sizing": real.REL}))
    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert list(_token_dir(repo).glob("*.json"))


def _fan_children():
    from coordinator_core.ops.dispatch_emit.fan_launcher import LaunchChild

    return [
        LaunchChild(run_id=f"ask-r{i}", sizing_rel=f"state/sizings/ask-r{i}.yaml",
                    emit_params={"ask": True, "sizing_path": f"state/sizings/ask-r{i}.yaml", "reserved_run_id": f"ask-r{i}"})
        for i in (1, 2)
    ]


def test_fan_overrun_children_are_deferred_into_the_launcher_not_refused(tmp_path, monkeypatch):
    import json
    import threading

    release = threading.Event()
    monkeypatch.setattr(mod, "_default_emit", lambda params, root: release.wait(5) and {})
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "0.25")
    try:
        launcher = mod._default_launch(tmp_path, "ask-parent", _fan_children(), None)
    finally:
        release.set()
    text = launcher.read_text(encoding="utf-8")
    emitted = json.loads(text.split("const EMITTED = ", 1)[1].split(";\n", 1)[0])
    for child, reply in zip(_fan_children(), emitted):
        assert "error" not in reply and reply["run_id"] == child.run_id, reply
        body = (tmp_path / reply["path"]).read_text(encoding="utf-8")
        params_file = Path(json.loads(re.search(r"^const PARAMS_FILE = (.*);$", body, re.M).group(1)))
        assert reply["path"].startswith("scratch/warp/defer-") and "dispatch.emit" in body
        assert child.run_id in params_file.read_text(encoding="utf-8")


def test_fan_overrun_after_a_run_was_minted_still_names_the_error_for_that_child(repo, monkeypatch):
    import threading

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    release = threading.Event()

    def emit(params, root):
        run_token.mint(root, "ask-20261029T200000-e3414a", "scratch/warp/ask-20261029T200000-e3414a.workflow.mjs",
                       session_id=params.get("session_id") or None)
        d = root / RECORD_DIR_ROOT / "ask-20261029T200000-e3414a"
        d.mkdir(parents=True)
        (d / RUN_STAGE_FILE).write_text("{}", encoding="utf-8")
        release.wait(5)
        return {}

    monkeypatch.setattr(mod, "_default_emit", emit)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "0.25")
    try:
        launcher = mod._default_launch(repo, "ask-parent", _fan_children(), "sess-fan")
    finally:
        release.set()
    emitted = launcher.read_text(encoding="utf-8").split("const EMITTED = ", 1)[1].split(";\n", 1)[0]
    assert "ask-20261029T200000-e3414a" in emitted and emitted.count("defer-") == 1  # the second child, never emitted, is deferred


def test_sizing_submission_emit_process_time_is_under_the_bar(repo):
    real._put_sizing(repo)
    payload = real._payload(repo, {"sizing": real.REL})
    mod.evaluate(payload)  # warm the imports; the bar is the steady-state emit
    (run_dir,) = real._run_dirs(repo)
    for p in sorted(run_dir.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    run_dir.rmdir()

    start = time.process_time()
    env = mod.evaluate(payload)
    spent = time.process_time() - start

    assert env["hookSpecificOutput"]["permissionDecision"] == "allow", env
    assert spent < BAR_S, f"submit hook emit took {spent:.3f}s process time (bar {BAR_S}s)"


def _hoist_states(monkeypatch, replies):
    from coordinator_core.ops.dispatch_emit import op, prompt_fragments

    seen = []

    def fake(params, root):
        seen.append(prompt_fragments.HOIST_ENABLED)
        reply = replies[len(seen) - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(op, "_dispatch_emit", fake)
    return seen


def test_over_cap_script_is_re_emitted_hoisted(monkeypatch, tmp_path):
    over = {"ok": False, "findings": ["composed ask script is over the Workflow runner's 100-byte cap: 200 bytes"]}
    seen = _hoist_states(monkeypatch, [over, {"path": "x.js"}])
    assert mod._default_emit({}, tmp_path) == {"path": "x.js"}
    assert seen == [False, True]


def test_over_cap_raise_is_re_emitted_hoisted(monkeypatch, tmp_path):
    seen = _hoist_states(monkeypatch, [ValueError("script is over the Workflow runner's 100-byte cap"), {"path": "x.js"}])
    assert mod._default_emit({}, tmp_path) == {"path": "x.js"}
    assert seen == [False, True]


def test_under_cap_and_other_errors_are_not_re_emitted(monkeypatch, tmp_path):
    seen = _hoist_states(monkeypatch, [{"path": "x.js"}])
    assert mod._default_emit({}, tmp_path) == {"path": "x.js"}
    assert seen == [False]
    seen = _hoist_states(monkeypatch, [ValueError("boom")])
    with pytest.raises(ValueError):
        mod._default_emit({}, tmp_path)
    assert seen == [False]


def test_deferred_script_carries_target_root_for_the_repo_unkeyed_cli(tmp_path):
    # The relay runs dispatch.emit over the CLI, which has no repo key: without target_root a raw ask refuses.
    import json
    import re

    script = mod._write_deferred_script(tmp_path, {"ask": "build the thing", "session_id": ""})
    params_file = Path(json.loads(re.search(r"^const PARAMS_FILE = (.*);$", script.read_text(encoding="utf-8"), re.M).group(1)))
    assert json.loads(params_file.read_text(encoding="utf-8"))["target_root"] == tmp_path.as_posix()


_NODE_HARNESS = """
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8').replace(/^export const meta[^\\n]*\\n/, '');
const cfg = JSON.parse(process.argv[3]);
const calls = [];
const agent = async (prompt) => { calls.push({ agent: prompt }); return cfg.replies.shift(); };
const workflow = async (o) => { calls.push({ workflow: o }); return cfg.results.shift(); };
const run = new (Object.getPrototypeOf(async function () {}).constructor)('agent', 'workflow', src);
run(agent, workflow).then((out) => console.log(JSON.stringify({ out, calls })));
"""


def _run_deferred(tmp_path, params, replies, results):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = mod._write_deferred_script(tmp_path, params)
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [node, str(harness), str(script), json.dumps({"replies": replies, "results": results})],
        capture_output=True, text=True, timeout=30, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(proc.stdout)


def _reply(path="scratch/warp/ask-1.workflow.mjs", run_id="ask-1"):
    return {"path": path, "run_id": run_id, "fire_args": {}}


def test_default_deferred_script_emits_the_ask_as_a_launcher_child(tmp_path):
    import json
    import re

    script = mod._write_deferred_script(tmp_path, {"ask": "build the thing", "session_id": ""})
    params_file = Path(json.loads(re.search(r"^const PARAMS_FILE = (.*);$", script.read_text(encoding="utf-8"), re.M).group(1)))
    assert json.loads(params_file.read_text(encoding="utf-8"))["launcher_child"] is True


@pytest.mark.spawns_process
def test_default_deferred_script_hops_the_chain_at_depth_one(tmp_path):
    handoff = {"chain": {"ask": "do it", "sizing_path": "state/sizings/s.yaml"}, "run_id": "ask-1"}
    got = _run_deferred(
        tmp_path, {"ask": "build the thing", "session_id": ""},
        replies=[_reply(), _reply("scratch/warp/chain-2.workflow.mjs", "chain-2")],
        results=[handoff, {"done": True}],
    )
    starts = [c["workflow"]["scriptPath"] for c in got["calls"] if "workflow" in c]
    assert [Path(s).name for s in starts] == ["ask-1.workflow.mjs", "chain-2.workflow.mjs"]
    chain_emit = [c["agent"] for c in got["calls"] if "agent" in c][1]
    assert '"launcher_child":true' in chain_emit
    assert got["out"] == {"done": True, "chained_from": "ask-1"}


@pytest.mark.spawns_process
def test_default_deferred_script_returns_a_plain_result_without_a_hop(tmp_path):
    got = _run_deferred(tmp_path, {"ask": "x", "session_id": ""}, replies=[_reply()], results=[{"done": 1}])
    assert got["out"] == {"done": 1}
    assert len([c for c in got["calls"] if "workflow" in c]) == 1


@pytest.mark.spawns_process
def test_a_second_hand_off_from_the_chained_run_fails_the_hop(tmp_path):
    handoff = {"chain": {"ask": "do it"}}
    got = _run_deferred(
        tmp_path, {"ask": "x", "session_id": ""},
        replies=[_reply(), _reply("scratch/warp/chain-2.workflow.mjs", "chain-2")],
        results=[handoff, handoff],
    )
    assert got["out"]["status"] == "failed" and "second hand-off" in got["out"]["error"]


@pytest.mark.spawns_process
@pytest.mark.parametrize("blank", ["","   ", '""', "''", ' "" '])
@pytest.mark.parametrize("child", [False, True])
def test_blank_reply_path_is_emit_refused_without_starting_a_workflow(tmp_path, blank, child):
    params = {"ask": "x", "session_id": ""}
    if child:
        params["launcher_child"] = True
    got = _run_deferred(tmp_path, params, replies=[_reply(path=blank)], results=[])
    assert got["out"]["status"] == "emit_refused"
    assert not [c for c in got["calls"] if "workflow" in c]


@pytest.mark.spawns_process
def test_launcher_child_deferred_script_still_hands_the_emit_back(tmp_path):
    got = _run_deferred(tmp_path, {"ask": "x", "session_id": "", "launcher_child": True}, replies=[_reply()], results=[])
    assert got["out"]["deferred_emit_pending"]["run_id"] == "ask-1"
    assert not [c for c in got["calls"] if "workflow" in c]
