"""Completion records and plans are delete-guard classes with their own guard sets."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.distill.delete_guard import evaluate_candidate_detailed, prefetch_citations

_COMPLETED = "archive/completed/2026-08/2026-08-01-thing-abc123.md"
_SPEC = "archive/specs/2026-08/2026-08-01-thing.md"


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _verdict(root: Path, rel: str):
    cls, results = evaluate_candidate_detailed(root / rel, root)
    return cls, [r.guard for r in results if not r.passed]


def test_harvested_completion_record_with_no_live_reference_clears(tmp_path):
    _write(tmp_path, _COMPLETED, "---\nstatus: done\n---\nbody\n")
    _write(tmp_path, "docs/wiki/thing.md", f"<!-- PROVENANCE: {_COMPLETED} -->\n")
    assert _verdict(tmp_path, _COMPLETED) == ("completion", [])


def test_unharvested_completion_record_is_blocked(tmp_path):
    _write(tmp_path, _COMPLETED, "---\nstatus: done\n---\nbody\n")
    assert _verdict(tmp_path, _COMPLETED) == ("completion", ["harvest-provenance"])


def test_a_test_that_opens_the_record_blocks_it(tmp_path):
    _write(tmp_path, _COMPLETED, "---\nstatus: done\n---\nbody\n")
    _write(tmp_path, "docs/wiki/thing.md", f"source: {_COMPLETED}\n")
    _write(tmp_path, "pkg/tests/test_oracle.py", f'ORACLE = "{_COMPLETED}"\n')
    assert _verdict(tmp_path, _COMPLETED) == ("completion", ["active-reference"])


def test_dated_logs_and_the_record_itself_are_not_live_references(tmp_path):
    _write(tmp_path, _COMPLETED, f"---\nstatus: done\n---\nself: {_COMPLETED}\n")
    _write(tmp_path, "docs/wiki/thing.md", f"source: {_COMPLETED}\n")
    _write(tmp_path, "archive/week-changelogs/2026-W31.md", f"- {_COMPLETED}\n")
    _write(tmp_path, "state/handoffs-archive/h.md", f"- {_COMPLETED}\n")
    assert _verdict(tmp_path, _COMPLETED) == ("completion", [])


def test_a_live_plan_citing_the_record_blocks_it(tmp_path):
    _write(tmp_path, _COMPLETED, "---\nstatus: done\n---\nbody\n")
    _write(tmp_path, "docs/wiki/thing.md", f"source: {_COMPLETED}\n")
    _write(tmp_path, "docs/plans/next.md", f"see {_COMPLETED}\n")
    assert _verdict(tmp_path, _COMPLETED) == ("completion", ["active-reference"])


def test_implemented_plan_needs_harvest(tmp_path):
    _write(tmp_path, _SPEC, "---\nstatus: implemented\n---\nbody\n")
    assert _verdict(tmp_path, _SPEC) == ("plan", ["harvest-provenance"])
    _write(tmp_path, "docs/decisions/DR-1.md", f"from {_SPEC}\n")
    assert _verdict(tmp_path, _SPEC) == ("plan", [])


def test_never_delivered_plan_clears_without_harvest(tmp_path):
    for status in ("superseded", "abandoned", "cancelled"):
        _write(tmp_path, _SPEC, f"---\nstatus: {status}\n---\nbody\n")
        assert _verdict(tmp_path, _SPEC) == ("plan", [])


def test_open_plan_fails_closed(tmp_path):
    _write(tmp_path, "docs/plans/live.md", "---\nstatus: executing\n---\nbody\n")
    _write(tmp_path, "docs/wiki/thing.md", "source: docs/plans/live.md\n")
    assert _verdict(tmp_path, "docs/plans/live.md") == ("plan", ["plan-status"])


def test_unknown_tree_stays_unclassified(tmp_path):
    _write(tmp_path, "docs/research/note.md", "---\nstatus: done\n---\n")
    cls, _ = evaluate_candidate_detailed(tmp_path / "docs/research/note.md", tmp_path)
    assert cls is None


def test_one_prefetch_answers_a_batch(tmp_path):
    other = "archive/completed/2026-08/2026-08-02-other-def456.md"
    _write(tmp_path, _COMPLETED, "---\nstatus: done\n---\n")
    _write(tmp_path, other, "---\nstatus: done\n---\n")
    _write(tmp_path, "docs/wiki/thing.md", f"source: {_COMPLETED}\n")
    citations = prefetch_citations([_COMPLETED, other], tmp_path)
    blocked = {
        rel: [r.guard for r in evaluate_candidate_detailed(tmp_path / rel, tmp_path, citations=citations)[1] if not r.passed]
        for rel in (_COMPLETED, other)
    }
    assert blocked == {_COMPLETED: [], other: ["harvest-provenance"]}
