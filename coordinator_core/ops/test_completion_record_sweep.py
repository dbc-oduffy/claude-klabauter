"""
coordinator_core.ops.test_completion_record_sweep

Coverage for the READ-ONLY hollow-completion-record sweep --
`sweep_repo`/`sweep_repos`. Never mutates disk; asserts on the findings list
only.

Spec backlink: docs/plans/2026-09-28 hollow-completion-record refusal chunk.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.completion_record_integrity import (
    REASON_EMPTY_COMMITS,
    REASON_PLACEHOLDER,
    REASON_PLAN_NOT_LANDED,
    REASON_PLAN_UNRESOLVABLE,
)
from coordinator_core.ops.completion_record_sweep import (
    sweep_repo,
    sweep_repos,
    summarize_by_check,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_no_archive_completed_dir_yields_empty(tmp_path: Path) -> None:
    assert sweep_repo(tmp_path) == []


def test_well_formed_record_is_not_reported(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "plans" / "myplan.md", "---\nstatus: landed\n---\nbody\n")
    _write(
        tmp_path / "archive" / "completed" / "2026-07" / "entry.md",
        '---\ntitle: "Real"\nchain: "myplan"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    assert sweep_repo(tmp_path) == []


def test_hollow_record_is_reported_with_every_failed_check(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    record = tmp_path / "archive" / "completed" / "2026-09" / "hollow.md"
    _write(
        record,
        (
            '---\ntitle: "PLACEHOLDER — replace with past-tense workstream title"\n'
            'chain: "myplan"\ncommits: []\n---\n\n'
            "<!-- PROSE: Replace this with a ≤8-sentence past-tense description of "
            "what shipped and why it matters. -->\n"
        ),
    )
    findings = sweep_repo(tmp_path)
    assert len(findings) == 1
    f = findings[0]
    assert f.record_path == "archive/completed/2026-09/hollow.md"
    assert f.failed_checks == [REASON_PLACEHOLDER, REASON_EMPTY_COMMITS, REASON_PLAN_NOT_LANDED]
    assert f.chain_slug == "myplan"
    assert f.plan_status == "executing"


def test_a_record_failing_only_one_check_is_still_reported(tmp_path: Path) -> None:
    """The sweep is unconditional -- unlike the writers' finalize-only
    gate, it flags ANY failing check on ANY record (not just one a writer
    tried to finalize), so in-flight WIP with just an empty `commits:`
    still surfaces."""
    _write(
        tmp_path / "archive" / "completed" / "2026-09" / "wip.md",
        '---\ntitle: "Real title"\ncommits: []\n---\n\nReal prose.\n',
    )
    findings = sweep_repo(tmp_path)
    assert len(findings) == 1
    assert findings[0].failed_checks == [REASON_EMPTY_COMMITS]


def test_legacy_subdirectory_is_excluded(tmp_path: Path) -> None:
    _write(
        tmp_path / "archive" / "completed" / "legacy" / "old.md",
        '---\ntitle: "PLACEHOLDER — replace with past-tense workstream title"\ncommits: []\n---\n\nbody\n',
    )
    assert sweep_repo(tmp_path) == []


def test_sweep_repos_concatenates_in_order(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write(
        repo_a / "archive" / "completed" / "2026-09" / "hollow-a.md",
        '---\ntitle: "Real"\ncommits: []\n---\n\nbody\n',
    )
    _write(
        repo_b / "archive" / "completed" / "2026-09" / "hollow-b.md",
        '---\ntitle: "Real"\ncommits: []\n---\n\nbody\n',
    )
    findings = sweep_repos([repo_a, repo_b])
    assert [f.repo_root for f in findings] == [str(repo_a), str(repo_b)]


def test_a_record_with_an_unresolvable_chain_is_reported_distinctly(tmp_path: Path) -> None:
    """A `chain:` slug with no plan file found anywhere (live or archived)
    reports REASON_PLAN_UNRESOLVABLE, never REASON_PLAN_NOT_LANDED -- the
    resolution-noise vs. genuine-hollowness distinction the report table
    depends on."""
    record = tmp_path / "archive" / "completed" / "2026-09" / "orphan-chain.md"
    _write(
        record,
        '---\ntitle: "Real"\nchain: "some-legacy-label"\ncommits:\n  - "deadbeef"\n---\n\nReal prose.\n',
    )
    findings = sweep_repo(tmp_path)
    assert len(findings) == 1
    assert findings[0].failed_checks == [REASON_PLAN_UNRESOLVABLE]


def test_summarize_by_check_counts_per_repo_per_reason(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _write(
        repo_a / "archive" / "completed" / "2026-09" / "hollow-a.md",
        '---\ntitle: "Real"\ncommits: []\n---\n\nReal prose.\n',
    )
    _write(
        repo_a / "archive" / "completed" / "2026-09" / "hollow-a2.md",
        '---\ntitle: "Real"\ncommits: []\n---\n\nReal prose.\n',
    )
    _write(
        repo_b / "archive" / "completed" / "2026-09" / "hollow-b.md",
        '---\ntitle: "PLACEHOLDER — replace with past-tense workstream title"\ncommits: []\n---\n\nbody\n',
    )
    findings = sweep_repos([repo_a, repo_b])
    summary = summarize_by_check(findings)

    assert summary[str(repo_a)] == {REASON_EMPTY_COMMITS: 2}
    assert summary[str(repo_b)] == {REASON_PLACEHOLDER: 1, REASON_EMPTY_COMMITS: 1}


def test_sweep_never_writes(tmp_path: Path) -> None:
    record = tmp_path / "archive" / "completed" / "2026-09" / "hollow.md"
    _write(record, '---\ntitle: "Real"\ncommits: []\n---\n\nbody\n')
    before = record.read_bytes()
    before_mtime = record.stat().st_mtime_ns

    sweep_repo(tmp_path)

    assert record.read_bytes() == before
    assert record.stat().st_mtime_ns == before_mtime
