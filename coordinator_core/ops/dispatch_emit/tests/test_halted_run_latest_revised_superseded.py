"""_latest_revised: a revised halt whose ruling a later note on the lineage superseded never displaces the newest halt.

Regression: example-game-repo 06ca65 refire resumed cc36f8 (revised 11-12) over the newest halt 0845c4, though 67ede5
carried a later EM re-ruling (11-15). The older-revised-wins case stays pinned in
hooks/tests/test_refire_halted_carries_revise_notes.py.
"""
import json

from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

SIZING = "state/sizings/2026-11/2026-11-12-foo-06ca65.yaml"
OLD = "warp-2026-11-12-foo-06ca65-20261112T070648-cc36f8"
MID = "warp-2026-11-12-foo-06ca65-20261112T121655-67ede5"
NEW = "warp-2026-11-12-foo-06ca65-20261115T102401-0845c4"


def _run(tmp_path, run_id, revised_at=None, note_at=None):
    d = tmp_path / hr.RECORD_DIR_ROOT / run_id
    hr.write_halt_marker(d, halted="criterion_not_met", sizing_rel=SIZING)
    if revised_at:
        assert hr.mark_revised(d, revised_at)
    if note_at:
        (d / rl.NOTES_FILE).write_text(json.dumps({"note": "ruling", "at": note_at}) + "\n", encoding="utf-8")


def _pick(tmp_path, monkeypatch):
    monkeypatch.setattr(hr, "halted_run_named", lambda root, name, sizing: name)
    return hr._latest_revised(tmp_path, SIZING, {OLD, MID, NEW}, NEW)


def test_later_ruling_on_the_lineage_supersedes_an_old_revise(tmp_path, monkeypatch):
    _run(tmp_path, OLD, revised_at="2026-11-12T10:57:08Z", note_at="2026-11-12T10:57:08Z")
    _run(tmp_path, MID, note_at="2026-11-15T05:42:17Z")
    _run(tmp_path, NEW)
    assert _pick(tmp_path, monkeypatch) == NEW


def test_revise_newer_than_every_note_still_wins(tmp_path, monkeypatch):
    _run(tmp_path, OLD, revised_at="2026-11-15T11:00:00Z", note_at="2026-11-15T11:00:00Z")
    _run(tmp_path, MID, note_at="2026-11-15T05:42:17Z")
    _run(tmp_path, NEW)
    assert _pick(tmp_path, monkeypatch) == OLD
