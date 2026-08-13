"""Unit tests for the ``decision_guides`` section porter's records seam.

Purpose: prove ``_query_decision_guide_records``/``collect`` resolve records via the
in-process ``ceremony.records_query.query_records`` call (post node-subprocess
retirement) rather than spawning ``node bin/query-records.js`` — exercised over a real
``tmp_path`` worktree fixture, not a subprocess mock, since there is no subprocess left
to mock.

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P15
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.emit.sections import decision_guides


def _make_ctx(repo_root: Path, subprocess_root: Path | None = None) -> EmitContext:
    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="test-branch",
        git_sha="deadbeef" * 5,
        git_sha_short="deadbeef",
        observed_at="2026-07-04T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
        subprocess_root=subprocess_root,
    )


def _write_guide(root: Path, name: str, frontmatter: str) -> None:
    guides_dir = root / "docs" / "guides"
    guides_dir.mkdir(parents=True, exist_ok=True)
    (guides_dir / name).write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")


def test_collect_reads_decision_guide_records_in_process(tmp_path: Path) -> None:
    _write_guide(
        tmp_path,
        "fifa-decisions.md",
        "title: FIFA decisions\ncreated: '2026-07-01T00:00:00Z'\nstatus: active\n"
        "owner: the VP-Product Reviewer\nsummary: consolidated FIFA DRs\nid_range: DR-001..DR-010\n"
        "decision_count: 10",
    )
    ctx = _make_ctx(tmp_path)

    records, malformed = decision_guides.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    rec = records[0]
    assert rec["title"] == "FIFA decisions"
    assert rec["created"] == "2026-07-01"
    assert rec["status"] == "active"
    assert rec["owner"] == "the VP-Product Reviewer"
    assert rec["decision_count"] == 10
    assert rec["path"].endswith("fifa-decisions.md")


def test_collect_quarantines_malformed_status(tmp_path: Path) -> None:
    _write_guide(
        tmp_path,
        "bad-decisions.md",
        "title: Bad guide\ncreated: '2026-07-01'\nstatus: unknown-status",
    )
    ctx = _make_ctx(tmp_path)

    records, malformed = decision_guides.collect(ctx)

    assert records == []
    assert len(malformed) == 1
    assert malformed[0]["reason"] == decision_guides._MALFORMED_REASON


def test_collect_is_fail_open_on_missing_worktree(tmp_path: Path) -> None:
    missing_root = tmp_path / "does-not-exist"
    ctx = _make_ctx(missing_root)

    records, malformed = decision_guides.collect(ctx)

    assert records == []
    assert malformed == []


def test_query_uses_subprocess_root_override_when_set(tmp_path: Path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    _write_guide(
        real_root,
        "override-decisions.md",
        "title: Override guide\ncreated: '2026-07-02'\nstatus: active",
    )
    decoy_root = tmp_path / "decoy"
    decoy_root.mkdir()

    ctx = _make_ctx(repo_root=decoy_root, subprocess_root=real_root)

    records, _malformed = decision_guides.collect(ctx)

    assert len(records) == 1
    assert records[0]["title"] == "Override guide"
