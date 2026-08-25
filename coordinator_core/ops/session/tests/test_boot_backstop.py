"""
coordinator_core.ops.session.tests.test_boot_backstop

Tests for the rebuilt session.boot_sweep mechanism (C4a of
docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md).

Import guard: coordinator_core.ops.session.boot_backstop MUST be imported at
module load time to fire the @register_op("session.boot_sweep") side-effect
and populate _REGISTRY. Without this import the registry assertion is
vacuously green on an empty registry
(lesson 2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) Terminal-status candidate archived, moved to archive/handoffs/YYYY-MM/,
      and committed as a single R100 rename — exit_code:0.
  (b) Terminal-deployment_state candidate (status non-terminal) also selected
      — the predicate is status OR deployment_state, either terminal.
  (c) Non-terminal handoff is left untouched in state/handoffs/.
  (d) Zero-candidate run — exit_code:0, no git spawn attempted (commit
      result carries reason "no candidates").
  (e) Unreadable/unparseable frontmatter — path excluded from candidates,
      surfaced in warnings, never raises.
  (f) AC1b — relocation routes through
      coordinator_core.session.scope.relocate_touched_path (asserted by
      patching it and checking the call, and by the presence of the
      no-bare-relocation guard passing repo-wide).
  (g) D3 repo_root mismatch → exit_code:1.
  (h) Idempotent replay — second handler run over an unchanged repo finds
      no new candidates (already-archived files are gone from
      state/handoffs/), exit_code:0, HEAD does not advance.

  DR-084 (a)/(b)/(d)/(e) — C4b, layered on the above:
    (a) recency floor  — a fresh claimed_at excludes a candidate silently;
        a stale one proceeds to ordinary archival.
    (b) skip-and-surface — a non-heir in_flight candidate is neither
        archived nor flipped; surfaced in handoffs.skipped with the
        awaiting-adjudication-dr084 token, left in place.
    (d) WARN marker — tasks/orphan-sweep-notes.md gets one line per
        archived candidate and one per (b) skip-and-surface candidate.
    (e) heir (b)-suppression — a heir-classified candidate (live successor
        names it via predecessor) is archived and stamped
        deployment_state: shipped unconditionally, bypassing (b) even when
        its own deployment_state literally reads in_flight.

Spec backlinks:
  - Plan (C4a): docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C4a
  - Plan (C4b): docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C4b
  - AC1b: coordinator_core/session/tests/test_no_untracked_relocation.py
    covers the static route-through-the-helper guard; this file covers the
    runtime behavior.
  - AC5 (C4b): the DR-084 (a),(b),(d),(e) test block above.

Negative-spec:
  - Does NOT test dag_index incompleteness / heir-classification "error"
    fail-closed retains directly (archive_handoffs._classify_heir_children's
    own test surface already covers that primitive; this file exercises the
    boot_backstop-side wiring, not archive_handoffs's own contract).
  - Does NOT test a dry_run/candidate_ids round-trip — session.boot_sweep is
    a one-shot self-selecting op with no cockpit reviewer loop.
  - Does NOT assert a specific commit sha — `_commit_relocations` never
    resolves one (see that function's own docstring); tests assert
    `committed: True` and inspect the working tree/HEAD directly instead.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.session.boot_backstop  # noqa: F401 — fires @register_op("session.boot_sweep")

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.session import boot_backstop
from coordinator_core.ops.session.boot_backstop import (
    _append_warn_markers,
    _handler,
    enumerate_handoffs,
    select_candidates,
)

_OP_NAME = "session.boot_sweep"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.boot_backstop @register_op did not fire"
)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


class BackstopRepo:
    """Temporary git repository for session.boot_backstop tests.

    Minimal skeleton vs. the retired composite's BootRepo — this chunk's own
    mechanism touches only state/handoffs/ and archive/handoffs/.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )

    @property
    def common_dir(self) -> Path:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    @property
    def head(self) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return result.stdout.decode().strip()

    def seed_handoff(
        self,
        name: str,
        status: str = "open",
        deployment_state: Optional[str] = None,
        claimed_at: Optional[str] = None,
        predecessor: Optional[str] = None,
        continued_into: Optional[str] = None,
    ) -> Path:
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm_lines = ["title: \"Test Handoff\"", f"status: {status}", "created: 2026-01-01"]
        if deployment_state is not None:
            fm_lines.append(f"deployment_state: {deployment_state}")
        if claimed_at is not None:
            fm_lines.append(f"claimed_at: \"{claimed_at}\"")
            fm_lines.append("claimed_by: test-session")
        if predecessor is not None:
            fm_lines.append(f"predecessor: {predecessor}")
        if continued_into is not None:
            fm_lines.append(f"continued_into: {continued_into}")
        fm_block = "\n".join(fm_lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path


@pytest.fixture
def backstop_repo(tmp_path) -> BackstopRepo:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "boot-backstop-test@claude-klabauter.test")
    _git("config", "user.name", "Boot Backstop Test")
    _git("config", "commit.gpgsign", "false")

    for d in ("state/handoffs", "archive/handoffs"):
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return BackstopRepo(repo_root)


# ---------------------------------------------------------------------------
# (a) terminal status candidate — archived, committed, R100 rename
# ---------------------------------------------------------------------------


def test_terminal_status_candidate_is_archived(backstop_repo):
    backstop_repo.seed_handoff("2026-01-05-consumed.md", status="consumed")
    head_before = backstop_repo.head

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["handoffs"]["failed"] == []
    assert len(result["handoffs"]["archived"]) == 1
    entry = result["handoffs"]["archived"][0]
    assert entry["from"] == "state/handoffs/2026-01-05-consumed.md"
    assert entry["to"] == "archive/handoffs/2026-01/2026-01-05-consumed.md"

    src = backstop_repo.root / "state" / "handoffs" / "2026-01-05-consumed.md"
    dst = backstop_repo.root / "archive" / "handoffs" / "2026-01" / "2026-01-05-consumed.md"
    assert not src.exists()
    assert dst.exists()

    assert result["commit"]["committed"] is True
    assert backstop_repo.head != head_before

    diff = subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{head_before}..HEAD"],
        cwd=str(backstop_repo.root),
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout.strip().startswith("R100")


# ---------------------------------------------------------------------------
# (b) terminal deployment_state candidate (status non-terminal)
# ---------------------------------------------------------------------------


def test_terminal_deployment_state_candidate_is_archived(backstop_repo):
    backstop_repo.seed_handoff(
        "2026-02-10-shipped.md", status="open", deployment_state="shipped"
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert len(result["handoffs"]["archived"]) == 1
    assert not (backstop_repo.root / "state" / "handoffs" / "2026-02-10-shipped.md").exists()


# ---------------------------------------------------------------------------
# (c) non-terminal handoff untouched
# ---------------------------------------------------------------------------


def test_non_terminal_handoff_is_untouched(backstop_repo):
    path = backstop_repo.seed_handoff("2026-03-01-open.md", status="open")

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["handoffs"]["archived"] == []
    assert path.exists()
    assert result["commit"]["committed"] is False
    assert result["commit"]["reason"] == "no candidates"


# ---------------------------------------------------------------------------
# (d) zero-candidate run — no git spawn attempted
# ---------------------------------------------------------------------------


def test_zero_candidates_no_commit_attempted(backstop_repo):
    head_before = backstop_repo.head

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["commit"] == {"committed": False, "sha": None, "reason": "no candidates"}
    assert backstop_repo.head == head_before


# ---------------------------------------------------------------------------
# (e) unreadable/unparseable frontmatter surfaces a warning, never raises
# ---------------------------------------------------------------------------


def test_unparseable_frontmatter_surfaces_warning(backstop_repo):
    path = backstop_repo.root / "state" / "handoffs" / "2026-04-01-broken.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("no frontmatter here at all\n", encoding="utf-8")
    subprocess.run(["git", "add", str(path)], cwd=str(backstop_repo.root), check=True)
    subprocess.run(
        ["git", "commit", "-m", "add broken handoff"],
        cwd=str(backstop_repo.root),
        capture_output=True,
        check=True,
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["handoffs"]["archived"] == []
    assert any("unreadable" in w["reason"] for w in result["warnings"])
    assert path.exists()


# ---------------------------------------------------------------------------
# (f) AC1b — relocation routes through relocate_touched_path
# ---------------------------------------------------------------------------


def test_relocation_routes_through_relocate_touched_path(backstop_repo, monkeypatch):
    backstop_repo.seed_handoff("2026-05-01-consumed.md", status="consumed")

    calls = []
    real = boot_backstop.session_scope.relocate_touched_path

    def _spy(session_id, src_rel, dst_rel, cwd=None):
        calls.append((session_id, src_rel, dst_rel, cwd))
        return real(session_id, src_rel, dst_rel, cwd=cwd)

    monkeypatch.setattr(boot_backstop.session_scope, "relocate_touched_path", _spy)

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert len(calls) == 1
    session_id, src_rel, dst_rel, cwd = calls[0]
    assert src_rel == "state/handoffs/2026-05-01-consumed.md"
    assert dst_rel == "archive/handoffs/2026-05/2026-05-01-consumed.md"
    assert cwd == str(boot_backstop.main_worktree_root(backstop_repo.common_dir))


# ---------------------------------------------------------------------------
# (g) D3 repo_root mismatch
# ---------------------------------------------------------------------------


def test_repo_root_mismatch_returns_setup_error(backstop_repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(other), capture_output=True, check=True)

    result = _run(
        _handler({"repo_root": str(other)}, repo_root=backstop_repo.common_dir)
    )

    assert result["exit_code"] == 1
    assert "repo_root-mismatch" in result["commit"]["reason"]


def test_missing_repo_root_returns_setup_error():
    result = _run(_handler({}, repo_root=None))
    assert result["exit_code"] == 1


# ---------------------------------------------------------------------------
# (h) idempotent replay
# ---------------------------------------------------------------------------


def test_idempotent_replay_finds_no_new_candidates(backstop_repo):
    backstop_repo.seed_handoff("2026-06-01-consumed.md", status="consumed")

    first = _run(_handler({}, repo_root=backstop_repo.common_dir))
    assert first["exit_code"] == 0
    assert len(first["handoffs"]["archived"]) == 1
    head_after_first = backstop_repo.head

    second = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert second["exit_code"] == 0
    assert second["handoffs"]["archived"] == []
    assert second["commit"]["reason"] == "no candidates"
    assert backstop_repo.head == head_after_first


# ---------------------------------------------------------------------------
# DR-084 (a),(b),(d),(e) — C4b
# ---------------------------------------------------------------------------


def test_recency_floor_skips_fresh_claimed_at(backstop_repo):
    """(a) A candidate whose claimed_at is within the last 30 minutes is
    excluded — left untouched, no WARN marker, no failure."""
    from datetime import datetime, timezone

    fresh = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path = backstop_repo.seed_handoff(
        "2026-07-01-fresh.md", status="claimed", claimed_at=fresh
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["handoffs"]["archived"] == []
    assert result["handoffs"]["skipped"] == []
    assert path.exists()
    notes = backstop_repo.root / "tasks" / "orphan-sweep-notes.md"
    assert not notes.exists()


def test_stale_claimed_at_is_archived(backstop_repo):
    """(a) A candidate whose claimed_at is well outside the 30-minute floor
    proceeds to ordinary archival."""
    old = "2020-01-01T00:00:00Z"
    backstop_repo.seed_handoff(
        "2026-07-02-stale.md", status="claimed", claimed_at=old
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert len(result["handoffs"]["archived"]) == 1


def test_non_heir_in_flight_skip_and_surface(backstop_repo):
    """(b) A non-heir candidate with deployment_state: in_flight is neither
    archived nor flipped — surfaced in handoffs.skipped with the
    awaiting-adjudication-dr084 token, and left in place."""
    path = backstop_repo.seed_handoff(
        "2026-07-03-inflight.md", status="claimed", deployment_state="in_flight"
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    assert result["handoffs"]["archived"] == []
    assert len(result["handoffs"]["skipped"]) == 1
    skip = result["handoffs"]["skipped"][0]
    assert skip["path"] == "state/handoffs/2026-07-03-inflight.md"
    assert skip["reason"] == "awaiting-adjudication-dr084"
    assert path.exists()


def test_warn_marker_written_for_archived_and_skip_and_surface(backstop_repo):
    """(d) A WARN marker line is appended to tasks/orphan-sweep-notes.md for
    both an ordinarily-archived candidate ("archived") and a (b)
    skip-and-surface candidate ("skipped")."""
    backstop_repo.seed_handoff("2026-07-04-consumed.md", status="consumed")
    backstop_repo.seed_handoff(
        "2026-07-05-inflight.md", status="claimed", deployment_state="in_flight"
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    notes_path = backstop_repo.root / "tasks" / "orphan-sweep-notes.md"
    assert notes_path.exists()
    text = notes_path.read_text(encoding="utf-8")
    assert "| archived 2026-07-04-consumed.md" in text
    assert "| skipped 2026-07-05-inflight.md" in text


# ---------------------------------------------------------------------------
# Incremental scan (2026-08-23). The floor is sound because terminal-ness is a
# function of a handoff's own frontmatter, so it can only change by the file
# being written. These pin the two ways that argument can be got wrong: a
# changed file that gets skipped, and an owed file that ages out.
# ---------------------------------------------------------------------------


def _scan_cache(backstop_repo):
    import json
    path = backstop_repo.common_dir / "coordinator-boot-backstop-scan.json"
    assert path.exists(), "no scan cache was written"
    return json.loads(path.read_text(encoding="utf-8"))


def test_second_boot_reads_only_what_changed(backstop_repo, monkeypatch):
    """The steady-state claim: an unchanged corpus is not re-opened."""
    for i in range(5):
        backstop_repo.seed_handoff(f"2026-07-0{i+1}-quiet.md", status="open")

    assert _run(_handler({}, repo_root=backstop_repo.common_dir))["exit_code"] == 0
    assert _scan_cache(backstop_repo)["carried"] == []

    import coordinator_core.ops.session.boot_backstop as bb
    opened = []
    real_scan = bb.scan_corpus
    monkeypatch.setattr(bb, "scan_corpus", lambda paths: (opened.extend(paths), real_scan(paths))[1])

    # Nothing touched since the first sweep.
    assert _run(_handler({}, repo_root=backstop_repo.common_dir))["exit_code"] == 0
    assert opened == [], f"re-read an unchanged corpus: {opened}"

    # One handoff flips terminal; only that one is re-read, and it archives.
    opened.clear()
    backstop_repo.seed_handoff(
        "2026-07-06-now-terminal.md", status="claimed", deployment_state="shipped"
    )
    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert [p.name for p in opened] == ["2026-07-06-now-terminal.md"]
    archived = {item["from"] for item in result["handoffs"]["archived"]}
    assert "state/handoffs/2026-07-06-now-terminal.md" in archived


def test_cap_deferred_candidate_is_carried_and_drains(backstop_repo, monkeypatch):
    """A candidate deferred by the per-boot cap is old on disk, so the mtime
    floor would hide it forever. It must be carried forward by name and
    archived on a later boot -- the queue drains, it does not age out."""
    import coordinator_core.ops.session.boot_backstop as bb
    monkeypatch.setattr(bb, "_MAX_CANDIDATES_PER_BOOT", 2)

    for i in range(4):
        backstop_repo.seed_handoff(
            f"2026-07-0{i+1}-terminal.md", status="claimed", deployment_state="shipped"
        )

    first = _run(_handler({}, repo_root=backstop_repo.common_dir))
    assert len(first["handoffs"]["archived"]) == 2
    assert first["handoffs"]["deferred_to_next_boot"] == 2
    assert len(_scan_cache(backstop_repo)["carried"]) == 2

    second = _run(_handler({}, repo_root=backstop_repo.common_dir))
    assert len(second["handoffs"]["archived"]) == 2, "deferred queue aged out"

    live = backstop_repo.root / "state" / "handoffs"
    assert list(live.glob("*.md")) == []
    assert _scan_cache(backstop_repo)["carried"] == []


def test_absent_scan_cache_falls_back_to_a_full_read(backstop_repo, monkeypatch):
    """The cache is a cache. Losing it costs a full scan, never a wrong
    answer -- a terminal handoff older than any floor is still found."""
    backstop_repo.seed_handoff(
        "2026-07-01-old-terminal.md", status="claimed", deployment_state="shipped"
    )
    backstop_repo.seed_handoff("2026-07-02-quiet.md", status="open")

    cache = backstop_repo.common_dir / "coordinator-boot-backstop-scan.json"
    cache.write_text("{ not json at all", encoding="utf-8")

    import coordinator_core.ops.session.boot_backstop as bb
    opened = []
    real_scan = bb.scan_corpus
    monkeypatch.setattr(bb, "scan_corpus", lambda paths: (opened.extend(paths), real_scan(paths))[1])

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert len(opened) == 2, "a corrupt cache must not narrow the scan"
    archived = {item["from"] for item in result["handoffs"]["archived"]}
    assert "state/handoffs/2026-07-01-old-terminal.md" in archived


def test_unstamped_predecessor_is_not_treated_as_heir(backstop_repo):
    """A successor naming its predecessor is NOT, by itself, heir proof.

    Heir status is the `continued_into` stamp on the candidate's own
    frontmatter, not a reverse scan for who points at it. This asserts the
    deliberate 2026-08-23 change: the boot backstop stopped building a
    reverse-edge index across the whole corpus to rediscover an edge
    `archive_stamp` already writes and verifies at supersede time.

    The shape below — a live successor, an unstamped parent — is one the
    write side cannot produce (supersede REQUIRES continued_into and re-reads
    to confirm it landed); it exists only in records predating the stamp.
    Such a record reads as non-heir and takes the ordinary disposition, which
    for an in_flight parent is (b) skip-and-surface. That is the same outcome
    the old classifier produced whenever a heir had itself been archived --
    an admitted gap in `reverse_membership`'s own negative spec. Do not
    "fix" this by reintroducing a scan.
    """
    backstop_repo.seed_handoff(
        "2026-07-06-unstamped-parent.md", status="claimed", deployment_state="in_flight"
    )
    backstop_repo.seed_handoff(
        "2026-07-06-child.md", status="open", predecessor="2026-07-06-unstamped-parent.md"
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    archived_froms = {item["from"] for item in result["handoffs"]["archived"]}
    assert "state/handoffs/2026-07-06-unstamped-parent.md" not in archived_froms
    skipped = {item["path"] for item in result["handoffs"]["skipped"]}
    assert "state/handoffs/2026-07-06-unstamped-parent.md" in skipped


def test_heir_candidate_archived_bypasses_in_flight_and_stamps_shipped(backstop_repo):
    """(e) A superseded candidate — one carrying the `continued_into` stamp
    that `archive_stamp`'s supersede mode writes and re-reads to verify when a
    successor is created — is archived and stamped deployment_state: shipped
    unconditionally. Even though its own deployment_state literally reads
    in_flight, it never reaches the (b) skip-and-surface disposition."""
    backstop_repo.seed_handoff(
        "2026-07-06-predecessor.md",
        status="claimed",
        deployment_state="in_flight",
        continued_into="2026-07-06-successor.md",
    )
    backstop_repo.seed_handoff(
        "2026-07-06-successor.md",
        status="open",
        predecessor="2026-07-06-predecessor.md",
    )

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 0
    archived_froms = {item["from"] for item in result["handoffs"]["archived"]}
    assert "state/handoffs/2026-07-06-predecessor.md" in archived_froms
    assert result["handoffs"]["skipped"] == []

    dst = (
        backstop_repo.root / "archive" / "handoffs" / "2026-07"
        / "2026-07-06-predecessor.md"
    )
    assert dst.exists()
    assert "deployment_state: shipped" in dst.read_text(encoding="utf-8")

    notes_path = backstop_repo.root / "tasks" / "orphan-sweep-notes.md"
    text = notes_path.read_text(encoding="utf-8")
    assert "| archived 2026-07-06-predecessor.md" in text
    assert "succeeded by 2026-07-06-successor.md" in text


# ---------------------------------------------------------------------------
# Unit-level coverage of the pure helpers (no git repo needed)
# ---------------------------------------------------------------------------


def test_enumerate_handoffs_missing_dir_returns_empty(tmp_path):
    assert enumerate_handoffs(tmp_path) == []


def test_select_candidates_mixed_corpus(tmp_path):
    live = tmp_path / "state" / "handoffs"
    live.mkdir(parents=True)
    terminal = live / "2026-01-01-a.md"
    terminal.write_text(
        "---\nstatus: consumed\n---\n\nBody.\n", encoding="utf-8"
    )
    non_terminal = live / "2026-01-02-b.md"
    non_terminal.write_text("---\nstatus: open\n---\n\nBody.\n", encoding="utf-8")

    candidates, unreadable = select_candidates(enumerate_handoffs(tmp_path))

    assert candidates == [terminal]
    assert unreadable == []

# ---------------------------------------------------------------------------
# exit_code:2 — the two distinct partial-failure paths `any_failed` covers.
# ---------------------------------------------------------------------------


def test_partial_move_failure_returns_exit_code_2_and_reports_failed(backstop_repo, monkeypatch):
    """(i) A per-item move failure inside relocate_candidates does not abort
    the batch: the failing candidate lands in handoffs.failed and is left in
    place, the rest still archive, and exit_code reflects the mix."""
    backstop_repo.seed_handoff("2026-08-01-good.md", status="consumed")
    backstop_repo.seed_handoff("2026-08-02-bad.md", status="consumed")

    real = boot_backstop.session_scope.relocate_touched_path

    def _flaky(session_id, src_rel, dst_rel, cwd=None):
        if src_rel.endswith("bad.md"):
            raise OSError("simulated move failure")
        return real(session_id, src_rel, dst_rel, cwd=cwd)

    monkeypatch.setattr(boot_backstop.session_scope, "relocate_touched_path", _flaky)

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 2
    assert len(result["handoffs"]["archived"]) == 1
    assert result["handoffs"]["archived"][0]["from"] == "state/handoffs/2026-08-01-good.md"
    assert len(result["handoffs"]["failed"]) == 1
    failed = result["handoffs"]["failed"][0]
    assert failed["path"] == "state/handoffs/2026-08-02-bad.md"
    assert "simulated move failure" in failed["reason"]
    assert (backstop_repo.root / "state" / "handoffs" / "2026-08-02-bad.md").exists()
    assert result["commit"]["committed"] is True


def test_moved_but_commit_failed_returns_exit_code_2(backstop_repo, monkeypatch):
    """(ii) A candidate that was successfully moved but whose commit did not
    land still surfaces exit_code:2 -- the move already happened on disk,
    only the record of it is unstaged, and that must never read as success."""
    backstop_repo.seed_handoff("2026-08-03-consumed.md", status="consumed")

    real_git = boot_backstop._git

    def _fail_commit(worktree, args):
        if args and args[0] == "commit":
            return subprocess.CompletedProcess(
                args, returncode=1, stdout="", stderr="simulated commit failure"
            )
        return real_git(worktree, args)

    monkeypatch.setattr(boot_backstop, "_git", _fail_commit)

    result = _run(_handler({}, repo_root=backstop_repo.common_dir))

    assert result["exit_code"] == 2
    assert len(result["handoffs"]["archived"]) == 1
    assert result["commit"]["committed"] is False
    assert "git commit failed" in result["commit"]["reason"]

    dst = (
        backstop_repo.root / "archive" / "handoffs" / "2026-08"
        / "2026-08-03-consumed.md"
    )
    assert dst.exists(), "the move must survive even though the commit did not land"


# ---------------------------------------------------------------------------
# `_append_warn_markers` edges — the batch form's own docstring contract,
# only the happy path of which the earlier tests above exercise.
# ---------------------------------------------------------------------------


def test_append_warn_markers_dedupes_across_separate_boots(tmp_path):
    """A (verb, filename, sid) triple already logged on a prior boot must not
    be re-logged on a later one -- dedup reads the file's CURRENT on-disk
    contents, not just the in-batch set."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    row = ("2026-08-05-x.md", "sess-1", "no deployment_state change", "archived")

    _append_warn_markers(worktree, [row])
    _append_warn_markers(worktree, [row])

    text = (worktree / "tasks" / "orphan-sweep-notes.md").read_text(encoding="utf-8")
    assert text.count("| archived 2026-08-05-x.md") == 1


def test_append_warn_markers_collapses_in_batch_duplicates(tmp_path):
    """The per-row form got in-batch dedup for free by re-reading the file
    between appends; the batch form must reproduce that equivalence itself."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    row = ("2026-08-06-y.md", "sess-2", "no deployment_state change", "archived")

    _append_warn_markers(worktree, [row, row, row])

    text = (worktree / "tasks" / "orphan-sweep-notes.md").read_text(encoding="utf-8")
    assert text.count("| archived 2026-08-06-y.md") == 1


def test_append_warn_markers_relogs_after_rotation(tmp_path):
    """A rotated-out row is no longer "already logged" from the new file's
    point of view -- a post-rotation re-log of the same triple must still
    happen, per this function's own docstring."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    row = ("2026-08-07-z.md", "sess-3", "no deployment_state change", "archived")

    _append_warn_markers(worktree, [row])
    notes_path = worktree / "tasks" / "orphan-sweep-notes.md"
    assert notes_path.exists()

    # Simulate rotation: /workday-start Step 0.8 truncates the file down to a
    # fresh header, dropping the previously-logged row from the live file.
    notes_path.write_text("# Orphan sweep notes\n\n", encoding="utf-8")

    _append_warn_markers(worktree, [row])

    text = notes_path.read_text(encoding="utf-8")
    assert text.count("| archived 2026-08-07-z.md") == 1


def test_append_warn_markers_read_failure_degrades_to_append(tmp_path, monkeypatch):
    """An I/O error reading the marker file's existing contents must degrade
    toward risking a duplicate row rather than dropping the record --
    never silently swallow the write."""
    worktree = tmp_path / "repo"
    worktree.mkdir()
    notes_dir = worktree / "tasks"
    notes_dir.mkdir()
    notes_path = notes_dir / "orphan-sweep-notes.md"
    notes_path.write_text("# Orphan sweep notes\n\nexisting content\n", encoding="utf-8")

    real_read_text = Path.read_text

    def _flaky_read_text(self, *args, **kwargs):
        if self == notes_path:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _flaky_read_text)

    row = ("2026-08-08-w.md", "sess-4", "no deployment_state change", "archived")
    _append_warn_markers(worktree, [row])

    with open(notes_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert "| archived 2026-08-08-w.md" in text
    assert "existing content" in text


def test_boot_path_performs_no_remote_operation():
    """AC6b: `hooks/auto_push.py :: drain_pending_push` left the boot path with
    the composite, and nothing replaced it. A session start must not block on a
    remote (G3 (b)).

    Asserted over the module source rather than by running the handler: the
    handler relocates handoffs and commits, so exercising it to prove a
    negative is the wrong trade. The two sanctioned git calls (`add -A`,
    `commit`) are local; every verb below reaches the network."""
    import inspect

    from coordinator_core.ops.session import boot_backstop

    src = inspect.getsource(boot_backstop)
    for verb in ("push", "fetch", "ls-remote", "pull", "clone", "remote"):
        assert f'"{verb}"' not in src, (
            f"boot path reaches the network: git {verb!r} appears in "
            f"boot_backstop. AC6b requires a session start never block on a remote."
        )
    assert "auto_push" not in src.replace(
        "handoff_reconcile, the eol riders, drain_pending_push", ""
    ), "boot_backstop imports or calls auto_push; drain_pending_push must stay off boot"


def test_boot_path_git_calls_are_record_only_not_query():
    """AC1's oracle, pinned so a later edit cannot quietly reintroduce a query
    spawn. Walks the AST for real `_git(...)` call sites and reads the verb out
    of each argument list -- a raw substring search over the source matches
    frontmatter field names like "status" and proves nothing."""
    import ast
    import inspect

    from coordinator_core.ops.session import boot_backstop

    RECORD_VERBS = {"add", "commit"}
    tree = ast.parse(inspect.getsource(boot_backstop))
    verbs = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name != "_git":
            continue
        for arg in node.args:
            if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts:
                first = arg.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    verbs.append(first.value)

    assert verbs, "no _git call sites found — the oracle would pass vacuously"
    offenders = sorted(set(verbs) - RECORD_VERBS)
    assert not offenders, (
        f"boot path issues non-record git verb(s) {offenders}; AC1 requires zero "
        f"query spawns. Found verbs: {sorted(set(verbs))}"
    )
