"""
Tests for coordinator_core.commit_ledger.oracle.

Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C6.md
Spec backlink: state/dispatch-briefs/2026-08-19-the-baton-carries-its-commits/C8.md
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.commit_ledger import oracle, store
from coordinator_core.workstream_complete import directives_review


def _init_git_repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_handoff(path: Path, handoff_id: str) -> None:
    """Minimal handoff file with just enough frontmatter for
    ``store._read_handoff_frontmatter`` / ``_find_first_match`` to resolve
    it by ``handoff_id`` — mirrors ``test_store.py``'s own helper (C2), not
    a reinvention.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(["---", f"handoff_id: {handoff_id}", "kind: session-handoff", "---", "", "# body"]),
        encoding="utf-8",
    )


def test_no_ledger_file_reports_pending_not_zero(tmp_path):
    repo = _init_git_repo(tmp_path)
    report = oracle.evaluate("hnd-nowhere", cwd=str(repo))
    assert report.resolved is False
    assert report.code_only.weight is None
    assert report.with_docs.weight is None
    assert "pending" in report.code_only.basis.lower()


def test_ledger_present_zero_entries_is_resolved_zero_not_pending(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    # touch the ledger file with no entries by appending then truncating
    # via a direct write through the store's own path resolver.
    path = store.ledger_path("hnd-empty", cwd=cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")

    report = oracle.evaluate("hnd-empty", cwd=cwd)
    assert report.resolved is True
    assert report.code_only.weight == 0.0
    assert report.with_docs.weight == 0.0
    assert isinstance(report.code_only.basis, str) and report.code_only.basis


def test_code_and_docs_split_with_skipped_sha_basis(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-a", "sha1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-a", "sha2", "doctrine", weight_basis=2.0, cwd=cwd)

    report = oracle.evaluate("hnd-a", skipped_sha_count=1, cwd=cwd)

    assert report.resolved is True
    assert report.code_only.weight == 1.0
    assert report.with_docs.weight == 3.0
    assert "1 commit(s) skipped as sha-unresolved" in report.code_only.basis
    assert "1 commit(s) skipped as sha-unresolved" in report.with_docs.basis
    assert "doc-only" in report.code_only.basis


def test_return_type_has_no_exit_code_member():
    fields = set(oracle.OracleReport._fields) | set(oracle.OracleFigure._fields)
    for forbidden in ("exit_code", "returncode", "rc", "status_code", "exitcode"):
        assert forbidden not in fields


def test_malformed_weight_basis_never_raises(tmp_path):
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    store.append_entry("hnd-b", "sha1", "python", weight_basis="not-a-number", cwd=cwd)
    store.append_entry("hnd-b", "sha2", "python", weight_basis=None, cwd=cwd)

    report = oracle.evaluate("hnd-b", cwd=cwd)
    assert report.resolved is True
    assert report.code_only.weight == 0.0


# ---------------------------------------------------------------------------
# C8 — chain fan-in: the accumulated-small-sessions case, archive-aware
# ---------------------------------------------------------------------------


def test_chain_fan_in_accumulated_small_sessions_detected_via_archive(tmp_path):
    """Three small batons, each individually under the chain-wide ceiling
    C7 wires this oracle into (`directives_review._CHAIN_WEIGHT_CEILING`,
    mirroring row 4's 5-commit brightline), chained backward across
    `archive/handoffs/*/` -- the 153/154 split C2 measured, not
    `state/handoffs/`. A state-only fixture would exercise `read_chain`'s
    trivial single-directory branch and pass for the wrong reason (see this
    chunk's brief, § substrate correction); this fixture requires the
    archive-walking predecessor-pointer resolution to even find the two
    ancestors, so a regression in the archive-aware walk fails this test,
    not just a narrower `test_store.py` unit test.

    Each baton contributes weight 2.0 (two `python`-kind commits at the
    default 1.0 basis) -- under the 5.0 ceiling read in isolation -- but
    the three-baton chain totals 6.0, at/above the ceiling. This is
    row 6's old case (accumulated small-diff sessions row 4 alone never
    catches) resurfacing through the oracle's chain-wide fold, not through
    a single big session.
    """
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)
    ceiling = directives_review._CHAIN_WEIGHT_CEILING

    root_path = repo / "archive" / "handoffs" / "2026-06" / "2026-06-01-root.md"
    _write_handoff(root_path, "hnd-root")
    store.append_entry("hnd-root", "sha-r1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-root", "sha-r2", "python", weight_basis=1.0, cwd=cwd)

    mid_path = repo / "archive" / "handoffs" / "2026-07" / "2026-07-01-mid.md"
    _write_handoff(mid_path, "hnd-mid")
    store.append_entry("hnd-mid", "sha-m1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-mid", "sha-m2", "python", weight_basis=1.0, cwd=cwd)
    assert store.record_predecessor_pointer("hnd-mid", "hnd-root", repo_root=str(repo), cwd=cwd)

    # The leaf is the live, not-yet-archived baton closing right now.
    store.append_entry("hnd-leaf", "sha-l1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-leaf", "sha-l2", "python", weight_basis=1.0, cwd=cwd)
    assert store.record_predecessor_pointer("hnd-leaf", "hnd-mid", repo_root=str(repo), cwd=cwd)

    # Each session's OWN ledger, read in isolation (no chain fold), sits
    # under the ceiling -- the "individually small" half of the case.
    for handoff_id in ("hnd-root", "hnd-mid", "hnd-leaf"):
        own_report = oracle.evaluate(handoff_id, read_chain_fn=store.read_entries, cwd=cwd)
        assert own_report.resolved is True
        assert own_report.with_docs.weight < ceiling

    # The archive-spanning chain fold (real `read_chain`, not a stub) is
    # what surfaces the accumulated weight the isolated reads each missed.
    chain_report = oracle.evaluate("hnd-leaf", cwd=cwd)
    assert chain_report.resolved is True
    assert chain_report.with_docs.weight >= ceiling
    assert chain_report.code_only.weight >= ceiling
    assert chain_report.with_docs.weight == 6.0


def test_chain_fan_in_reaches_decide_review_scale_end_to_end(tmp_path):
    """AC8 end-to-end: the fan-in fixture above proves the oracle's chain-
    weight arithmetic crosses the ceiling; C7's own tests
    (`test_directives_review_oracle.py`) prove the chain-wide arm reacts to
    a STUBBED `OracleReport` at ceiling weight. Neither proves the two
    compose. This test reuses the same three-baton archive-walking
    construction (root/mid/leaf, real `record_predecessor_pointer` calls,
    no `read_chain_fn` stub) and feeds the real `oracle.evaluate` result
    straight into `decide_review_scale`, so a regression in the wiring
    between the two halves -- not just in either half alone -- fails this
    test.
    """
    repo = _init_git_repo(tmp_path)
    cwd = str(repo)

    root_path = repo / "archive" / "handoffs" / "2026-06" / "2026-06-01-root.md"
    _write_handoff(root_path, "hnd-root")
    store.append_entry("hnd-root", "sha-r1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-root", "sha-r2", "python", weight_basis=1.0, cwd=cwd)

    mid_path = repo / "archive" / "handoffs" / "2026-07" / "2026-07-01-mid.md"
    _write_handoff(mid_path, "hnd-mid")
    store.append_entry("hnd-mid", "sha-m1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-mid", "sha-m2", "python", weight_basis=1.0, cwd=cwd)
    assert store.record_predecessor_pointer("hnd-mid", "hnd-root", repo_root=str(repo), cwd=cwd)

    store.append_entry("hnd-leaf", "sha-l1", "python", weight_basis=1.0, cwd=cwd)
    store.append_entry("hnd-leaf", "sha-l2", "python", weight_basis=1.0, cwd=cwd)
    assert store.record_predecessor_pointer("hnd-leaf", "hnd-mid", repo_root=str(repo), cwd=cwd)

    chain_report = oracle.evaluate("hnd-leaf", cwd=cwd)
    assert chain_report.resolved is True
    assert chain_report.with_docs.weight == 6.0  # at/above the 5.0 ceiling

    no_review_kwargs = dict(
        gross_loc=0,
        code_loc=0,
        commit_count=0,
        surface_count=0,
        executor_dispatched=False,
        shared_schema_touched=False,
        chain_disposition="single-session",
    )
    decision = directives_review.decide_review_scale(
        **no_review_kwargs, oracle_report=chain_report
    )
    assert decision.scale == "code-reviewer"
    assert "chain-wide arm" in decision.reason

    # AC11/B4: the real chain read, far above the ceiling, still cannot
    # touch partition_mandatory -- the arm has no path into that field
    # (ReviewScaleDecision is a NamedTuple; _apply_chain_wide_arm only ever
    # `_replace(scale=, reason=)`).
    assert decision.partition_mandatory is False
    assert decision.scale != "partitioned"
