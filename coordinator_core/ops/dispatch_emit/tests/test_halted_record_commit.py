"""The halt-record commit never stages a review literal another halted run withholds, and fails closed
when the hold cannot be computed."""

from __future__ import annotations

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import halted_run, land_run, review_edits, sibling_claims
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _landed,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LITERAL = "pkg/twin.py"


def _reviewed(repo, monkeypatch) -> None:
    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / LITERAL).write_text("# reviewed\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {LITERAL: "agent-1"})


def test_a_foreign_held_review_literal_is_not_staged(repo, monkeypatch):
    _reviewed(repo, monkeypatch)
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda _root, _run, _plan, paths: {p: "other-halted" for p in paths if p == LITERAL},
    )

    out = _call(repo)

    assert LITERAL not in _landed(repo, out, "--name-only")
    assert out["review_edits_unlanded"] == {LITERAL: "halted-run"}
    assert out["withheld"][LITERAL] == "foreign-halted-withheld"


def test_an_oserror_reading_the_halt_record_excludes_the_review_literal(repo, monkeypatch):
    _reviewed(repo, monkeypatch)

    def unreadable(*_a, **_k):
        raise OSError("halt record unreadable")

    # held_paths falls back to the raw record reader when the normalizer fails, so the unreadable
    # record is simulated at that reader too.
    monkeypatch.setattr(warp_run_deliverable, "halted_withheld_paths", unreadable)
    monkeypatch.setattr(halted_run, "halted_withheld_strict", unreadable)
    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *_a, **_k: ["ask-x"])
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda *_a, **_k: True)

    out = _call(repo)

    assert LITERAL not in _landed(repo, out, "--name-only")
    assert out["withheld"][LITERAL] == "foreign-halted-withheld"


def test_a_record_file_written_during_the_op_is_relanded_and_push_is_reported(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
    from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import _status

    _write_record(repo, RUN)
    rec = repo / "state" / "warp" / RUN
    real = hrc._baton_cascade

    def late_write(*a, **k):
        (rec / "withheld-prints.json").write_text("{}\n", encoding="utf-8")
        return real(*a, **k)

    monkeypatch.setattr(hrc, "_baton_cascade", late_write)
    from coordinator_core.ops.dispatch_emit import run_push

    monkeypatch.setattr(run_push, "nudge_push", lambda w, r: {"status": "not_pushed", "reason": "stub"})

    out = _call(repo)

    assert out["committed"] is True
    assert "state/warp/%s/withheld-prints.json" % RUN in out["relanded"]
    assert not [p for p in _status(repo) if p.startswith("state/warp")]
    assert out["push"] == {"status": "not_pushed", "reason": "stub"}
    assert "run_artifact_residue" not in out
