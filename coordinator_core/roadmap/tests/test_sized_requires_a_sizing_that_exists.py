"""`sized` is a claim about disk, not about frontmatter.

It used to be `bool(record["sizing_objects"])` — pure citation presence. A baton
citing a sizing object that is not there therefore read `sized: true`, and
plan-blitz's args contract says in as many words that a sized baton SKIPS the
sizing scout. So the wave planned a baton whose size nobody had determined, and
the blitz-em — whose job is to interrogate the scout's reasoning — had no
reasoning to interrogate. Silent both ways: the gate reported it sized and the
wave reported it planned.

Measured 2026-09-10, wave 0 of run 20260910T000000Z:
`dlv-deliverable-id-as-a-machine-derived-chai-606ae0` cited a
`state/sizings/...yaml` that did not exist — the sizing had been archived and
the citation never repointed. Its own executor found it and reported it.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap.plan_gate import (
    _archived_sizing_index,
    _resolve_sizing_citations,
)


def _sizing(root: Path, rel: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("tshirt: M\n", encoding="utf-8")


def test_a_live_sizing_resolves(tmp_path):
    _sizing(tmp_path, "state/sizings/a.yaml")
    resolved, unresolved = _resolve_sizing_citations(
        tmp_path, ["state/sizings/a.yaml"], {}
    )
    assert resolved == ["state/sizings/a.yaml"]
    assert unresolved == []


def test_a_citation_nothing_answers_is_unresolved(tmp_path):
    resolved, unresolved = _resolve_sizing_citations(
        tmp_path, ["state/sizings/gone.yaml"], {}
    )
    assert resolved == []
    assert unresolved == ["state/sizings/gone.yaml"]


def test_an_archived_sizing_still_counts_as_sized(tmp_path):
    """The work was done and filed. Refusing it would re-scout a baton whose
    sizing exists — the opposite error, and just as wasteful."""
    _sizing(tmp_path, "archive/sizings/2026-08/a.yaml")
    index = _archived_sizing_index(tmp_path)
    resolved, unresolved = _resolve_sizing_citations(
        tmp_path, ["state/sizings/a.yaml"], index
    )
    assert resolved == ["state/sizings/a.yaml"]
    assert unresolved == []


def test_the_archive_index_is_empty_when_there_is_no_archive(tmp_path):
    assert _archived_sizing_index(tmp_path) == {}


def test_a_mixed_citation_list_splits(tmp_path):
    """One good citation does not launder a dangling sibling: both are reported."""
    _sizing(tmp_path, "state/sizings/here.yaml")
    resolved, unresolved = _resolve_sizing_citations(
        tmp_path, ["state/sizings/here.yaml", "state/sizings/gone.yaml"], {}
    )
    assert resolved == ["state/sizings/here.yaml"]
    assert unresolved == ["state/sizings/gone.yaml"]


def test_blank_and_non_string_citations_are_ignored_not_counted(tmp_path):
    resolved, unresolved = _resolve_sizing_citations(tmp_path, ["", "   ", None], {})
    assert resolved == []
    assert unresolved == []
