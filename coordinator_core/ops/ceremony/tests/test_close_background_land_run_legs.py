"""close.background's deferred landing legs: review-stamp mint, coded stamp, sizing ship, implemented
flip, deliverable cascade, completion entry, baton ship and lessons write tracked records, and
``close-commit`` lands exactly the paths they wrote in one commit."""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.tests.test_close_out_goal_refusal import _prime_exit_criterion_block
from coordinator_core.git.commit import CommitRefused
from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops import review_stamp
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import run_close
from coordinator_core.ops.dispatch_emit.run_close import LegResult
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _init_repo, _sizing_body
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run-legs-1"
DID = "dlv-landrun-legs-000001"
PLAN = "docs/plans/2026-08-27-landrun-legs.md"
SIZING = "state/sizings/2026-08-27-fixture-sizing.yaml"
BATON = "state/handoffs/20260827-landrun-baton.md"
LAND_LEGS = (
    "review-stamp", "coded-stamp", "sizing-ship", "plan-reconciliation", "deliverable-cascade",
    "completion-entry", "baton-ship", "lessons", "close-commit",
)
_STAMP = (
    "review_stamp:\n  terminal_commit_sha: abc\n  terminal_tree_sha: def\n"
    "  criterion:\n    status: met\n    observation: o\n    sidecar: null\n"
    "  stamped_at: '2026-10-01T00:00:00Z'\n"
)
_PLAN = """---
title: "Landrun legs fixture"
created: 2026-08-27
author: test-fixture
status: approved
branch: "work/test-fixture/2026-08-27"
plan_id: "pln-landrun-legs-000001"
deliverable_id: "{did}"
sizing_object: {sizing}
{goal}---

# Landrun legs fixture

{spine}
"""
_SPINE = """## Tasks

```yaml plan-tasks
- id: C1
  title: one
  change_kind: code-edit
  surface: src/a.py
  writes: [src/a.py]
  disposition: open
- id: C2
  title: two
  change_kind: code-edit
  surface: src/b.py
  writes: [src/b.py]
  disposition: open
```
"""


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    )
    return done.stdout.strip()


def _commit(root: Path, message: str, *paths: str) -> str:
    _git(root, "add", *paths)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _baton_text() -> str:
    return (
        '---\ntitle: "Baton"\ncreated: 2026-08-27\nbranch: work/test/2026-08-27\nstatus: open\n'
        f'predecessor: "none"\ndeployment_state: ready_to_fire\ndeliverable_id: {DID}\n'
        "category: infra\nsummary: fixture baton\n---\n\n# Baton\n\n## Session Ledger\n\n"
    )


def _build(root: Path, *, spine: bool = False, goal: bool = True) -> str:
    """A repo holding a plan (a spine, or the spineless prime-criterion shape), its sizing and baton,
    and a terminal commit carrying ``Lands-Chunks: C1``; returns the terminal sha."""
    _init_repo(root)
    _write(root, "fixture.py", "v1")
    base = _commit(root, "fixture", "fixture.py")
    block = _prime_exit_criterion_block(baseline_ref=base, exit_criterion_met=None) if goal else ""
    _write(root, PLAN, _PLAN.format(did=DID, sizing=SIZING, goal=block, spine=_SPINE if spine else "No spine."))
    _write(root, SIZING, _sizing_body(status="routed", deliverable_id=DID, plan=PLAN) + f"baton: {BATON}\n")
    _write(root, BATON, _baton_text())
    _commit(root, "seed", PLAN, SIZING, BATON)
    _write(root, "src/a.py", "landed\n")
    return _commit(root, "land the run\n\nLands-Chunks: C1", "src/a.py")


def _job(root: Path, sha: str, legs=LAND_LEGS, *, inputs=None, criterion="default", **extra) -> None:
    if criterion == "default":
        criterion = {"status": "met", "observation": "o", "completion_summary": "The run landed."}
    run_close.write_close_job(
        root, RUN, legs, datetime.now().astimezone() + timedelta(minutes=10),
        plan_rel=PLAN, terminal_sha=sha, criterion=criterion, inputs=inputs, **extra,
    )


def _ctx(root: Path, **kw) -> cb.JobContext:
    return cb.JobContext(root, root / ".git", RUN, tuple(kw.pop("batons", ())), None, **kw)


def _fake_mint(plan_path, repo_root, **_kw):
    plan_path.write_text(plan_path.read_text(encoding="utf-8").replace("status: approved\n", "status: approved\n" + _STAMP, 1), encoding="utf-8")
    return {}


def _record(root: Path) -> dict:
    return run_close._load(root, RUN)["legs"]


def _at_head(root: Path, rel: str) -> str:
    return _git(root, "show", f"HEAD:{rel}")


def _status(root: Path) -> list:
    return [line for line in _git(root, "status", "--porcelain").splitlines() if ".coordinator-local" not in line]


def test_landed_run_closes_plan_sizing_and_baton_in_one_commit_with_peer_dirt_left_alone(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    monkeypatch.setattr(review_stamp, "mint", _fake_mint)
    _write(root, "README.md", "a peer is editing this\n")
    _write(root, "peer.txt", "a peer's new file\n")
    legs = tuple(leg for leg in LAND_LEGS if leg != "baton-ship")
    _job(root, sha, legs, claimed_batons=[BATON])
    before = _git(root, "rev-parse", "HEAD")

    outcomes = cb.run_background(_ctx(root, batons=[BATON]))

    assert {leg: r.status for leg, r in outcomes.items() if r.status == "failed"} == {}, outcomes
    assert {"review-stamp", "sizing-ship", "plan-reconciliation", "close-commit"} <= {leg for leg, r in outcomes.items() if r.status == "ran"}
    recorded = _record(root)
    assert {leg: recorded[leg]["status"] for leg in legs} == {leg: outcomes[leg].status for leg in legs}
    assert "status: implemented" in _at_head(root, PLAN) and "review_stamp:" in _at_head(root, PLAN)
    assert yaml.safe_load(_at_head(root, SIZING))["status"] == "shipped"
    assert "deployment_state: shipped" in _at_head(root, BATON)
    message = _git(root, "log", "-1", "--format=%B")
    assert f"Sizing-Shipped: {SIZING} {sha}" in message
    assert _git(root, "rev-parse", "HEAD~1") == before
    committed = set(_git(root, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert {PLAN, SIZING, BATON} <= committed and not {"README.md", "peer.txt"} & committed
    assert sorted(line.strip() for line in _status(root)) == ["?? peer.txt", "M README.md"]


def test_peer_commit_landed_before_the_close_commit_stays_under_the_close_commit(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    monkeypatch.setattr(review_stamp, "mint", _fake_mint)
    _job(root, sha, ("review-stamp", "plan-reconciliation", "close-commit"))
    import coordinator_core.git.commit as commit_mod

    real = commit_mod.commit_paths
    calls = []

    def racing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            _write(root, "peer.txt", "peer\n")
            _commit(root, "peer lands first", "peer.txt")
            raise CommitRefused("compare-and-swap failed -- refs/heads/x moved since abc was captured.")
        return real(*args, **kwargs)

    monkeypatch.setattr(commit_mod, "commit_paths", racing)
    outcomes = cb.run_background(_ctx(root))

    assert outcomes["close-commit"].status == "ran", outcomes["close-commit"]
    assert len(calls) == 2
    assert _git(root, "log", "-1", "--format=%s", "HEAD~1") == "peer lands first"
    assert "status: implemented" in _at_head(root, PLAN)
    assert _at_head(root, "peer.txt") == "peer"


def test_close_commit_gives_up_after_the_retry_bound_naming_the_dirty_paths(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    monkeypatch.setattr(review_stamp, "mint", _fake_mint)
    _job(root, sha, ("review-stamp", "close-commit"))
    import coordinator_core.git.commit as commit_mod

    calls = []

    def always_lost(*_a, **_k):
        calls.append(1)
        raise CommitRefused("compare-and-swap failed -- refs/heads/x moved since abc was captured.")

    monkeypatch.setattr(commit_mod, "commit_paths", always_lost)
    head = _git(root, "rev-parse", "HEAD")
    outcomes = cb.run_background(_ctx(root))

    assert outcomes["close-commit"].status == "failed"
    assert PLAN in outcomes["close-commit"].detail
    assert len(calls) == cb.CLOSE_COMMIT_CAS_RETRIES + 1
    assert _git(root, "rev-parse", "HEAD") == head
    assert _record(root)["close-commit"]["status"] == "failed"


def test_budget_stop_does_not_strand_the_writes_of_the_legs_that_ran(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    monkeypatch.setattr(review_stamp, "mint", _fake_mint)
    _job(root, sha, ("review-stamp", "plan-reconciliation", "close-commit"))
    clock = iter([0.0, 0.0, 999.0, 999.0, 999.0])

    outcomes = cb.run_background(_ctx(root), cpu_ms=lambda: next(clock))

    assert outcomes["review-stamp"].status == "ran"
    assert outcomes["plan-reconciliation"].status == "queued" and "deferred#1" in outcomes["plan-reconciliation"].detail
    assert outcomes["close-commit"].status == "ran"
    assert _record(root)["close-commit"]["status"] == "queued"
    assert "review_stamp:" in _at_head(root, PLAN) and not _status(root)


def test_close_commit_without_a_written_path_is_not_applicable(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root)
    _job(root, sha, ("close-commit",))
    assert cb.run_background(_ctx(root))["close-commit"].status == "not-applicable"


def test_close_commit_never_stages_a_gitignored_plan(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root)
    _write(root, ".gitignore", "scratch/\n")
    _commit(root, "ignore", ".gitignore")
    _write(root, "scratch/spine.md", "spine\n")
    ctx = _ctx(root, record={"plan_rel": PLAN})
    cb._note_written(ctx, "scratch/spine.md")
    head = _git(root, "rev-parse", "HEAD")
    result = cb._close_commit(ctx)
    assert result.status == "ran" and _git(root, "rev-parse", "HEAD") == head, (result, sha)


def test_review_stamp_refusal_is_the_legs_failed_detail(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)

    def refuse(*_a, **_k):
        raise review_stamp.MintRefusal("review-stamp: no terminal commit found within 200 commits")

    monkeypatch.setattr(review_stamp, "mint", refuse)
    _job(root, sha, ("review-stamp", "close-commit"))
    outcomes = cb.run_background(_ctx(root))
    assert outcomes["review-stamp"].status == "failed" and "no terminal commit found" in outcomes["review-stamp"].detail
    assert outcomes["close-commit"].status == "not-applicable"
    assert not _status(root)


def test_a_minted_stamp_queues_the_review_leg_for_post_close(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    monkeypatch.setattr(review_stamp, "mint", _fake_mint)
    _job(root, sha, ("review-stamp",))
    cb.run_background(_ctx(root))
    record = run_close._load(root, RUN)
    assert record["review_receipt"] == PLAN and record["legs"]["review"]["status"] == "queued"


# ------------------------------------------------------------------------- coded stamp


def _rows(root: Path) -> dict:
    text = (root / PLAN).read_text(encoding="utf-8")
    block = text.split("```yaml plan-tasks\n", 1)[1].split("```", 1)[0]
    return {r["id"]: r for r in yaml.safe_load(block)}


def test_coded_stamp_closes_the_credited_row_against_the_terminal_commit(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root, spine=True, goal=False)
    _job(root, sha, ("coded-stamp", "close-commit"))
    outcomes = cb.run_background(_ctx(root))
    rows = _rows(root)
    assert outcomes["coded-stamp"].status == "ran" and "1 row(s) coded" in outcomes["coded-stamp"].detail
    assert rows["C1"]["disposition"] == "coded" and rows["C1"]["disposition_ref"] == sha
    assert rows["C2"]["disposition"] == "open"
    assert outcomes["close-commit"].status == "ran" and not _status(root)


def test_coded_stamp_explicit_credited_rows_override_the_commit_trailer(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root, spine=True, goal=False)
    _job(root, sha, ("coded-stamp",), inputs={"credited_rows": ["C2"]})
    cb.run_background(_ctx(root))
    rows = _rows(root)
    assert rows["C2"]["disposition"] == "coded" and rows["C1"]["disposition"] == "open"
    assert rows["C2"].get("disposition_detail", "").startswith("resolved as a no-op")


def test_coded_stamp_closes_a_row_the_runs_own_checkpoint_landed(tmp_path):
    root = tmp_path / "repo"
    _build(root, spine=True, goal=False)
    _write(root, "src/b.py", "checkpointed\n")
    cp = _commit(
        root, f"checkpoint(wave 1): 1 row — C2\n\nCheckpoint-Plan: {PLAN}", "src/b.py"
    )
    _write(root, "notes.txt", "terminal\n")
    sha = _commit(root, "terminal", "notes.txt")
    _job(root, sha, ("coded-stamp",), inputs={"credited_rows": ["C2"]})
    cb.run_background(_ctx(root))
    row = _rows(root)["C2"]
    assert row["disposition"] == "coded" and row["disposition_ref"] == cp


def test_coded_stamp_closes_a_row_an_earlier_fire_checkpointed_though_this_run_credited_none(tmp_path):
    root = tmp_path / "repo"
    _build(root, spine=True, goal=False)
    _write(root, "src/b.py", "earlier fire\n")
    cp = _commit(root, f"checkpoint(wave 1): 1 row — C2\n\nCheckpoint-Plan: {PLAN}", "src/b.py")
    _write(root, "notes.txt", "terminal\n")
    sha = _commit(root, "terminal", "notes.txt")
    _job(root, sha, ("coded-stamp",), inputs={"credited_rows": []})
    cb.run_background(_ctx(root))
    row = _rows(root)["C2"]
    assert row["disposition"] == "coded" and row["disposition_ref"] == cp


def test_coded_stamp_codes_a_resumed_halts_landed_rows_at_the_halt_commit(tmp_path):
    root = tmp_path / "repo"
    _build(root, spine=True, goal=False)
    _write(root, "src/b.py", "halted run landed\n")
    halt = _commit(root, "halt record\n\nLands-Chunks: C2", "src/b.py")
    _write(root, "notes.txt", "terminal\n")
    sha = _commit(root, "terminal", "notes.txt")
    _job(root, sha, ("coded-stamp",), inputs={"credited_rows": [], "resume_halt_sha": halt})
    cb.run_background(_ctx(root))
    row = _rows(root)["C2"]
    assert row["disposition"] == "coded" and row["disposition_ref"] == halt


def test_coded_stamp_review_only_row_closes_at_the_newest_commit_touching_its_writes(tmp_path):
    root = tmp_path / "repo"
    sha_c1 = _build(root, spine=True, goal=False)
    base = _git(root, "rev-parse", "HEAD~1")
    _write(root, "src/b.py", "reviewed fix\n")
    fix = _commit(root, "review fix", "src/b.py")
    _job(
        root, fix, ("coded-stamp",),
        inputs={"credited_rows": [], "review_only": {"open_rows": ["C1", "C2"], "run_base": base,
                                                     "rows": [{"id": "C2", "paths": ["src/b.py"]},
                                                              {"id": "C1", "paths": ["never.py"]}]}},
    )
    out = cb.run_background(_ctx(root))["coded-stamp"]
    rows = _rows(root)
    assert rows["C2"]["disposition_ref"] == fix and rows["C1"]["disposition"] == "open", sha_c1
    assert "C1" in out.detail


# ------------------------------------------------------------------- implemented flip


def _flip_ctx(root: Path, sha: str, **record) -> cb.JobContext:
    base = {"plan_rel": PLAN, "terminal_sha": sha}
    base.update(record)
    return _ctx(root, record=base, terminal_sha=sha)


def test_flip_refuses_a_plan_whose_falsifier_integrity_is_broken(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    _fake_mint(root / PLAN, root)
    result = cb._plan_flip(_flip_ctx(root, sha, falsifier_broken=["no-baseline"]))
    assert result.status == "failed" and "BROKEN" in result.detail and "no-baseline" in result.detail
    assert "status: approved" in (root / PLAN).read_text(encoding="utf-8")


def test_flip_refuses_without_a_minted_stamp_or_with_incomplete_rows(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root)
    assert cb._plan_flip(_flip_ctx(root, sha)).detail == "no review stamp minted"
    _fake_mint(root / PLAN, root)
    refused = cb._plan_flip(_flip_ctx(root, sha, inputs={"incomplete": ["C2"]}))
    assert refused.status == "failed" and "incomplete chunks remain" in refused.detail


def test_flip_refuses_a_plan_with_open_spine_rows(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root, spine=True)
    _fake_mint(root / PLAN, root)
    result = cb._plan_flip(_flip_ctx(root, sha))
    assert result.status == "failed" and "spine row(s) still open" in result.detail


def test_flip_writes_implemented_and_never_runs_the_cascade(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    _fake_mint(root / PLAN, root)
    monkeypatch.setattr(pst, "_run_cascade", lambda *a, **k: pytest.fail("the flip leg ran the cascade"))
    ctx = _flip_ctx(root, sha)
    result = cb._plan_flip(ctx)
    assert result.status == "ran", result
    assert "status: implemented" in (root / PLAN).read_text(encoding="utf-8")
    assert ctx.written == [PLAN]


def test_stamp_implemented_cascade_default_is_unchanged(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    _fake_mint(root / PLAN, root)
    seen = []
    monkeypatch.setattr(pst, "_run_cascade", lambda *a, **k: seen.append(a) or 0)
    flags = dict(falsifier_verdict="pass", falsifier_output="right", prose="p", no_commit=True, ship_sha=sha)
    assert pst.stamp_implemented(str(root / PLAN), **flags) == 0
    assert len(seen) == 1
    (root / PLAN).write_text((root / PLAN).read_text(encoding="utf-8").replace("status: implemented", "status: approved"), encoding="utf-8")
    seen.clear()
    assert pst.stamp_implemented(str(root / PLAN), cascade=False, no_commit=True, ship_sha=sha) == 0
    assert seen == []
    (root / PLAN).write_text((root / PLAN).read_text(encoding="utf-8").replace("status: implemented", "status: approved"), encoding="utf-8")
    assert pst.stamp_implemented(str(root / PLAN), no_commit=True, ship_sha=sha) == 0
    assert len(seen) == 1


def test_no_cascade_flag_is_refused_outside_stamp_implemented(tmp_path, capsys):
    plan = tmp_path / "p.md"
    plan.write_text("---\nstatus: approved\n---\n", encoding="utf-8")
    assert pst.main(["stamp-superseded", "--plan", str(plan), "--by", "x.md", "--no-cascade"]) == 1
    assert "--no-cascade" in capsys.readouterr().err


# -------------------------------------------------------- cascade, ship, record vocabulary


def test_deliverable_cascade_waits_for_an_implemented_plan(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root)
    result = cb._deliverable_cascade(_flip_ctx(root, sha))
    assert result.status == "not-applicable" and result.detail == "plan is not implemented"


def test_deliverable_cascade_reads_only_the_records_the_plan_names(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    for i in range(40):
        _write(root, f"state/sizings/20250101-pad-{i:03d}.yaml", _sizing_body(deliverable_id=f"dlv-pad-{i:06d}"))
    text = (root / PLAN).read_text(encoding="utf-8").replace("status: approved", "status: implemented")
    (root / PLAN).write_text(text, encoding="utf-8")
    import coordinator_core.ops.deliverable_cascade as cascade_mod

    reads = []
    original = cascade_mod._read_sizing_meta
    object.__setattr__(cascade_mod._SIZING_KIND, "reader", lambda p: reads.append(str(p)) or original(p))
    try:
        ctx = _flip_ctx(root, sha)
        result = cb._deliverable_cascade(ctx)
    finally:
        object.__setattr__(cascade_mod._SIZING_KIND, "reader", original)
    assert result.status == "ran", result
    assert reads and all("pad-" not in r for r in reads)
    assert SIZING in ctx.written


def test_baton_drain_leaves_the_corpus_cascade_to_the_run_scoped_leg(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _build(root)
    run_close.write_close_job(
        root, RUN, ("deliverable-cascade", "baton-drain"), datetime.now().astimezone() + timedelta(minutes=10)
    )
    run_close.record_leg_outcome(root, RUN, "deliverable-cascade", "ran", "cascade over 2 record(s)")
    import coordinator_core.ops.dispatch_emit.run_baton_cascade as rbc

    monkeypatch.setattr(rbc, "fire_for_batons", lambda *a, **k: pytest.fail("the corpus cascade ran"))
    note, failed = cb._fire_cascade(_ctx(root), [BATON])
    assert (note, failed) == ("cascade: paid by deliverable-cascade", False)


def test_land_run_legs_are_in_the_vocabulary_in_ledger_order():
    legs = cb.BACKGROUND_LEGS
    assert set(legs) <= set(run_close.RUN_BOUND_LEGS)
    assert legs.index("coded-stamp") < legs.index("sizing-ship") < legs.index("plan-reconciliation")
    assert legs.index("plan-reconciliation") < legs.index("deliverable-cascade") < legs.index("close-commit")
    assert legs.index("close-commit") < legs.index("baton-drain")
    assert set(cb.DEFAULT_RUNNERS) == set(legs) and cb.UNBUDGETED_LEGS == {"close-commit"}


def test_a_close_block_that_omits_the_land_run_only_legs_reads_them_not_applicable():
    block = run_close.build_close_block("r1", True, {})
    for leg in run_close.LAND_RUN_ONLY_LEGS:
        assert block["legs"][leg]["status"] == "not-applicable"
    assert "review" in block["outstanding"] and "close-commit" not in block["outstanding"]


def test_close_job_inputs_round_trip_and_unknown_keys_are_refused(tmp_path):
    run_close.write_close_job(
        tmp_path, RUN, ("coded-stamp",), datetime.now().astimezone() + timedelta(minutes=1),
        inputs={"credited_rows": ["C1"], "ledger_met": "ledger met"},
    )
    assert run_close._load(tmp_path, RUN)["inputs"] == {"credited_rows": ["C1"], "ledger_met": "ledger met"}
    with pytest.raises(ValueError, match="unknown close input"):
        run_close.write_close_job(
            tmp_path, "r2", ("coded-stamp",), datetime.now().astimezone() + timedelta(minutes=1),
            inputs={"nope": 1},
        )


def test_baton_ship_needs_a_discharging_criterion_and_notes_the_batons_it_wrote(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    sha = _build(root)
    seen = []
    import coordinator_core.ops.dispatch_emit.run_close_baton_ship as shipper

    monkeypatch.setattr(shipper, "baton_ship_leg", lambda *a: seen.append(a) or LegResult("ran", "shipped"))
    unmet = _ctx(root, batons=[BATON], terminal_sha=sha, record={"criterion": {"status": "not_met"}})
    assert cb._baton_ship(unmet).detail == "criterion not_met: batons not shipped" and seen == []
    met = _ctx(root, batons=[BATON], terminal_sha=sha, record={"criterion": {"status": "met"}})
    assert cb._baton_ship(met).status == "ran" and met.written == [BATON] and seen[0][3] == sha
    ledger = _ctx(root, batons=[BATON], terminal_sha=sha, record={"criterion": None, "inputs": {"ledger_met": "ok"}})
    assert cb._baton_ship(ledger).status == "ran"
    assert cb._baton_ship(_ctx(root, terminal_sha=sha)).status == "not-applicable"


def test_a_leg_that_raises_is_recorded_failed_and_close_commit_still_runs(tmp_path):
    root = tmp_path / "repo"
    sha = _build(root)
    _job(root, sha, ("coded-stamp", "close-commit"))

    def boom(_ctx):
        raise RuntimeError("boom")

    runners = {**cb.DEFAULT_RUNNERS, "coded-stamp": boom}
    outcomes = cb.run_background(_ctx(root), runners)
    assert outcomes["coded-stamp"] == LegResult("failed", "RuntimeError: boom")
    assert outcomes["close-commit"].status == "not-applicable"
