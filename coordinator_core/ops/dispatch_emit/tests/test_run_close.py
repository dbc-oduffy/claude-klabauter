"""Tests for run_close: leg vocabulary, close-block builder, close-job record."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from coordinator_core.ops.dispatch_emit import run_close as rc
from coordinator_core.ops.dispatch_emit.run_close import LegResult

T0 = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_missing_leg_becomes_failed_and_outstanding():
    block = rc.build_close_block("r1", True, {"review": LegResult("ran")})
    assert block["legs"]["post-close"] == {"status": "failed", "detail": "leg did not run"}
    assert block["legs"]["review"]["status"] == "ran"
    assert set(block["legs"]) == set(rc.RUN_BOUND_LEGS) | set(rc.SESSION_BOUND_LEGS)
    assert block["legs"]["session-shape"]["status"] == "session-bound"


def test_outstanding_equals_failed_set():
    legs = {leg: LegResult("ran") for leg in rc.RUN_BOUND_LEGS}
    legs["lessons"] = LegResult("failed", "boom")
    legs["completion-entry"] = LegResult("failed", "no judge summary")
    block = rc.build_close_block("r1", True, legs, "entry.md")
    assert block["outstanding"] == ["lessons", "completion-entry"]
    assert block["completion_entry"] == "entry.md"
    assert block["run_id"] == "r1" and block["applies"] is True


def test_queued_is_not_outstanding():
    legs = {leg: LegResult("ran") for leg in rc.RUN_BOUND_LEGS}
    legs["baton-drain"] = LegResult("queued", "job record .coordinator-local/close-jobs/r1.json")
    assert rc.build_close_block("r1", True, legs)["outstanding"] == []


def test_not_applies_marks_every_run_bound_leg_not_applicable():
    block = rc.build_close_block("r1", False, {})
    assert block["outstanding"] == []
    assert {block["legs"][leg]["status"] for leg in rc.RUN_BOUND_LEGS} == {"not-applicable"}


@pytest.mark.parametrize(
    "detail",
    ["see C:\\w\\f.md", "see C:/w/f", "wrote /abs/f", "state\\a.md", "(/abs/path)"],  # abs-path-ok: refusal fixtures
)
def test_non_relative_path_in_detail_is_refused(detail):
    with pytest.raises(ValueError):
        LegResult("ran", detail)


def test_repo_relative_forward_slash_detail_is_accepted():
    LegResult("ran", "wrote state/lessons-outbox/a.md and/or b")


def test_unknown_status_refused():
    with pytest.raises(ValueError):
        LegResult("pending")


def test_kind_defaults_to_plan_run_and_is_present_in_every_block():
    assert rc.build_close_block("r1", True, {})["kind"] == "plan-run"
    assert rc.build_close_block("r1", False, {})["kind"] == "plan-run"
    for kind in rc.CLOSE_KINDS:
        assert rc.build_close_block("r1", True, {}, kind=kind)["kind"] == kind


def test_mise_run_presets_plan_reconciliation_and_memo_lifecycle():
    presets = rc.kind_presets("mise-run", plan_governs=False)
    assert set(presets) == {"plan-reconciliation", "memo-lifecycle"}
    assert {p.status for p in presets.values()} == {"not-applicable"}


def test_xs_s_run_preset_depends_on_a_governing_plan():
    ungoverned = rc.kind_presets("xs-s-run", plan_governs=False)
    assert set(ungoverned) == {"plan-reconciliation"}
    assert ungoverned["plan-reconciliation"].status == "not-applicable"
    assert rc.kind_presets("xs-s-run", plan_governs=True) == {}


@pytest.mark.parametrize("kind", ["plan-run", "run-less-xs-s"])
@pytest.mark.parametrize("governs", [True, False])
def test_kinds_owing_every_leg_have_no_presets(kind, governs):
    assert rc.kind_presets(kind, plan_governs=governs) == {}


def test_every_preset_reason_is_hygienic_and_capped():
    for kind in rc.CLOSE_KINDS:
        for governs in (True, False):
            for leg, result in rc.kind_presets(kind, plan_governs=governs).items():
                assert leg in rc.RUN_BOUND_LEGS
                assert 0 < len(result.detail) <= rc.DETAIL_CAP
                rc.check_path_hygiene(result.detail)


def test_unknown_kind_refused_by_presets_and_block():
    with pytest.raises(ValueError):
        rc.kind_presets("queue-grind", plan_governs=True)
    with pytest.raises(ValueError):
        rc.build_close_block("r1", True, {}, kind="queue-grind")


def test_close_job_path_is_forward_slash_relative():
    assert rc.close_job_path("r1") == ".coordinator-local/close-jobs/r1.json"


def test_read_close_job_missing_record_is_failed(tmp_path):
    got = rc.read_close_job(tmp_path, "r1", T0, legs=["baton-drain"])
    assert got == {"baton-drain": LegResult("failed", "job record missing")}


def test_read_close_job_late_job_is_failed(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain", "post-close"], T0 + timedelta(seconds=5))
    rc.record_leg_outcome(tmp_path, "r1", "post-close", "ran", "done")
    got = rc.read_close_job(tmp_path, "r1", T0 + timedelta(seconds=6))
    assert got["post-close"] == LegResult("ran", "done")
    assert got["baton-drain"].status == "failed"
    assert got["baton-drain"].detail.startswith("job not finished by ")


def test_read_close_job_before_deadline_stays_queued(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain"], T0 + timedelta(seconds=5))
    got = rc.read_close_job(tmp_path, "r1", T0)
    assert got["baton-drain"].status == "queued"
    assert ".coordinator-local/close-jobs/r1.json" in got["baton-drain"].detail


def test_record_is_json_with_relative_return(tmp_path):
    rel = rc.write_close_job(tmp_path, "r1", ["memo-lifecycle"], T0)
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["legs"] == {"memo-lifecycle": {"status": "queued", "detail": ""}}


def test_record_leg_outcome_rejects_unqueued_leg_and_missing_record(tmp_path):
    with pytest.raises(FileNotFoundError):
        rc.record_leg_outcome(tmp_path, "r1", "post-close", "ran")
    rc.write_close_job(tmp_path, "r1", ["post-close"], T0)
    with pytest.raises(ValueError):
        rc.record_leg_outcome(tmp_path, "r1", "baton-drain", "ran")


def test_corrupt_record_reads_as_missing(tmp_path):
    (tmp_path / rc.close_job_path("r1")).parent.mkdir(parents=True)
    (tmp_path / rc.close_job_path("r1")).write_text("{not json", encoding="utf-8")
    got = rc.read_close_job(tmp_path, "r1", T0, legs=["post-close"])
    assert got["post-close"].detail == "job record missing"


SID = "d2946a18-f3c8-4c68-afbf-4d67a1113e41"
FUTURE = T0 + timedelta(hours=1)


def _write(tmp_path, run_id, sid=SID, queued=("baton-drain",), **kw):
    rc.write_close_job(tmp_path, run_id, queued, FUTURE, session_id=sid, **kw)


def test_record_round_trips_every_new_field(tmp_path):
    rel = rc.write_close_job(
        tmp_path, "r1", ["post-close"], FUTURE,
        session_id=SID, plan_rel="docs/plans/p.md", source_memo="m.md",
        claimed_batons=["a.md", "b.md"], completion_entry="archive/completed/202610/e.md",
        settled={"baton-ship": LegResult("ran", "shipped a.md")},
    )
    data = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert data["session_id"] == SID and data["plan_rel"] == "docs/plans/p.md"
    assert data["source_memo"] == "m.md" and data["claimed_batons"] == ["a.md", "b.md"]
    assert data["completion_entry"] == "archive/completed/202610/e.md"
    assert data["settled"] == {"baton-ship": {"status": "ran", "detail": "shipped a.md"}}
    rc.record_leg_outcome(tmp_path, "r1", "post-close", "ran")
    again = json.loads((tmp_path / rel).read_text(encoding="utf-8"))
    assert {k: again[k] for k in data if k != "legs"} == {k: data[k] for k in data if k != "legs"}
    (close,), unreadable = rc.session_run_closes(tmp_path, SID, T0)
    assert unreadable == []
    assert close.plan_rel == "docs/plans/p.md" and close.claimed_batons == ("a.md", "b.md")
    assert close.legs["baton-ship"].status == "ran" and close.legs["post-close"].status == "ran"


def test_settled_detail_with_windows_path_is_refused(tmp_path):
    with pytest.raises(ValueError):
        LegResult("ran", "see C:\\w\\f.md")  # abs-path-ok: refusal fixture
    with pytest.raises(ValueError):
        rc.write_close_job(
            tmp_path, "r1", [], FUTURE, session_id=SID, plan_rel="C:\\w\\p.md"  # abs-path-ok: refusal fixture
        )


def test_ws1_shaped_record_is_never_attributed(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain"], FUTURE)
    assert rc.session_run_closes(tmp_path, SID, T0) == ([], [])
    assert rc.session_run_closes(tmp_path, "other", T0) == ([], [])


def test_other_sessions_records_are_filtered_out(tmp_path):
    _write(tmp_path, "mine")
    _write(tmp_path, "theirs", sid="someone-else")
    closes, _ = rc.session_run_closes(tmp_path, SID, T0)
    assert [c.run_id for c in closes] == ["mine"]


def test_since_skips_old_files_by_stat(tmp_path):
    _write(tmp_path, "old")
    path = tmp_path / rc.close_job_path("old")
    os.utime(path, (1000, 1000))
    assert rc.session_run_closes(tmp_path, SID, T0, since=2000.0)[0] == []
    assert len(rc.session_run_closes(tmp_path, SID, T0, since=500.0)[0]) == 1


def test_malformed_record_is_unreadable_and_verdict_indeterminate(tmp_path):
    _write(tmp_path, "good", settled={"completion-entry": LegResult("ran")}, queued=())
    bad = tmp_path / rc.close_job_path("bad")
    bad.write_text("{not json", encoding="utf-8")
    closes, unreadable = rc.session_run_closes(tmp_path, SID, T0)
    assert [c.run_id for c in closes] == ["good"]
    assert unreadable == [".coordinator-local/close-jobs/bad.json"]
    payload = rc.discharge([], lambda d: None, closes, unreadable=unreadable)
    assert payload["verdict"] == "indeterminate" and "bad.json" in payload["reason"]


def _close(run_id, **legs):
    return rc.RunClose(run_id=run_id, legs={k: LegResult(v) for k, v in legs.items()})


def _directives(*ids):
    return [{"id": i} for i in ids]


def _leg_of(d):
    return {"d-a": "baton-drain", "d-b": "post-close"}.get(d["id"])


def test_late_queued_leg_does_not_discharge(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["baton-drain"], T0, session_id=SID)
    (close,), _ = rc.session_run_closes(tmp_path, SID, T0 + timedelta(seconds=1))
    assert close.legs["baton-drain"].status == "failed"
    ds = _directives("d-a")
    payload = rc.discharge(ds, _leg_of, [close])
    assert ds[0][rc.DISCHARGED_BY_KEY] is None and payload["outstanding"] == ["d-a"]


@pytest.mark.parametrize("status", ["not-applicable", "queued", "failed", "session-bound"])
def test_non_paying_statuses_do_not_discharge(status):
    ds = _directives("d-a")
    rc.discharge(ds, _leg_of, [_close("r1", **{"baton-drain": status})])
    assert ds[0][rc.DISCHARGED_BY_KEY] is None


@pytest.mark.parametrize("status", sorted(rc.DISCHARGING_STATUSES))
def test_paying_statuses_discharge(status):
    ds = _directives("d-a")
    payload = rc.discharge(ds, _leg_of, [_close("r1", **{"baton-drain": status})])
    assert ds[0][rc.DISCHARGED_BY_KEY] == "r1" and payload["discharged"] == {"d-a": "r1"}


def test_latest_close_wins():
    ds = _directives("d-a")
    rc.discharge(ds, _leg_of, [_close("r1", **{"baton-drain": "ran"}), _close("r2", **{"baton-drain": "ran"})])
    assert ds[0][rc.DISCHARGED_BY_KEY] == "r2"


def test_eligible_filters_and_falls_back_to_earlier_close():
    ds = _directives("d-a")
    closes = [_close("r1", **{"baton-drain": "ran"}), _close("r2", **{"baton-drain": "ran"})]
    rc.discharge(ds, _leg_of, closes, eligible=lambda d, c: c.run_id == "r1")
    assert ds[0][rc.DISCHARGED_BY_KEY] == "r1"


def test_outstanding_is_exactly_mapped_undischarged_while_applies():
    ds = _directives("d-a", "d-b", "d-unmapped")
    payload = rc.discharge(ds, _leg_of, [_close("r1", **{"baton-drain": "ran", "post-close": "failed"})])
    assert payload["applies"] is True and payload["verdict"] == "applicable"
    assert payload["outstanding"] == ["d-b"] and payload["run_ids"] == ["r1"]
    assert [d[rc.DISCHARGED_BY_KEY] for d in ds] == ["r1", None, None]


def test_no_closes_means_not_applicable_and_nothing_outstanding():
    ds = _directives("d-a")
    payload = rc.discharge(ds, _leg_of, [])
    assert payload["applies"] is False and payload["outstanding"] == []
    assert payload["verdict"] == "not-applicable" and ds[0][rc.DISCHARGED_BY_KEY] is None


def _entry(tmp_path, sid, name="20261013-adhoc", month="202610", authored=None, run_id=None,
           filled=True):
    folder = tmp_path / "archive" / "completed" / month
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{sid[-6:]}.md"
    lines = ["---", f"authored_by: {authored or sid}"]
    if run_id:
        lines.append(f"run_id: {run_id}")
    if filled:
        lines += ["title: Shipped a thing", "nature: shipped"]
    else:
        lines += ["title: 'PLACEHOLDER \u2014 replace with past-tense workstream title'"]
    lines += ["---", "", "Prose written by the EM." if filled else "scaffold"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_run_less_close_keyed_on_filled_entry_path(tmp_path):
    _entry(tmp_path, SID)
    close = rc.run_less_close(tmp_path, SID)
    assert close is not None
    rel = f"archive/completed/202610/20261013-adhoc-{SID[-6:]}.md"
    assert close.run_id == rel and close.completion_entry == rel
    assert set(close.legs) == {"completion-entry"}
    assert "\\" not in close.run_id and ":" not in close.run_id


def test_run_less_close_refuses_scaffold_run_entry_and_foreign_author(tmp_path):
    assert rc.run_less_close(tmp_path, SID) is None
    path = _entry(tmp_path, SID, filled=False)
    assert rc.run_less_close(tmp_path, SID) is None
    path.unlink()
    path = _entry(tmp_path, SID, run_id="r1")
    assert rc.run_less_close(tmp_path, SID) is None
    path.unlink()
    _entry(tmp_path, SID, authored="another-session")
    assert rc.run_less_close(tmp_path, SID) is None


def test_run_less_close_pays_only_completion_entry():
    close = rc.RunClose(run_id="e.md", legs={"completion-entry": LegResult("ran")})
    ds = _directives("d-a", "d-ce")
    rc.discharge(ds, lambda d: {"d-a": "baton-drain", "d-ce": "completion-entry"}[d["id"]], [close])
    assert [d[rc.DISCHARGED_BY_KEY] for d in ds] == [None, "e.md"]


BASE = "a" * 40
TERM = "b" * 40


def test_review_receipt_and_shas_round_trip(tmp_path):
    _write(tmp_path, "r1", review_receipt="state/mise-inventory/r.md",
           run_base_sha=BASE, terminal_sha=TERM)
    (close,), _ = rc.session_run_closes(tmp_path, SID, T0)
    assert close.review_receipt == "state/mise-inventory/r.md"
    assert (close.run_base_sha, close.terminal_sha) == (BASE, TERM)


def test_ws2_shaped_record_parses_new_fields_as_none(tmp_path):
    _write(tmp_path, "r1")
    path = tmp_path / rc.close_job_path("r1")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("review_receipt", "run_base_sha", "terminal_sha"):
        del data[key]
    path.write_text(json.dumps(data), encoding="utf-8")
    (close,), unreadable = rc.session_run_closes(tmp_path, SID, T0)
    assert unreadable == []
    assert (close.review_receipt, close.run_base_sha, close.terminal_sha) == (None, None, None)


@pytest.mark.parametrize("kw", [{"run_base_sha": "abc123"}, {"terminal_sha": "G" * 40}])
def test_short_or_non_hex_sha_is_refused(tmp_path, kw):
    with pytest.raises(ValueError):
        _write(tmp_path, "r1", **kw)
    assert not (tmp_path / rc.close_job_path("r1")).exists()


def test_backslashed_review_receipt_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _write(tmp_path, "r1", review_receipt="state\\mise-inventory\\r.md")


def _shas(n):
    return [f"{i:02x}" * 20 for i in range(n)]


@pytest.mark.parametrize("n", [0, 1, 8, 50])
def test_render_unreceipted_is_capped_and_names_newest(n):
    shas = _shas(n)
    text = rc.render_unreceipted(shas, truncated=False, cap=500)
    assert len(text) <= 160
    assert text.startswith(f"{n} unreceipted ancestor commits")
    if n:
        assert shas[0][:12] in text
    if n == 50:
        assert "(+" in text and " more)" in text
    if n == 8:
        assert shas[7][:12] in text and "more" not in text
    LegResult("failed", text)


def test_render_unreceipted_truncated_form():
    shas = _shas(50)
    text = rc.render_unreceipted(shas, truncated=True, cap=500)
    assert len(text) <= 160
    assert "range truncated at 500 commits" in text
    assert shas[-1][:12] in text and shas[0][:12] in text
    assert "truncated" in rc.render_unreceipted([], truncated=True, cap=200)


def _rec(tmp_path, run_id="r1"):
    return json.loads((tmp_path / rc.close_job_path(run_id)).read_text(encoding="utf-8"))


def test_criterion_and_falsifier_round_trip(tmp_path):
    crit = {"verdict": "met", "evidence": "see /abs/path ok"}
    rc.write_close_job(tmp_path, "r1", [], T0, criterion=crit, falsifier_broken=[])
    rec = _rec(tmp_path)
    assert rec["criterion"] == crit and rec["falsifier_broken"] == []
    rc.write_close_job(tmp_path, "r2", [], T0, falsifier_broken=["a", "b"])
    assert _rec(tmp_path, "r2")["falsifier_broken"] == ["a", "b"]


def test_absent_arguments_write_null(tmp_path):
    rc.write_close_job(tmp_path, "r1", [], T0)
    rec = _rec(tmp_path)
    assert rec["criterion"] is None and rec["falsifier_broken"] is None


@pytest.mark.parametrize("kw", [{"criterion": "met"}, {"falsifier_broken": "x"}, {"falsifier_broken": [1]}])
def test_bad_criterion_or_falsifier_refused(tmp_path, kw):
    with pytest.raises(ValueError):
        rc.write_close_job(tmp_path, "r1", [], T0, **kw)
    with pytest.raises(ValueError):
        rc.amend_close_job(tmp_path, "r1", **kw)


def test_amend_changes_one_field_only(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["post-close"], T0, plan_rel="p.md", session_id="s")
    before = _rec(tmp_path)
    rc.amend_close_job(tmp_path, "r1", criterion={"verdict": "met"})
    after = _rec(tmp_path)
    assert after.pop("criterion") == {"verdict": "met"}
    before.pop("criterion")
    assert after == before


def test_amend_falsifier_none_vs_unchanged(tmp_path):
    rc.write_close_job(tmp_path, "r1", [], T0)
    path = tmp_path / rc.close_job_path("r1")
    rec = _rec(tmp_path)
    del rec["falsifier_broken"]
    path.write_text(json.dumps(rec), encoding="utf-8")
    rc.amend_close_job(tmp_path, "r1", criterion={"v": 1})
    assert "falsifier_broken" not in _rec(tmp_path)
    rc.amend_close_job(tmp_path, "r1", falsifier_broken=None)
    assert _rec(tmp_path)["falsifier_broken"] is None
    rc.amend_close_job(tmp_path, "r1", falsifier_broken=["t"])
    assert _rec(tmp_path)["falsifier_broken"] == ["t"]


def test_amend_absent_record_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="r9"):
        rc.amend_close_job(tmp_path, "r9", criterion={})


def test_amend_settled_and_receipt(tmp_path):
    rc.write_close_job(tmp_path, "r1", ["post-close"], T0)
    rc.amend_close_job(
        tmp_path, "r1", review_receipt="a/b.md", settled={"review": LegResult("ran", "ok")}
    )
    rec = _rec(tmp_path)
    assert rec["review_receipt"] == "a/b.md" and rec["settled"]["review"]["status"] == "ran"
    with pytest.raises(ValueError):
        rc.amend_close_job(tmp_path, "r1", settled={"post-close": LegResult("ran")})
    with pytest.raises(ValueError):
        rc.amend_close_job(tmp_path, "r1", review_receipt="/abs/x")
    with pytest.raises(ValueError):
        rc.amend_close_job(tmp_path, "r1", settled={"nope": LegResult("ran")})


def test_amend_queue_requires_deadline(tmp_path):
    rc.write_close_job(tmp_path, "r1", [], T0)
    with pytest.raises(ValueError):
        rc.amend_close_job(tmp_path, "r1", queue=["lessons"])
    with pytest.raises(ValueError):
        rc.amend_close_job(
            tmp_path, "r1", queue=["lessons"], deadline=T0, settled={"lessons": LegResult("ran")}
        )


def test_amend_queue_fails_stale_queued_and_queues_named(tmp_path):
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    rc.write_close_job(tmp_path, "r1", ["baton-drain"], past)
    new = datetime(2999, 1, 1, tzinfo=timezone.utc)
    rc.amend_close_job(tmp_path, "r1", queue=["lessons"], deadline=new)
    legs = _rec(tmp_path)["legs"]
    assert legs["baton-drain"]["status"] == "failed"
    assert "job not finished by" in legs["baton-drain"]["detail"]
    assert legs["lessons"] == {"status": "queued", "detail": ""}
    assert _rec(tmp_path)["deadline"] == new.isoformat()
