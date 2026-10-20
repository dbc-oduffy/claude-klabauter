"""halted_run: marker round-trip, newest-dir rule, and the refusals that yield None."""

import json
import subprocess

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

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
    assert hr.mark_landed(resumed, sha) is False
    assert (resumed / hr.HALTED_FILE).read_bytes() == before


def test_newest_dir_rule(tmp_path):
    old = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(old, halted="x", sizing_rel=SIZING)
    _dir(tmp_path, "20261016T020000-bbbbbb").mkdir(parents=True)
    assert hr.halted_run_for(tmp_path, SIZING) is None


def test_a_sibling_sizing_with_an_extended_stem_does_not_shadow_the_newest_dir(tmp_path):
    d = _dir(tmp_path, "20261016T010000-aaaaaa")
    hr.write_halt_marker(d, halted="x", sizing_rel=SIZING)
    hr.write_halt_marker(
        tmp_path / RECORD_DIR_ROOT / f"warp-{STEM}-slice-1-20261016T020000-bbbbbb",
        halted="x", sizing_rel="state/sizings/2026-10-16-foo-slice-1.yaml",
    )
    assert hr.halted_run_for(tmp_path, SIZING) == d.name


def test_sizing_rel_mismatch(tmp_path):
    hr.write_halt_marker(_dir(tmp_path, "20261016T010000-aaaaaa"), halted="x",
                         sizing_rel="state/sizings/other/2026-10-16-foo.yaml")
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
