"""workflow.revise flags (never refuses) a note that contradicts the PM's words or a SETTLED decision."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import REL, _bound, repo  # noqa: F401

MODAL_ASK = "Connect should open a dismissable connect-accounts modal"
INCIDENT = "Connect (to /welcome) is the click path"
FINE = "Connect opens the connect-accounts modal"


def _with_pm_words(repo):
    path = repo / REL
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["pm_verbatims"] = [{"text": MODAL_ASK}]
    path.write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _notes(repo, run_id):
    dir_ = rl.run_dir(repo, run_id)
    return dir_, run_notes.entries(dir_ / rl.NOTES_FILE, run_id)


def test_emitted_run_note_contradicting_the_pm_words_is_flagged_and_stored(repo):
    _with_pm_words(repo)
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "note": INCIDENT}, repo_root=repo)
    assert reply["ok"] is True
    assert reply["canvas_conflicts"] and reply["canvas_conflicts"][0]["rule"] == "surface-kind"
    assert "note contradicts" in reply["warning"] and reply["canvas_conflicts"][0]["source_ref"] in reply["warning"]
    dir_, items = _notes(repo, "run-one")
    assert len(items) == 1 and items[0]["note"] == INCIDENT
    assert items[0]["canvas_conflicts"] == [reply["canvas_conflicts"][0]["source_ref"]]
    rendered = "\n".join(run_notes.render_section(items))
    assert "(flagged: contradicts " in rendered and INCIDENT in rendered


def test_a_consistent_note_replies_with_neither_key_and_no_flag(repo):
    _with_pm_words(repo)
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "note": FINE}, repo_root=repo)
    assert reply["ok"] is True
    assert "canvas_conflicts" not in reply and "warning" not in reply
    items = _notes(repo, "run-one")[1]
    assert items[0]["canvas_conflicts"] == []
    assert "flagged" not in "\n".join(run_notes.render_section(items))


def _held(repo, monkeypatch, run_id):
    monkeypatch.setattr(rl, "_parent_lists_run", lambda root, rid: True)
    assert rl.read_run(rl.run_dir(repo, run_id)) is None


def test_held_run_note_is_queued_unflagged_when_no_source_governs(repo, monkeypatch):
    _held(repo, monkeypatch, "run-held")
    reply = rl._handler({"run_id": "run-held", "note": INCIDENT}, repo_root=repo)
    assert reply["ok"] is True and "canvas_conflicts" not in reply and "warning" not in reply
    assert len(_notes(repo, "run-held")[1]) == 1


def test_held_run_note_is_flagged_and_queued_when_pm_words_govern(repo, monkeypatch):
    from coordinator_core import canvas_contradiction as cc

    _held(repo, monkeypatch, "run-held")
    monkeypatch.setattr(
        cc, "governing_sources", lambda *a, **k: [{"kind": "pm-verbatim", "ref": "pm#1", "text": MODAL_ASK}]
    )
    reply = rl._handler({"run_id": "run-held", "note": INCIDENT}, repo_root=repo)
    assert reply["ok"] is True and reply["held"] is True
    assert reply["canvas_conflicts"][0]["source_ref"] == "pm#1" and "pm#1" in reply["warning"]
    assert _notes(repo, "run-held")[1][0]["canvas_conflicts"] == ["pm#1"]
