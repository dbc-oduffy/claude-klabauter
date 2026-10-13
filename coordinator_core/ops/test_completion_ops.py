"""
coordinator_core.ops.test_completion_ops — coverage for ``completion.day_coverage_sweep``.

Fixture pattern: tmp git repo, real subprocess git — not mocked.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.completion_ops import day_coverage_sweep
from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A tmp git repo with identity configured."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "chore: seed repo")
    return root


def _write_entry(root: Path, rel_path: str, *, status: str, commits: list[str]) -> Path:
    commits_block = "\n".join(f'  - "{c}"' for c in commits)
    text = (
        "---\n"
        f"status: {status}\n"
        "commits:\n"
        f"{commits_block}\n"
        "---\n"
        "# Completion entry\n"
    )
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# completion.day_coverage_sweep — reverse (commit -> entry) membership sweep
# ---------------------------------------------------------------------------


def _commit_on(
    root: Path, rel_path: str, content: str, subject: str, *, iso_date: str, trailer: str | None = None
) -> str:
    """Like ``_commit`` but pins author/committer date and optionally appends a
    trailer line to the commit message (e.g. ``Session-Id: <sid>``)."""
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    _git(root, "add", rel_path)
    message = subject if trailer is None else f"{subject}\n\n{trailer}"
    env = dict(os.environ)
    env["GIT_AUTHOR_DATE"] = iso_date
    env["GIT_COMMITTER_DATE"] = iso_date
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _write_handoff(root: Path, rel_path: str, *, claimed_by: str) -> Path:
    path = root / "state" / "handoffs" / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nclaimed_by: {claimed_by}\n---\n# handoff\n", encoding="utf-8")
    return path


def test_day_coverage_sweep_partitions_unclaimed_commits(repo: Path) -> None:
    day = "2026-07-20"
    iso = f"{day}T12:00:00+00:00"

    claimed_sha = _commit_on(repo, "src/a.py", "a", "work: a", iso_date=iso)
    recoverable_sha = _commit_on(
        repo, "src/b.py", "b", "work: b", iso_date=iso, trailer="Session-Id: sess-recoverable"
    )
    in_flight_sha = _commit_on(
        repo, "src/c.py", "c", "work: c", iso_date=iso, trailer="Session-Id: sess-inflight"
    )
    orphan_sha = _commit_on(repo, "src/d.py", "d", "work: d", iso_date=iso)

    _write_entry(repo, "archive/completed/claimed.md", status="released", commits=[claimed_sha])

    recoverable_entry = repo / "archive" / "completed" / "recoverable.md"
    recoverable_entry.parent.mkdir(parents=True, exist_ok=True)
    recoverable_entry.write_text(
        "---\n"
        "status: pending-release\n"
        "authored_by: sess-recoverable\n"
        "commits:\n"
        '  - "deadbeef"\n'
        "---\n"
        "# entry\n",
        encoding="utf-8",
    )

    _write_handoff(repo, "open.md", claimed_by="sess-inflight")

    result = day_coverage_sweep(repo, day)

    assert result["day"] == day
    assert result["total_commits"] == 4
    assert result["claimed_count"] == 1
    assert result["unclaimed_count"] == 3
    assert result["recoverable"] == [recoverable_sha]
    assert result["in_flight"] == [in_flight_sha]
    assert result["orphaned"] == [orphan_sha]
    claimed_elsewhere = set(result["recoverable"]) | set(result["in_flight"]) | set(result["orphaned"])
    assert claimed_sha not in claimed_elsewhere


def test_day_coverage_sweep_separates_foreign_authored_deliveries(repo: Path) -> None:
    """Foreign-authored cross-repo deliveries land in their own bucket, and the
    predicate's two halves each carry their weight.

    The four negative cases are the point: a trailer-less commit outside the
    inbox, an inbox commit whose subject is not the delivery shape, and a
    delivery-shaped commit that also touched a source file all stay ORPHANED
    — so neither absence-of-``Session-Id`` nor the subject string alone can
    reclassify a real orphan. The false-trailer case covers deliveries made
    before the all-hooks-off mechanism, where the receiver's own
    ``prepare-commit-msg`` injected a ``Session-Id`` that no claude-klabauter session
    ever owned.
    """
    day = "2026-07-20"
    iso = f"{day}T12:00:00+00:00"

    foreign_sha = _commit_on(
        repo,
        "cross-repo/inbox/2026-07-20-doe-thing.md",
        "memo",
        "cross-repo: deliver Thing broke memo from coordinator-content-repo-em",
        iso_date=iso,
    )
    foreign_false_trailer_sha = _commit_on(
        repo,
        "cross-repo/inbox/2026-07-20-rag-other.md",
        "memo",
        "cross-repo: deliver Other thing memo from example-retrieval-repo-em",
        iso_date=iso,
        trailer="Session-Id: sess-injected-by-receiver-hook",
    )
    # Negative 1 — no trailer, but claude-klabauter's own work: a genuine orphan.
    untrailed_orphan_sha = _commit_on(
        repo, "src/a.py", "a", "install: fix the thing", iso_date=iso
    )
    # Negative 2 — touches the inbox, but is not a delivery commit.
    inbox_edit_sha = _commit_on(
        repo,
        "cross-repo/inbox/2026-07-20-notes.md",
        "notes",
        "cross-repo: triage the inbox",
        iso_date=iso,
    )
    # Negative 3 — delivery-shaped subject, but touched a source file too.
    mislabelled_sha = _commit_on(
        repo, "src/b.py", "b", "cross-repo: deliver Fake memo from coordinator-content-repo-em", iso_date=iso
    )

    result = day_coverage_sweep(repo, day)

    assert set(result["foreign"]) == {foreign_sha, foreign_false_trailer_sha}
    assert result["foreign_count"] == 2
    assert set(result["orphaned"]) == {
        untrailed_orphan_sha,
        inbox_edit_sha,
        mislabelled_sha,
    }
    assert result["total_commits"] == 5
    assert (
        result["claimed_count"]
        + result["foreign_count"]
        + result["recoverable_count"]
        + result["in_flight_count"]
        + result["orphaned_count"]
        == result["total_commits"]
    )
    assert result["unclaimed_count"] == result["total_commits"] - result["claimed_count"]


def test_day_coverage_sweep_claimed_beats_foreign(repo: Path) -> None:
    """An entry that literally lists the SHA is ground truth: a delivery commit
    some entry already claimed stays CLAIMED, never reclassified as foreign."""
    day = "2026-07-20"
    sha = _commit_on(
        repo,
        "cross-repo/inbox/2026-07-20-doe-thing.md",
        "memo",
        "cross-repo: deliver Thing broke memo from coordinator-content-repo-em",
        iso_date=f"{day}T12:00:00+00:00",
    )
    _write_entry(repo, "archive/completed/claimed.md", status="released", commits=[sha])

    result = day_coverage_sweep(repo, day)

    assert result["claimed_count"] == 1
    assert result["foreign"] == []
    assert result["unclaimed_count"] == 0


def _register_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **repos: Path) -> Path:
    """Point the machine-local registry ladder at a per-test registry directory
    declaring ``repos.<id>`` for each given root.

    Uses ``MACHINE_LOCAL_REGISTRY_DIR`` — rung 1 of ``machine_resolver.
    registry_dir()``'s ladder and the same override
    ``coordinator_core.testing.registry_sandbox`` uses — so no test can reach
    the live registry under settings-home.
    """
    reg_dir = tmp_path / "machine-local"
    reg_dir.mkdir(parents=True, exist_ok=True)
    lines = ["schema = 1"] + [
        f'"repos.{repo_id}" = {str(root)!r}' for repo_id, root in repos.items()
    ]
    (reg_dir / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    return reg_dir


def _seed_session_share(root: Path, session_id: str) -> Path:
    """Provision the session-scoped share directory a session leaves in the repo
    it is homed in (``state/subagent-share/<session-id>/``)."""
    path = root / "state" / "subagent-share" / session_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.md").write_text("findings\n", encoding="utf-8")
    return path


def _seed_ceremony_record(root: Path, session_id: str) -> Path:
    """Provision a ceremony record, whose filename carries only the session id's
    first 12 characters (``state/ceremony/wsc/<prefix>-<stamp>.json``)."""
    path = root / "state" / "ceremony" / "wsc" / f"{session_id[:12]}-20260729T120000Z.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ceremony": "wsc"}\n', encoding="utf-8")
    return path


def test_day_coverage_sweep_separates_sibling_homed_sessions(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commits authored by a session homed in another fleet repo get their own
    bucket instead of reading as this repo's failures.

    The negative cases are the point. A session whose only footprint is in THIS
    tree stays ORPHANED — the predicate is positive sibling-tree evidence, never
    the inverse "absent locally" test, which would fail open on every claude-klabauter
    session that dispatched no subagent. And a session with no footprint
    anywhere stays ORPHANED too: no evidence means no reclassification.
    """
    sibling = tmp_path / "sibling-repo"
    sibling.mkdir()
    _register_repos(monkeypatch, tmp_path, content_root=sibling, claude_klabauter=repo)

    day = "2026-07-20"
    iso = f"{day}T12:00:00+00:00"
    share_sid = "11111111-aaaa-4bbb-8ccc-dddddddddddd"
    ceremony_sid = "22222222-eeee-4fff-8000-111111111111"

    sibling_share_sha = _commit_on(
        repo, "src/a.py", "a", "guards: a", iso_date=iso, trailer=f"Session-Id: {share_sid}"
    )
    sibling_ceremony_sha = _commit_on(
        repo, "src/b.py", "b", "guards: b", iso_date=iso, trailer=f"Session-Id: {ceremony_sid}"
    )
    # Negative 1 — footprint in THIS repo only: a local session, still orphaned.
    local_only_sid = "33333333-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    local_only_sha = _commit_on(
        repo, "src/c.py", "c", "fleet: c", iso_date=iso, trailer=f"Session-Id: {local_only_sid}"
    )
    # Negative 2 — no footprint anywhere: a genuine orphan.
    unknown_sha = _commit_on(
        repo,
        "src/d.py",
        "d",
        "fleet: d",
        iso_date=iso,
        trailer="Session-Id: 44444444-cccc-4ddd-8eee-ffffffffffff",
    )

    _seed_session_share(sibling, share_sid)
    _seed_ceremony_record(sibling, ceremony_sid)
    _seed_session_share(repo, local_only_sid)

    result = day_coverage_sweep(repo, day)

    assert set(result["sibling_homed"]) == {sibling_share_sha, sibling_ceremony_sha}
    assert result["sibling_homed_count"] == 2
    assert set(result["orphaned"]) == {local_only_sha, unknown_sha}
    assert result["total_commits"] == 4
    assert (
        result["claimed_count"]
        + result["foreign_count"]
        + result["recoverable_count"]
        + result["in_flight_count"]
        + result["sibling_homed_count"]
        + result["orphaned_count"]
        == result["total_commits"]
    )
    assert result["unclaimed_count"] == result["total_commits"] - result["claimed_count"]


def test_day_coverage_sweep_local_evidence_outranks_sibling_homed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``recoverable``/``in_flight`` beat ``sibling_homed``: a trailer matching
    one of this repo's own entries or open handoffs is local positive evidence of
    local ownership, so the sibling scan must not reclassify it. Conversely, a
    sibling-homed session that ALSO dispatched a subagent while working in this
    tree still classifies sibling-homed — a local footprint is not a
    disqualifier."""
    sibling = tmp_path / "sibling-repo"
    sibling.mkdir()
    _register_repos(monkeypatch, tmp_path, content_root=sibling, claude_klabauter=repo)

    day = "2026-07-20"
    iso = f"{day}T12:00:00+00:00"
    recoverable_sid = "55555555-dddd-4eee-8fff-000000000000"
    in_flight_sid = "66666666-eeee-4fff-8000-111111111111"
    both_trees_sid = "77777777-ffff-4000-8111-222222222222"

    recoverable_sha = _commit_on(
        repo, "src/a.py", "a", "work: a", iso_date=iso, trailer=f"Session-Id: {recoverable_sid}"
    )
    in_flight_sha = _commit_on(
        repo, "src/b.py", "b", "work: b", iso_date=iso, trailer=f"Session-Id: {in_flight_sid}"
    )
    both_trees_sha = _commit_on(
        repo, "src/c.py", "c", "work: c", iso_date=iso, trailer=f"Session-Id: {both_trees_sid}"
    )

    entry = repo / "archive" / "completed" / "recoverable.md"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        f"---\nstatus: pending-release\nauthored_by: {recoverable_sid}\ncommits: []\n---\n# entry\n",
        encoding="utf-8",
    )
    _write_handoff(repo, "open.md", claimed_by=in_flight_sid)

    for sid in (recoverable_sid, in_flight_sid, both_trees_sid):
        _seed_session_share(sibling, sid)
    _seed_session_share(repo, both_trees_sid)

    result = day_coverage_sweep(repo, day)

    assert result["recoverable"] == [recoverable_sha]
    assert result["in_flight"] == [in_flight_sha]
    assert result["sibling_homed"] == [both_trees_sha]
    assert result["orphaned"] == []


def test_day_coverage_sweep_sibling_homed_fails_closed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every rung of the sibling scan fails CLOSED — an empty registry and a
    registered clone that is not present on this machine both leave the commit
    ORPHANED (an honest over-count) rather than silently exonerating it."""
    day = "2026-07-20"
    sid = "88888888-0000-4111-8222-333333333333"
    sha = _commit_on(
        repo,
        "src/a.py",
        "a",
        "guards: a",
        iso_date=f"{day}T12:00:00+00:00",
        trailer=f"Session-Id: {sid}",
    )

    empty_reg = tmp_path / "empty-machine-local"
    empty_reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty_reg))
    assert day_coverage_sweep(repo, day)["orphaned"] == [sha]

    _register_repos(monkeypatch, tmp_path, content_root=tmp_path / "not-cloned-here")
    result = day_coverage_sweep(repo, day)
    assert result["orphaned"] == [sha]
    assert result["sibling_homed"] == []


def test_day_coverage_sweep_is_day_bounded_not_merge_base_bounded(repo: Path) -> None:
    in_day = _commit_on(repo, "src/x.py", "x", "work: x", iso_date="2026-07-20T08:00:00+00:00")
    outside_day = _commit_on(repo, "src/y.py", "y", "work: y", iso_date="2026-07-21T08:00:00+00:00")

    result = day_coverage_sweep(repo, "2026-07-20")

    assert result["total_commits"] == 1
    assert in_day in result["orphaned"]
    swept = set(result["recoverable"]) | set(result["in_flight"]) | set(result["orphaned"])
    assert outside_day not in swept


def test_day_coverage_sweep_rejects_malformed_day(repo: Path) -> None:
    with pytest.raises(ValueError):
        day_coverage_sweep(repo, "not-a-day")


def test_day_coverage_sweep_exact_midnight_boundaries(repo: Path) -> None:
    """Commits to the exact instants
    ``_day_commit_log`` computes as its boundaries (``00:00:00``/``23:59:59``
    UTC), not an arbitrary interior time — the prior boundary test used
    ``08:00:00`` on both sides, nowhere near either edge the code actually
    filters on."""
    start_of_day = _commit_on(
        repo, "src/start.py", "start", "work: start", iso_date="2026-07-20T00:00:00+00:00"
    )
    end_of_day = _commit_on(
        repo, "src/end.py", "end", "work: end", iso_date="2026-07-20T23:59:59+00:00"
    )
    next_day_midnight = _commit_on(
        repo, "src/next.py", "next", "work: next", iso_date="2026-07-21T00:00:00+00:00"
    )

    result = day_coverage_sweep(repo, "2026-07-20")

    assert result["total_commits"] == 2
    swept = set(result["recoverable"]) | set(result["in_flight"]) | set(result["orphaned"])
    assert start_of_day in swept
    assert end_of_day in swept
    assert next_day_midnight not in swept
