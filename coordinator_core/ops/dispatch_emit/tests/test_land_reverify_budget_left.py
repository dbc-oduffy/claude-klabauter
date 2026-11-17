"""A landing whose earlier legs finish in budget reaches `clean_overlay_verify.reverify_on_mismatch` with
budget left, and the landing lands: on the first send or on the designed re-send the park directs, never in
a terminal parked halt. Driven through the real producer (`measured_rev`) and the default
`land_run._handler` over a mismatch-keyed overlay verdict; the detached overlay child is replaced by a
no-op script plus a synchronous write of the green verdict it would have written. The step's process
time and spawns are held to the 500ms brightline (process time, never wall clock)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import detached_spawn
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_deadline, land_resume, land_run
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.ops.dispatch_emit.ask_contract import measured_rev_record_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (
    FIXTURE,
    LANDING,
    _VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage
from coordinator_core.telemetry import spawn_counter

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Path the profile script (`coordinator_core/benchmarks/land_reverify_step_profile.py`) sets so the
#: scenario writes its measured row there; the tests themselves never need it.
OUT_ENV = "LAND_REVERIFY_PROFILE_OUT"
BAR_MS = 500.0
_NOOP_CHILD = "# detached overlay stand-in: the verdict is written by the seam, never by this process\n"


def _arm(root: Path, monkeypatch, tmp_path: Path) -> dict:
    """Replace the resume queue dir, the detached overlay child and meter `reverify_on_mismatch`."""
    probe: dict = {"calls": 0, "spawns": 0, "spawn_ok": None}
    monkeypatch.setattr(land_resume, "queue_dir", lambda root=None: tmp_path / "land-resume")
    child = tmp_path / "noop_child.py"
    child.write_text(_NOOP_CHILD, encoding="utf-8")
    real_spawn = detached_spawn.spawn_detached

    def spawn(repo_root, script_path, args=None, env_extra=None, site_free=False):
        probe["spawns"] += 1
        probe["spawn_ok"] = real_spawn(repo_root, str(child), args, env_extra, site_free)
        run = args[args.index("--run-id") + 1]
        at = Path(args[args.index("--repo-root") + 1])
        record = json.loads(measured_rev_record_path(at, run).read_text(encoding="utf-8"))
        cov.write_verdict(at, run, record, cov.OverlayVerdict("green", "seam", tuple(record.get("paths") or ())))
        cov._release_inflight(at, run)
        return probe["spawn_ok"]

    monkeypatch.setattr(detached_spawn, "spawn_detached", spawn)
    real = cov.reverify_on_mismatch

    def metered(root_, run, measured, stale, **kw):
        probe["calls"] += 1
        deadline = land_deadline._DEADLINE.get()
        left = None if deadline is None else deadline - land_deadline._now()
        legs = land_deadline.leg_costs()
        probe.setdefault("budget_left_secs", left)
        probe.setdefault("earlier_legs_secs", round(sum(e["elapsed_secs"] for e in legs), 3))
        probe.setdefault("earlier_legs", [e["leg"] for e in legs])
        spawns0, cpu0 = spawn_counter.scope_spawns(), time.process_time()
        try:
            return real(root_, run, measured, stale, **kw)
        finally:
            probe.setdefault("step_process_time_ms", round((time.process_time() - cpu0) * 1000.0, 1))
            probe.setdefault("step_spawns", spawn_counter.scope_spawns() - spawns0)

    monkeypatch.setattr(cov, "reverify_on_mismatch", metered)
    return probe


def _reaper_ms() -> float | None:
    """Process time of one `find_orphans` scan at default arguments; None when psutil is absent."""
    from coordinator_core.testing import orphan_reaper

    if orphan_reaper.psutil is None:
        return None
    me = orphan_reaper.psutil.Process()
    cpu0 = time.process_time()
    orphan_reaper.find_orphans(str(Path(sys.executable).parent), os.getpid(), me.create_time(), None)
    return round((time.process_time() - cpu0) * 1000.0, 1)


class _Version:
    server_sha = "deadbeef"

    def is_skewed(self, client_token: str) -> bool:
        return False

    def is_token_skewed(self, client_token: str) -> bool:
        return False


def _drain_resend(root: Path, monkeypatch, tmp_path: Path) -> tuple[dict | None, dict | None, bool]:
    """Re-send through the warm engine's land-resume drain (`_land_resume_tick`) instead of by hand: the
    queued entry's own params go to `dispatch.land_run` (the product `_handler`) once the entry is due.
    Returns (reply, queue `.done` entry, whether the entry was claimable before it was due)."""
    from coordinator_core.warm import breadcrumb, idle, lifecycle, server

    lifecycle.reset_shutdown_guard_for_test()
    idle.reset_idle_clock_for_test()
    monkeypatch.setattr(breadcrumb, "svc_dir", lambda r=None: tmp_path / "svc")
    monkeypatch.setattr(server._ServerContext, "served_repos", lambda self: [root])
    ctx = server._ServerContext(name="pipe-reverify", sid="sid-reverify", version_state=_Version(), engine_root=tmp_path)
    replies: list = []

    def pool_dispatch(msg, **kw):
        assert msg["method"] == "dispatch.land_run", msg
        reply = land_run._handler(msg["params"], repo_root=Path(msg["_origin_worktree"]) / ".git")
        replies.append(reply)
        return {"jsonrpc": "2.0", "id": msg["id"], "result": reply}

    monkeypatch.setattr(ctx, "_pool_dispatch", pool_dispatch)
    claimable_early = ctx._land_resume_tick()
    real_next_due = land_resume.next_due
    monkeypatch.setattr(
        land_resume, "next_due", lambda r, now: real_next_due(r, now + cov.REVERIFY_RESUME_DELAY_SECS + 1.0)
    )
    try:
        claimed = ctx._land_resume_tick()
        thread = ctx.__dict__.get("_land_resume_thread")
        if thread is not None:
            thread.join(60)
            assert not thread.is_alive()
    finally:
        lifecycle.reset_shutdown_guard_for_test()
        idle.reset_idle_clock_for_test()
    done = tmp_path / "land-resume" / (RUN2 + land_resume.DONE_SUFFIX)
    entry = json.loads(done.read_text(encoding="utf-8")) if done.is_file() else None
    return (replies[0] if claimed and replies else None), entry, claimable_early


def run_scenario(
    root: Path, monkeypatch, tmp_path: Path, *, advance_base: bool = False, via_drain: bool = False
) -> dict:
    """First send over a mismatch-keyed verdict, then the designed re-send through the same entry (or,
    `via_drain`, through the warm land-resume drain); `advance_base` lands an unrelated commit between the
    two (a peer landing while the overlay ran)."""
    probe = _arm(root, monkeypatch, tmp_path)
    _track_fixture(root)
    resume, token = _stage(root)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=root)
    cov.write_verdict(
        root, RUN2, {**measured, "blobs": {"x": "0" * 40}}, cov.OverlayVerdict("green", "stale", ("t/x.py",))
    )
    assert cov.check_verdict(root, RUN2, measured).verdict == "mismatch"
    _write(root, FIXTURE, '{"a": 9, "dirty": true}\n')
    head = _git(root, "rev-parse", "HEAD").strip()

    first = _terminal_with(root, token, resume, _VERDICT)
    second = None
    drained: dict = {}
    if first.get("parked"):
        if advance_base:
            _write(root, "peer/unrelated.txt", "peer landed\n")
            _git(root, "add", "peer/unrelated.txt")
            _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "peer")
        if via_drain:
            second, done, early = _drain_resend(root, monkeypatch, tmp_path)
            drained = {"claimable_before_due": early, "done": (done or {}).get("outcome")}
        else:
            second = _terminal_with(root, token, resume, _VERDICT)
    final = second if second is not None else first
    row = {
        "outcome": "landed" if final.get("committed") is True else "parked" if final.get("parked") else "refused",
        "first_send": "landed" if first.get("committed") is True else "parked" if first.get("parked") else "refused",
        "resent": second is not None,
        "bound_by": None if second is None and not first.get("parked") else (first.get("bound_by") or "step"),
        "status": first.get("status"),
        "queued_for_resume": first.get("queued_for_resume"),
        "resume_not_before": (land_resume.load(None, RUN2) or {}).get("not_before") if not via_drain else None,
        "drained": drained,
        "refused": final.get("refused"),
        "budget_left_secs": None if probe.get("budget_left_secs") is None else round(probe["budget_left_secs"], 3),
        "earlier_legs_secs": probe.get("earlier_legs_secs"),
        "earlier_legs": probe.get("earlier_legs"),
        "step_process_time_ms": probe.get("step_process_time_ms"),
        "step_spawns": probe.get("step_spawns"),
        "step_calls": probe["calls"],
        "overlay_spawns": probe["spawns"],
        "head_moved": _git(root, "rev-parse", "HEAD").strip() != head,
        "final_detail": {k: final.get(k) for k in ("refused", "paths", "leg", "bound_by") if final.get(k)},
        "reaper_process_time_ms": _reaper_ms(),
        "first_error": first.get("error"),
        "final_error": final.get("error"),
    }
    out = os.environ.get(OUT_ENV)
    if out:
        Path(out).write_text(json.dumps(row), encoding="utf-8")
    return row


@pytest.fixture
def scenario(halted, monkeypatch, tmp_path):  # noqa: F811
    return run_scenario(halted, monkeypatch, tmp_path)


def test_the_step_is_reached_with_budget_left_and_the_op_budget_is_unchanged(scenario) -> None:
    assert scenario["step_calls"] >= 1, scenario
    assert scenario["budget_left_secs"] is not None, scenario
    assert scenario["budget_left_secs"] > cov.REVERIFY_STEP_SECS, scenario
    assert (cov.REVERIFY_STEP_SECS, land_deadline.LAND_BUDGET_SECS) == (8.0, 10.0)


def test_a_mismatch_landing_lands_on_the_first_send_or_the_designed_resend(scenario) -> None:
    assert scenario["outcome"] == "landed", scenario
    assert scenario["head_moved"] is True, scenario
    assert scenario["refused"] is None, scenario
    if scenario["first_send"] == "parked":
        assert scenario["queued_for_resume"] is True and scenario["bound_by"] == "step", scenario
        assert scenario["resume_not_before"] is not None, scenario


def test_the_designed_resend_still_lands_after_a_peer_landing_moved_the_base(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    row = run_scenario(halted, monkeypatch, tmp_path, advance_base=True)
    assert row["first_send"] == "parked" and row["outcome"] == "landed", row


def test_the_warm_drain_re_lands_the_parked_landing_once_the_entry_is_due(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    row = run_scenario(halted, monkeypatch, tmp_path, via_drain=True)
    assert row["first_send"] == "parked" and row["queued_for_resume"] is True, row
    assert row["drained"] == {"claimable_before_due": False, "done": "landed"}, row
    assert row["outcome"] == "landed" and row["head_moved"] is True and row["refused"] is None, row


def test_the_warm_drain_re_lands_after_a_peer_landing_moved_the_base(halted, monkeypatch, tmp_path) -> None:  # noqa: F811
    row = run_scenario(halted, monkeypatch, tmp_path, advance_base=True, via_drain=True)
    assert row["first_send"] == "parked" and row["drained"]["done"] == "landed", row
    assert row["outcome"] == "landed", row


def test_the_reverify_step_stays_inside_the_brightline(scenario) -> None:
    assert scenario["step_process_time_ms"] is not None, scenario
    assert scenario["step_process_time_ms"] < BAR_MS, scenario
    assert scenario["overlay_spawns"] == 1, scenario
    assert scenario["step_spawns"] is not None and scenario["step_spawns"] <= 1, scenario
