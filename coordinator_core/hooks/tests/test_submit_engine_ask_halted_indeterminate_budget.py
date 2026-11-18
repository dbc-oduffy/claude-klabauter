"""A `{sizing}` refire of a run halted `indeterminate`, or at commit with an unresolved -32004, answers
inside the hook budget whichever write leg of the refire stalls: each `_resume_route` write leg, and the
caller-thread legs that run after the worker join (`_log_fail_open`, `_spool_envelope`, `_durable_refusal`).

`evaluate` runs at default arguments with the real `_resume_route`; the budget is shrunk to two seconds and
its two fixed graces are scaled with it, so the proportions are the production ones.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from coordinator_core import warp_live
from coordinator_core._settings_home import settings_home
from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
from coordinator_core.ops.dispatch_emit import halted_run, requirements_ledger, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

pytestmark = pytest.mark.deliberate_wall_clock(reason="bounds the hook answer against a stalled write leg; blocked time burns no CPU")

SIZING_REL = "state/sizings/2026-11-18-halt-fixture.yaml"
PLAN_REL = "docs/plans/2026-11-18-halt-fixture.md"
RUN_ID = "warp-2026-11-18-halt-fixture-20261118T150000-abc123"
RESERVED_ID = "ask-20261118T140001-bbbbbb"
PARENT_ID = "ask-20261118T140000-aaaaaa"
HEAD = "a" * 40
BUDGET_S = 2.0
HOLD_S = 4.0
ENGINE_LIVE = "1" * 40
ENGINE_DEAD = "2" * 40

REASONS = {
    "indeterminate": "gate verdict indeterminate: dispatch -32004 unresolved, keys of engine 51484 unknowable",
    "commit": "terminal commit halted: dispatch.terminal_commit replied -32004 and its keys belong to a dead engine",
}


class _Stall:
    """A leg that blocks past the budget; teardown releases it so no thread outlives the test."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.entered = threading.Event()

    def block(self, *args, **kwargs):
        self.entered.set()
        self.event.wait(HOLD_S)
        raise OSError("stall released")


@pytest.fixture
def stalls():
    made: list[_Stall] = []

    def make() -> _Stall:
        made.append(_Stall())
        return made[-1]

    yield make
    for stall in made:
        stall.event.set()


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    for name in ("CLAUDE_CODE_REMOTE", "CLAUDE_CODE_REMOTE_SESSION_ID", "CLAUDE_CODE_CONTAINER_ID", "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COORDINATOR_WORKFLOW_HOOK_TIMEOUT_S", str(BUDGET_S))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setattr(mod, "_is_first_turn", lambda _payload: True)
    # The production graces (0.8s scan, 0.35s envelope) are fixed against an 8s budget; keep that ratio at 2s.
    monkeypatch.setattr(mod, "_SCAN_GRACE_S", 0.2)
    monkeypatch.setattr(mod, "_ENVELOPE_GRACE_S", 0.09)
    monkeypatch.setattr(hrr, "engine_sha", lambda: ENGINE_LIVE)
    monkeypatch.setattr(mod, "_spawn_recompose", lambda root, run_id: True)
    assert mod._caller_budget_s() == BUDGET_S


def _repo(
    tmp_path: Path, monkeypatch, *, kind: str = "indeterminate", dead_engine: bool = False,
    marker: bool = True, ask_parent: bool = False,
) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    sizing = "schema: sizing-object\n"
    if ask_parent:
        sizing += f"em_analysis:\n  ask_parent: {PARENT_ID} {RESERVED_ID}\n"
    for rel, text in ((SIZING_REL, sizing), (PLAN_REL, "# plan\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    rd = root / RECORD_DIR_ROOT / RUN_ID
    if marker:
        monkeypatch.setattr(hrr, "engine_sha", lambda: ENGINE_DEAD if dead_engine else ENGINE_LIVE)
        write_halt_marker(rd, halted=kind, sizing_rel=SIZING_REL, plan_rel=PLAN_REL, run_base_sha=HEAD, reason=REASONS[kind])
        monkeypatch.setattr(hrr, "engine_sha", lambda: ENGINE_LIVE)
    else:
        rd.mkdir(parents=True)
    return root


def _payload(root: Path, args: dict | None = None) -> dict:
    return {
        "tool_name": "Workflow",
        "tool_input": {"name": mod.SUBMIT_WORKFLOW_NAME, "args": args or {"sizing": SIZING_REL}},
        "session_id": "s1",
        "tool_use_id": "toolu_x",
        "transcript_path": str(root / "transcript.jsonl"),
        "cwd": str(root),
    }


def _emit(params: dict, root: Path) -> dict:
    script = root / "scratch" / "warp" / "fresh.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("export const meta = {}\n", encoding="utf-8")
    return {"ok": True, "path": str(script), "run_id": "fresh", "fire_args": {}}


def _drive(root: Path, args: dict | None = None) -> tuple[dict, float]:
    started = time.monotonic()
    envelope = mod.evaluate(_payload(root, args), emit=_emit)
    return envelope, time.monotonic() - started


def _assert_answered(envelope: dict, elapsed: float, leg: str, *, names: tuple[str, ...]) -> None:
    """An envelope inside the budget; a deny carries its cause (one of `names`)."""
    assert elapsed < mod._caller_budget_s(), f"{leg}: answered after {elapsed * 1000:.0f}ms, budget {BUDGET_S * 1000:.0f}ms"
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] in ("allow", "deny")
    if hso["permissionDecision"] == "deny":
        reason = hso["permissionDecisionReason"]
        assert reason.strip() and any(n in reason for n in names), f"{leg}: deny names no cause: {reason!r}"


_RESUME_NAMES = (RUN_ID, "outran the hook budget")


def _on_main_thread(real, stall: _Stall, match):
    """`real`, except the hook's own (main) thread entering `match` blocks: the worker legs stay untouched."""

    def wrapper(*args, **kwargs):
        if threading.current_thread() is threading.main_thread() and match(*args, **kwargs):
            return stall.block()
        return real(*args, **kwargs)

    return wrapper


def _named(name: str):
    return lambda path, *a, **k: Path(path).name == name


# ---- the write legs of `_resume_route`: the worker join already bounds each (control) ----

def _patch_attr(owner, attr):
    return lambda mp, root, stall, trigger: mp.setattr(owner, attr, stall.block)


def _arrange_adopt(mp, root, stall, trigger):
    mp.setattr(mod, "_unmarked_emitted_run", lambda r, s: RUN_ID)
    from coordinator_core.ops.dispatch_emit import orphan_adopt

    mp.setattr(orphan_adopt, "adopt_orphaned_run", stall.block)


def _arrange_carry_inbox(mp, root, stall, trigger):
    mp.setattr(mod, "_slice_deny", lambda r, m: None)
    mp.setattr(mod, "_slice_advisory", lambda r, m: "")
    mp.setattr(requirements_ledger, "carry_inbox", stall.block)


def _arrange_persist(mp, root, stall, trigger):
    from coordinator_core import atomic_append

    real = atomic_append.append_line
    mp.setattr(atomic_append, "append_line", lambda path, *a, **k: stall.block() if Path(path).name == "notes.jsonl" else real(path, *a, **k))


def _arrange_mint(mp, root, stall, trigger):
    mp.setattr(run_token, "mint", stall.block)


# leg -> (repo options, call args, arrangement)
_LEGS = {
    "mark_superseded_by_refire": ({"dead_engine": True}, None, _patch_attr(halted_run, "mark_superseded_by_refire")),
    "_adopt_unmarked_run": ({"marker": False}, None, _arrange_adopt),
    "carry_inbox": ({"ask_parent": True}, None, _arrange_carry_inbox),
    "_persist_em_context": ({}, {"sizing": SIZING_REL, "brief": "carry this note"}, _arrange_persist),
    "hrr.claim_recompose": ({}, None, _patch_attr(hrr, "claim_recompose")),
    "_spawn_recompose": ({}, None, _patch_attr(mod, "_spawn_recompose")),
    "launch": ({}, None, _patch_attr(mod, "_default_launch")),
    "_ensure_launcher_run_token": ({}, None, _arrange_mint),
    "_record_refire": ({}, None, _patch_attr(hrr, "write_refire_record")),
    "warp_live.record_submission": ({}, None, _patch_attr(warp_live, "record_submission")),
}


@pytest.mark.parametrize("leg", list(_LEGS))
def test_stalled_resume_route_write_leg_answers_inside_the_budget(leg, tmp_path, monkeypatch, stalls):
    options, args, arrange = _LEGS[leg]
    root = _repo(tmp_path, monkeypatch, **options)
    stall = stalls()
    arrange(monkeypatch, root, stall, None)
    envelope, elapsed = _drive(root, args)
    assert stall.entered.is_set(), f"{leg}: the refire never reached the leg"
    _assert_answered(envelope, elapsed, leg, names=_RESUME_NAMES)


@pytest.mark.parametrize("kind", sorted(REASONS))
def test_unstalled_refire_is_taken_for_each_halt_kind(kind, tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch, kind=kind)
    envelope, elapsed = _drive(root)
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow", hso.get("permissionDecisionReason")
    assert RUN_ID in hso["additionalContext"]
    assert elapsed < mod._caller_budget_s()


@pytest.mark.parametrize("kind", sorted(REASONS))
def test_stalled_recompose_answers_for_each_halt_kind(kind, tmp_path, monkeypatch, stalls):
    root = _repo(tmp_path, monkeypatch, kind=kind)
    stall = stalls()
    monkeypatch.setattr(mod, "_spawn_recompose", stall.block)
    envelope, elapsed = _drive(root)
    assert stall.entered.is_set()
    _assert_answered(envelope, elapsed, f"kind={kind}", names=_RESUME_NAMES)


# ---- the caller-thread legs after the worker join: a stalled refire overruns, then answers ----

def _overrun_by_recompose(mp, stalls):
    trigger = stalls()
    mp.setattr(mod, "_spawn_recompose", trigger.block)
    return trigger


def _fail_open_append(mp, root, stall):
    from coordinator_core import atomic_append

    mp.setattr(atomic_append, "append_line", _on_main_thread(atomic_append.append_line, stall, _named("door-fail-open.log")))


def _fail_open_mkdir(mp, root, stall):
    state = settings_home() / "state"
    mp.setattr(Path, "mkdir", _on_main_thread(Path.mkdir, stall, lambda path, *a, **k: Path(path) == state))


def _refusal_log_mkdir(mp, root, stall):
    log_dir = root / mod.REFUSAL_LOG_REL
    mp.setattr(Path, "mkdir", _on_main_thread(Path.mkdir, stall, lambda path, *a, **k: Path(path) == log_dir.parent))


def _refusal_log_append(mp, root, stall):
    name = Path(mod.REFUSAL_LOG_REL).name
    mp.setattr(Path, "open", _on_main_thread(Path.open, stall, lambda path, *a, **k: Path(path).name == name))


def _refusal_log_trim(mp, root, stall):
    from coordinator_core import atomic_replace

    log = root / mod.REFUSAL_LOG_REL
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b'{"refusal": "old"}\n' * ((mod._REFUSAL_LOG_CAP_BYTES // 19) + 8))
    mp.setattr(atomic_replace, "atomic_write_bytes", _on_main_thread(atomic_replace.atomic_write_bytes, stall, lambda *a, **k: True))


_CALLER_LEGS = {
    "_log_fail_open.append_line": _fail_open_append,
    "_log_fail_open.mkdir": _fail_open_mkdir,
    "_durable_refusal.mkdir": _refusal_log_mkdir,
    "_durable_refusal.append": _refusal_log_append,
    "_durable_refusal.trim": _refusal_log_trim,
}


@pytest.mark.parametrize("leg", list(_CALLER_LEGS))
def test_stalled_caller_thread_leg_after_the_join_answers_inside_the_budget(leg, tmp_path, monkeypatch, stalls):
    root = _repo(tmp_path, monkeypatch)
    _overrun_by_recompose(monkeypatch, stalls)
    stall = stalls()
    _CALLER_LEGS[leg](monkeypatch, root, stall)
    envelope, elapsed = _drive(root)
    assert stall.entered.is_set(), f"{leg}: the overrun answer never reached the leg"
    _assert_answered(envelope, elapsed, leg, names=_RESUME_NAMES)


def test_overrun_refire_answers_a_named_refusal_with_every_caller_leg_healthy(tmp_path, monkeypatch, stalls):
    root = _repo(tmp_path, monkeypatch)
    _overrun_by_recompose(monkeypatch, stalls)
    envelope, elapsed = _drive(root)
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert RUN_ID in hso["permissionDecisionReason"] and "outran the hook budget" in hso["permissionDecisionReason"]
    assert elapsed < mod._caller_budget_s()


def _overrun_ask_route(mp, stalls):
    """A plain `{ask}` whose worker outlasts the answer deadline with no resume: the spool leg answers."""
    trigger = stalls()
    mp.setattr(warp_live, "run_for_ask", trigger.block)
    return trigger


_SPOOL_NAMES = ("budget", "stalled", "spooled")


def test_overrun_ask_is_spooled_inside_the_budget(tmp_path, monkeypatch, stalls):
    root = _repo(tmp_path, monkeypatch, marker=False)
    _overrun_ask_route(monkeypatch, stalls)
    envelope, elapsed = _drive(root, {"ask": "do x"})
    assert elapsed < mod._caller_budget_s()
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow" and "spooled as" in hso["additionalContext"]


def test_stalled_spool_write_after_the_join_answers_inside_the_budget(tmp_path, monkeypatch, stalls):
    from coordinator_core.ask_spool import record

    root = _repo(tmp_path, monkeypatch, marker=False)
    _overrun_ask_route(monkeypatch, stalls)
    stall = stalls()
    monkeypatch.setattr(record, "spool_ask", stall.block)
    envelope, elapsed = _drive(root, {"ask": "do x"})
    assert stall.entered.is_set(), "the overrun answer never reached the spool write"
    _assert_answered(envelope, elapsed, "_spool_envelope", names=_SPOOL_NAMES)
