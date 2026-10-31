"""The submit hook's whole emit stays under the 500ms process-time bar on the real `dispatch.emit`."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real

pytestmark = [pytest.mark.cadence]

BAR_S = 0.5
repo = real.repo


def test_listing_scope_lists_each_launch_dir_once(tmp_path, monkeypatch):
    from coordinator_core import warp_live

    (tmp_path / "workflow-run-sess-a.json").write_text("{}", encoding="utf-8")
    calls = []
    real_listdir = warp_live.os.listdir
    monkeypatch.setattr(warp_live.os, "listdir", lambda p: calls.append(p) or real_listdir(p))
    dirs = [str(tmp_path)]
    with warp_live.listing_scope():
        for session in ("sess", "other", "sess"):
            warp_live.LaunchSnapshot(session, dirs)
    assert len(calls) == 1
    warp_live.LaunchSnapshot("sess", dirs)
    warp_live.LaunchSnapshot("sess", dirs)
    assert len(calls) == 3  # outside a scope every snapshot lists afresh


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
        if mint:
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
    assert script.parent == repo / "scratch" / "warp" and "dispatch.emit" in text and "build the thing" in text
    assert script.stem.split(".")[0] in hso["additionalContext"] and "Do not re-submit" in hso["additionalContext"]
    assert "nothing was taken" not in str(env)


def test_overrun_after_a_run_was_minted_still_refuses_and_names_it(repo, monkeypatch):
    env = _overrun(repo, monkeypatch, mint=True)
    hso = env["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny", env
    assert "ask-20261029T200000-e3414a" in hso["permissionDecisionReason"]


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
        assert reply["path"].startswith("scratch/warp/defer-") and "dispatch.emit" in body and child.run_id in body


def test_fan_overrun_after_a_run_was_minted_still_names_the_error_for_that_child(tmp_path, monkeypatch):
    import threading

    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, RUN_STAGE_FILE

    release = threading.Event()

    def emit(params, root):
        d = root / RECORD_DIR_ROOT / "ask-20261029T200000-e3414a"
        d.mkdir(parents=True)
        (d / RUN_STAGE_FILE).write_text("{}", encoding="utf-8")
        release.wait(5)
        return {}

    monkeypatch.setattr(mod, "_default_emit", emit)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", "0.25")
    try:
        launcher = mod._default_launch(tmp_path, "ask-parent", _fan_children(), None)
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
