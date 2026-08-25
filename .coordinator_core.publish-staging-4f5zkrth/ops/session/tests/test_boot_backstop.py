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
    a one-shot self-selecting op with no opticon reviewer loop.
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
    _git("config", "user.email", "boot-backstop-test@makima.test")
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


def test_heir_candidate_archived_bypasses_in_flight_and_stamps_shipped(backstop_repo):
    """(e) A heir-classified candidate (a live successor names it via
    predecessor) is archived and stamped deployment_state: shipped
    unconditionally — even though its own deployment_state literally reads
    in_flight, it never reaches the (b) skip-and-surface disposition."""
    backstop_repo.seed_handoff(
        "2026-07-06-predecessor.md", status="claimed", deployment_state="in_flight"
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
