"""The halt-record commit lands a review edit only when it is a declared write; every other review edit stays unlanded (halted-run, or peer-held when a peer run holds it)."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import review_edits
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _hold,
    _landed,
    _marker_script,
    _status,
    LEDGER,
    PASSING,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _review(monkeypatch, *paths: str) -> None:
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {p: "agent-1" for p in paths})


def test_review_edit_inside_declared_writes_lands_in_halt_record_commit(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    _review(monkeypatch, "done.py")

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["review_edits_unlanded"] == {}
    assert "done.py" in _landed(repo, out, "--name-only")
    assert "done.py" not in _status(repo)


def test_review_edit_outside_declared_writes_stays_in_tree_as_halted_run(repo, monkeypatch):
    review_file = "elsewhere/ask_roadmap.py"
    _write_record(repo, RUN)
    (repo / "done.py").write_text("d = 1\n", encoding="utf-8")
    (repo / "elsewhere").mkdir()
    (repo / review_file).write_text("# reviewed\n", encoding="utf-8")
    _marker_script(repo, [("C1", ["done.py"])], {"C1": "DONE: C1\n"})
    _review(monkeypatch, review_file)

    out = _call(repo, LEDGER, **PASSING)

    assert out["committed"] is True, out
    assert out["review_edits_unlanded"] == {review_file: "halted-run"}
    seed = _git(["rev-list", "--max-parents=0", "HEAD"], repo).strip()
    assert review_file not in _git(["log", f"{seed}..HEAD", "--name-only", "--format="], repo).split()
    assert "done.py" in _landed(repo, out, "--name-only")
    assert _status(repo)[review_file] == "??"
    assert "done.py" not in _status(repo)


def test_deleted_review_edit_stays_unlanded_and_named(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "product.py").unlink()
    _review(monkeypatch, "product.py")

    out = _call(repo)

    assert out["review_edits_unlanded"] == {"product.py": "halted-run"}
    assert "Review-Edit-Unlanded: product.py" in _git(["log", "-1", "--format=%B"], repo)
    assert "product.py" in _status(repo)


def test_peer_held_review_edit_stays_unlanded_and_named(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / "pkg" / "twin.py").write_text("# reviewed\n", encoding="utf-8")
    _review(monkeypatch, "pkg/twin.py")
    _hold(monkeypatch, {"pkg/twin.py"})

    out = _call(repo)

    assert out["review_edits_unlanded"] == {"pkg/twin.py": "peer-held"}
    assert "pkg/twin.py" not in _landed(repo, out, "--name-only")
    assert "Review-Edit-Unlanded: pkg/twin.py" in _git(["log", "-1", "--format=%B"], repo)


def test_scratch_review_edit_is_never_staged(repo, monkeypatch):
    _write_record(repo, RUN)
    (repo / "scratch").mkdir()
    (repo / "scratch" / "note.py").write_text("x\n", encoding="utf-8")
    _review(monkeypatch, "scratch/note.py")

    out = _call(repo)

    assert "scratch/note.py" not in _landed(repo, out, "--name-only")
    assert out["review_edits_unlanded"] == {"scratch/note.py": "halted-run"}
