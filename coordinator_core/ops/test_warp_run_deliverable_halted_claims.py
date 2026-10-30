"""Tests for warp_run_deliverable.halted_run_write_claims and its scoped `_manifest_declared` memo."""

from __future__ import annotations

import json
import time
from calendar import timegm

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED

NOW = float(timegm((2026, 10, 19, 12, 0, 0, 0, 0, 0)))
HALTED_ID = "ask-20261019T100000-aaaaaa"
OTHER_ID = "ask-20261019T110000-bbbbbb"
OLD_ID = "ask-20261001T100000-cccccc"
JOB = "tools/runpod-jobs/job.py"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _record(repo, run_id, *, writes=(), withheld=(), status=STATUS_HALTED, manifest_text=None):
    rec = repo / "state/warp" / run_id
    rec.mkdir(parents=True)
    if manifest_text is None:
        manifest_text = json.dumps({"rows": [{"writes": list(writes)}], "doc_writes": []})
    (rec / "manifest.json").write_text(manifest_text, encoding="utf-8")
    if status is not None:
        marker = {"status": status}
        if withheld:
            marker["run_artifacts"] = {"withheld": {p: "peer-held" for p in withheld}}
            for p in withheld:  # a withheld path clean in the worktree releases (RELEASE_WITHHELD_CLEAN)
                (repo / p).parent.mkdir(parents=True, exist_ok=True)
                (repo / p).write_text("dirty\n", encoding="utf-8")
        (rec / halted_run.HALTED_FILE).write_text(json.dumps(marker), encoding="utf-8")
    return rec


def _claims(repo, paths, **kw):
    kw.setdefault("exclude_run", None)
    return wrd.halted_run_write_claims(repo, paths, now=NOW, **kw)


def test_withheld_write_maps_to_run_id(repo):
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    assert _claims(repo, [JOB, "src/other.py"]) == {JOB: [HALTED_ID]}


def test_declared_but_not_withheld_path_is_not_held(repo):
    _record(repo, HALTED_ID, writes=[JOB, "src/other.py"], withheld=["src/other.py"])
    assert _claims(repo, [JOB, "src/other.py"]) == {"src/other.py": [HALTED_ID]}


def test_halt_without_any_withheld_holds_nothing(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_backslash_path_is_normalized(repo):
    _record(repo, HALTED_ID, withheld=["docs/a.md"])
    assert _claims(repo, ["docs\\a.md"]) == {"docs/a.md": [HALTED_ID]}


def test_two_halted_runs_both_named(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    _record(repo, OTHER_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {JOB: sorted([HALTED_ID, OTHER_ID])}


def test_resumed_marker_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB], status="resumed")
    assert _claims(repo, [JOB]) == {}


def test_no_marker_yields_empty(repo):
    _record(repo, HALTED_ID, writes=[JOB], status=None)
    assert _claims(repo, [JOB]) == {}


def test_out_of_window_stamp_yields_empty(repo):
    _record(repo, OLD_ID, withheld=[JOB])
    assert _claims(repo, [JOB]) == {}


def test_exclude_run_yields_empty(repo):
    _record(repo, HALTED_ID, withheld=[JOB])
    assert _claims(repo, [JOB], exclude_run=HALTED_ID) == {}


def test_unreadable_manifest_does_not_hide_a_withheld_hold(repo):
    _record(repo, HALTED_ID, withheld=[JOB], manifest_text="{not json")
    assert _claims(repo, [JOB]) == {JOB: [HALTED_ID]}


def test_unstamped_name_skipped_without_read(repo, monkeypatch):
    _record(repo, "no-stamp-here", withheld=[JOB])

    def boom(*a, **k):
        raise AssertionError("read an unstamped record dir")

    monkeypatch.setattr(wrd, "_halted", boom)
    assert _claims(repo, [JOB]) == {}


SIZING = "state/sizings/s.yaml"
PLAN = "docs/plans/p.md"


def _bind(repo, run_id, *, sizing=SIZING, plan=None, marker=STATUS_HALTED):
    rec = repo / "state/warp" / run_id
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    body = {"status": marker, "sizing_rel": sizing}
    try:
        body.update(run_artifacts=json.loads((rec / halted_run.HALTED_FILE).read_text(encoding="utf-8"))["run_artifacts"])
    except (OSError, KeyError, ValueError):
        pass
    if plan:
        body["plan"] = plan
    (rec / halted_run.HALTED_FILE).write_text(json.dumps(body), encoding="utf-8")


def _sizing(repo, rel, status="open", deliverable=""):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"status: {status}\n" + (f"deliverable_id: {deliverable}\n" if deliverable else ""), encoding="utf-8")


def test_superseded_sizing_halted_run_claims_nothing(repo):
    _sizing(repo, SIZING, "superseded")
    _record(repo, HALTED_ID, writes=[JOB])
    _bind(repo, HALTED_ID)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}


def test_same_sizing_successor_releases_and_unrelated_halted_still_holds(repo):
    _sizing(repo, SIZING)
    _sizing(repo, "state/sizings/u.yaml")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID)
    _bind(repo, OTHER_ID)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}
    _bind(repo, OTHER_ID, sizing="state/sizings/u.yaml")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {JOB: [HALTED_ID]}


def test_same_deliverable_successor_releases(repo):
    _sizing(repo, SIZING, deliverable="dlv-x")
    _sizing(repo, "state/sizings/t.yaml", deliverable="dlv-x")
    _record(repo, HALTED_ID, writes=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID)
    _bind(repo, OTHER_ID, sizing="state/sizings/t.yaml")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}


def test_own_plan_landing_releases_and_other_plan_holds(repo):
    _sizing(repo, SIZING)
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID, plan=PLAN)
    assert _claims(repo, [JOB], exclude_run=None, own_plan=PLAN) == {}
    assert _claims(repo, [JOB], exclude_run=None, own_plan="docs/plans/q.md") == {JOB: [HALTED_ID]}
    assert _claims(repo, [JOB], exclude_run=None) == {JOB: [HALTED_ID]}


def test_no_candidates_or_no_state_dir(repo):
    assert _claims(repo, []) == {}
    assert _claims(repo, [JOB]) == {}


def _baselined(repo, run_id, baseline, *, halted_at=None, started_at=None):
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_STAGE_FILE

    rec = repo / "state/warp" / run_id
    manifest = json.loads((rec / "manifest.json").read_text(encoding="utf-8"))
    manifest["write_baseline"] = baseline
    (rec / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    stage = {}
    if halted_at:
        stage.update(stage="halted", last_activity_at=halted_at)
    if started_at:
        stage["started_at"] = started_at
    (rec / RUN_STAGE_FILE).write_text(json.dumps(stage), encoding="utf-8")


def _dirty_job(repo, text="landing run edit\n"):
    path = repo / JOB
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _blob(text):
    from coordinator_core.ops.dispatch_emit.kept_work import blob_sha

    return blob_sha(text.encode("utf-8"))


HALT_AT = "2026-10-19T10:30:00Z"
START_AT = "2026-10-19T11:00:00Z"


def _two_runs(repo, b_h, b_l):
    _record(repo, HALTED_ID, writes=[JOB])
    _baselined(repo, HALTED_ID, {JOB: b_h}, halted_at=HALT_AT)
    _record(repo, OTHER_ID, status=None)
    _baselined(repo, OTHER_ID, {JOB: b_l}, started_at=START_AT)


def test_halted_holder_with_no_hunks_in_dirty_path_releases(repo):
    base = _blob("base\n")
    _two_runs(repo, base, base)
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {}


def test_halted_holder_with_hunks_in_dirty_path_holds(repo):
    _two_runs(repo, _blob("base\n"), _blob("landing base\n"))
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_halted_holder_without_write_baseline_holds_dirty_path(repo):
    _record(repo, HALTED_ID, writes=[JOB])
    _dirty_job(repo)
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_withheld_path_holds_even_when_tree_equals_baseline(repo):
    base = _blob("base\n")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _baselined(repo, HALTED_ID, {JOB: base}, halted_at=HALT_AT)
    _dirty_job(repo, "base\n")
    assert _claims(repo, [JOB], exclude_run=OTHER_ID, dirty=lambda: {JOB}) == {JOB: [HALTED_ID]}


def test_default_now_uses_wall_clock(repo):
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_id = f"ask-{stamp}-abcdef"
    _record(repo, run_id, withheld=[JOB])
    assert wrd.halted_run_write_claims(repo, [JOB], exclude_run=None) == {JOB: [run_id]}


def test_b9002c_replay_superseded_sizing_halted_run_claims_nothing_for_a_landing_on_another_sizing(repo):
    _sizing(repo, SIZING, "superseded")
    _sizing(repo, "state/sizings/land.yaml")
    _record(repo, HALTED_ID, writes=[JOB], withheld=[JOB])
    _bind(repo, HALTED_ID)
    _record(repo, OTHER_ID, status=None)
    _bind(repo, OTHER_ID, sizing="state/sizings/land.yaml", marker=STATUS_HALTED)
    (repo / "state/warp" / OTHER_ID / halted_run.HALTED_FILE).unlink()
    assert _claims(repo, [JOB], exclude_run=OTHER_ID) == {}


def _live_run(repo, run_id, session, writes):
    from coordinator_core.ops.dispatch_emit import run_token

    rec = repo / "state/warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": writes}], "doc_writes": []}), encoding="utf-8")
    run_token.mint(repo, run_id, "s.mjs", session_id=session)


def test_scan_lists_each_declarer_oldest_first_and_session_reader_dedupes(repo, monkeypatch):
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    me, peer = "1" * 8 + "-1111-1111-1111-111111111111", "2" * 8 + "-2222-2222-2222-222222222222"
    _live_run(repo, HALTED_ID, peer, [JOB])
    _live_run(repo, OTHER_ID, peer, [JOB])
    _live_run(repo, "ask-20261019T120000-dddddd", peer, ["other.py"])
    got = wrd._live_claim_scan(repo, [JOB], me, None, None, None)[JOB]
    assert got == sorted(got)
    assert sorted(r for _, r, _ in got) == [HALTED_ID, OTHER_ID]
    senior, junior = got
    assert wrd.live_run_write_claims(repo, [JOB], exclude_session=me, minted_before=(junior[0], junior[1])) == {JOB: [peer]}
    assert wrd.live_run_write_claims(repo, [JOB], exclude_session=me, minted_before=(senior[0], senior[1])) == {}
    assert wrd.live_run_write_claims(repo, [JOB], exclude_session=me) == {JOB: [peer]}


def test_scan_halted_run_is_not_a_holder(repo, monkeypatch):
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)
    peer = "2" * 8 + "-2222-2222-2222-222222222222"
    _live_run(repo, HALTED_ID, peer, [JOB])
    (repo / "state/warp" / HALTED_ID / halted_run.HALTED_FILE).write_text(json.dumps({"status": STATUS_HALTED}), encoding="utf-8")
    assert wrd._live_claim_scan(repo, [JOB], "x", None, None, None) == {}


def _corrupt(repo, run_id, *, writes=(JOB,)):
    rec = _record(repo, run_id, writes=writes, status=None)
    (rec / halted_run.HALTED_FILE).write_text("{not json", encoding="utf-8")
    return rec


def test_corrupt_marker_is_a_holding_halt(repo):
    _corrupt(repo, HALTED_ID)
    assert wrd._halted(repo, HALTED_ID) is True


def test_absent_marker_is_not_halted_or_landed(repo):
    _record(repo, HALTED_ID, status=None)
    assert wrd._halted(repo, HALTED_ID) is False
    assert wrd._landed(repo, HALTED_ID) is False
    assert wrd.halted_withheld_paths(repo, HALTED_ID) == set()


def test_a_claim_reason_string_is_not_a_run_id_and_is_neither_halted_nor_landed(repo):
    reason = f"unreadable-halt-record:{HALTED_ID}"
    assert wrd._halted(repo, reason) is False
    assert wrd._landed(repo, reason) is False


def test_corrupt_marker_is_not_landed_and_not_released(repo):
    _corrupt(repo, HALTED_ID)
    assert wrd._landed(repo, HALTED_ID) is False
    assert wrd.resumed_run_released(repo, HALTED_ID, None) is False


def test_corrupt_marker_claims_every_wanted_path_with_run_reason(repo):
    _corrupt(repo, HALTED_ID)
    reason = f"unreadable-halt-record:{HALTED_ID}"
    assert _claims(repo, [JOB, "src/other.py"]) == {JOB: [reason], "src/other.py": [reason]}


def test_corrupt_marker_withheld_paths_raise_with_run_reason(repo):
    _corrupt(repo, HALTED_ID)
    with pytest.raises(halted_run.UnreadableHaltRecord) as exc:
        wrd.halted_withheld_paths(repo, HALTED_ID)
    assert exc.value.reason == f"unreadable-halt-record:{HALTED_ID}"


def _count_parses(monkeypatch):
    calls = []
    real = wrd._manifest_declared_uncached

    def spy(run_id, root):
        calls.append(run_id)
        return real(run_id, root)

    monkeypatch.setattr(wrd, "_manifest_declared_uncached", spy)
    return calls


def test_manifest_declared_parses_once_per_run_inside_a_scope(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    _record(repo, HALTED_ID, writes=[JOB])
    calls = _count_parses(monkeypatch)
    with halted_hold_release.release_scope():
        first = wrd._manifest_declared(HALTED_ID, repo)
        second = wrd._manifest_declared(HALTED_ID, repo)
    assert first == second == ((JOB,), (), False)
    assert calls == [HALTED_ID]


def test_manifest_declared_caches_nothing_outside_a_scope(repo, monkeypatch):
    _record(repo, HALTED_ID, writes=[JOB])
    calls = _count_parses(monkeypatch)
    wrd._manifest_declared(HALTED_ID, repo)
    wrd._manifest_declared(HALTED_ID, repo)
    assert calls == [HALTED_ID, HALTED_ID]


def test_unreadable_manifest_still_flags_unreadable_when_memoised(repo):
    from coordinator_core.ops.dispatch_emit import halted_hold_release

    _record(repo, HALTED_ID, manifest_text="{not json")
    with halted_hold_release.release_scope():
        assert wrd._manifest_declared(HALTED_ID, repo)[2] is True
        assert wrd._manifest_declared(HALTED_ID, repo)[2] is True
