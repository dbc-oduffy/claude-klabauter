"""Tests for run_close_gate: the directive-to-leg map, its eligibility rules and exclusions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_close as rc
from coordinator_core.ops.dispatch_emit.run_close import LegResult
from coordinator_core.workstream_complete import run_close_gate as gate

SID = "11111111-2222-3333-4444-555555555555"
RUN = "run-1"
PLAN = "docs/plans/p.md"
NOW = datetime.now(timezone.utc)


def _plan(root, *, stamp: bool, status: str, rel: str = PLAN):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = f"status: {status}\n" + ("review_stamp: {verdict: PASS}\n" if stamp else "")
    path.write_text(f"---\n{fm}---\n\n# p\n", encoding="utf-8")


def _record(root, run_id=RUN, *, plan_rel=PLAN, memo=None, batons=("b.md",), entry="archive/completed/202610/e.md",
            legs=("review", "plan-reconciliation", "completion-entry", "baton-ship", "baton-drain",
                  "memo-lifecycle", "post-close"), session=SID):
    settled = {leg: LegResult("ran", "") for leg in legs}
    rc.write_close_job(
        root, run_id, [], NOW + timedelta(minutes=5), session_id=session, plan_rel=plan_rel,
        source_memo=memo, claimed_batons=batons, completion_entry=entry, settled=settled,
    )


def _ds(*ids):
    return [{"id": i} for i in ids]


def _stamps(directives):
    return {d["id"]: d[rc.DISCHARGED_BY_KEY] for d in directives}


def _run(root, directives, plan_rel=PLAN):
    return gate.compute_run_close_gate(root, SID, directives, governing_plan_rel=plan_rel, since=None)


def test_review_leg_needs_a_review_stamp_on_the_runs_plan(tmp_path):
    _record(tmp_path)
    ids = ("d-run-review-brightline-gate", "d-close-coverage-advisory")
    _plan(tmp_path, stamp=True, status="implemented")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {RUN}
    _plan(tmp_path, stamp=False, status="implemented")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {None}


def _mise_record(root, *, review_status="ran"):
    settled = {"review": LegResult(review_status, "")}
    rc.write_close_job(
        root, RUN, [], NOW + timedelta(minutes=5), session_id=SID, plan_rel=PLAN,
        review_receipt="state/inventory/r.md", settled=settled,
    )


def test_mise_close_review_receipt_with_stamp_discharges_review_directives(tmp_path):
    ids = ("d-run-review-brightline-gate", "d-close-coverage-advisory")
    _plan(tmp_path, stamp=False, status="implemented")
    _plan(tmp_path, stamp=True, status="implemented", rel="state/inventory/r.md")
    _mise_record(tmp_path)
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {RUN}


def test_mise_close_with_failed_review_discharges_neither(tmp_path):
    ids = ("d-run-review-brightline-gate", "d-close-coverage-advisory")
    _plan(tmp_path, stamp=False, status="implemented")
    _plan(tmp_path, stamp=True, status="implemented", rel="state/inventory/r.md")
    _mise_record(tmp_path, review_status="failed")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {None}


def test_mise_close_receipt_without_stamp_discharges_neither(tmp_path):
    ids = ("d-run-review-brightline-gate", "d-close-coverage-advisory")
    _plan(tmp_path, stamp=True, status="implemented")
    _plan(tmp_path, stamp=False, status="implemented", rel="state/inventory/r.md")
    _mise_record(tmp_path)
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {None}


def test_plan_reconciliation_needs_governing_plan_to_be_the_runs_and_implemented(tmp_path):
    _record(tmp_path)
    ids = ("d-claim-plan-execution-lock", "d-stamp-plan-implemented", "d-harvest-deferrals-1",
           "d-fold-execution-observations")
    _plan(tmp_path, stamp=True, status="implemented")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {RUN}
    ds = _ds(*ids)
    _run(tmp_path, ds, plan_rel="docs/plans/other.md")
    assert set(_stamps(ds).values()) == {None}
    _plan(tmp_path, stamp=True, status="executing")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {None}


def test_completion_entry_needs_a_named_entry(tmp_path):
    _record(tmp_path)
    ds = _ds("d-complete-entry")
    _run(tmp_path, ds)
    assert _stamps(ds) == {"d-complete-entry": RUN}
    _record(tmp_path, entry=None)
    ds = _ds("d-complete-entry")
    _run(tmp_path, ds)
    assert _stamps(ds) == {"d-complete-entry": None}


def test_ship_directive_needs_its_basename_in_claimed_batons(tmp_path):
    _record(tmp_path, batons=("state/handoffs/b.md",))
    ds = _ds("d-ship-consumed-handoff:b.md", "d-ship-consumed-handoff:other.md")
    _run(tmp_path, ds)
    assert _stamps(ds) == {"d-ship-consumed-handoff:b.md": RUN, "d-ship-consumed-handoff:other.md": None}


def test_sweeps_are_held_back_by_an_undischarged_ship_directive(tmp_path):
    _record(tmp_path, batons=("b.md",))
    sweeps = ("d-sweep-terminal-handoffs", "d-sweep-terminal-sizings")
    ds = _ds("d-ship-consumed-handoff:b.md", *sweeps)
    _run(tmp_path, ds)
    assert {_stamps(ds)[s] for s in sweeps} == {RUN}
    ds = _ds("d-ship-consumed-handoff:b.md", "d-ship-consumed-handoff:other.md", *sweeps)
    payload = _run(tmp_path, ds)
    assert {_stamps(ds)[s] for s in sweeps} == {None}
    assert set(sweeps) <= set(payload["outstanding"])


def test_memo_directives_discharge_only_for_the_runs_source_memo(tmp_path):
    _record(tmp_path, memo="state/cross-repo/inbox/m.md")
    ids = ("d-claim-memo-stamp:m.md", "d-flip-memo-status:m.md",
           "d-claim-memo-stamp:x.md", "d-flip-memo-status:x.md")
    ds = _ds(*ids)
    _run(tmp_path, ds)
    assert _stamps(ds) == {ids[0]: RUN, ids[1]: RUN, ids[2]: None, ids[3]: None}
    _record(tmp_path, memo=None)
    ds = _ds(ids[0])
    _run(tmp_path, ds)
    assert _stamps(ds) == {ids[0]: None}


def test_post_close_directives_always_discharge(tmp_path):
    _record(tmp_path, plan_rel=None)
    ids = ("d-baton-chain-closure", "d-structural-index-refresh", "d-ceremony-reindex",
           "d-check-machine-local-regeneratability")
    ds = _ds(*ids)
    _run(tmp_path, ds, plan_rel=None)
    assert set(_stamps(ds).values()) == {RUN}


def test_em_authored_directives_never_discharge_and_are_not_outstanding(tmp_path):
    _record(tmp_path)
    ids = ("d-add-lesson-1", "d-queue-append-lesson-2", "d-append-orientation-pinboard", "d-unmapped")
    ds = _ds(*ids)
    payload = _run(tmp_path, ds)
    assert set(_stamps(ds).values()) == {None}
    assert payload["outstanding"] == []


def test_run_less_close_discharges_only_the_completion_entry(tmp_path):
    folder = tmp_path / "archive" / "completed" / "202610"
    folder.mkdir(parents=True)
    entry = folder / f"20261013-adhoc-{SID[-6:]}.md"
    entry.write_text(
        f"---\nauthored_by: {SID}\ntitle: Shipped a thing\nnature: shipped\n---\n\nProse.\n", encoding="utf-8"
    )
    rel = f"archive/completed/202610/{entry.name}"
    ds = _ds("d-complete-entry", "d-baton-chain-closure", "d-sweep-terminal-handoffs", "d-ceremony-reindex")
    payload = _run(tmp_path, ds, plan_rel=None)
    assert _stamps(ds) == {
        "d-complete-entry": rel,
        "d-baton-chain-closure": None,
        "d-sweep-terminal-handoffs": None,
        "d-ceremony-reindex": None,
    }
    assert payload["run_ids"] == [rel] and payload["applies"] is True
    assert payload["outstanding"] == ["d-baton-chain-closure", "d-sweep-terminal-handoffs", "d-ceremony-reindex"]


def test_no_close_means_not_applicable_and_a_foreign_record_is_ignored(tmp_path):
    _record(tmp_path, session="another-session")
    ds = _ds("d-complete-entry", "d-ceremony-reindex")
    payload = _run(tmp_path, ds)
    assert payload["applies"] is False and payload["verdict"] == "not-applicable"
    assert set(_stamps(ds).values()) == {None}


def test_malformed_record_makes_the_gate_indeterminate(tmp_path):
    jobs = tmp_path / ".coordinator-local" / "close-jobs"
    jobs.mkdir(parents=True)
    (jobs / "bad.json").write_text("{not json", encoding="utf-8")
    payload = _run(tmp_path, _ds("d-ceremony-reindex"))
    assert payload["verdict"] == "indeterminate" and "bad.json" in payload["reason"]


def test_no_absolute_path_reaches_any_value(tmp_path):
    _plan(tmp_path, stamp=True, status="implemented")
    _record(tmp_path, memo="state/cross-repo/inbox/m.md")
    ds = _ds("d-complete-entry", "d-ceremony-reindex", "d-stamp-plan-implemented", "d-claim-memo-stamp:m.md")
    payload = _run(tmp_path, ds)
    blob = json.dumps([payload, ds])
    assert str(tmp_path) not in blob and "\\\\" not in blob
    assert ":/" not in blob.replace("https://", "")


def test_undischarged_keeps_only_directives_no_close_paid(tmp_path):
    _record(tmp_path)
    ds = _ds("d-ceremony-reindex", "d-add-lesson-1")
    _run(tmp_path, ds)
    assert [d["id"] for d in gate.undischarged(ds)] == ["d-add-lesson-1"]


def test_since_floor_newer_than_the_record_excludes_it(tmp_path):
    _record(tmp_path)
    ds = _ds("d-ceremony-reindex")
    payload = gate.compute_run_close_gate(
        tmp_path, SID, ds, governing_plan_rel=PLAN, since=NOW.timestamp() + 3600
    )
    assert payload["applies"] is False and _stamps(ds) == {"d-ceremony-reindex": None}


@pytest.mark.parametrize("did", ["d-close-tail-args", "d-run-wsc-tail", "d-release-plan-claim", "d-tail"])
def test_dr358_kill_residue_stays_unmapped(did):
    assert gate.leg_of({"id": did}) is None
