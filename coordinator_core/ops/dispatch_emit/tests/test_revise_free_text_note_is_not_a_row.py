"""workflow.revise: a free-text `note` adds no requirement row; a `verbatim` requirement still does."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_revise_reused_sizing import REL, _bound, _rows, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl


def test_a_note_adds_no_register_row(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "note": "just fyi, the box is busy"}, repo_root=repo)
    assert reply["ok"] is True and reply["item"] is None
    assert [r["anchor"] for r in _rows(repo)] == ["ask"]


def test_a_requirement_still_adds_a_row(repo):
    _bound(repo, "run-one", ask="first ask")
    reply = rl._handler({"run_id": "run-one", "verbatim": "also do this"}, repo_root=repo)
    assert reply["item"]
    assert [r["anchor"].split("@")[0] for r in _rows(repo)] == ["ask", "revision"]


def test_a_note_beside_a_requirement_adds_only_the_requirement(repo):
    _bound(repo, "run-one", ask="first ask")
    rl.revise(repo, "run-one", verbatim="also do this", note="context")
    assert len(_rows(repo)) == 2


def test_a_note_on_an_unbound_run_queues_nothing(repo):
    rl.start(repo, "run-raw")
    _, _, added = rl.revise(repo, "run-raw", note="free text")
    assert added is None


def test_a_note_is_persisted_and_noted_paths_resolves_existing_files(repo):
    _bound(repo, "run-one", ask="first ask")
    (repo / "state" / "reviews").mkdir(parents=True)
    (repo / "state" / "reviews" / "board.md").write_text("x", encoding="utf-8")
    _, dir_, added = rl.revise(
        repo, "run-one", note="see `state/reviews/board.md`, and (state/reviews/missing.md). also ../../etc/passwd"
    )
    assert added is None
    assert len((dir_ / rl.NOTES_FILE).read_text(encoding="utf-8").splitlines()) == 1
    assert len(_rows(repo)) == 1
    assert rl.noted_paths(repo, dir_) == ["state/reviews/board.md"]


def test_noted_paths_tolerates_a_torn_tail_and_no_file(repo):
    rl.start(repo, "run-raw")
    dir_ = rl.run_dir(repo, "run-raw")
    assert rl.noted_paths(repo, dir_) == []
    (dir_ / rl.NOTES_FILE).write_text('{"note": "x"}\n{"note": "tor', encoding="utf-8")
    assert rl.noted_paths(repo, dir_) == []


def test_a_note_on_an_unbound_run_is_persisted(repo):
    rl.start(repo, "run-raw")
    _, dir_, _ = rl.revise(repo, "run-raw", note="free text")
    assert (dir_ / rl.NOTES_FILE).is_file()


def test_an_empty_note_alone_is_refused(repo):
    _bound(repo, "run-one", ask="first ask")
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "run-one", note="   ")
