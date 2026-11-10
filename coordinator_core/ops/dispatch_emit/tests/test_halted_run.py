"""halted_run: marker round-trip, newest-dir rule, and the refusals that yield None."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import call_io_scope
from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

SIZING = "state/sizings/2026-10-16-foo.yaml"
STEM = "2026-10-16-foo"


def _dir(root, stamp):
    return root / RECORD_DIR_ROOT / f"warp-{STEM}-{stamp}"


def test_round_trip(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    p = hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert p == d / hr.HALTED_FILE
    assert json.loads(p.read_text()) == {
        "status": "halted", "halted": "criterion_not_met", "sizing_rel": SIZING,
    }
    assert hr.halted_run_for(tmp_path, SIZING) == d.name
    assert hr.mark_resumed(d) is True
    assert json.loads(p.read_text())["status"] == hr.STATUS_RESUMED
    assert json.loads(p.read_text())["halted"] == "criterion_not_met"
    assert hr.halted_run_for(tmp_path, SIZING) is None
    assert hr.mark_resumed(d) is False


def _indexed_halted_run(root, stamp):
    """A halted run whose marker predates its index entry, so `fresh_entry` answers from the entry."""
    d = _dir(root, stamp)
    marker = hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    old = time.time() - 10
    os.utime(marker, (old, old))
    idx.rebuild(root)
    idx.add_run(root, d.name, ["a.py"], at_ns=time.time_ns() - 1_000_000_000)
    return d


def test_fresh_entry_memo_never_serves_the_state_before_record_released(tmp_path):
    d = _indexed_halted_run(tmp_path, "20261016T010000-aaaaaa")
    with call_io_scope.call_io_scope():
        assert idx.fresh_entry(tmp_path, d.name) is not None
        assert idx.fresh_entry(tmp_path, d.name) is not None
        assert hr.record_released(d, "b" * 40, "landed") is True
        assert idx.fresh_entry(tmp_path, d.name) is None


def test_fresh_entry_memo_never_serves_the_state_before_mark_landed(tmp_path):
    d = _indexed_halted_run(tmp_path, "20261016T020000-bbbbbb")
    with call_io_scope.call_io_scope():
        assert idx.fresh_entry(tmp_path, d.name) is not None
        assert hr.mark_landed(d, "c" * 40) is True
        assert idx.fresh_entry(tmp_path, d.name) is None


def test_mark_superseded_writes_dispositions_only_when_given(tmp_path):
    a = _dir(tmp_path, "20261016T010000-aaaaaa")
    b = _dir(tmp_path, "20261016T020000-bbbbbb")
    for d in (a, b):
        hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    disp = {"a.py": hr.DISPOSITION_LANDED, "b.py": hr.DISPOSITION_SUPERSEDED, "scratch/c.log": hr.DISPOSITION_SCRATCH}
    assert hr.mark_superseded(a, hr._read(a), {"e": 1}, dispositions=disp) is True
    data = json.loads((a / hr.HALTED_FILE).read_text())
    assert data["status"] == hr.STATUS_SUPERSEDED and data[hr.PATH_DISPOSITIONS_FIELD] == disp
    assert hr.mark_superseded(b, hr._read(b), {"e": 1}) is True
    data = json.loads((b / hr.HALTED_FILE).read_text())
    assert data["status"] == hr.STATUS_SUPERSEDED and hr.PATH_DISPOSITIONS_FIELD not in data
    assert hr.mark_superseded(a, hr._read(a), {"e": 1}, dispositions=disp) is False


def test_mark_landed_rewrites_only_a_halted_marker(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    sha = "a" * 40
    assert hr.mark_landed(d, sha) is False
    assert not (d / hr.HALTED_FILE).exists()
    p = hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert hr.mark_landed(d, sha) is True
    data = json.loads(p.read_text())
    assert data["status"] == hr.STATUS_LANDED and data["sha"] == sha and data["halted"] == "criterion_not_met"
    assert hr.halted_run_for(tmp_path, SIZING) is None
    assert hr.mark_landed(d, "b" * 40) is False
    assert json.loads(p.read_text())["sha"] == sha
    resumed = _dir(tmp_path, "20261016T020000-bbbbbb")
    hr.write_halt_marker(resumed, halted="x", sizing_rel=SIZING)
    hr.mark_resumed(resumed)
    before = (resumed / hr.HALTED_FILE).read_bytes()
    assert hr.mark_landed(resumed, sha) is True
    assert json.loads((resumed / hr.HALTED_FILE).read_text())["status"] == hr.STATUS_LANDED
    assert (resumed / hr.HALTED_FILE).read_bytes() != before
    running = _dir(tmp_path, "20261016T030000-cccccc")
    running.mkdir(parents=True)
    (running / hr.HALTED_FILE).write_text(json.dumps({"status": "running"}), encoding="utf-8")
    before = (running / hr.HALTED_FILE).read_bytes()
    assert hr.mark_landed(running, sha) is False
    assert (running / hr.HALTED_FILE).read_bytes() == before


def test_record_released_stamps_only_a_halted_marker(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    sha = "a" * 40
    assert hr.record_released(d, sha, "criterion-met-at-head") is False
    assert not (d / hr.HALTED_FILE).exists()
    p = hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    assert hr.record_released(d, sha, "criterion-met-at-head") is True
    data = json.loads(p.read_text())
    assert data[hr.HALT_RELEASED_FIELD] == {"reason": "criterion-met-at-head", "sha": sha}
    assert data["status"] == "halted"
    assert hr.halted_run_for(tmp_path, SIZING) == d.name
    for other in (hr.mark_resumed, lambda rd: hr.mark_landed(rd, sha)):
        other(d)
        before = p.read_bytes()
        assert hr.record_released(d, "b" * 40, "r") is False
        assert p.read_bytes() == before


def test_newest_dir_rule(tmp_path):
    old = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(old, halted="x", sizing_rel=SIZING)
    newer = _dir(tmp_path, "20261016T020000-bbbbbb")
    newer.mkdir(parents=True)
    (newer / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    assert hr.halted_run_for(tmp_path, SIZING) is None


def test_a_sibling_sizing_with_an_extended_stem_does_not_shadow_the_newest_dir(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    hr.write_halt_marker(
        tmp_path / RECORD_DIR_ROOT / f"warp-{STEM}-slice-1-20261016T020000-bbbbbb",
        halted="x", sizing_rel="state/sizings/2026-10-16-foo-slice-1.yaml",
    )
    assert hr.halted_run_for(tmp_path, SIZING) == d.name


def test_a_marker_bound_to_the_same_stem_in_another_dir_resolves_and_is_rebound(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel="state/sizings/2026-11/2026-10-16-foo.yaml")
    assert hr.halted_run_to_resume(tmp_path, SIZING) == d.name
    assert json.loads((d / hr.HALTED_FILE).read_text())["sizing_rel"] == SIZING
    assert hr.halted_run_to_resume(tmp_path, SIZING, build=False) == d.name


def test_sizing_rel_mismatch(tmp_path):
    hr.write_halt_marker(_dir(tmp_path, "20261016T010000-aaaaaa"), halted="x",
                         sizing_rel="state/sizings/other/2026-10-16-bar.yaml")
    assert hr.halted_run_for(tmp_path, SIZING) is None


def test_malformed_marker(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    d.mkdir(parents=True)
    (d / hr.HALTED_FILE).write_text("{not json", encoding="utf-8")
    assert hr.halted_run_for(tmp_path, SIZING) is None
    assert hr.mark_resumed(d) is False
    (d / hr.HALTED_FILE).write_text("[1]", encoding="utf-8")
    assert hr.halted_run_for(tmp_path, SIZING) is None


def test_missing_state_warp(tmp_path):
    assert hr.halted_run_for(tmp_path, SIZING) is None


def _receipt(root, run_id, session_id):
    script = root / "scratch" / "warp" / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("// s\n", encoding="utf-8")
    (script.parent / (script.name + ".emitted.json")).write_text(
        json.dumps({"session_id": session_id}), encoding="utf-8"
    )


def _live(monkeypatch, live_ids):
    from coordinator_core.session import liveness

    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd=None: sid in live_ids)


def test_rebind_blocker_own_session_is_none(tmp_path, monkeypatch):
    _live(monkeypatch, set())
    _receipt(tmp_path, "r1", "me")
    assert hr.rebind_blocker(tmp_path, "r1", "me") is None


def test_rebind_blocker_live_peer_is_none(tmp_path, monkeypatch):
    _live(monkeypatch, {"peer"})
    _receipt(tmp_path, "r1", "peer")
    assert hr.rebind_blocker(tmp_path, "r1", "me") is None


def test_rebind_blocker_dead_peer_is_emitter_gone(tmp_path, monkeypatch):
    _live(monkeypatch, set())
    _receipt(tmp_path, "r1", "peer")
    assert hr.rebind_blocker(tmp_path, "r1", "me") == "emitter-gone"


def test_rebind_blocker_missing_receipt_or_script_is_none(tmp_path, monkeypatch):
    _live(monkeypatch, set())
    assert hr.rebind_blocker(tmp_path, "r1", "me") is None
    script = tmp_path / "scratch" / "warp" / "r1.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text("// s\n", encoding="utf-8")
    assert hr.rebind_blocker(tmp_path, "r1", "me") is None


def test_rebind_blocker_spawns_nothing(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    _live(monkeypatch, set())
    _receipt(tmp_path, "r1", "peer")
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert hr.rebind_blocker(tmp_path, "r1", "me") == "emitter-gone"


def test_no_subprocess(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=None)
    assert hr.halted_run_for(tmp_path, SIZING) is None
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    assert hr.halted_run_for(tmp_path, SIZING) == d.name
    assert hr.mark_resumed(d)


BASE = "a" * 40


def _ask(root, stamp):
    return root / RECORD_DIR_ROOT / f"ask-{stamp}"


def test_marker_without_base_is_byte_identical_and_with_base_carries_it(tmp_path):
    a = hr.write_halt_marker(_dir(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)
    b = hr.write_halt_marker(_dir(tmp_path, "20261016T020000-bbbbbb"), halted="x", sizing_rel=SIZING,
                             run_base_sha=None)
    assert a.read_bytes() == b.read_bytes()
    assert hr.HALT_RUN_BASE_FIELD not in json.loads(a.read_text())
    c = hr.write_halt_marker(_dir(tmp_path, "20261016T030000-cccccc"), halted="x", sizing_rel=SIZING,
                             run_base_sha=BASE)
    assert json.loads(c.read_text())[hr.HALT_RUN_BASE_FIELD] == BASE
    d = hr.write_halt_marker(_dir(tmp_path, "20261016T040000-dddddd"), halted="x", sizing_rel=SIZING,
                             run_base_sha="nothex")
    assert d.read_bytes() == a.read_bytes()


def test_halted_run_base_reader(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    assert hr.halted_run_base(d) is None
    d.mkdir(parents=True)
    (d / hr.HALTED_FILE).write_text("{bad", encoding="utf-8")
    assert hr.halted_run_base(d) is None
    (d / hr.HALTED_FILE).write_text(json.dumps({"run_base_sha": "xyz"}), encoding="utf-8")
    assert hr.halted_run_base(d) is None
    (d / hr.HALTED_FILE).write_text(json.dumps({"run_base_sha": 5}), encoding="utf-8")
    assert hr.halted_run_base(d) is None
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING, run_base_sha=BASE)
    assert hr.halted_run_base(d) == BASE


def test_ask_lookup_matches_newest_halted_ask(tmp_path):
    hr.write_halt_marker(_ask(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)
    hr.write_halt_marker(_ask(tmp_path, "20261016T020000-bbbbbb"), halted="x", sizing_rel=SIZING)
    assert hr.halted_ask_run_for(tmp_path, SIZING) == "ask-20261016T020000-bbbbbb"


def test_ask_lookup_skips_mismatch_and_resumed(tmp_path):
    hr.write_halt_marker(_ask(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)
    hr.write_halt_marker(_ask(tmp_path, "20261016T020000-bbbbbb"), halted="x",
                         sizing_rel="state/sizings/2026-10-16-other.yaml")
    done = _ask(tmp_path, "20261016T030000-cccccc")
    hr.write_halt_marker(done, halted="x", sizing_rel=SIZING)
    hr.mark_resumed(done)
    assert hr.halted_ask_run_for(tmp_path, SIZING) == "ask-20261016T010000-aaaaaa"


def test_ask_dir_older_than_sizing_date_is_not_read(tmp_path):
    hr.write_halt_marker(_ask(tmp_path, "20261015T230000-aaaaaa"), halted="x", sizing_rel=SIZING)
    assert hr.halted_ask_run_for(tmp_path, SIZING) is None


def test_a_newer_warp_run_supersedes_the_ask_run(tmp_path):
    hr.write_halt_marker(_ask(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)
    _dir(tmp_path, "20261016T020000-bbbbbb").mkdir(parents=True)
    assert hr.halted_ask_run_for(tmp_path, SIZING) is None


def test_an_older_warp_run_does_not_supersede_the_ask_run(tmp_path):
    _dir(tmp_path, "20261016T010000-aaaaaa").mkdir(parents=True)
    hr.write_halt_marker(_ask(tmp_path, "20261016T020000-bbbbbb"), halted="x", sizing_rel=SIZING)
    assert hr.halted_ask_run_for(tmp_path, SIZING) == "ask-20261016T020000-bbbbbb"


def test_undated_sizing_name_returns_none(tmp_path):
    rel = "state/sizings/foo.yaml"
    hr.write_halt_marker(_ask(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=rel)
    assert hr.halted_ask_run_for(tmp_path, rel) is None


def test_halt_marker_carries_the_reason_only_when_given(tmp_path):
    import json

    from coordinator_core.ops.dispatch_emit.halted_run import HALTED_FILE, write_halt_marker

    with_reason = write_halt_marker(tmp_path / "a", halted="dependents_unstarted", sizing_rel=None, reason="A: half")
    assert json.loads(with_reason.read_text(encoding="utf-8"))["reason"] == "A: half"
    without = write_halt_marker(tmp_path / "b", halted="dependents_unstarted", sizing_rel=None)
    assert "reason" not in json.loads((tmp_path / "b" / HALTED_FILE).read_text(encoding="utf-8"))


def test_halt_marker_carries_held_rows_only_when_given(tmp_path):
    import json

    from coordinator_core.ops.dispatch_emit.ask_contract import HALT_HELD_ROWS_FIELD
    from coordinator_core.ops.dispatch_emit.halted_run import write_halt_marker

    rows = [{"id": "X1", "reason": "performer em"}]
    given = write_halt_marker(tmp_path / "a", halted="ledger_check", sizing_rel=None, held_rows=rows)
    assert json.loads(given.read_text(encoding="utf-8"))[HALT_HELD_ROWS_FIELD] == rows
    bare = write_halt_marker(tmp_path / "b", halted="ledger_check", sizing_rel=None)
    assert HALT_HELD_ROWS_FIELD not in json.loads(bare.read_text(encoding="utf-8"))


def _indexed(root):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    return idx.withheld_by_path(root)


def test_index_follows_record_withheld_then_mark_resumed(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert hr.record_withheld(d, {"a.py": "r", "DELIVERY.md": "r"}) is True
    assert _indexed(tmp_path) == {"a.py": d.name, "DELIVERY.md": d.name}
    assert hr.mark_resumed(d) is True
    assert _indexed(tmp_path) == {}


def test_index_follows_mark_landed(tmp_path):
    d = _dir(tmp_path, "20261016T010000-bbbbbb")
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    hr.record_withheld(d, {"a.py": "r"})
    assert _indexed(tmp_path) == {"a.py": d.name}
    assert hr.mark_landed(d, "a" * 40) is True
    assert _indexed(tmp_path) == {}


def _live_row_present(root, run_id):
    from coordinator_core.ops.dispatch_emit import live_run_index as lri

    path = lri.index_path(root)
    return path is not None and run_id in lri._load(root, path)


@pytest.fixture
def _live_root(tmp_path, monkeypatch):
    from coordinator_core import _settings_home

    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.mark.parametrize("how", ["resumed", "superseded", "landed", "closed"])
def test_marker_leaving_halted_drops_the_live_run_row(_live_root, how):
    tmp_path = _live_root
    from coordinator_core.ops.dispatch_emit import live_run_index as lri

    d = _dir(tmp_path, "20261016T010000-eeeeee")
    peer = _dir(tmp_path, "20261016T020000-ffffff")
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert lri.stage(tmp_path, d.name, ["a.py"], 1.0) and lri.stage(tmp_path, peer.name, ["b.py"], 1.0)
    assert _live_row_present(tmp_path, d.name)
    if how == "resumed":
        assert hr.mark_resumed(d)
    elif how == "superseded":
        assert hr.mark_superseded(d, hr._read(d), {"e": 1})
    elif how == "landed":
        assert hr.mark_landed(d, "a" * 40)
    else:
        assert hr.mark_closed(d, "gone", "2026-11-03T00:00:00Z")
    assert not _live_row_present(tmp_path, d.name)
    assert _live_row_present(tmp_path, peer.name)


def test_a_refused_transition_keeps_the_live_run_row(_live_root):
    tmp_path = _live_root
    from coordinator_core.ops.dispatch_emit import live_run_index as lri

    d = _dir(tmp_path, "20261016T010000-aabbcc")
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    lri.stage(tmp_path, d.name, ["a.py"], 1.0)
    assert hr.mark_superseded(d, {"status": "halted", "other": 1}, {"e": 1}) is False
    assert hr.mark_closed(d, "", "t") is False
    assert _live_row_present(tmp_path, d.name)


def test_nothing_withheld_or_not_halted_leaves_index_untouched(tmp_path):
    d = _dir(tmp_path, "20261016T010000-cccccc")
    assert hr.record_withheld(d, {"a.py": "r"}) is False
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    assert hr.record_withheld(d, {}) is False
    assert _indexed(tmp_path) == {}


def test_worktree_rooted_record_dir_indexes_in_that_checkout(tmp_path):
    main, wt = tmp_path / "main", tmp_path / "wt"
    d = _dir(wt, "20261016T010000-dddddd")
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    hr.record_withheld(d, {"a.py": "r"})
    assert _indexed(wt) == {"a.py": d.name}
    assert _indexed(main) == {}


def test_read_strict_absent_is_none_and_corrupt_raises(tmp_path):
    import pytest

    assert hr.read_strict(tmp_path) is None
    (tmp_path / hr.HALTED_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        hr.read_strict(tmp_path)
    assert hr._read(tmp_path) is None
    (tmp_path / hr.HALTED_FILE).write_text('{"status": "halted"}', encoding="utf-8")
    assert hr.read_strict(tmp_path) == {"status": "halted"}


def test_halted_withheld_strict_absent_is_empty(tmp_path):
    assert hr.halted_withheld_strict(tmp_path / "ask-x") == {}
    assert hr.read_strict_run(tmp_path) is None


def test_halted_withheld_strict_reads_the_withheld_map(tmp_path):
    hr.write_halt_marker(tmp_path, halted="x", sizing_rel="s.yaml")
    assert hr.record_withheld(tmp_path, {"a.py": "dirty"})
    assert hr.halted_withheld_strict(tmp_path) == {"a.py": "dirty"}


def test_corrupt_marker_raises_with_the_run_dir_reason(tmp_path):
    import pytest

    run = tmp_path / "ask-20260101T000000-abcdef"
    run.mkdir()
    (run / hr.HALTED_FILE).write_text("{not json", encoding="utf-8")
    with pytest.raises(hr.UnreadableHaltRecord) as exc:
        hr.halted_withheld_strict(run)
    assert exc.value.run_id == run.name
    assert exc.value.reason == f"unreadable-halt-record:{run.name}"
    assert isinstance(exc.value, ValueError)
    assert hr.halted_withheld(run) == {}


def test_non_object_marker_raises(tmp_path):
    import pytest

    (tmp_path / hr.HALTED_FILE).write_text("[1]", encoding="utf-8")
    with pytest.raises(hr.UnreadableHaltRecord) as exc:
        hr.read_strict_run(tmp_path)
    assert exc.value.reason == f"unreadable-halt-record:{tmp_path.name}"


def test_a_halt_schedules_the_resume_build_with_root_and_run_id(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hr, "_RESUME_SPAWNER", lambda root, run_id: calls.append((root, run_id)))
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    assert calls == [(tmp_path, d.name)]


def test_a_failing_spawner_never_fails_the_halt(tmp_path, monkeypatch):
    def boom(root, run_id):
        raise OSError("no spawn")

    monkeypatch.setattr(hr, "_RESUME_SPAWNER", boom)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    assert hr.write_halt_marker(d, halted="x", sizing_rel=SIZING) == d / hr.HALTED_FILE


def test_no_real_process_is_spawned_under_pytest(tmp_path, monkeypatch):
    import coordinator_core.ops.ceremony.detached_spawn as ds

    monkeypatch.setattr(hr, "_RESUME_SPAWNER", None)
    monkeypatch.setattr(ds, "spawn_detached", lambda *a, **k: pytest.fail("spawned"))
    hr.write_halt_marker(_dir(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)


def _no_scan(monkeypatch, root):
    real = hr.os.scandir

    def refuse(path=".", *a, **k):
        if str(path).startswith(str(root)):
            raise AssertionError("scanned a run dir listing")
        return real(path, *a, **k)

    monkeypatch.setattr(hr.os, "scandir", refuse)


def test_a_sizing_with_no_indexed_run_answers_none_without_scanning_run_dirs(tmp_path, monkeypatch):
    hr.write_halt_marker(_ask(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel="state/sizings/2026-10-16-other.yaml")
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None  # builds the index
    _no_scan(monkeypatch, tmp_path)
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None


def _halt_corpus(root, count):
    """`count` halted ask runs of other sizings, written straight to disk (no index)."""
    other = "state/sizings/2026-10-16-other.yaml"
    for n in range(count):
        d = _ask(root, f"20261016T{n // 3600 % 24:02d}{n // 60 % 60:02d}{n % 60:02d}-{n:06x}")
        d.mkdir(parents=True)
        (d / hr.HALTED_FILE).write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": other}), encoding="utf-8")


def _count_reads(monkeypatch):
    reads = []
    real = hr._read
    monkeypatch.setattr(hr, "_read", lambda d: reads.append(Path(d).name) or real(d))
    return reads


def test_a_sizing_with_no_halt_in_a_large_corpus_is_one_listing_once_the_index_is_built(tmp_path, monkeypatch):
    _halt_corpus(tmp_path, 500)
    hr.rebuild_lookup_index(tmp_path)
    reads = _count_reads(monkeypatch)
    _no_scan(monkeypatch, tmp_path)
    assert hr.halted_run_to_resume(tmp_path, SIZING, build=False) is None
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None
    assert reads == []


def test_the_hook_path_never_rebuilds_an_absent_index_over_a_large_corpus(tmp_path, monkeypatch):
    _halt_corpus(tmp_path, 500)
    sentinel = tmp_path / hr.LOOKUP_INDEX_DIR / hr.LOOKUP_SENTINEL
    assert not sentinel.exists()
    reads = _count_reads(monkeypatch)
    assert hr.halted_run_to_resume(tmp_path, SIZING, build=False) is hr.STALLED
    assert reads == [] and not sentinel.exists()
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None  # the default call builds it
    assert sentinel.exists()


def test_the_hook_path_with_no_index_still_finds_a_warp_halt_by_prefix(tmp_path):
    _halt_corpus(tmp_path, 20)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    d.mkdir(parents=True)
    (d / hr.HALTED_FILE).write_text(json.dumps({"status": "halted", "halted": "x", "sizing_rel": SIZING}), encoding="utf-8")
    assert hr.halted_run_to_resume(tmp_path, SIZING, build=False) == d.name


def test_the_ask_lookup_reads_only_indexed_runs(tmp_path, monkeypatch):
    for n in range(5):
        hr.write_halt_marker(_ask(tmp_path, f"20261016T0{n}0000-aaaaa{n}"), halted="x", sizing_rel="state/sizings/2026-10-16-other.yaml")
    ask = _ask(tmp_path, "20261016T090000-bbbbbb")
    hr.write_halt_marker(ask, halted="x", sizing_rel=SIZING)
    hr.halted_run_to_resume(tmp_path, "state/sizings/2026-10-16-none.yaml")  # builds the index
    reads = []
    real = hr._read
    monkeypatch.setattr(hr, "_read", lambda d: reads.append(Path(d).name) or real(d))
    assert hr.halted_run_to_resume(tmp_path, SIZING) == ask.name
    assert reads == [ask.name]


def test_a_marker_present_before_the_index_existed_is_found_by_the_first_lookup(tmp_path):
    ask = _ask(tmp_path, "20261016T010000-aaaaaa")
    ask.mkdir(parents=True)
    (ask / hr.HALTED_FILE).write_text(
        json.dumps({"status": "halted", "halted": "x", "sizing_rel": SIZING}), encoding="utf-8"
    )
    assert hr.halted_run_to_resume(tmp_path, SIZING) == ask.name


@pytest.mark.parametrize("leave", ["resumed", "landed", "closed"])
def test_the_index_drops_a_run_whose_marker_left_halted(tmp_path, leave):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    assert hr.halted_run_to_resume(tmp_path, SIZING) == d.name
    {
        "resumed": lambda: hr.mark_resumed(d),
        "landed": lambda: hr.mark_landed(d, "c" * 40),
        "closed": lambda: hr.mark_closed(d, "gone", "2026-10-16T00:00:00Z"),
    }[leave]()
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None


def test_a_newer_run_past_minted_still_supersedes_an_indexed_halt(tmp_path):
    old = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(old, halted="x", sizing_rel=SIZING)
    assert hr.halted_run_to_resume(tmp_path, SIZING) == old.name
    newer = _dir(tmp_path, "20261016T020000-bbbbbb")
    newer.mkdir(parents=True)
    (newer / "run-stage.json").write_text(json.dumps({"stage": "staged"}), encoding="utf-8")
    assert hr.halted_run_to_resume(tmp_path, SIZING) is None


def test_a_resumed_run_whose_resumer_died_unstaged_is_halted_again_through_the_index(tmp_path):
    old = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(old, halted="x", sizing_rel=SIZING)
    hr.mark_resumed(old)
    _dir(tmp_path, "20261016T020000-bbbbbb").mkdir(parents=True)  # a minted-only shell
    assert hr.halted_run_to_resume(tmp_path, SIZING) == old.name
    assert json.loads((old / hr.HALTED_FILE).read_text())["status"] == "halted"


def test_the_hook_path_builds_a_small_unbuilt_index_inline(tmp_path):
    _halt_corpus(tmp_path, 3)
    sentinel = tmp_path / hr.LOOKUP_INDEX_DIR / hr.LOOKUP_SENTINEL
    assert hr.halted_run_to_resume(tmp_path, SIZING, build=False) is None
    assert sentinel.exists()


def test_rag_corpus_down_marker_carries_the_restore_message_in_a_refire_envelope(tmp_path):
    from coordinator_core.ops.dispatch_emit.rag_use import RESTORE_MESSAGE

    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    data = json.loads(hr.write_halt_marker(d, halted="rag_corpus_down", sizing_rel=SIZING).read_text())
    assert data["next_action"]["detail"] == RESTORE_MESSAGE
    assert data["next_action"]["kind"] == "refire"
    other_dir = _dir(tmp_path, "20261016T020000-bbbbbb")
    other = json.loads(hr.write_halt_marker(other_dir, halted="criterion_not_met", sizing_rel=SIZING).read_text())
    assert "next_action" not in other


def test_citation_unbacked_marker_names_the_unbacked_citations_through_its_reason(tmp_path):
    reason = "docs-check citations the run does not back: [rag: project_symbol FooBar]"
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    data = json.loads(hr.write_halt_marker(d, halted="citation_unbacked", sizing_rel=SIZING, reason=reason).read_text())
    assert data["reason"] == reason
    assert "next_action" not in data


def _split_fixture(tmp_path, *, lock_newer=True, build_script=True):
    import os

    pkg = tmp_path / "control" / "server"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "node_modules").mkdir()
    manifest = {"name": "example-game-repo-server", "main": "dist/index.js"}
    if build_script:
        manifest["scripts"] = {"build": "tsc"}
    (pkg / "package.json").write_text(json.dumps(manifest))
    entry, lock = pkg / "dist" / "index.js", pkg / "node_modules" / ".package-lock.json"
    entry.write_text("")
    lock.write_text("{}")
    old, new = 1_700_000_000, 1_700_001_000
    os.utime(entry, (old if lock_newer else new,) * 2)
    os.utime(lock, (new if lock_newer else old,) * 2)


def test_a_split_dependency_tree_is_named_as_live_breakage(tmp_path):
    _split_fixture(tmp_path)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    data = json.loads(hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING).read_text())
    assert [(s["package_dir"], s["package_name"]) for s in data["live_breakage"]] == [("control/server", "example-game-repo-server")]


def test_no_split_or_no_build_script_leaves_the_marker_without_live_breakage(tmp_path):
    _split_fixture(tmp_path, lock_newer=False)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    assert "live_breakage" not in json.loads(hr.write_halt_marker(d, halted="x", sizing_rel=SIZING).read_text())


def test_a_newer_lockfile_without_a_build_script_is_not_a_split(tmp_path):
    _split_fixture(tmp_path, build_script=False)
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    assert "live_breakage" not in json.loads(hr.write_halt_marker(d, halted="x", sizing_rel=SIZING).read_text())


def test_a_split_alerts_the_live_gem_once_and_no_holder_means_no_alert(tmp_path, monkeypatch):
    from coordinator_core.group_em import nomination
    from coordinator_core.ops.dispatch_emit import halt_split_tree as hst

    _split_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", lambda name, text: calls.append((name, text)))
    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "live", "holder": {"name": "gem-1"}})
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING)
    assert len(calls) == 1 and calls[0][0] == "gem-1"
    assert d.name in calls[0][1] and "control/server" in calls[0][1]

    calls.clear()
    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "none"})
    fresh = _dir(tmp_path, "20261016T020000-bbbbbb")
    data = json.loads(hr.write_halt_marker(fresh, halted="ledger-check", sizing_rel=SIZING).read_text())
    assert calls == [] and data["live_breakage"]


def test_a_split_halt_spawns_no_real_process_under_pytest(tmp_path, monkeypatch):
    import coordinator_core.ops.ceremony.detached_spawn as ds
    from coordinator_core.group_em import nomination

    _split_fixture(tmp_path)
    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "live", "holder": {"name": "gem-1"}})
    monkeypatch.setattr(ds, "spawn_detached", lambda *a, **k: pytest.fail("spawned"))
    hr.write_halt_marker(_dir(tmp_path, "20261016T010000-aaaaaa"), halted="x", sizing_rel=SIZING)


def test_a_rehalt_over_the_same_split_does_not_realert_but_a_changed_split_does(tmp_path, monkeypatch):
    import os

    from coordinator_core.group_em import nomination
    from coordinator_core.ops.dispatch_emit import halt_split_tree as hst

    _split_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", lambda name, text: calls.append(name))
    monkeypatch.setattr(nomination, "identify", lambda root: {"state": "live", "holder": {"name": "gem-1"}})
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING)
    hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING)
    assert len(calls) == 1
    lock = tmp_path / "control" / "server" / "node_modules" / ".package-lock.json"
    os.utime(lock, (1_700_002_000,) * 2)
    hr.write_halt_marker(d, halted="ledger-check", sizing_rel=SIZING)
    assert len(calls) == 2
