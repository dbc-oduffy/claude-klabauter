"""A refire of a halted sizing resumes from the halt-time record: a matching fingerprint emits the
stored script; a missing, stale or stalled record spawns the detached recompose and starts the run
through a one-child launcher. The hook never composes and never reads git."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

SIZING_REL = "state/sizings/2026-10-29-resume-fixture.yaml"
PLAN_REL = "docs/plans/2026-10-29-resume-fixture.md"
HALTED_ID = "warp-2026-10-29-resume-fixture-20261029T010101-abc123"
HEAD = "a" * 40
STORED_REL = f"scratch/warp/{HALTED_ID}.workflow.mjs"


def _repo(tmp_path: Path, *, record: bool = True, verdict=None, halted_id: str = HALTED_ID) -> Path:
    (tmp_path / ".git").mkdir()
    for rel, text in ((SIZING_REL, "schema: sizing-object\n"), (PLAN_REL, "# plan\n"), (STORED_REL, "// stored\n")):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    rd = tmp_path / RECORD_DIR_ROOT / halted_id
    write_halt_marker(rd, halted="ledger-check", sizing_rel=SIZING_REL, plan_rel=PLAN_REL, run_base_sha=HEAD)
    if record:
        fp = hrr.fingerprint_for_run(tmp_path, rd, SIZING_REL, PLAN_REL)
        hrr.write(
            rd,
            hrr.build_record(
                composed_script_rel=STORED_REL, hoisted_fragments=[], gate_verdict=verdict,
                landed_state_sha=None, fingerprint=fp, resume_plan_rel=PLAN_REL,
            ),
        )
    return tmp_path


def _payload(root: Path) -> dict:
    return {
        "tool_name": "Workflow", "cwd": str(root), "session_id": "s1", "tool_use_id": "toolu_x",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": {"sizing": SIZING_REL}},
    }


@pytest.fixture
def seams(monkeypatch):
    """No emit, no compose, a recording recompose spawner."""
    spawned: list = []
    monkeypatch.setattr(mod, "_spawn_recompose", lambda root, run_id: spawned.append(run_id) or True)

    def no_compose(*a, **k):
        raise AssertionError("compose_ask_script called in the hook process")

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.ask_compose.compose_ask_script", no_compose)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.resumable_plan", no_compose)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.emit.resume_landed_state", no_compose)
    monkeypatch.setattr(mod, "_default_emit", no_compose)
    emitted: list = []

    def emit(params, root):
        emitted.append(params)
        return {"path": str(root / "emitted.js"), "run_id": "fresh", "fire_args": {}}

    return spawned, emitted, emit


def _hso(env: dict) -> dict:
    return env["hookSpecificOutput"]


def test_matching_fingerprint_emits_the_stored_script(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    assert HALTED_ID in _hso(env)["additionalContext"]
    assert spawned == [] and emitted == []


@pytest.mark.parametrize("changed", ["sizing", "plan", "head"])
def test_stale_fingerprint_spawns_recompose_and_starts_via_launcher(tmp_path, seams, changed):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    if changed == "sizing":
        (root / SIZING_REL).write_text("schema: sizing-object\nname: edited\n", encoding="utf-8")
    elif changed == "plan":
        (root / PLAN_REL).write_text("# edited\n", encoding="utf-8")
    else:
        rd = root / RECORD_DIR_ROOT / HALTED_ID
        write_halt_marker(rd, halted="ledger-check", sizing_rel=SIZING_REL, plan_rel=PLAN_REL, run_base_sha="b" * 40)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    launcher = Path(_hso(env)["updatedInput"]["scriptPath"])
    assert launcher != root / STORED_REL and HALTED_ID in launcher.read_text(encoding="utf-8")
    assert spawned == [HALTED_ID] and emitted == []


def test_missing_record_spawns_recompose_and_starts_via_launcher(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path, record=False)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [HALTED_ID] and emitted == []


def test_stalled_record_read_starts_the_run_within_the_deadline(tmp_path, seams, monkeypatch):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    real = hrr._read_file

    def slow(path):
        if Path(path).name == hrr.RESUME_FILE:
            time.sleep(1.5)
        return real(path)

    monkeypatch.setattr(hrr, "_read_file", slow)
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    started = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    assert time.perf_counter() - started < 1.0
    assert _hso(env)["permissionDecision"] == "allow"
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [HALTED_ID] and emitted == []


def test_stored_gate_halt_is_a_named_refusal(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _repo(tmp_path, verdict={"halt": {"kind": "touchpoint", "reason": "needs the PM"}})
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert "touchpoint: needs the PM" in _hso(env)["permissionDecisionReason"]
    assert spawned == [] and emitted == []


def _refused_repo(tmp_path: Path, retry_not_before) -> Path:
    root = _repo(tmp_path, record=False)
    rd = root / RECORD_DIR_ROOT / HALTED_ID
    fp = hrr.fingerprint_for_run(root, rd, SIZING_REL, PLAN_REL)
    verdict = {"refused": ["needs the PM"], "halt": {"kind": "SizingFireRefused", "reason": "needs the PM"}}
    hrr.write(
        rd,
        hrr.build_record(
            composed_script_rel=None, hoisted_fragments=[], gate_verdict=verdict, landed_state_sha=None,
            fingerprint=fp, resume_plan_rel=PLAN_REL, retry_not_before=retry_not_before,
        ),
    )
    return root


def test_refused_record_inside_backoff_is_a_refusal_with_no_recompose(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _refused_repo(tmp_path, time.time() + 600)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert "needs the PM" in _hso(env)["permissionDecisionReason"]
    assert spawned == [] and emitted == []


def test_refused_record_past_backoff_recomposes_through_the_launcher(tmp_path, seams):
    spawned, emitted, emit = seams
    root = _refused_repo(tmp_path, time.time() - 1)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert spawned == [HALTED_ID] and emitted == []


def test_no_halted_run_takes_the_plain_emit(tmp_path, seams):
    spawned, emitted, emit = seams
    (tmp_path / ".git").mkdir()
    (tmp_path / SIZING_REL).parent.mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text("schema: sizing-object\n", encoding="utf-8")
    env = mod.evaluate(_payload(tmp_path), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert len(emitted) == 1 and spawned == []


def test_hook_source_names_none_of_the_killed_resume_calls():
    text = Path(mod.__file__).read_text(encoding="utf-8")
    for needle in ("resumable_plan", "compose_ask_script", "live_footprints", "resume_landed_state"):
        assert needle not in text, needle


def test_a_halted_ask_run_resumes_from_its_record(tmp_path, seams):
    """An `ask-<stamp>` halt is found the way the emit body finds it, so its record is read."""
    spawned, emitted, emit = seams
    ask_id = "ask-20261029T010101-abc123"
    root = _repo(tmp_path, halted_id=ask_id)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "allow"
    assert _hso(env)["updatedInput"]["scriptPath"] == str(root / STORED_REL)
    assert ask_id in _hso(env)["additionalContext"]
    assert spawned == [] and emitted == []


@pytest.mark.parametrize("reserved", [HALTED_ID, "ask-20261029T020202-ffffff"])
def test_a_halted_sizing_resumes_whatever_run_id_the_slice_reserved(tmp_path, seams, reserved):
    """A halted marker for the sizing wins over a fan-out slice's reserved run id: the halted run
    resumes from its record whether the slice reserved that id or another."""
    root = _repo(tmp_path)
    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {}, "s1", None, reserved_run_id=reserved)
    assert env is not None or result is not None


def test_a_refire_carrying_its_own_params_is_not_served_the_halt_time_script(tmp_path, seams):
    """The stored script was composed with halt-time params; a call naming its own (stop_after here)
    starts through the launcher with them, and a matching record is not recomposed."""
    spawned, _, _ = seams
    root = _repo(tmp_path)
    launched: list = []

    def launch(r, parent_id, children, session_id):
        launched.extend(children)
        return r / "scratch" / "launcher.workflow.mjs"

    env, result = mod._resume_route(root, SIZING_REL, _payload(root), {"stop_after": "plan"}, "s1", launch)
    assert result is None and env is not None
    assert [c.emit_params.get("stop_after") for c in launched] == ["plan"]
    assert spawned == []


def _sleeping(calls: list, label: str, seconds: float = 1.5):
    def slow(*a, **k):
        calls.append(label)
        time.sleep(seconds)
        return None

    return slow


def test_a_spent_budget_short_circuits_every_later_read(tmp_path, seams, monkeypatch):
    """The record read returns a real record but spends the whole hook budget doing so; the fingerprint
    read, which a returned record would otherwise trigger, then never starts, and the run still starts
    through the launcher without an inline compose."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    real = hrr.read

    def slow(record_dir, deadline_ms):
        time.sleep(0.2)
        return real(record_dir, deadline_ms)

    calls: list = []
    monkeypatch.setattr(hrr, "read", slow)
    monkeypatch.setattr(hrr, "fingerprint_for_run", _sleeping(calls, "fingerprint"))
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 120.0)
    started = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    elapsed = time.perf_counter() - started
    assert calls == []
    assert elapsed < 0.5
    assert _hso(env)["permissionDecision"] == "allow"
    assert HALTED_ID in Path(_hso(env)["updatedInput"]["scriptPath"]).read_text(encoding="utf-8")
    assert spawned == [HALTED_ID] and emitted == []


def test_distinct_keys_stalling_after_the_sizing_read_share_one_budget(tmp_path, seams, monkeypatch):
    """The sizing read returns at once; the halted lookup and the record read each eat part of the
    budget and the fingerprint read stalls past it. No read starts after the budget is spent, the
    hook call total stays inside it, and the run still starts through the launcher."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    budget_ms = 300.0
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", budget_ms)
    starts: dict = {}
    t0 = [0.0]

    def timed(label, real, pause):
        def wrapper(*a, **k):
            starts[label] = (time.perf_counter() - t0[0]) * 1000.0
            time.sleep(pause)
            return real(*a, **k)

        return wrapper

    from coordinator_core.ops.dispatch_emit import halted_run

    monkeypatch.setattr(halted_run, "halted_run_to_resume", timed("halted", halted_run.halted_run_to_resume, 0.1))
    monkeypatch.setattr(hrr, "read", timed("record", hrr.read, 0.1))
    monkeypatch.setattr(hrr, "fingerprint_for_run", timed("fingerprint", lambda *a, **k: None, 1.5))
    t0[0] = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    elapsed_ms = (time.perf_counter() - t0[0]) * 1000.0
    assert {"halted", "record", "fingerprint"} <= set(starts)
    assert elapsed_ms < budget_ms + 150.0
    assert all(at <= budget_ms for at in starts.values())
    assert _hso(env)["permissionDecision"] == "allow"
    assert spawned == [HALTED_ID] and emitted == []


def test_every_read_stalled_never_composes_inline_and_stays_under_the_bar(tmp_path, seams, monkeypatch):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    calls: list = []
    monkeypatch.setattr(hrr, "_read_file", _sleeping(calls, "hrr"))
    monkeypatch.setattr(hrr, "fingerprint_for_run", _sleeping(calls, "fingerprint"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.load_sizing", _sleeping(calls, "sizing"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.halted_run.halted_run_to_resume", _sleeping(calls, "halted"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.live_run_index.read", _sleeping(calls, "index"))
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.cross_plan_write_overlap._declared_write_paths", _sleeping(calls, "spine")
    )
    started = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert _hso(env)["permissionDecision"] == "deny"
    assert emitted == [] and spawned == []


def test_a_stalled_sizing_read_keeps_the_marker_and_retires_nothing(tmp_path, seams, monkeypatch):
    """`{}` for a stalled sizing would drop the ask_parent marker, mint a fresh run and retire the halt."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    retired: list = []
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.load_sizing", _sleeping([], "sizing"))
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.ask_gate.retire_token_spent_halt", lambda *a, **k: retired.append(a)
    )
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert "sizing read" in _hso(env)["permissionDecisionReason"]
    assert retired == [] and emitted == [] and spawned == []


def test_a_stalled_halted_lookup_is_a_refusal_not_the_plain_emit(tmp_path, seams, monkeypatch):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.halted_run.halted_run_to_resume", _sleeping([], "halted"))
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert "halted-run lookup stalled" in _hso(env)["permissionDecisionReason"]
    assert emitted == [] and spawned == []


def test_a_lookup_already_in_flight_is_a_refusal_not_the_plain_emit(tmp_path, seams, monkeypatch):
    """A second hook call while the first's lookup is still stuck returns STALLED at once."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.halted_run.halted_run_to_resume", _sleeping([], "halted"))
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    mod.evaluate(_payload(root), emit=emit)
    started = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    assert time.perf_counter() - started < 0.2
    assert _hso(env)["permissionDecision"] == "deny"
    assert emitted == []


def test_a_record_naming_another_resume_plan_is_not_served(tmp_path, seams):
    """The reader resolves the plan from the sizing and halt marker, not from the record's own field."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    other = "docs/plans/2026-10-29-other.md"
    (root / other).write_text("# other\n", encoding="utf-8")
    rd = root / RECORD_DIR_ROOT / HALTED_ID
    fp = hrr.fingerprint_for_run(root, rd, SIZING_REL, other)
    hrr.write(
        rd,
        hrr.build_record(
            composed_script_rel=STORED_REL, hoisted_fragments=[], gate_verdict=None,
            landed_state_sha=None, fingerprint=fp, resume_plan_rel=other,
        ),
    )
    env = mod.evaluate(_payload(root), emit=emit)
    assert Path(_hso(env)["updatedInput"]["scriptPath"]) != root / STORED_REL
    assert spawned == [HALTED_ID]


def test_a_resolution_change_from_the_sizing_bytes_is_not_served_the_old_record(tmp_path, seams):
    """Halt base unchanged, record intact; the sizing now names another plan, so the current
    resolution differs from the one the record was fingerprinted under."""
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    other = "docs/plans/2026-10-29-other.md"
    (root / other).write_text("# other\n", encoding="utf-8")
    (root / SIZING_REL).write_text(f"schema: sizing-object\nplan: {other}\n", encoding="utf-8")
    env = mod.evaluate(_payload(root), emit=emit)
    assert Path(_hso(env)["updatedInput"]["scriptPath"]) != root / STORED_REL
    assert spawned == [HALTED_ID]


def test_a_second_refire_does_not_stack_a_recompose(tmp_path, monkeypatch):
    root = _repo(tmp_path, record=False)
    spawns: list = []
    monkeypatch.setattr("coordinator_core.ops.ceremony.detached_spawn.spawn_detached", lambda *a, **k: spawns.append(a) or True)
    emit = lambda params, r: {"path": str(r / "e.js"), "run_id": "fresh", "fire_args": {}}  # noqa: E731
    mod.evaluate(_payload(root), emit=emit)
    mod.evaluate(_payload(root), emit=emit)
    assert len(spawns) == 1
    hrr.release_recompose(root / RECORD_DIR_ROOT / HALTED_ID)
    mod.evaluate(_payload(root), emit=emit)
    assert len(spawns) == 2


def _stall_every_read_but_the_hint(monkeypatch, calls):
    real = hrr._read_file
    sleeper = _sleeping(calls, "hrr")

    def read_file(path):
        return real(path) if hrr.HINT_DIR in str(path).replace("\\", "/") else sleeper(path)

    monkeypatch.setattr(hrr, "_read_file", read_file)
    monkeypatch.setattr(hrr, "fingerprint_for_run", _sleeping(calls, "fingerprint"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.load_sizing", _sleeping(calls, "sizing"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.halted_run.halted_run_to_resume", _sleeping(calls, "halted"))
    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.live_run_index.read", _sleeping(calls, "index"))
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.cross_plan_write_overlap._declared_write_paths", _sleeping(calls, "spine")
    )


def test_every_read_stalled_with_a_halt_hint_resumes_through_the_launcher(tmp_path, seams, monkeypatch):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    hrr.write_hint(root, SIZING_REL, HALTED_ID)
    _stall_every_read_but_the_hint(monkeypatch, [])
    started = time.perf_counter()
    env = mod.evaluate(_payload(root), emit=emit)
    assert time.perf_counter() - started < 0.5
    assert _hso(env)["permissionDecision"] == "allow"
    assert emitted == [] and spawned == [HALTED_ID]


def test_a_halt_hint_for_another_sizing_does_not_resume(tmp_path, seams, monkeypatch):
    spawned, emitted, emit = seams
    root = _repo(tmp_path)
    hrr.write_hint(root, "state/sizings/other.yaml", HALTED_ID)
    _stall_every_read_but_the_hint(monkeypatch, [])
    env = mod.evaluate(_payload(root), emit=emit)
    assert _hso(env)["permissionDecision"] == "deny"
    assert emitted == [] and spawned == []
