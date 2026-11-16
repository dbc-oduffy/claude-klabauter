"""``_grade``'s ledger branch regrades an incomplete chunk whose report is DONE.

The ledger is the judge of an incomplete row; a DONE report on a row the run still lists incomplete is
no reason to skip it, so a passed ledger plus a clean review credits it ``ledger-met``. Without a
ledger the branch grades nothing. A BLOCKED report still bars the credit.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

_VERDICT = {"items": [{"id": "R1", "status": "met"}]}
_REVIEW = {
    "prep": {"slice_files": ["a.py"]},
    "delivery": {"verdict": "PASS"},
    "integration": {"unresolved": [], "confinement_violations": []},
}


def _grade(tmp_path, report_text, *, ledger=True, held=frozenset()):
    (tmp_path / "report.md").write_text(report_text, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",), report="report.md"),))
    return land_run._grade(
        tmp_path, request, {"C1"}, _REVIEW, SimpleNamespace(items=[]) if ledger else None, _VERDICT,
        set(held), None, None, None, None, {},
    )


@pytest.mark.parametrize("report", ["DONE: C1\n", "DONE_WITH_CONCERNS: C1\n", "status: DONE\n"])
def test_a_delivered_incomplete_chunk_is_regraded_by_the_ledger(tmp_path, report):
    assert _grade(tmp_path, report) == {"C1": "ledger-met: R1"}


def test_a_delivered_chunk_earns_nothing_without_a_ledger(tmp_path):
    assert _grade(tmp_path, "DONE: C1\n", ledger=False) == {}


def test_a_delivered_chunk_still_earns_nothing_when_held_or_blocked(tmp_path):
    assert _grade(tmp_path, "DONE: C1\n", held={"C1"}) == {}
    assert _grade(tmp_path, "BLOCKED: C1\n") == {}
