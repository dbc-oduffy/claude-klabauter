"""workflow.revise build_only: drops the bound sizing's research block, adds no row."""

from __future__ import annotations

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import REL, _bound, _rows, repo  # noqa: F401

RESEARCH = {"value_class": "decision", "appetite": "S", "sources": ["web"]}


def _doc(repo):
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8"))


def _add_research(repo):
    doc = _doc(repo)
    doc["research"] = dict(RESEARCH)
    (repo / REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_build_only_alone_drops_the_block_and_replies_true(repo):
    _bound(repo, "run-one", ask="first ask")
    _add_research(repo)
    rows = _rows(repo)
    reply = rl._handler({"run_id": "run-one", "build_only": True}, repo_root=repo)
    assert reply["ok"] is True and reply["research_dropped"] is True and reply["item"] is None
    assert "research" not in _doc(repo)
    assert _rows(repo) == rows


def test_an_already_absent_block_replies_false_and_writes_nothing(repo):
    _bound(repo, "run-one", ask="first ask")
    before = (repo / REL).read_bytes()
    reply = rl._handler({"run_id": "run-one", "build_only": True}, repo_root=repo)
    assert reply["ok"] is False and reply["research_dropped"] is False
    assert [r["input"] for r in reply["refused"]] == ["build_only"]
    assert (repo / REL).read_bytes() == before


def test_a_reply_without_build_only_carries_no_research_dropped(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "note": "just a note"}, repo_root=repo)
    assert "research_dropped" not in reply


def test_a_note_alone_never_drops_the_block(repo):
    _bound(repo, "run-one", ask="first ask")
    _add_research(repo)
    rl.revise(repo, "run-one", note="build only from here")
    assert _doc(repo)["research"] == RESEARCH


def test_a_held_run_refuses_and_creates_nothing(repo):
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-never", build_only=True)
    assert not rl.run_dir(repo, "run-never").exists()


def test_a_run_with_no_bound_sizing_refuses_and_writes_nothing(repo):
    rl.start(repo, "run-unbound")
    with pytest.raises(rl.LedgerRefused, match="research.close"):
        rl.revise(repo, "run-unbound", build_only=True, note="n")
    assert not (rl.run_dir(repo, "run-unbound") / rl.NOTES_FILE).exists()


def test_combined_with_a_verbatim_it_still_adds_the_row(repo):
    _bound(repo, "run-one", ask="first ask")
    _add_research(repo)
    reply = rl._handler({"run_id": "run-one", "build_only": True, "verbatim": "now build it"}, repo_root=repo)
    assert reply["research_dropped"] is True and reply["item"]
    assert "research" not in _doc(repo)
    assert [r["source_text"] for r in _rows(repo)] == ["first ask", "now build it"]


def test_the_empty_precondition_text_lists_build_only(repo):
    _bound(repo, "run-one", ask="first ask")
    with pytest.raises(rl.LedgerRefused, match="build_only"):
        rl.revise(repo, "run-one")
