"""Every refire mint path carries the halted run's live and retracted revise notes to the run it answers."""

from __future__ import annotations

import json
import re

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import halted_run, op
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED, parent_record_rel, status_rel
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    REL,
    WARP,
    _fire,
    _halt,
    _status,
    composed,
    repo,
)

LIVE_A = "carry A: also write the test"
LIVE_B = "carry B: keep the schema"
GONE = "carry C: obsolete"
RETRACT_REASON = "applied by hand"
BASE = "a" * 40


def _seed_notes(repo, run_id):
    notes = rl.run_dir(repo, run_id) / rl.NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"note": LIVE_A, "at": "2026-10-26T10:00:00Z"}),
        json.dumps({"note": GONE, "at": "2026-10-26T10:01:00Z"}),
        json.dumps({"note": LIVE_B, "at": "2026-10-26T10:02:00Z"}),
    ]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with notes.open("ab") as fh:
        fh.write(run_notes.retraction_line("N2", RETRACT_REASON, "2026-10-26T10:03:00Z", "em"))


def _assert_carried(repo, new_id):
    by_note = {e["note"]: e for e in rl.carried_notes(repo, new_id)}
    assert set(by_note) == {LIVE_A, LIVE_B, GONE}
    assert by_note[LIVE_A]["retracted"] is None and by_note[LIVE_B]["retracted"] is None
    assert by_note[GONE]["retracted"]["reason"] == RETRACT_REASON
    view = (rl.run_dir(repo, new_id) / rl.LEDGER_VIEW_FILE).read_text(encoding="utf-8")
    assert "## Revise notes" in view
    assert f"> {LIVE_A}" in view and f"> {LIVE_B}" in view
    assert f"> {GONE}" not in view and "was retracted" in view


def test_a_halted_ask_predecessor_carries_its_notes(repo, composed):  # noqa: F811
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    reply = _fire(repo)
    assert reply["run_id"] != ASK
    _assert_carried(repo, reply["run_id"])


def _spy_texts(monkeypatch):
    from coordinator_core.ops.dispatch_emit import ask_compose

    out = {"kw": None, "script": None}
    real = ask_compose.compose_ask_script

    def spy(**kw):
        out["kw"] = kw
        out["script"] = real(**kw)
        return out["script"]

    monkeypatch.setattr(ask_compose, "compose_ask_script", spy)
    return out


def test_a_refire_composes_the_carried_notes_under_the_em_label(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    reply = _fire(repo)
    assert reply["run_id"] != ASK
    assert {e["note"] for e in seen["kw"]["carried_notes"]} >= {LIVE_A, LIVE_B}
    text = seen["script"] + "\n".join((seen["kw"].get("staged_out") or {}).values())
    label = "## EM revise notes (the EM's words, not PM verbatim; act on them)"
    assert label in text
    assert f"> {LIVE_A}" in text and f"> {LIVE_B}" in text
    assert f"> {GONE}" not in text


def test_a_fresh_fire_composes_no_notes_block(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _fire(repo)
    assert list(seen["kw"]["carried_notes"]) == []
    text = seen["script"] + "\n".join((seen["kw"].get("staged_out") or {}).values())
    assert "## EM revise notes" not in text


def test_a_blocked_rebind_carries_the_halted_warp_runs_notes(repo, composed, monkeypatch):  # noqa: F811
    _halt(repo, WARP, BASE)
    _seed_notes(repo, WARP)
    monkeypatch.setattr(halted_run, "rebind_blocker", lambda *_a, **_k: "emitter-gone")
    reply = _fire(repo)
    assert reply["run_id"] != WARP
    assert rl.read_run(rl.run_dir(repo, reply["run_id"]))[rl.RESUMED_FROM_FIELD] == WARP
    _assert_carried(repo, reply["run_id"])


def test_a_rebound_warp_run_keeps_its_own_notes(repo, composed):  # noqa: F811
    _halt(repo, WARP, BASE)
    _seed_notes(repo, WARP)
    assert _fire(repo)["run_id"] == WARP
    _assert_carried(repo, WARP)


def test_a_rebound_reserved_slice_keeps_its_own_notes(repo, composed):  # noqa: F811
    parent = "warp-2026-10-16-slice-parent-20260101T000000-cccccc"
    slice_run = "warp-2026-10-16-refire-c3b-20260102T000000-dddddd"
    doc = yaml.safe_load((repo / REL).read_text(encoding="utf-8"))
    doc["em_analysis"] = {"ask_parent": f"{parent} {slice_run}"}
    (repo / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (repo / parent_record_rel(parent)).parent.mkdir(parents=True)
    (repo / parent_record_rel(parent)).write_text("{}\n", encoding="utf-8")
    (repo / status_rel(slice_run)).parent.mkdir(parents=True, exist_ok=True)
    (repo / status_rel(slice_run)).write_text(json.dumps({"run_id": slice_run, "status": STATUS_HALTED}), encoding="utf-8")
    rl.start(repo, slice_run)
    _seed_notes(repo, slice_run)
    reply = op._dispatch_emit({"ask": True, "sizing_path": REL, "reserved_run_id": slice_run}, repo_root=repo)
    assert reply["run_id"] == slice_run
    _assert_carried(repo, slice_run)


def test_a_halted_ask_run_behind_a_newer_unmarked_warp_dir_still_carries_its_notes(repo, composed):  # noqa: F811
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    newer = "warp-2026-10-16-refire-c3b-20271231T000000-eeeeee"
    record_dir(repo, newer).mkdir(parents=True)
    reply = _fire(repo)
    assert reply["run_id"] not in (ASK, newer)
    _assert_carried(repo, reply["run_id"])
    assert _status(repo, ASK) == "halted"
    assert halted_run.halted_ask_run_for(repo, REL) is None


EM_LABEL = "## EM revise notes (the EM's words, not PM verbatim; act on them)"


def _assert_leg_carries(leg_text, what):
    assert EM_LABEL in leg_text, what
    at = leg_text.index(EM_LABEL)
    for note in (LIVE_A, LIVE_B):
        assert f"> {note}" in leg_text[at:], what
    assert f"> {GONE}" not in leg_text, what
    last = max(leg_text.index(f"> {n}") for n in (LIVE_A, LIVE_B))
    assert not re.search(r"## PM", leg_text[at:last]), what


def _legs(seen, prefix):
    staged = seen["kw"].get("staged_out") or {}
    return {k: v for k, v in staged.items() if k == prefix or k.startswith(f"{prefix}-")}


def _executor_prompt(script):
    # The executor prompt text sits in the script, after the brief-path pointer.
    cut = script.index("-- read it completely, then execute it as written.")
    return script[cut:].replace("\\'", "'")


def test_a_refired_halted_run_briefs_executor_and_halt_fix_legs_with_the_note(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    _fire(repo)
    _assert_leg_carries(_executor_prompt(seen["script"]), "executor")
    fix = {k: v for k, v in _legs(seen, "halt-fix").items() if EM_LABEL in v}
    assert fix, "no halt-fix leg carries the EM-note label"
    for k, v in fix.items():
        _assert_leg_carries(v, k)


def test_a_refired_halted_s_run_briefs_the_ledger_fix_leg_with_the_note(repo, monkeypatch):  # noqa: F811
    path = repo / REL
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["estimate"]["tshirt"] = "S"
    doc["route"] = "spec-dispatch"
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed_notes(repo, ASK)
    _fire(repo)
    fix = {k: v for k, v in _legs(seen, "ledger-fix").items() if EM_LABEL in v}
    assert fix, "no ledger-fix leg carries the EM-note label"
    for k, v in fix.items():
        _assert_leg_carries(v, k)
