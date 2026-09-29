"""
coordinator_core.ops.test_completion_record_repair

Coverage for the hollow-completion-record repair op
(`coordinator_core.ops.completion_record_repair`). Each test that needs git
history builds a REAL tmp git repo (one `git init` + a handful of commits) --
`build_git_history_index` shells out to `git log` for real, so faking that
seam would leave the one-spawn-per-repo contract unverified.

Spec backlink: docs/research/2026-09-28-hollow-completion-records.md
"""

from __future__ import annotations

import datetime
import subprocess
from pathlib import Path

import pytest

from coordinator_core.completion_record_integrity import (
    REASON_EMPTY_COMMITS,
    REASON_PLACEHOLDER,
    REASON_PLAN_NOT_LANDED,
    REASON_PLAN_UNRESOLVABLE,
)
from coordinator_core.ops.completion_record_repair import (
    LEGAL_STATUS_VALUES,
    MAX_COMMITS_PER_RECORD,
    build_git_history_index,
    repair_repo,
    repair_repos,
)
from coordinator_core.win_portability import no_console_creationflags

# Real `git init`/`commit` spawns via the `_git` helper below -- load-bearing
# (verifies `build_git_history_index`'s actual `git log` parsing), so this
# module runs at cadence gates rather than per-commit, matching
# `test_archive_stamp.py`'s identical posture.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_TODAY = datetime.date.today().isoformat()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo: Path, message: str, filename: str = "f.txt") -> str:
    (repo / filename).write_text(message + "\n", encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# build_git_history_index
# ---------------------------------------------------------------------------


def test_history_index_resolves_deliverable_id_trailer(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "did work\n\nDeliverable-Id: dlv-abc123")
    index = build_git_history_index(tmp_path)
    assert index.ok
    assert index.by_deliverable_id["dlv-abc123"] == [sha]
    assert index.commits_scanned == 1


def test_history_index_reports_failure_on_no_git_repo(tmp_path: Path) -> None:
    index = build_git_history_index(tmp_path)  # never git-init'd
    assert not index.ok
    assert index.error


def test_history_index_never_reads_full_body(tmp_path: Path) -> None:
    """A term that appears ONLY in the commit body (never the subject line)
    must NOT be visible anywhere in the parsed index -- the cost fix's whole
    point is dropping `%B` in favour of `%s` (subject only)."""
    _init_repo(tmp_path)
    _commit(tmp_path, "short subject\n\nBODY-ONLY-SECRET-TERM appears only here")
    index = build_git_history_index(tmp_path)
    assert index.ok
    assert all("BODY-ONLY-SECRET-TERM" not in row.subject for row in index.commits)


def test_history_index_window_discards_commits_outside_every_record_window(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "in window\n\nDeliverable-Id: dlv-in")
    today = datetime.date.today()
    window = [(today - datetime.timedelta(days=1), today + datetime.timedelta(days=1))]
    index = build_git_history_index(tmp_path, windows=window)
    assert index.ok
    assert index.commits_scanned == 1  # git emitted it
    assert len(index.commits) == 1  # and it survived the window
    assert index.commits[0].sha == sha

    far_window = [(today - datetime.timedelta(days=100), today - datetime.timedelta(days=90))]
    index2 = build_git_history_index(tmp_path, windows=far_window)
    assert index2.commits_scanned == 1  # still emitted by git
    assert len(index2.commits) == 0  # but discarded -- outside every window


# ---------------------------------------------------------------------------
# repair_repo -- empty-commits, deliverable_id match (precision ladder)
# ---------------------------------------------------------------------------


def test_empty_commits_backfilled_via_deliverable_id_unbounded_when_no_session_or_date(tmp_path: Path) -> None:
    """No `authored_by:`/`created:` on the record at all -- last-resort
    unbounded Deliverable-Id match (case 3)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "shipped it\n\nDeliverable-Id: dlv-xyz")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\ndeliverable_id: "dlv-xyz"\ncommits: []\n---\n\nReal prose.\n',
    )

    report = repair_repo(tmp_path, apply=False)

    assert len(report.repairable) == 1
    r = report.repairable[0]
    assert r.record_path == "archive/completed/2026-09/entry.md"
    assert r.shas == (sha,)
    assert r.match_source == "deliverable_id-unbounded"
    assert r.written is False
    assert "commits: []" in record.read_text(encoding="utf-8")


def test_empty_commits_backfilled_via_deliverable_id_and_session_apply(tmp_path: Path) -> None:
    """The tightest match: Deliverable-Id AND Session-Id both agree."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "shipped it\n\nDeliverable-Id: dlv-xyz\nSession-Id: sess-abc")
    _commit(tmp_path, "unrelated other session\n\nDeliverable-Id: dlv-xyz\nSession-Id: sess-other")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real title"\ndeliverable_id: "dlv-xyz"\nauthored_by: "sess-abc"\ncommits: []\n---\n\nReal prose.\n',
    )

    report = repair_repo(tmp_path, apply=True)

    assert len(report.repairable) == 1
    r = report.repairable[0]
    assert r.shas == (sha,)  # only the matching-session commit, not the other session's
    assert r.match_source == "deliverable_id+session_id"
    assert r.written is True
    text = record.read_text(encoding="utf-8")
    assert f'  - "{sha}"' in text
    assert "commits: []" not in text
    assert 'title: "Real title"' in text


def test_empty_commits_backfilled_via_deliverable_id_and_date_window(tmp_path: Path) -> None:
    """No session match, but `created:` bounds the Deliverable-Id candidates
    to the ones actually near the record's own authoring date."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "shipped it\n\nDeliverable-Id: dlv-xyz")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        f'---\ntitle: "Real title"\ndeliverable_id: "dlv-xyz"\ncreated: "{_TODAY}"\ncommits: []\n---\n\nprose\n',
    )

    report = repair_repo(tmp_path, apply=False)

    assert len(report.repairable) == 1
    r = report.repairable[0]
    assert r.shas == (sha,)
    assert r.match_source == "deliverable_id+date-window"


def test_over_cap_match_sent_to_residue_not_written(tmp_path: Path) -> None:
    """A Deliverable-Id shared across MORE than MAX_COMMITS_PER_RECORD commits
    is too broad for one completion -- residue, never written, count named."""
    _init_repo(tmp_path)
    for i in range(MAX_COMMITS_PER_RECORD + 1):
        _commit(tmp_path, f"work item {i}\n\nDeliverable-Id: dlv-wide", filename=f"f{i}.txt")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        f'---\ntitle: "Real"\ndeliverable_id: "dlv-wide"\ncreated: "{_TODAY}"\ncommits: []\n---\n\nprose\n',
    )

    report = repair_repo(tmp_path, apply=True)

    assert report.repairable == ()
    assert len(report.over_cap) == 1
    assert report.over_cap[0] == ("archive/completed/2026-09/entry.md", MAX_COMMITS_PER_RECORD + 1)
    reasons = [reason for path, reason in report.residue if path == "archive/completed/2026-09/entry.md"]
    assert len(reasons) == 1
    assert "too broad" in reasons[0]
    # Never written.
    assert "commits: []" in record.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# repair_repo -- empty-commits, plan-path/chain-slug fallback (subject only)
# ---------------------------------------------------------------------------


def test_empty_commits_backfilled_via_chain_slug_subject_match(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "landed myplan work")  # slug is IN the subject line
    _write(tmp_path / "docs" / "plans" / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(
        record,
        '---\ntitle: "Real"\nchain: "myplan"\ncommits: []\n---\n\nprose\n',
    )

    report = repair_repo(tmp_path, apply=False)

    assert len(report.repairable) == 1
    r = report.repairable[0]
    assert r.shas == (sha,)
    assert r.match_source == "plan-path/chain-slug"


def test_chain_slug_match_never_sees_body_only_text(tmp_path: Path) -> None:
    """The slug appears ONLY in the commit body -- subject-only scanning must
    NOT match it (this is the cost fix's whole point)."""
    _init_repo(tmp_path)
    _commit(tmp_path, "short subject\n\nlanded myplan work in the body only")
    _write(tmp_path / "docs" / "plans" / "myplan.md", "---\nstatus: implemented\n---\nbody\n")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(record, '---\ntitle: "Real"\nchain: "myplan"\ncommits: []\n---\n\nprose\n')

    report = repair_repo(tmp_path, apply=True)

    assert report.repairable == ()
    reasons = [reason for path, reason in report.residue if path == "archive/completed/2026-09/entry.md"]
    assert len(reasons) == 1
    assert "no commit found" in reasons[0]
    assert "commits: []" in record.read_text(encoding="utf-8")


def test_empty_commits_no_match_goes_to_residue_never_written(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "unrelated work")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    _write(record, '---\ntitle: "Real"\ncommits: []\n---\n\nprose\n')

    report = repair_repo(tmp_path, apply=True)

    assert report.repairable == ()
    assert len(report.residue) == 1
    assert report.residue[0][0] == "archive/completed/2026-09/entry.md"
    assert "no deliverable_id/session/date match and no chain/plan-path" in report.residue[0][1]
    # Never written.
    assert "commits: []" in record.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# repair_repo -- placeholder-marker: reported, never invented
# ---------------------------------------------------------------------------


def test_placeholder_marker_reports_missing_needs_author_status_never_writes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    record = tmp_path / "archive" / "completed" / "2026-09" / "hollow.md"
    original = (
        '---\ntitle: "PLACEHOLDER — replace with past-tense workstream title"\n'
        'commits:\n  - "deadbeef"\n---\n\n'
        "<!-- PROSE: Replace this with a ≤8-sentence past-tense description of "
        "what shipped and why it matters. -->\n"
    )
    _write(record, original)

    report = repair_repo(tmp_path, apply=True)

    reasons = [reason for path, reason in report.residue if path == "archive/completed/2026-09/hollow.md"]
    assert len(reasons) == 1
    assert "needs-author" in reasons[0]
    assert str(LEGAL_STATUS_VALUES) in reasons[0] or "pending-release" in reasons[0]
    # File byte-identical -- commits: was already non-empty so no backfill
    # attempt fires, and the placeholder prose/title is never touched.
    assert record.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# repair_repo -- plan-not-landed / plan-unresolvable: residue only, never faked
# ---------------------------------------------------------------------------


def test_plan_not_landed_is_residue_and_never_written(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "plans" / "myplan.md", "---\nstatus: executing\n---\nbody\n")
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    original = '---\ntitle: "Real"\nchain: "myplan"\ncommits:\n  - "deadbeef"\n---\n\nprose\n'
    _write(record, original)

    report = repair_repo(tmp_path, apply=True)

    reasons = [reason for path, reason in report.residue if path == "archive/completed/2026-09/entry.md"]
    assert len(reasons) == 1
    assert reasons[0].startswith("plan-not-landed")
    assert record.read_text(encoding="utf-8") == original


def test_plan_unresolvable_is_residue_and_never_written(tmp_path: Path) -> None:
    record = tmp_path / "archive" / "completed" / "2026-09" / "entry.md"
    original = '---\ntitle: "Real"\nchain: "some-legacy-label"\ncommits:\n  - "deadbeef"\n---\n\nprose\n'
    _write(record, original)

    report = repair_repo(tmp_path, apply=True)

    reasons = [reason for path, reason in report.residue if path == "archive/completed/2026-09/entry.md"]
    assert len(reasons) == 1
    assert reasons[0].startswith("plan-unresolvable")
    assert record.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# No findings, no spawn / no-op path
# ---------------------------------------------------------------------------


def test_no_findings_yields_empty_report_and_no_git_spawn(tmp_path: Path) -> None:
    # No archive/completed dir at all -- sweep_repo short-circuits before any
    # git history index would ever be built.
    report = repair_repo(tmp_path, apply=True)
    assert report.repairable == ()
    assert report.residue == ()
    assert report.commits_scanned == 0


def test_repair_repos_concatenates(tmp_path: Path) -> None:
    repo_a = tmp_path / "a"
    repo_b = tmp_path / "b"
    _init_repo(repo_a)
    sha_a = _commit(repo_a, "work\n\nDeliverable-Id: dlv-a")
    _write(
        repo_a / "archive" / "completed" / "2026-09" / "entry.md",
        '---\ntitle: "Real"\ndeliverable_id: "dlv-a"\ncommits: []\n---\n\nprose\n',
    )
    _init_repo(repo_b)
    _commit(repo_b, "unrelated")
    _write(
        repo_b / "archive" / "completed" / "2026-09" / "entry.md",
        '---\ntitle: "Real"\ncommits: []\n---\n\nprose\n',
    )

    reports = repair_repos([repo_a, repo_b], apply=False)

    assert len(reports) == 2
    assert reports[0].repairable[0].shas == (sha_a,)
    assert reports[1].repairable == ()
    assert len(reports[1].residue) == 1


# ---------------------------------------------------------------------------
# process_ms / commits_scanned reporting
# ---------------------------------------------------------------------------


def test_report_carries_process_ms_and_commit_counts(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _commit(tmp_path, "work\n\nDeliverable-Id: dlv-a")
    _write(
        tmp_path / "archive" / "completed" / "2026-09" / "entry.md",
        f'---\ntitle: "Real"\ndeliverable_id: "dlv-a"\ncreated: "{_TODAY}"\ncommits: []\n---\n\nprose\n',
    )

    report = repair_repo(tmp_path, apply=False)

    assert report.process_ms >= 0.0
    assert report.commits_scanned >= 1
    assert report.commits_kept >= 1
