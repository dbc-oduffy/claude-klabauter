"""
coordinator_core.ops.session.tests.test_boot_sweep

Tests for the session.boot_sweep composite boot-time archival sweep.

Import guard: coordinator_core.ops.session.boot_sweep MUST be imported at module
load time to fire the @register_op("session.boot_sweep") side-effect and populate
_REGISTRY.  Without this import the registry assertion is vacuously green on an empty
registry (lesson 2026-07-04-universal-registry-completeness-tests-ov.yaml).

Coverage:
  (a) All-four-sweeps-shape — composite result has all four sub-dicts with
      archived/skipped/failed keys; exit_code:0 on empty/non-terminal repo.
  (b) non-heir consumed+in_flight skip-and-surface (DR-084 stop-gap, 2026-07-22) — a
      consumed handoff with deployment_state:in_flight is neither flipped nor
      archived; it lands in consumed_skipped with reason
      "awaiting-adjudication-dr084", gets a WARN marker, and stays in
      state/handoffs/ untouched (the Staff Engineer F0 / AC2, DR-084 no-automated-abandonment).
  (c) shipped_in stamp — when scope path has a real commit, shipped_in is stamped
      in the archived handoff frontmatter (best-effort scope-path git log, AC2).
  (d) WARN marker — tasks/orphan-sweep-notes.md created with a marker line after
      a consumed handoff is successfully archived (AC2 / mirrors the deleted session-init.sh, example-doctrine-repo 2f8b8450).
  (e) Recency floor — claimed_at within last 30 min → skipped with reason
      "consumed_at within 30min recency floor" (writer literal, old-name tolerance);
      file NOT moved (AC2 / mirrors the deleted session-init.sh, example-doctrine-repo 2f8b8450,
      bias-to-life for just-consumed handoffs).
  (f) Idempotent replay — second handler run on same repo finds no new candidates;
      exit_code:0; HEAD does not advance (DR-211 D2(i) commutative / idempotent).
  (g) Act-time terminality drift — claimed_by session goes live between preview
      and act → skipped with "re-live" reason; file NOT moved (DR-211 D1 re-verify).
  (h) Partial failure → exit_code:2 — injected failure in memos sweep → exit_code:2;
      git status clean (DETERMINATE-PARTIAL, no half-staged index).
  (i) Liveness guard — consumed handoff whose claimed_by session is currently live
      → excluded from candidates (not archived), file intact.

Spec backlinks:
  - Plan C8b: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C8b
  - AC2:  consumed-handoff boot-path behavioral contract (deployment_state flip,
          shipped_in stamp, WARN marker, 30-min recency floor).
  - AC6:  import-to-register positive floor; test matrix per pcore-11 pattern.
  - Design-decision 3 / the Staff Engineer F0: fleet op is archival-only; boot entrypoint carries
    the four behavioral additions over fleet.archive_completed_handoffs.
  - DR-211 D1: act-time terminality re-verify (drift test).
  - DR-211 D2(i): commutative / idempotent (replay test).

Negative-spec:
  - Does NOT test a dry_run/candidate_ids round-trip — session.boot_sweep is a
    one-shot self-selecting sweep with no cockpit reviewer loop (Anti-scope).
  - Does NOT assert fleet.archive_completed_{handoffs,plans} wire output — those ops
    are byte-for-byte frozen (AC4).  Tests invoke boot_sweep._handler directly.
  - Does NOT test session.reap — different op, different substrate, different test file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test function so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — ops/__init__.py triggers all op registrations
import coordinator_core.ops.session.boot_sweep  # noqa: F401 — fires @register_op("session.boot_sweep")

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.fleet.reap_unintegrated_findings import (
    _AGE_THRESHOLD_DAYS,
)
from coordinator_core.ops.session.boot_sweep import _append_warn_marker, _handler

# Positive floor assertion: session.boot_sweep must be registered before any test runs.
# "positive floor" per lesson 2026-07-04-universal-registry-completeness-tests-ov.yaml:
# assert len >= N BEFORE completeness check so a vacuously-empty registry is caught.
_OP_NAME = "session.boot_sweep"
_session_ops = [k for k in _REGISTRY if k.startswith("session.")]
assert len(_session_ops) >= 1, (
    "import guard failed: no session.* ops in _REGISTRY — "
    "check coordinator_core.ops.session.boot_sweep import"
)
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY; "
    "coordinator_core.ops.session.boot_sweep @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


# Patch target for the liveness check inside archive_handoffs (used in the four
# test cases that involve claimed_by session IDs).
_LIVE_SIDS_PATCH = "coordinator_core.ops.fleet.archive_handoffs.resolve_live_session_ids"

# Patch target for the per-family actioned-memos internal (used in partial-failure test).
_MEMOS_INTERNAL_PATCH = "coordinator_core.ops.session.boot_sweep.archive_actioned_memos_internal"


# Ownership guard (2026-07-22): the default "own" session id every BootRepo._git
# commit is stamped with — the autouse fixture below sets CLAUDE_SESSION_ID to
# this SAME value so stamp_shipped_in (now delegated to via
# _stamp_shipped_in_besteff) resolves the caller as the author of these
# fixtures' commits by default. Tests that care about ownership pass an
# explicit `session_id=` to `BootRepo._git`/`.seed_handoff` to seed a PEER
# commit instead — see test_two_repo_shipped_in_stamp_uses_git_root and any
# TestOwnershipGuard-shaped addition.
_DEFAULT_TEST_SESSION_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    """See _DEFAULT_TEST_SESSION_ID above — mirrors
    coordinator_core/test_archive_stamp.py's identically-named fixture."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


# ---------------------------------------------------------------------------
# BootRepo — git repo fixture helper for session.boot_sweep tests
# ---------------------------------------------------------------------------


class BootRepo:
    """Temporary git repository for session.boot_sweep tests.

    Provides all directories required by the four archival sweeps in boot_sweep:
    - docs/plans/         (terminal-plans sweep)
    - state/handoffs/     (consumed-handoffs sweep)
    - archive/specs/      (plans archival destination)
    - archive/handoffs/   (handoffs archival destination)
    - cross-repo/inbox/   (actioned-memos sweep)
    - cross-repo/archive/ (memos archival destination)

    The fleet.archive_completed_handoffs conftest (FleetRepo) is intentionally NOT
    imported here — session tests extend the directory layout with cross-repo/ dirs,
    and the fixture is kept self-contained per the session test package convention.
    Negative-spec: does NOT extend FleetRepo (conftest.py negative-spec on coupling).
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID) -> subprocess.CompletedProcess:
        """Runs a git command in this repo.

        Ownership guard (2026-07-22): `commit -m <msg>` calls get a
        `Session-Id: <session_id>` trailer auto-appended as a second paragraph —
        UNLESS the message already contains "Session-Id:" (an explicit trailer
        always wins, letting a test seed a PEER commit), or `session_id=None` is
        passed explicitly (a genuinely trailer-less commit). Mirrors
        coordinator_core/test_archive_stamp.py's `_git()` helper — same fix,
        same reason: stamp_shipped_in now refuses to stamp a derived sha unless
        its trailer matches the CALLING session's own id (see the
        `_default_caller_session_id` autouse fixture below, which sets
        CLAUDE_SESSION_ID to this same default).
        """
        args_list = list(args)
        if (
            len(args_list) >= 3
            and args_list[0] == "commit"
            and args_list[1] == "-m"
            and session_id is not None
            and "Session-Id:" not in args_list[2]
        ):
            args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
        return subprocess.run(
            ["git"] + args_list,
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (.git for a non-worktree repo)."""
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(self.root),
            capture_output=True,
            check=True,
        )
        return Path(result.stdout.decode().strip()).resolve()

    def seed_handoff(
        self,
        name: str,
        status: str,
        claimed_by: Optional[str] = None,
        extra_frontmatter: str = "",
    ) -> Path:
        """Write and commit a state/handoffs/<name>.md.

        extra_frontmatter is appended as raw YAML lines inside the frontmatter block,
        allowing tests to inject deployment_state, claimed_at, scope, etc.

        Returns the absolute path of the created file.
        """
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        claimed_by_line = f"claimed_by: {claimed_by}" if claimed_by else ""
        extra_block = extra_frontmatter.strip()

        fm_lines = ["title: \"Test Handoff\"", f"status: {status}", "created: 2026-01-01"]
        if claimed_by_line:
            fm_lines.append(claimed_by_line)
        if extra_block:
            fm_lines.append(extra_block)

        fm_block = "\n".join(fm_lines)
        content = f"---\n{fm_block}\n---\n\n# Handoff\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add handoff {name}")
        return path

    def seed_plan(self, name: str, status: str) -> Path:
        """Write and commit a docs/plans/<name>.md.

        Returns the absolute path of the created file.
        """
        path = self.root / "docs" / "plans" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(f"""\
            ---
            title: "Test Plan"
            status: {status}
            created: 2026-01-01
            ---

            # Test Plan

            Body.
        """)
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add plan {name}")
        return path

    def seed_memo(self, name: str, status: str) -> Path:
        """Write and commit a cross-repo/inbox/<name>.md.

        Returns the absolute path of the created file.
        """
        path = self.root / "cross-repo" / "inbox" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        content = textwrap.dedent(f"""\
            ---
            title: "Test Memo"
            status: {status}
            created: 2026-01-01
            ---

            # Memo

            Body.
        """)
        path.write_text(content, encoding="utf-8")
        self._git("add", str(path))
        self._git("commit", "-m", f"add memo {name}")
        return path

    def path_exists(self, repo_rel: str) -> bool:
        """Return True iff repo_root / repo_rel exists on disk."""
        return (self.root / repo_rel).exists()

    def git_status_clean(self) -> bool:
        """Return True iff working tree and index are clean (no uncommitted changes)."""
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(self.root),
            capture_output=True,
        )
        return result.stdout.strip() == b""

    def git_head_sha(self) -> str:
        """Return the current HEAD commit SHA."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(self.root),
            capture_output=True,
        )
        return result.stdout.decode().strip()


@pytest.fixture
def boot_repo(tmp_path) -> BootRepo:
    """Provide a temporary git repository pre-configured for session.boot_sweep tests.

    The repo has:
    - git config user.email / user.name / commit.gpgsign=false set
    - Standard artifact directory skeleton committed (with .gitkeep sentinels)
    - An initial commit so git read-tree HEAD and git log work from the first test

    Extended vs. fleet_repo fixture: includes cross-repo/inbox/ and cross-repo/archive/
    directories for the actioned-memos sweep (sweep 4 of session.boot_sweep).

    Usage::

        def test_something(boot_repo):
            boot_repo.seed_handoff("2026-07-01-h.md", "consumed")
            result = _run(_handler({}, repo_root=boot_repo.common_dir))
            assert result["exit_code"] == 0
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo_root),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "boot-sweep-test@claude-klabauter.test")
    _git("config", "user.name", "Boot Sweep Test")
    _git("config", "commit.gpgsign", "false")

    # Standard artifact directory skeleton — same dirs as fleet_repo conftest plus
    # cross-repo/ dirs for the memos sweep.
    dirs = [
        "docs/plans",
        "state/handoffs",
        "state/bug-backlog",
        "archive/specs",
        "archive/handoffs",
        "archive/bug-backlog",
        "cross-repo/inbox",
        "cross-repo/archive",
    ]
    for d in dirs:
        (repo_root / d).mkdir(parents=True, exist_ok=True)
        (repo_root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return BootRepo(repo_root)


# ---------------------------------------------------------------------------
# (a) All-four-sweeps: result has all four sub-dicts with correct shape
# ---------------------------------------------------------------------------


def test_all_four_sweeps_result_shape(boot_repo):
    """Composite result has all four sub-dicts; exit_code:0 on an empty repo.

    Verifies that session.boot_sweep runs all four archival sweeps and produces
    the correct result envelope shape, even when all sweeps find no candidates.
    """
    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, (
        f"empty repo must return exit_code:0; got {result!r}"
    )

    # Each of the four sweeps must be present with archived/skipped/failed keys.
    for sweep_key in ("consumed_handoffs", "plans", "shipped_handoffs", "memos"):
        assert sweep_key in result, f"result must have {sweep_key!r} key"
        sweep = result[sweep_key]
        for field in ("archived", "skipped", "failed"):
            assert field in sweep, (
                f"result[{sweep_key!r}] must have {field!r} key"
            )
        assert isinstance(sweep["archived"], list)
        assert isinstance(sweep["skipped"], list)
        assert isinstance(sweep["failed"], list)

    # Empty repo: nothing to archive in any sweep.
    assert result["consumed_handoffs"]["archived"] == []
    assert result["plans"]["archived"] == []
    assert result["shipped_handoffs"]["archived"] == []
    assert result["memos"]["archived"] == []


# ---------------------------------------------------------------------------
# (b) Non-heir consumed+in_flight: skip-and-surface (DR-084 stop-gap,
#     2026-07-22) — the deleted in_flight→abandoned flip's replacement.
# ---------------------------------------------------------------------------


def test_non_heir_in_flight_skip_and_surfaced_not_archived(boot_repo):
    """Consumed handoff with deployment_state:in_flight → skip-and-surface, NOT archived.

    2026-07-22 (DR-084 stop-gap, C1): the former in_flight→abandoned flip is
    DELETED, not merely bypassed — a non-heir consumed+in_flight candidate is
    now neither flipped nor archived. It lands in consumed_skipped with the
    distinct reason token "awaiting-adjudication-dr084", gets a WARN marker,
    and stays in state/handoffs/ with deployment_state:in_flight untouched —
    the durable adjudication queue, pending a human decision or the DR-084
    "continued" schema landing.
    """
    boot_repo.seed_handoff(
        "2026-07-01-flip.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    cid = "state/handoffs/2026-07-01-flip.md"
    before_text = (boot_repo.root / cid).read_text(encoding="utf-8")

    # No claimed_by → resolve_live_session_ids is not called; no patch needed.
    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"

    # Must NOT be archived.
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, (
        f"non-heir consumed+in_flight candidate must NOT be archived "
        f"(DR-084 stop-gap); got archived_ids={archived_ids!r}"
    )

    # Must appear in consumed_skipped with the distinct reason token.
    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert cid in skip_map, (
        f"non-heir consumed+in_flight candidate must appear in "
        f"consumed_handoffs.skipped; got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert skip_map[cid] == "awaiting-adjudication-dr084", (
        f"skip reason must be the distinct DR-084 reason token; "
        f"got reason={skip_map[cid]!r}"
    )

    # Source file must remain in state/handoffs/, byte-identical (untouched).
    assert boot_repo.path_exists(cid), (
        "a skip-and-surface candidate must remain in state/handoffs/ "
        "(the durable adjudication queue)"
    )
    after_text = (boot_repo.root / cid).read_text(encoding="utf-8")
    assert after_text == before_text, (
        "a skip-and-surface candidate's frontmatter must be byte-identical — "
        "no partial flip/stamp side effects"
    )
    assert "deployment_state: in_flight" in after_text, (
        "deployment_state: in_flight must remain intact on a skip-and-surface candidate"
    )
    assert "deployment_state: abandoned" not in after_text, (
        "abandoned must NEVER be written by this sweep (DR-084 stop-gap)"
    )

    # WARN marker written for the skip-and-surface disposition.
    marker_path = boot_repo.root / "tasks" / "orphan-sweep-notes.md"
    assert marker_path.exists(), (
        "tasks/orphan-sweep-notes.md must be written for a skip-and-surface candidate"
    )
    marker_content = marker_path.read_text(encoding="utf-8")
    assert "2026-07-01-flip.md" in marker_content
    assert "awaiting human adjudication or DR-084 continued semantics" in marker_content
    assert "skipped" in marker_content, (
        "the marker line must use the 'skipped' verb, not falsely claim 'archived'"
    )


# ---------------------------------------------------------------------------
# (b2) deployment_state absent: no field added to archived handoff
# ---------------------------------------------------------------------------


def test_deployment_state_absent_field_not_modified(boot_repo):
    """Consumed handoff with NO deployment_state field → archived as-is; field NOT injected.

    The (b) skip-and-surface disposition (DR-084 stop-gap, 2026-07-22) only
    diverts a candidate whose frontmatter literally reads deployment_state:
    in_flight.  When the field is absent, the candidate is not a skip-and-
    surface candidate at all — archival proceeds normally and the archive
    destination does NOT gain a spurious 'deployment_state: abandoned' line
    (there is no code path left in this sweep that could write it).

    This is the most common real-world shape: handoffs that were never explicitly tagged
    with a deployment_state.

    Coverage for the absent-field case documents the "absent field passes through unchanged"
    invariant and guards against a regression that inadvertently inserts
    deployment_state: abandoned for all consumed handoffs.
    """
    boot_repo.seed_handoff(
        "2026-07-01-no-ds.md", "consumed",
        # Intentionally no extra_frontmatter — deployment_state field absent.
    )
    cid = "state/handoffs/2026-07-01-no-ds.md"

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    # Handoff must be archived (absent deployment_state does NOT block archival).
    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff without deployment_state must still be archived; "
        f"got archived_ids={archived_ids!r}"
    )

    # Archive destination must exist on disk.
    dest = "archive/handoffs/2026-07/2026-07-01-no-ds.md"
    assert boot_repo.path_exists(dest), f"archived file must exist at {dest!r}"

    # The archived file must NOT have deployment_state: abandoned spuriously injected —
    # no code path in this sweep writes "abandoned" (DR-084 stop-gap, 2026-07-22).
    content = (boot_repo.root / dest).read_text(encoding="utf-8")
    assert "deployment_state: abandoned" not in content, (
        "deployment_state: abandoned must NOT be injected when field was absent in source; "
        f"got content head={content[:400]!r}"
    )

    assert boot_repo.git_status_clean(), "git index must be clean after archival"


# ---------------------------------------------------------------------------
# (c) shipped_in stamp: stamped when scope path has a real git commit
# ---------------------------------------------------------------------------


def test_shipped_in_stamped_when_scope_has_commit(boot_repo):
    """shipped_in is stamped when the scope path has a real git commit (best-effort, AC2).

    Creates a committed file matching the handoff's scope field, then verifies
    that after archival the archive destination has a shipped_in: <SHA> line in its
    frontmatter.
    """
    # Create and commit a file that the scope field will reference.
    scope_path = "docs/plans/2026-07-01-scope-target.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add scope target")

    # Seed the consumed handoff with scope pointing to the committed file.
    boot_repo.seed_handoff(
        "2026-07-01-scoped.md", "consumed",
        extra_frontmatter=f"scope: {scope_path}",
    )
    cid = "state/handoffs/2026-07-01-scoped.md"

    # No claimed_by → no liveness patch needed.
    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff with scope must be archived; got archived_ids={archived_ids!r}"
    )

    dest = "archive/handoffs/2026-07/2026-07-01-scoped.md"
    assert boot_repo.path_exists(dest), f"archived file must exist at {dest!r}"

    content = (boot_repo.root / dest).read_text(encoding="utf-8")
    assert "shipped_in:" in content, (
        f"archived file must have shipped_in: stamp (scope path has a real commit); "
        f"got content head={content[:400]!r}"
    )

    assert boot_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (d) WARN marker: tasks/orphan-sweep-notes.md appended after successful archive
# ---------------------------------------------------------------------------


def test_warn_marker_appended_for_archived_consumed_handoff(boot_repo):
    """tasks/orphan-sweep-notes.md is created/appended after a consumed handoff archives.

    /workday-start Step 0.8 reads this file to surface orphaned workstream handoffs
    archived without closure ceremony.  The marker is written ONLY after successful
    archival (acted[]), not for skipped or failed items (AC2 / mirrors the deleted session-init.sh, example-doctrine-repo 2f8b8450).

    2026-07-22 (DR-084 stop-gap): a candidate seeded WITH deployment_state:
    in_flight is now skip-and-surfaced rather than archived (see the (b)
    tests above) — this fixture is seeded WITHOUT deployment_state so it
    still takes the ordinary archival path, and the marker's disposition
    note reads the unconditional "no deployment_state change" (the flip that
    used to make this claim conditional is deleted).
    """
    boot_repo.seed_handoff(
        "2026-07-01-warn.md", "consumed",
        claimed_by="session-warn-test-abc",
    )
    cid = "state/handoffs/2026-07-01-warn.md"

    # Patch liveness: session is dead → handoff is terminal.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff must be archived before WARN marker check; "
        f"got archived_ids={archived_ids!r}"
    )

    # WARN marker file must exist.
    marker_path = boot_repo.root / "tasks" / "orphan-sweep-notes.md"
    assert marker_path.exists(), (
        "tasks/orphan-sweep-notes.md must be created after consumed handoff archival"
    )

    content = marker_path.read_text(encoding="utf-8")
    # The marker line must contain the archived handoff filename.
    assert "2026-07-01-warn.md" in content, (
        f"orphan-sweep-notes.md must reference archived filename; got content={content!r}"
    )
    # session-id (claimed_by) appears in the marker line.
    assert "session-warn-test-abc" in content, (
        f"orphan-sweep-notes.md must reference claimed_by session id; got content={content!r}"
    )
    # verb must be "archived" for an actually-archived handoff.
    assert "archived 2026-07-01-warn.md" in content, (
        f"WARN marker verb must be 'archived' for an acted candidate; got content={content!r}"
    )
    # unconditional disposition note (the flip that made this conditional is deleted).
    assert "no deployment_state change" in content, (
        f"WARN marker must carry the unconditional 'no deployment_state change' "
        f"disposition note; got content={content!r}"
    )
    assert "abandoned" not in content, (
        "WARN marker must never reference 'abandoned' (DR-084 stop-gap, 2026-07-22)"
    )


# ---------------------------------------------------------------------------
# (d2) _append_warn_marker dedup (2026-07-29): re-invoking with the same
# (handoff_filename, consumed_sid, verb) triple must not re-log an identical
# row — but a re-log after the file is rotated (replaced with fresh content)
# must still happen, since the dedup check reads only the file's CURRENT
# on-disk contents, never history.
# ---------------------------------------------------------------------------


def test_append_warn_marker_dedupes_identical_triple(tmp_path):
    _append_warn_marker(tmp_path, "2026-07-25-dup.md", "sid-abc", "no deployment_state change", verb="skipped")
    once = (tmp_path / "tasks" / "orphan-sweep-notes.md").read_text(encoding="utf-8")
    assert once.count("skipped 2026-07-25-dup.md") == 1

    # Same triple, even with a different disposition_note text — the dedup
    # key is (handoff_filename, consumed_sid, verb) only.
    _append_warn_marker(tmp_path, "2026-07-25-dup.md", "sid-abc", "a different note text", verb="skipped")
    twice = (tmp_path / "tasks" / "orphan-sweep-notes.md").read_text(encoding="utf-8")
    assert twice == once, (
        f"repeat invocation with the same (filename, sid, verb) triple must be a "
        f"no-op; got a changed file:\n{twice!r}"
    )
    assert twice.count("skipped 2026-07-25-dup.md") == 1


def test_append_warn_marker_distinguishes_verb_and_sid(tmp_path):
    _append_warn_marker(tmp_path, "2026-07-25-multi.md", "sid-1", "note", verb="skipped")
    _append_warn_marker(tmp_path, "2026-07-25-multi.md", "sid-1", "note", verb="archived")
    _append_warn_marker(tmp_path, "2026-07-25-multi.md", "sid-2", "note", verb="skipped")
    content = (tmp_path / "tasks" / "orphan-sweep-notes.md").read_text(encoding="utf-8")
    assert content.count("skipped 2026-07-25-multi.md (consumed_by=sid-1") == 1
    assert content.count("archived 2026-07-25-multi.md (consumed_by=sid-1") == 1
    assert content.count("skipped 2026-07-25-multi.md (consumed_by=sid-2") == 1


def test_append_warn_marker_relogs_after_rotation(tmp_path):
    """Dedup is against the file's CURRENT contents only — after /workday-start's
    rotation (simulated here by overwriting the file with fresh content), a
    re-log of the identical triple is correct and must still happen."""
    _append_warn_marker(tmp_path, "2026-07-25-rot.md", "sid-rot", "note", verb="skipped")
    marker_path = tmp_path / "tasks" / "orphan-sweep-notes.md"
    assert marker_path.read_text(encoding="utf-8").count("skipped 2026-07-25-rot.md") == 1

    # Simulate /workday-start Step 0.8's rotation: the live file is replaced
    # with fresh (empty-of-this-row) content.
    marker_path.write_text("# Orphan sweep notes\n\n(rotated)\n\n", encoding="utf-8")

    _append_warn_marker(tmp_path, "2026-07-25-rot.md", "sid-rot", "note", verb="skipped")
    post_rotation = marker_path.read_text(encoding="utf-8")
    assert post_rotation.count("skipped 2026-07-25-rot.md") == 1, (
        "a re-log after rotation must still happen — history-based dedup would "
        "wrongly suppress it forever"
    )


def test_append_warn_marker_read_failure_degrades_to_append(tmp_path, monkeypatch):
    """A read failure during the dedup check must fall through to appending
    (risking a duplicate row) rather than silently dropping the marker —
    this function is on the session-boot path and must never wedge a boot."""
    _append_warn_marker(tmp_path, "2026-07-25-unreadable.md", "sid-x", "note", verb="skipped")
    marker_path = tmp_path / "tasks" / "orphan-sweep-notes.md"
    assert marker_path.read_text(encoding="utf-8").count("skipped 2026-07-25-unreadable.md") == 1

    real_read_text = Path.read_text

    def _boom_read_text(self, *args, **kwargs):
        if self == marker_path:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom_read_text)
    _append_warn_marker(tmp_path, "2026-07-25-unreadable.md", "sid-x", "note", verb="skipped")

    monkeypatch.undo()
    content = marker_path.read_text(encoding="utf-8")
    assert content.count("skipped 2026-07-25-unreadable.md") == 2, (
        "a dedup-check read failure must degrade toward appending (a duplicate "
        "row), never toward silently dropping the marker"
    )


# ---------------------------------------------------------------------------
# (e) Recency floor: consumed_at within 30 min → skipped
# ---------------------------------------------------------------------------


def test_recency_floor_skips_recent_consumed_handoff(boot_repo):
    """Consumed handoff with claimed_at within 30 min → skipped, NOT archived.

    Bias-to-life guard: the consuming session is almost certainly still live even if
    resolve_live_session_ids returned False (heartbeat lag, session dir not yet present
    on this machine).  Mirrors the deleted session-init.sh (example-doctrine-repo 2f8b8450, 2026-07-16).
    """
    # claimed_at = 5 minutes ago (within the 30-min recency floor).
    now = datetime.now(tz=timezone.utc)
    recent = now - timedelta(minutes=5)
    claimed_at_iso = recent.strftime("%Y-%m-%dT%H:%M:%SZ")

    boot_repo.seed_handoff(
        "2026-07-01-recent.md", "consumed",
        extra_frontmatter=f"claimed_at: {claimed_at_iso}",
    )
    cid = "state/handoffs/2026-07-01-recent.md"

    # No claimed_by → liveness not called; no patch needed for liveness.
    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    # Source file must NOT be moved.
    assert boot_repo.path_exists(cid), (
        "recently consumed handoff (within 30-min floor) must NOT be moved"
    )

    # Must appear in consumed_handoffs.skipped with the recency floor reason.
    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert cid in skip_map, (
        f"recent handoff must appear in consumed_handoffs.skipped; "
        f"got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert "recency floor" in skip_map[cid], (
        f"skip reason must contain 'recency floor'; got reason={skip_map[cid]!r}"
    )

    # Must NOT appear in archived.
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, "recent handoff must NOT be in archived[]"

    assert boot_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (f) Idempotent replay: second run finds no new candidates, exit_code:0
# ---------------------------------------------------------------------------


def test_idempotent_replay_finds_nothing_new(boot_repo):
    """Second handler run after first archival finds no new candidates; exit_code:0.

    boot_sweep self-scans state/handoffs/ — once the handoff is git-mv'd to archive/,
    it no longer appears in the live-handoffs scan.  Second run is a clean no-op
    with exit_code:0 (DR-211 D2(i) commutative idempotency).
    """
    boot_repo.seed_handoff("2026-07-01-idem.md", "consumed")
    cid = "state/handoffs/2026-07-01-idem.md"

    # First run: archive the handoff.
    first = _run(_handler({}, repo_root=boot_repo.common_dir))
    assert first["exit_code"] == 0, f"first run must succeed; got {first!r}"
    first_archived = [a["id"] for a in first["consumed_handoffs"]["archived"]]
    assert cid in first_archived, (
        f"first run must archive the consumed handoff; got {first_archived!r}"
    )
    assert not boot_repo.path_exists(cid), "source must be gone after first run"

    head_after_first = boot_repo.git_head_sha()

    # Second run: source is gone from state/handoffs/ → nothing new to archive.
    second = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert second["exit_code"] == 0, (
        f"second (idempotent) run must return exit_code:0; got {second!r}"
    )
    assert second["consumed_handoffs"]["archived"] == [], (
        "second run must not archive anything new (idempotent)"
    )
    assert second["consumed_handoffs"]["failed"] == [], (
        "second run must not produce failures"
    )

    # HEAD must not have advanced (no new commit on idempotent replay).
    head_after_second = boot_repo.git_head_sha()
    assert head_after_second == head_after_first, (
        "idempotent replay must not create a new commit"
    )


# ---------------------------------------------------------------------------
# (g) Act-time terminality drift: claimed_by session goes live between preview/act
# ---------------------------------------------------------------------------


def test_act_time_terminality_drift_skip(boot_repo):
    """Drift: claimed_by session goes live between preview and act → skipped 're-live'.

    DR-211 D1 act-time re-verify: the per-family internal _handle_act_handoffs
    re-checks terminality for each candidate_id.  If the session became live after
    the preview, the handoff is skipped with 're-live'; source file is NOT moved;
    git status stays clean.
    """
    boot_repo.seed_handoff(
        "2026-07-01-drift.md", "consumed",
        claimed_by="session-drift-xyz",
    )
    cid = "state/handoffs/2026-07-01-drift.md"

    # side_effect list: first call (preview) → not live; second call (act) → live.
    # resolve_live_session_ids is called once per candidate in preview, once in act.
    # A third fallback value guards against StopIteration / RuntimeError if the handler
    # calls resolve_live_session_ids more than twice (e.g. future refactor, extra sweep).
    # Extend list beyond 2 so a 3rd call does not raise StopIteration / RuntimeError
    # if the handler calls resolve_live_session_ids more than twice (e.g. future refactor).
    with patch(_LIVE_SIDS_PATCH, side_effect=[
        frozenset(),                          # preview: session not live → candidate appears terminal
        frozenset({"session-drift-xyz"}),     # act re-verify: session became live → re-live skip
        frozenset({"session-drift-xyz"}),     # fallback: safe value for any additional call
    ]):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    # Source file must NOT have been moved (drift guard fired).
    assert boot_repo.path_exists(cid), (
        "drifted handoff (re-live at act time) must NOT be moved"
    )

    # Must appear in consumed_handoffs.skipped with 're-live' in the reason.
    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert cid in skip_map, (
        f"re-live handoff must appear in consumed_handoffs.skipped; "
        f"got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert "re-live" in skip_map[cid], (
        f"skip reason must contain 're-live'; got reason={skip_map[cid]!r}"
    )

    # Must NOT appear in archived.
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, (
        "re-live handoff must NOT be in consumed_handoffs.archived"
    )

    assert boot_repo.git_status_clean(), "git index must be clean after drift skip"


# ---------------------------------------------------------------------------
# (h) Partial failure → exit_code:2, git status clean
# ---------------------------------------------------------------------------


def test_partial_failure_returns_exit_code_2_clean_index(boot_repo):
    """Injected failure in memos sweep → exit_code:2; git status clean.

    DETERMINATE-PARTIAL: one or more per-item failures in any sweep produce
    exit_code:2 (not 1).  The git index must remain clean — no half-staged
    state (DR-211 D3 scoped-pathspec discipline ensures no orphan staging).
    """
    _injected_id = "cross-repo/inbox/injected-fail.md"
    _injected_failure = [{"id": _injected_id, "reason": "injected-test-failure"}]

    with patch(
        _MEMOS_INTERNAL_PATCH,
        new_callable=AsyncMock,
        return_value=([], [], _injected_failure),
    ):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 2, (
        f"partial failure must return exit_code:2; got {result!r}"
    )
    assert result["memos"]["failed"] == _injected_failure, (
        f"injected failure must appear in memos.failed; got {result['memos']['failed']!r}"
    )

    # Other sweeps must be unaffected (empty repo → no candidates elsewhere).
    assert result["consumed_handoffs"]["failed"] == []
    assert result["plans"]["failed"] == []
    assert result["shipped_handoffs"]["failed"] == []

    # Index must be clean despite partial failure — no orphan staging.
    assert boot_repo.git_status_clean(), (
        "git index must be clean after partial failure (no half-staged state)"
    )


# ---------------------------------------------------------------------------
# (i) Liveness guard: live claimed_by session → handoff excluded from candidates
# ---------------------------------------------------------------------------


def test_liveness_guard_excludes_live_claimed_by_session(boot_repo):
    """Consumed handoff whose claimed_by session is live is excluded from candidates.

    The consuming session is still active — archiving would abandon an active
    workstream handoff.  The liveness check in _is_terminal excludes it from the
    preview candidates, so it is never in archived[], skipped[], or failed[].
    Source file must remain intact.
    """
    boot_repo.seed_handoff(
        "2026-07-01-live.md", "consumed",
        claimed_by="live-session-abc",
    )
    cid = "state/handoffs/2026-07-01-live.md"

    # Patch: the consuming session is live.
    with patch(_LIVE_SIDS_PATCH, return_value=frozenset({"live-session-abc"})):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    # Source must be intact.
    assert boot_repo.path_exists(cid), (
        "handoff consumed by a live session must NOT be moved"
    )

    # Must NOT appear in archived (it was excluded at preview; not a candidate).
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, (
        "handoff with live claimed_by must be excluded from archived[]"
    )

    # Liveness-excluded handoffs are dropped at _handle_preview_handoffs (non-terminal
    # filter) — they never enter the candidate list for _handle_act_handoffs.  All three
    # consumed_handoffs sub-lists must be empty: the excluded handoff should appear in
    # none of archived[], skipped[], or failed[].
    # Assert all three consumed_handoffs lists are empty so a regression that accidentally
    # archives or silently drops the item is caught.
    assert result["consumed_handoffs"]["archived"] == [], (
        "no handoffs should be archived when only candidate is live-excluded"
    )
    assert result["consumed_handoffs"]["skipped"] == [], (
        "live-excluded handoff must not appear in skipped[] (filtered before candidacy)"
    )
    assert result["consumed_handoffs"]["failed"] == [], (
        "live-excluded handoff must not appear in failed[] (filtered before candidacy)"
    )

    # exit_code:0 — exclusion is not a failure.
    assert result["exit_code"] == 0, (
        f"liveness-excluded handoff must not raise exit_code; got {result!r}"
    )

    assert boot_repo.git_status_clean()


# ---------------------------------------------------------------------------
# (j) Claim-dir liveness (Finding 3, C1): dead lock no longer strands a boot-path
#     consumed handoff; a live lock still blocks archival.
# ---------------------------------------------------------------------------


_CS_CLAIM_HOLDER_LIVE_PATCH = (
    "coordinator_core.ops.fleet.archive_handoffs.cs_claim_holder_live"
)


def test_dead_claim_dir_lock_no_longer_strands_consumed_handoff(boot_repo):
    """A DEAD claim-dir lock must no longer strand a consumed handoff (Finding 3, C1).

    Mirrors the fleet-op assertion: cs_claim_holder_live is now the PRIMARY Check 4
    liveness key.  A claim dir on disk whose holder reads as not-live must not block
    archival — pre-C1 this handoff would have stranded because Check 4 had no way
    to consult claim-dir liveness (only claimed_by-in-live-session-ids).
    """
    boot_repo.seed_handoff("2026-07-14-dead-claim.md", "consumed")
    cid = "state/handoffs/2026-07-14-dead-claim.md"

    claim_dir = (
        boot_repo.common_dir
        / "coordinator-sessions"
        / "handoff-claims"
        / "2026-07-14-dead-claim.md"
    )
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=False):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"dead-holder claim-dir lock must not strand the handoff; "
        f"got archived={result['consumed_handoffs']['archived']!r}, "
        f"skipped={result['consumed_handoffs']['skipped']!r}"
    )
    assert not boot_repo.path_exists(cid), (
        "handoff must be git-mv'd out of state/handoffs/ once terminal"
    )


def test_live_claim_dir_lock_blocks_boot_path_archival(boot_repo):
    """A LIVE claim-dir lock still blocks archival on the boot path (AC3 safety)."""
    boot_repo.seed_handoff("2026-07-14-live-claim.md", "consumed")
    cid = "state/handoffs/2026-07-14-live-claim.md"

    claim_dir = (
        boot_repo.common_dir
        / "coordinator-sessions"
        / "handoff-claims"
        / "2026-07-14-live-claim.md"
    )
    claim_dir.mkdir(parents=True, exist_ok=True)

    with patch(_CS_CLAIM_HOLDER_LIVE_PATCH, return_value=True):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, (
        "live-holder claim-dir lock must block archival; "
        f"got archived={result['consumed_handoffs']['archived']!r}"
    )
    assert boot_repo.path_exists(cid), (
        "handoff with a live claim-dir holder must NOT be moved"
    )


# ---------------------------------------------------------------------------
# Two-repo split helpers and tests (AC3 / AC4 / AC5 / AC6 / AC7)
# ---------------------------------------------------------------------------


def _build_test_repo(root: Path, dirs: list) -> BootRepo:
    """Build a minimal git repo at root with the given directory skeleton.

    Used by two-repo tests to construct independent STATE repo and GIT_ROOT repo
    without depending on the boot_repo fixture (which builds only a single repo).
    Both repos get commit.gpgsign=false to allow setup commits; individual tests
    may flip gpgsign=true after setup (see test_two_repo_commit_succeeds_under_gpgsign_true).
    """
    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
        )

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main")
    _git("config", "user.email", "two-repo-test@claude-klabauter.test")
    _git("config", "user.name", "Two Repo Test")
    _git("config", "commit.gpgsign", "false")

    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / ".gitkeep").write_text("", encoding="utf-8")

    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return BootRepo(root)


# Minimal directory skeletons for two-repo layout.
# STATE repo: owns handoffs (consumed + shipped) and their archival destinations.
# GIT_ROOT repo: owns plans, memos, specs archival — NOT handoffs.
_STATE_DIRS = [
    "state/handoffs",
    "archive/handoffs",
    "state/bug-backlog",
    "archive/bug-backlog",
]

_GIT_ROOT_DIRS = [
    "docs/plans",
    "archive/specs",
    "cross-repo/inbox",
    "cross-repo/archive",
]


def _git_ls_files(repo_root: Path, rel_path: str) -> str:
    """Return `git ls-files <rel_path>` output (stripped) for the given repo root.

    Note: `git ls-files` reports files present in the index (staged or committed).
    This is sufficient for location assertions (proving a file is in STATE vs GIT_ROOT)
    but does NOT distinguish staged-but-uncommitted from fully committed.  Use
    `_git_rev_count` delta or `BootRepo.git_status_clean()` for committed-state
    assertions (e.g. verifying a metadata commit completed successfully).
    """
    return subprocess.run(
        ["git", "ls-files", rel_path],
        cwd=str(repo_root),
        capture_output=True,
    ).stdout.decode().strip()


def _git_rev_count(repo_root: Path) -> int:
    """Return total number of commits reachable from HEAD."""
    return int(subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        check=True,
    ).stdout.decode().strip())


def _seed_shipped_handoff_two_repo(state_repo: BootRepo, name: str, sha: str) -> str:
    """Write and commit a handoff with deployment_state:shipped + resolvable shipped_in SHA.

    Uses status:active (NOT consumed). Reachable by the dedicated shipped sweep
    (deployment_state:shipped + SHA-gate) always; ALSO reachable by the consumed
    sweep's terminal-deployment_state Branch B (archive_handoffs.py, 2026-07-13
    widening) since Branch B qualifies on deployment_state regardless of status —
    see test_two_repo_shipped_handoff_lands_in_state_repo's NOTE for the resulting
    either-sub-sweep-can-claim-it assertion shape.

    Mirrors coordinator_core/ops/fleet/tests/test_archive_shipped_handoffs.py::_seed_shipped_handoff.
    Returns the repo-relative candidate_id path.
    """
    path = state_repo.root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        f'title: "Test Shipped Handoff"\n'
        "status: active\n"
        "deployment_state: shipped\n"
        f"shipped_in: {sha}\n"
        "created: 2026-01-01\n"
        "---\n"
        "\n"
        "# Handoff\n"
        "\n"
        "Body.\n"
    )
    path.write_text(content, encoding="utf-8")
    state_repo._git("add", str(path))
    state_repo._git("commit", "-m", f"add shipped handoff {name}")
    return f"state/handoffs/{name}"


# ---------------------------------------------------------------------------
# (j) Two-repo: consumed handoff archived to STATE repo, not GIT_ROOT (AC5 a+b)
# ---------------------------------------------------------------------------


def test_two_repo_consumed_handoff_lands_in_state_repo(tmp_path):
    """Two-repo: consumed handoff archived to STATE repo, absent from GIT_ROOT (AC5 a+b).

    Location assertions (git -C <repo> ls-files):
      STATE: archive/handoffs/2026-07/... IS tracked.
      GIT_ROOT: archive/handoffs/2026-07/... is NOT tracked.
    If orphan-sweep-notes.md is produced it must be committed in GIT_ROOT only.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    state_repo.seed_handoff("2026-07-01-two-consumed.md", "consumed")
    cid = "state/handoffs/2026-07-01-two-consumed.md"
    dest_rel = "archive/handoffs/2026-07/2026-07-01-two-consumed.md"

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff must be archived; got archived_ids={archived_ids!r}"
    )

    # AC5a: archived file tracked in STATE repo (git -C <state> ls-files).
    state_ls = _git_ls_files(state_repo.root, dest_rel)
    assert dest_rel in state_ls, (
        f"archived handoff must be tracked in STATE repo (git ls-files); "
        f"got state_ls={state_ls!r}"
    )

    # AC5b: archived file NOT tracked in GIT_ROOT (git -C <gitroot> ls-files).
    gitroot_ls = _git_ls_files(git_root_repo.root, dest_rel)
    assert gitroot_ls == "", (
        f"archived handoff must NOT be tracked in GIT_ROOT; got gitroot_ls={gitroot_ls!r}"
    )

    # Orphan-sweep-notes.md write target is GIT_ROOT — must exist there and be tracked,
    # and must NOT appear in STATE.  The consumed handoff has no claimed_by, so
    # _append_warn_marker is called with sid="unknown" and WILL write the file.
    notes_rel = "tasks/orphan-sweep-notes.md"
    assert (git_root_repo.root / notes_rel).exists(), (
        "orphan-sweep-notes.md must be written to GIT_ROOT after consumed handoff archival"
    )
    assert _git_ls_files(git_root_repo.root, notes_rel), (
        "orphan-sweep-notes.md must be tracked/staged in GIT_ROOT"
    )
    assert _git_ls_files(state_repo.root, notes_rel) == "", (
        "orphan-sweep-notes.md must NOT be tracked in STATE repo"
    )


# ---------------------------------------------------------------------------
# (k) Two-repo: shipped handoff archived to STATE repo, not GIT_ROOT (AC5c, AC7)
# ---------------------------------------------------------------------------


def test_two_repo_shipped_handoff_lands_in_state_repo(tmp_path):
    """Two-repo: shipped handoff (deployment_state:shipped + reachable SHA) archived to STATE.

    Location assertion (git -C <repo> ls-files):
      STATE: archive/handoffs/... IS tracked.
      GIT_ROOT: archive/handoffs/... is NOT tracked.

    Mirrors _seed_shipped_handoff pattern from test_archive_shipped_handoffs.py.

    NOTE (2026-07-13 archive_handoffs Branch-B widening): _is_terminal's new
    terminal-deployment_state branch (deployment_state in {shipped, abandoned}
    regardless of status) means the CONSUMED sub-sweep (which runs before the
    SHIPPED sub-sweep in boot_sweep's fixed order) now ALSO recognizes this
    active+shipped+resolvable-SHA fixture as terminal and archives it first —
    the shipped sub-sweep then finds the source already gone and is a harmless
    no-op.  This is the documented, intended cross-op overlap (see
    archive_handoffs.py module docstring "Branch B does NOT subsume
    fleet.archive_shipped_handoffs"): the file is still correctly archived to
    the same STATE-repo destination either way, so this test asserts on the
    union of both sub-sweep archived[] lists rather than pinning to
    shipped_handoffs specifically (which predates the widening and is no
    longer the only path that can claim this fixture).
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # Use the initial skeleton commit SHA as a reachable SHA for shipped_in.
    initial_sha = state_repo.git_head_sha()
    cid = _seed_shipped_handoff_two_repo(state_repo, "2026-07-02-two-shipped.md", initial_sha)
    dest_rel = "archive/handoffs/2026-07/2026-07-02-two-shipped.md"

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    consumed_archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    shipped_archived_ids = [a["id"] for a in result["shipped_handoffs"]["archived"]]
    assert cid in consumed_archived_ids or cid in shipped_archived_ids, (
        "shipped handoff must be archived by EITHER the consumed sub-sweep "
        "(Branch B widening) or the dedicated shipped sub-sweep; "
        f"got consumed_archived_ids={consumed_archived_ids!r}, "
        f"shipped_archived_ids={shipped_archived_ids!r}"
    )

    # AC5c / AC7: shipped archival lands in STATE repo (git -C <state> ls-files).
    state_ls = _git_ls_files(state_repo.root, dest_rel)
    assert dest_rel in state_ls, (
        f"shipped handoff must be tracked in STATE repo after archival; "
        f"got state_ls={state_ls!r}"
    )

    # NOT in GIT_ROOT (git -C <gitroot> ls-files).
    gitroot_ls = _git_ls_files(git_root_repo.root, dest_rel)
    assert gitroot_ls == "", (
        f"shipped handoff must NOT be tracked in GIT_ROOT; got gitroot_ls={gitroot_ls!r}"
    )


# ---------------------------------------------------------------------------
# (l) Two-repo: enumeration scans STATE repo, not GIT_ROOT (AC5d)
# ---------------------------------------------------------------------------


def test_two_repo_enumeration_scans_state_repo(tmp_path):
    """Two-repo: consumed handoff placed ONLY in STATE repo is discovered and acted on.

    Proves that the boot sweep enumerates state_worktree (STATE repo) for handoffs,
    not GIT_ROOT.  GIT_ROOT has no state/handoffs/ at all; if enumeration scanned
    GIT_ROOT the handoff would never be found and archived[] would be empty.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    # GIT_ROOT has NO state/handoffs/ — handoffs live exclusively in STATE repo.
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    state_repo.seed_handoff("2026-07-01-state-only.md", "consumed")
    cid = "state/handoffs/2026-07-01-state-only.md"

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"

    # Handoff must be discovered + archived — proves enumeration scanned STATE repo.
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff in STATE repo must be discovered and archived; "
        f"got archived_ids={archived_ids!r}. "
        f"If empty, enumeration scanned GIT_ROOT instead of STATE repo."
    )

    # Source is gone from STATE repo.
    assert not (state_repo.root / cid).exists(), "source must be removed after archival"

    # Location: archive destination committed in STATE, absent from GIT_ROOT.
    dest_rel = "archive/handoffs/2026-07/2026-07-01-state-only.md"
    assert dest_rel in _git_ls_files(state_repo.root, dest_rel), (
        "archive destination must be tracked in STATE repo"
    )
    assert _git_ls_files(git_root_repo.root, dest_rel) == "", (
        "archive destination must NOT appear in GIT_ROOT"
    )


# ---------------------------------------------------------------------------
# (m) Two-repo: empty STATE partition produces no commit in STATE repo (AC6)
# ---------------------------------------------------------------------------


def test_two_repo_empty_state_partition_no_commit(tmp_path):
    """Two-repo: STATE repo with no consumed/shipped handoffs → no new commit in STATE.

    AC6: empty partition → no commit. Measured by git -C <state> rev-list --count HEAD
    before and after; count must be unchanged.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # No handoffs seeded in STATE repo — nothing to archive.
    state_commits_before = _git_rev_count(state_repo.root)
    gitroot_commits_before = _git_rev_count(git_root_repo.root)

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    assert result["consumed_handoffs"]["archived"] == [], "no consumed handoffs to archive"
    assert result["shipped_handoffs"]["archived"] == [], "no shipped handoffs to archive"

    # AC6: STATE repo must NOT have received a new commit.
    state_commits_after = _git_rev_count(state_repo.root)
    assert state_commits_after == state_commits_before, (
        f"STATE repo must get no new commit when no handoffs archived; "
        f"commit count changed from {state_commits_before} to {state_commits_after}"
    )

    # Symmetric: GIT_ROOT must also be unchanged (no plans, memos, or WARN markers seeded).
    gitroot_commits_after = _git_rev_count(git_root_repo.root)
    assert gitroot_commits_after == gitroot_commits_before, (
        f"GIT_ROOT repo must get no new commit on empty sweep; "
        f"commit count changed from {gitroot_commits_before} to {gitroot_commits_after}"
    )


# ---------------------------------------------------------------------------
# (n) Unified-state single commit regression: no state_common_dir → one metadata commit
# ---------------------------------------------------------------------------


def test_unified_state_single_commit_regression(boot_repo):
    """Unified-state: no state_common_dir → single metadata commit from _commit_consumed_metadata.

    AC4: unified-state collapse (state_common_dir absent) is byte-identical to pre-plan
    behavior.  The metadata commit for a consumed handoff sweep in unified mode is ONE
    commit (covering both archive-destination files and orphan-sweep-notes.md together),
    NOT two commits as in the two-repo split.  This asserts the delta from the boot_repo
    HEAD is exactly 2 (1 git-mv commit + 1 unified metadata commit) for a single consumed
    handoff.
    """
    boot_repo.seed_handoff("2026-07-01-unified.md", "consumed")

    commits_before = _git_rev_count(boot_repo.root)

    # No state_common_dir → unified-state mode.
    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    cid = "state/handoffs/2026-07-01-unified.md"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff must be archived in unified mode; got archived_ids={archived_ids!r}"
    )

    commits_after = _git_rev_count(boot_repo.root)
    delta = commits_after - commits_before

    # Unified mode: 2 commits (Commit A: git-mv; Commit B: unified metadata+notes).
    # Two-repo mode would produce 3 (A in STATE + B in STATE + C in GIT_ROOT).
    assert delta == 2, (
        f"unified-state must produce exactly 2 commits for one consumed handoff "
        f"(1 git-mv + 1 unified metadata); got delta={delta} "
        f"(commits {commits_before} → {commits_after})"
    )


# ---------------------------------------------------------------------------
# (o) Two-repo: archived commit succeeds under gpgsign=true (AC3)
# ---------------------------------------------------------------------------


def test_two_repo_commit_succeeds_under_gpgsign_true(tmp_path):
    """Two-repo: consumed archival commit in STATE repo succeeds even with gpgsign=true.

    AC3 / GAP-6: -c commit.gpgsign=false in every git commit invocation must
    neutralise the repo's gpgsign=true config.  Without the fix, git would attempt
    to sign using INVALID_KEY_ID_DOESNT_EXIST and fail, blocking the archival commit.

    Mirrors coordinator_core/ops/fleet/tests/test_fleet_common.py::test_archive_and_commit_gpgsign_override.
    Setup commits are done while gpgsign=false; the setting is flipped to true JUST
    before calling _handler, so the flip is the LAST git config write before the handler.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # Seed the consumed handoff while gpgsign is still false (setup commit succeeds).
    # Include a scope field pointing at a real committed path so
    # _stamp_shipped_in_besteff produces a real disk modification (the
    # shipped_in stamp) → main-index resync stages it → Commit B has actual
    # content to commit.  2026-07-22 (DR-084 stop-gap): deployment_state:
    # in_flight can no longer be used for this purpose — it now diverts the
    # candidate to the (b) skip-and-surface disposition instead of archival.
    # Without a real modification here, the private-index commit and disk are
    # identical (no stamp), Commit B gets "nothing to commit" regardless of
    # gpgsign, and git_status_clean() would pass vacuously even if
    # gpgsign=false were dropped from _commit_consumed_metadata.
    state_repo.seed_handoff(
        "2026-07-01-gpgsign.md", "consumed",
        extra_frontmatter="scope: archive/handoffs/.gitkeep",
    )
    cid = "state/handoffs/2026-07-01-gpgsign.md"
    dest_rel = "archive/handoffs/2026-07/2026-07-01-gpgsign.md"

    # Flip gpgsign=true on STATE repo just before calling the handler.
    # This simulates a machine with commit.gpgsign=true + an invalid signing key.
    state_repo._git("config", "commit.gpgsign", "true")
    state_repo._git("config", "user.signingkey", "INVALID_KEY_ID_DOESNT_EXIST")

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    # The handler must succeed — -c commit.gpgsign=false neutralises the signing req.
    assert result["exit_code"] == 0, (
        f"handler must succeed under gpgsign=true; got {result!r}. "
        "If this fails, -c commit.gpgsign=false is not being applied to the STATE repo commit."
    )
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff must be archived even with gpgsign=true; got archived_ids={archived_ids!r}"
    )

    # AC3: archived file IS committed in STATE repo (git -C <state> ls-files).
    state_ls = _git_ls_files(state_repo.root, dest_rel)
    assert dest_rel in state_ls, (
        f"archived handoff must be committed in STATE repo under gpgsign=true; "
        f"got state_ls={state_ls!r}"
    )

    # Commit B (metadata stamp in _commit_consumed_metadata) swallows errors silently.
    # If -c commit.gpgsign=false were removed from that code path only, the commit
    # would fail quietly: exit_code stays 0, archived_ids is non-empty (Commit A
    # succeeded), and git ls-files still shows the file (staged by main-index resync).
    # A clean working tree asserts that Commit B completed and left no dirty index.
    assert state_repo.git_status_clean(), (
        "STATE repo must have a clean index after the handler — Commit B "
        "(_commit_consumed_metadata) must not fail silently under gpgsign=true "
        "(AC3: -c commit.gpgsign=false required on every git commit invocation)"
    )


# ---------------------------------------------------------------------------
# (q) Two-repo: shipped_in stamp resolves scope_paths against GIT_ROOT (P1 regression)
# ---------------------------------------------------------------------------


def test_two_repo_shipped_in_stamp_uses_git_root(tmp_path):
    """Two-repo: shipped_in stamp runs git log in GIT_ROOT, not STATE repo.

    P1 regression gate: in two-repo mode, _stamp_shipped_in_besteff must pass
    git_root_worktree (not state_worktree) as the git log cwd.  Handoff scope_paths
    are GIT_ROOT-relative code paths; running git log in state_worktree returns an
    empty SHA for any GIT_ROOT-scoped handoff, silently leaving shipped_in absent
    (mirrors the deleted session-init.sh, example-doctrine-repo 2f8b8450, which ran git-log against GIT_ROOT).

    This test FAILS without fix #1 (the cwd correction): git log runs in STATE repo,
    finds no commit for the GIT_ROOT scope file, sha is empty, shipped_in is not
    stamped, and the assert "shipped_in:" in content assertion fails.

    Setup: a scope-target file is committed ONLY in GIT_ROOT (not STATE repo).
    After _handler, the archived handoff in STATE must carry shipped_in: <SHA> where
    SHA is the GIT_ROOT commit touching the scope file.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # Create and commit a scope-target file in GIT_ROOT only.
    # This file does NOT exist in STATE repo — git log in state_worktree would find nothing.
    scope_path = "docs/plans/2026-07-07-scope-target.md"
    scope_file = git_root_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: scope target\n---\n", encoding="utf-8")
    git_root_repo._git("add", str(scope_file))
    git_root_repo._git("commit", "-m", "add scope target for shipped_in regression test")

    # Record the GIT_ROOT commit SHA that touches the scope file.
    gitroot_sha = git_root_repo.git_head_sha()
    # Discriminating-power guard (2026-07-22): the assertion below distinguishes
    # "resolved GIT_ROOT's commit" from "resolved nothing" via the FIRST assertion
    # ("shipped_in:" in content — git log in the wrong repo finds no commit for a
    # GIT_ROOT-only path, so shipped_in stays absent) and distinguishes "resolved
    # the RIGHT commit" from "resolved a different, wrong one" via the second
    # (gitroot_sha[:8] comparison below). That second check is only meaningful if
    # STATE repo's own HEAD doesn't coincidentally share an 8-char sha prefix with
    # GIT_ROOT's — assert that up front so a future collision fails loud here,
    # at the fixture, rather than silently turning the assertion into a tautology.
    state_head_sha = state_repo.git_head_sha()
    assert state_head_sha[:8] != gitroot_sha[:8], (
        "fixture collision: STATE and GIT_ROOT HEAD shas share an 8-char prefix — "
        "this would make the shipped_in[:8] assertion below pass even if resolution "
        "ran in the wrong repo. Re-seed the fixture so the two repos' commits diverge."
    )

    # Seed a consumed handoff in STATE repo with scope pointing to the GIT_ROOT file.
    state_repo.seed_handoff(
        "2026-07-07-two-repo-shipped-in.md", "consumed",
        extra_frontmatter=f"scope: {scope_path}",
    )
    cid = "state/handoffs/2026-07-07-two-repo-shipped-in.md"

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff with scope must be archived; got archived_ids={archived_ids!r}"
    )

    # The archived handoff in STATE repo must carry shipped_in: <GIT_ROOT SHA>.
    # Without fix #1, git log ran in STATE repo, found no commit, and left shipped_in absent.
    dest = "archive/handoffs/2026-07/2026-07-07-two-repo-shipped-in.md"
    assert (state_repo.root / dest).exists(), (
        f"archived file must exist in STATE repo at {dest!r}"
    )
    content = (state_repo.root / dest).read_text(encoding="utf-8")

    assert "shipped_in:" in content, (
        f"archived file must have shipped_in: stamp (scope path has commit in GIT_ROOT); "
        f"got content head={content[:500]!r}. "
        f"If absent, git log ran in STATE repo instead of GIT_ROOT (fix #1 regression)."
    )
    # 8-char exact-equality, not a substring/40-char match: `stamp_shipped_in`'s
    # contract truncates to `resolved[:8]` ("sha": resolved[:8] in
    # archive_stamp.py) — that truncation IS the fix for the 2026-07-22 corpus
    # defect this test's sibling investigation found (the formerly-duplicated
    # _stamp_shipped_in_besteff wrote the untruncated `%H`, no `[:8]`, and 15/27
    # archived handoffs ended up with a 40-char shipped_in that stamp_shipped_in
    # can never produce). This assertion now ALSO serves as the boot_sweep-layer
    # truncation check — do not "helpfully" restore a 40-char comparison here;
    # that would silently reintroduce the exact defect this fixes. Exact-equality
    # on the parsed frontmatter value (not `in content`) because a bare substring
    # check is how the 40-vs-8-char divergence hid undetected in this file's other
    # assertions for as long as it did.
    from coordinator_core.frontmatter.primitives import (
        read_fm_field_unquoted,
        split_frontmatter,
    )

    split = split_frontmatter(content)
    assert split is not None
    stamped = read_fm_field_unquoted(split.fm_text, "shipped_in")
    assert stamped == gitroot_sha[:8], (
        f"shipped_in must equal the GIT_ROOT commit sha's 8-char truncation "
        f"{gitroot_sha[:8]!r}; got {stamped!r} "
        f"(full GIT_ROOT sha: {gitroot_sha!r})"
    )


# ---------------------------------------------------------------------------
# (p) Guard: state_common_dir pointing to worktree root rejected (exit_code:1)
# ---------------------------------------------------------------------------


def test_state_common_dir_worktree_root_rejected(tmp_path):
    """Guard: passing a worktree ROOT (not .git common dir) as state_common_dir → error.

    The handler validates that state_common_dir is a git common dir (has a HEAD file).
    Passing the worktree root (which has no HEAD file at the root level) must return
    exit_code:1 without performing any archival.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # Pass the WORKTREE ROOT (state_repo.root), not the .git common dir.
    # The worktree root has no HEAD file at the top level — guard must fire.
    result = _run(_handler(
        {"state_common_dir": str(state_repo.root)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 1, (
        f"worktree root passed as state_common_dir must return exit_code:1 (guard); "
        f"got exit_code={result.get('exit_code')!r}, result={result!r}"
    )


# ---------------------------------------------------------------------------
# (r) Two-repo: Commit C (GIT_ROOT orphan-notes) succeeds under GIT_ROOT gpgsign=true (AC3)
# ---------------------------------------------------------------------------


def test_two_repo_gitroot_notes_commit_succeeds_under_gpgsign_true(tmp_path):
    """Two-repo: orphan-notes Commit C in GIT_ROOT succeeds even with GIT_ROOT gpgsign=true.

    AC3 / GAP-6: -c commit.gpgsign=false in EVERY git commit invocation must neutralise
    gpgsign=true on the repo being committed to.  The existing
    test_two_repo_commit_succeeds_under_gpgsign_true covers Commit B (STATE repo).
    This test covers Commit C (_commit_consumed_metadata two-repo branch, ~:619) which
    commits tasks/orphan-sweep-notes.md into GIT_ROOT.

    Without -c commit.gpgsign=false on Commit C, git would try to sign using
    INVALID_KEY_ID_DOESNT_EXIST, fail, and leave orphan-sweep-notes.md staged-but-
    uncommitted in GIT_ROOT (dirty index) — undetected by any prior test.

    Setup: STATE repo stays gpgsign=false (this test targets Commit C, not Commit B).
    GIT_ROOT repo is flipped to gpgsign=true + invalid signing key just before
    calling _handler, so any signing attempt on GIT_ROOT will fail hard.

    Spec backlink: docs/plans/2026-07-06-strang-11-b8-session-init-op-absorption.md § C8b
    Negative-spec: does NOT flip STATE repo's gpgsign (that is covered by the existing
    test_two_repo_commit_succeeds_under_gpgsign_true test).
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    # Seed a plain consumed handoff (no deployment_state) — it archives normally,
    # ensuring 'acted' is non-empty → _append_warn_marker fires → orphan-sweep-notes.md
    # is written to GIT_ROOT → Commit C has content to commit. 2026-07-22 (DR-084
    # stop-gap): deployment_state: in_flight is no longer usable to force this shape —
    # it now diverts the candidate to the (b) skip-and-surface disposition instead.
    # All setup commits happen while gpgsign=false on both repos (flip comes later).
    state_repo.seed_handoff(
        "2026-07-01-gitroot-gpgsign.md", "consumed",
    )
    cid = "state/handoffs/2026-07-01-gitroot-gpgsign.md"

    # Flip gpgsign=true on GIT_ROOT ONLY, just before calling the handler.
    # STATE repo stays gpgsign=false — this test isolates Commit C (GIT_ROOT).
    git_root_repo._git("config", "commit.gpgsign", "true")
    git_root_repo._git("config", "user.signingkey", "INVALID_KEY_ID_DOESNT_EXIST")

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    # The handler must succeed — -c commit.gpgsign=false neutralises the signing req.
    assert result["exit_code"] == 0, (
        f"handler must succeed under GIT_ROOT gpgsign=true; got {result!r}. "
        "If this fails, -c commit.gpgsign=false is missing from the Commit C invocation "
        "in _commit_consumed_metadata (boot_sweep.py ~:619)."
    )

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid in archived_ids, (
        f"consumed handoff must be archived even with GIT_ROOT gpgsign=true; "
        f"got archived_ids={archived_ids!r}"
    )

    # AC3a: tasks/orphan-sweep-notes.md must be COMMITTED in GIT_ROOT.
    # git ls-files verifies it is tracked (staged or committed).
    notes_rel = "tasks/orphan-sweep-notes.md"
    gitroot_ls = _git_ls_files(git_root_repo.root, notes_rel)
    assert notes_rel in gitroot_ls, (
        f"orphan-sweep-notes.md must be tracked in GIT_ROOT after Commit C; "
        f"got gitroot_ls={gitroot_ls!r}"
    )

    # AC3b: GIT_ROOT working tree must be clean for the notes path — i.e. the file is
    # NOT left staged-but-uncommitted.  If -c commit.gpgsign=false were dropped from
    # Commit C, git would fail to sign, the commit would not land, and the notes file
    # would remain dirty (staged but not committed → git status --porcelain shows 'M ').
    gitroot_porcelain = subprocess.run(
        ["git", "status", "--porcelain", "--", notes_rel],
        cwd=str(git_root_repo.root),
        capture_output=True,
    ).stdout.decode().strip()
    assert gitroot_porcelain == "", (
        f"GIT_ROOT must have a clean index for {notes_rel!r} after Commit C — "
        f"a non-empty porcelain output means the notes commit failed silently under gpgsign=true; "
        f"got porcelain={gitroot_porcelain!r}. "
        "AC3: -c commit.gpgsign=false is required on every git commit invocation "
        "(boot_sweep.py ~:619)."
    )


# ---------------------------------------------------------------------------
# (s) Unintegrated-findings-reap composition: fifth family wired into boot_sweep
# ---------------------------------------------------------------------------


def _findings_reap_dates() -> tuple[str, str]:
    """Compute (aged_iso, young_iso) filename-date stems relative to *now*.

    Negative-spec: this test previously hardcoded "2026-06-01" (aged) and
    "2026-07-13" (young) directly in the seeded filenames. That is a time
    bomb — reap_unintegrated_findings._is_reapable ages a sidecar off the
    date IN ITS FILENAME against `datetime.now(timezone.utc).date()`, so a
    fixture date's distance from *today* determines the assertion's outcome,
    not the date itself. "2026-07-13" was 15 days old by 2026-07-28 (past
    _AGE_THRESHOLD_DAYS==14), silently flipping the "young, must be kept"
    fixture into "aged, gets reaped" and turning this test RED with no code
    change (detonated 2026-07-27). Deriving both dates relative to `today`,
    with a wide margin on both sides of the threshold, keeps the fixture's
    meaning ("aged" / "young") true no matter when the suite runs.
    """
    today = datetime.now(timezone.utc).date()
    aged = today - timedelta(days=_AGE_THRESHOLD_DAYS + 20)
    young = today - timedelta(days=1)
    return aged.isoformat(), young.isoformat()


def _seed_findings_sidecar(
    repo_root: Path,
    filename: str,
    marker_present: bool,
) -> Path:
    """Write (but do not commit — reap scope is untracked-until-git-rm) a findings sidecar.

    Findings sidecars under state/review-trail/findings/ are not required to be
    committed for the reap scan/act to observe them (rm_and_commit performs the
    plain `git rm`, which stages the deletion regardless of prior tracked state,
    matching fleet.reap_unintegrated_findings' own test-fixture idiom).
    """
    findings_dir = repo_root / "state" / "review-trail" / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    path = findings_dir / filename
    body = "# Finding\n\nSome finding body.\n"
    if marker_present:
        body += "\n## Integrator Dispositions\n\nDisposed.\n"
    path.write_text(body, encoding="utf-8")
    return path


def test_unintegrated_findings_reap_wired_into_boot_sweep(boot_repo):
    """Fifth family (unintegrated-findings-reap) is wired into session.boot_sweep.

    An aged (>14d, filename-dated) marker-absent sidecar is reaped on the first
    boot; a young marker-absent sidecar and an aged marker-present sidecar are
    both kept. A second boot is a no-op (idempotent — nothing left to reap).
    """
    aged_iso, young_iso = _findings_reap_dates()
    aged_absent = _seed_findings_sidecar(
        boot_repo.root, f"{aged_iso}-aged-unintegrated.md", marker_present=False,
    )
    young_absent = _seed_findings_sidecar(
        boot_repo.root, f"{young_iso}-young-unintegrated.md", marker_present=False,
    )
    aged_present = _seed_findings_sidecar(
        boot_repo.root, f"{aged_iso}-aged-integrated.md", marker_present=True,
    )

    boot_repo._git("add", "-A")
    boot_repo._git("commit", "-m", "seed findings sidecars")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    assert "unintegrated_findings" in result, (
        "result must have 'unintegrated_findings' key (fifth family)"
    )
    for field in ("reaped", "skipped", "failed"):
        assert field in result["unintegrated_findings"], (
            f"result['unintegrated_findings'] must have {field!r} key"
        )

    reaped_ids = [r["id"] for r in result["unintegrated_findings"]["reaped"]]
    aged_absent_rel = aged_absent.relative_to(boot_repo.root).as_posix()
    assert aged_absent_rel in reaped_ids, (
        f"aged marker-absent sidecar must be reaped; got reaped={reaped_ids!r}"
    )
    assert not aged_absent.exists(), (
        "aged marker-absent sidecar must be removed from disk after reap"
    )

    # Young marker-absent and aged marker-present sidecars must be KEPT.
    assert young_absent.exists(), (
        "young marker-absent sidecar must NOT be reaped (not yet aged)"
    )
    assert aged_present.exists(), (
        "aged marker-present sidecar must NOT be reaped (integrated — example-doctrine-repo's leg (a))"
    )

    assert result["unintegrated_findings"]["failed"] == [], (
        f"no failures expected; got {result['unintegrated_findings']['failed']!r}"
    )

    assert boot_repo.git_status_clean(), "git index must be clean after reap"

    # Second boot is a no-op: nothing left to reap.
    second = _run(_handler({}, repo_root=boot_repo.common_dir))
    assert second["exit_code"] == 0, f"second boot must succeed; got {second!r}"
    assert second["unintegrated_findings"]["reaped"] == [], (
        "second boot must find nothing new to reap (idempotent)"
    )


# ---------------------------------------------------------------------------
# (t) Two-repo: Sweep-5 findings-reap routes to STATE repo, not GIT_ROOT
# ---------------------------------------------------------------------------
# Review: code-reviewer — slice4 F1/C1. A single-repo test can't prove
# state_worktree routing (state_worktree == worktree when state_common_dir is
# absent — the two vars are the same object). Seed the findings sidecar ONLY
# in the STATE repo (absent from GIT_ROOT) so a regression that routes Sweep 5
# to worktree instead of state_worktree fails this test.


def test_two_repo_findings_reap_routes_to_state_repo(tmp_path):
    """Two-repo: aged marker-absent findings sidecar, seeded ONLY in the STATE
    repo, is reaped from STATE; GIT_ROOT (which has no state/review-trail/
    findings/ tree at all) is untouched.
    """
    state_repo = _build_test_repo(tmp_path / "state", _STATE_DIRS)
    git_root_repo = _build_test_repo(tmp_path / "gitroot", _GIT_ROOT_DIRS)

    aged_absent = _seed_findings_sidecar(
        state_repo.root, "2026-06-01-two-repo-aged.md", marker_present=False,
    )
    state_repo._git("add", "-A")
    state_repo._git("commit", "-m", "seed two-repo findings sidecar")

    result = _run(_handler(
        {"state_common_dir": str(state_repo.common_dir)},
        repo_root=git_root_repo.common_dir,
    ))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"

    reaped_ids = [r["id"] for r in result["unintegrated_findings"]["reaped"]]
    aged_absent_rel = aged_absent.relative_to(state_repo.root).as_posix()
    assert aged_absent_rel in reaped_ids, (
        f"aged marker-absent sidecar in STATE repo must be discovered and reaped "
        f"via state_worktree routing; got reaped={reaped_ids!r}. If empty, "
        f"Sweep 5 scanned GIT_ROOT instead of STATE repo."
    )
    assert not aged_absent.exists(), "reaped sidecar must be removed from STATE repo disk"

    # GIT_ROOT has no state/review-trail/findings/ tree at all — must be untouched
    # (no such dir was even created in _GIT_ROOT_DIRS).
    assert not (git_root_repo.root / "state" / "review-trail").exists(), (
        "GIT_ROOT must not have gained a state/review-trail/ tree"
    )


# ---------------------------------------------------------------------------
# (u) Heir branch (2026-07-22, F2/F3/I1, revised FIX-1) — session.boot_sweep's
# own heir-skip behavior: heir candidates surface via the structured "heir"
# wire field, get NO in_flight→abandoned flip, and instead get a
# DR-224/FIX-1-consistent terminal deployment_state rather than being
# silently left with no terminal stamp at all.
#
# FIX 1 (2026-07-22) revision: a heir candidate is now eligibility-gated on
# a resolvable shipped_in (archive_handoffs._is_terminal's H4 check) BEFORE
# the git-mv — a heir with no resolvable shipped_in is RETAINED, never
# archived, and _resolve_heir_deployment_state may only ever return
# "shipped" (the "no resolvable shipped_in → abandoned" DR-224 branch 2 path
# is DELETED — sweep-authored abandoned no longer exists, fleet-wide
# coordinator doctrine; reaper-scoped precedent, handoff-tracker-system.md:
# 536-540, which names the reaper, not this sweep).
#
# Spec backlinks:
#   - Finding 1 (P1): boot_sweep.py _append_warn_marker unconditional claim.
#   - Finding 2 (P2): zero test coverage for the heir-skip.
#   - I1 (EM finding): heir-archived records must never carry deployment_state:
#     in_flight.
#   - FIX 1 (abandoned retirement fleet-wide; reaper-scoped precedent,
#     handoff-tracker-system.md:536-540): sweep-authored abandoned no
#     longer exists; a heir with no ship evidence is RETAINED.
# ---------------------------------------------------------------------------


def test_heir_candidate_lands_in_heir_cids_and_skips_generic_flip(boot_repo):
    """(a)+(b) A heir candidate (with a resolvable shipped_in — FIX 1 H4) is
    detected via the structured "heir" field and does NOT go through the
    generic in_flight→abandoned flip — its archived frontmatter must not
    read deployment_state:in_flight (I1 bullet (d)) and must carry a
    coherent "shipped" terminal state instead of being left unstamped."""
    head_sha = boot_repo.git_head_sha()
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir.md", "consumed",
        extra_frontmatter=f"deployment_state: in_flight\nshipped_in: {head_sha}",
    )
    parent_rel = "state/handoffs/2026-07-22-boot-heir.md"

    # Live successor referencing the parent via predecessor — the heir edge.
    child_path = boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir-child.md"
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel in archived_ids, (
        f"heir-succeeded consumed+in_flight parent with a resolvable "
        f"shipped_in must be archived; got archived_ids={archived_ids!r}"
    )

    dest = "archive/handoffs/2026-07/2026-07-22-boot-heir.md"
    assert boot_repo.path_exists(dest), f"archived file must exist at {dest!r}"
    content = (boot_repo.root / dest).read_text(encoding="utf-8")

    # I1 bullet (d): NEVER in_flight on an archived record.
    assert "deployment_state: in_flight" not in content, (
        f"heir-archived record must never carry deployment_state:in_flight; "
        f"got content head={content[:400]!r}"
    )
    # FIX 1: a resolvable shipped_in must be stamped "shipped" — "abandoned"
    # is no longer a producible outcome of the heir branch.
    assert "deployment_state: shipped" in content, (
        f"heir-archived record with a resolvable shipped_in must be stamped "
        f"shipped; got content head={content[:400]!r}"
    )
    assert "deployment_state: abandoned" not in content, (
        f"the heir branch must never stamp abandoned (FIX 1, abandoned "
        f"retirement fleet-wide; reaper-scoped precedent, "
        f"handoff-tracker-system.md:536-540); got content head={content[:400]!r}"
    )

    assert boot_repo.git_status_clean()


def test_heir_no_resolvable_shipped_in_is_retained_not_archived(boot_repo):
    """FIX 1: a heir candidate with NO resolvable shipped_in (and no scope
    field for the pre-pass to stamp one) is RETAINED by session.boot_sweep —
    never archived, frontmatter untouched. This directly replaces the
    pre-FIX-1 "stamps abandoned" behavior this dispatch removes."""
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-retained.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
        # No scope field → the FIX-1 pre-pass' best-effort stamp finds nothing;
        # no shipped_in field either → H4 eligibility gate fails → RETAIN.
    )
    parent_rel = "state/handoffs/2026-07-22-boot-heir-retained.md"
    before_text = parent_path.read_text(encoding="utf-8")

    child_path = (
        boot_repo.root / "state" / "handoffs"
        / "2026-07-22-boot-heir-retained-child.md"
    )
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-retained child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"a heir candidate with no resolvable shipped_in must NOT be "
        f"archived (FIX 1 H4 gate); got archived_ids={archived_ids!r}"
    )
    assert not boot_repo.path_exists(
        "archive/handoffs/2026-07/2026-07-22-boot-heir-retained.md"
    ), "handoff must not have moved to archive/"
    assert boot_repo.path_exists(parent_rel), "source must remain in state/handoffs/"
    assert parent_path.read_text(encoding="utf-8") == before_text, (
        "a retained candidate's frontmatter must be untouched — no partial "
        "flip/stamp side effects on the RETAIN path"
    )


def test_non_heir_candidate_unaffected_by_heir_logic(boot_repo):
    """(c) A plain (non-heir) consumed+in_flight candidate, seeded alongside a
    heir candidate (with NO resolvable shipped_in, so it is RETAINED per
    FIX 1) in the same boot, still gets the ordinary (b) skip-and-surface
    disposition (DR-084 stop-gap, 2026-07-22) — proving heir_cids scoping is
    per-candidate and does not leak into the non-heir path. Note: FIX 1
    scopes the "no sweep-authored abandoned" rule to the heir branch only —
    the non-heir (b) skip-and-surface disposition is orthogonal, unrelated
    machinery this dispatch's C1 chunk introduces alongside it."""
    # Non-heir candidate: no referencing children at all.
    boot_repo.seed_handoff(
        "2026-07-22-boot-non-heir.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    non_heir_rel = "state/handoffs/2026-07-22-boot-non-heir.md"

    # Heir candidate in the same boot — deliberately NO shipped_in/scope, so
    # FIX 1's H4 gate retains it (proving the non-heir path is unaffected by
    # a co-present retained heir).
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir2.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    heir2_rel = "state/handoffs/2026-07-22-boot-heir2.md"
    child_path = boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir2-child.md"
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir2 child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert non_heir_rel not in archived_ids, (
        f"non-heir consumed+in_flight candidate must NOT be archived "
        f"(DR-084 stop-gap skip-and-surface); got archived_ids={archived_ids!r}"
    )
    assert heir2_rel not in archived_ids, (
        f"the co-present heir candidate with no resolvable shipped_in must "
        f"be RETAINED (FIX 1), not archived; got archived_ids={archived_ids!r}"
    )

    # Non-heir candidate: skip-and-surfaced via the ORDINARY (b) path — same
    # disk shape as test_non_heir_in_flight_skip_and_surfaced_not_archived —
    # proving the co-present RETAINED heir did not leak into this disposition.
    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert skip_map.get(non_heir_rel) == "awaiting-adjudication-dr084", (
        f"non-heir candidate must be skip-and-surfaced with the DR-084 reason "
        f"token; got skipped={result['consumed_handoffs']['skipped']!r}"
    )
    non_heir_content = (boot_repo.root / non_heir_rel).read_text(encoding="utf-8")
    assert "deployment_state: abandoned" not in non_heir_content, (
        "abandoned must NEVER be written by this sweep (DR-084 stop-gap)"
    )
    assert "deployment_state: in_flight" in non_heir_content, (
        "a skip-and-surface candidate's deployment_state:in_flight must remain intact"
    )

    assert boot_repo.git_status_clean()


def test_heir_warn_marker_reflects_heir_disposition(boot_repo):
    """(d) The WARN marker for a heir-archived candidate (with a resolvable
    shipped_in — FIX 1 H4) names the succession disposition, NOT the generic
    "deployment_state flipped to abandoned" claim (F1 fix) — the marker text
    must say WHY (succeeded by a live successor, stamped shipped), not just
    repeat the generic flip claim used for genuinely-orphaned non-heir
    candidates."""
    head_sha = boot_repo.git_head_sha()
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-marker.md", "consumed",
        claimed_by="session-heir-marker-abc",
        extra_frontmatter=f"deployment_state: in_flight\nshipped_in: {head_sha}",
    )
    child_path = boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir-marker-child.md"
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-marker child")

    with patch(_LIVE_SIDS_PATCH, return_value=frozenset()):
        result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert "state/handoffs/2026-07-22-boot-heir-marker.md" in archived_ids

    marker_path = boot_repo.root / "tasks" / "orphan-sweep-notes.md"
    assert marker_path.exists()
    content = marker_path.read_text(encoding="utf-8")

    assert "2026-07-22-boot-heir-marker.md" in content
    assert "succeeded by a live successor" in content, (
        f"heir-archived marker must name the succession disposition, not the "
        f"generic flip claim; got content={content!r}"
    )


def test_heir_shipped_in_resolvable_stamps_shipped(boot_repo):
    """I1 bullet 1: shipped_in present AND resolvable → deployment_state:shipped.

    Seeds a heir parent whose scope field points at a real committed file, so
    _stamp_shipped_in_besteff finds a resolvable SHA — the heir resolution
    must then stamp deployment_state:shipped, NOT abandoned."""
    scope_path = "docs/plans/2026-07-22-heir-scope-target.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add heir scope target")

    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-shipped.md", "consumed",
        extra_frontmatter=f"deployment_state: in_flight\nscope: {scope_path}",
    )
    child_path = boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir-shipped-child.md"
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-shipped child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert "state/handoffs/2026-07-22-boot-heir-shipped.md" in archived_ids

    dest = "archive/handoffs/2026-07/2026-07-22-boot-heir-shipped.md"
    content = (boot_repo.root / dest).read_text(encoding="utf-8")

    assert "shipped_in:" in content, (
        f"resolvable scope commit must be stamped as shipped_in; "
        f"got content head={content[:500]!r}"
    )
    assert "deployment_state: shipped" in content, (
        f"heir with a resolvable shipped_in must be stamped "
        f"deployment_state:shipped (DR-224 branch 1); "
        f"got content head={content[:500]!r}"
    )
    # I1: NEVER abandoned + a populated shipped_in together.
    assert "deployment_state: abandoned" not in content, (
        f"a resolvable-shipped_in heir must NEVER also carry "
        f"deployment_state:abandoned (DR-224 pathology fingerprint); "
        f"got content head={content[:500]!r}"
    )
    assert "deployment_state: in_flight" not in content


def test_heir_no_resolvable_shipped_in_stamps_abandoned_never_both(boot_repo):
    """FIX 1 revision (2026-07-22): the "no resolvable shipped_in → abandoned"
    DR-224 branch 2 path is DELETED (sweep-authored abandoned no longer
    exists, fleet-wide coordinator doctrine; reaper-scoped precedent,
    handoff-tracker-system.md:536-540). A heir candidate
    with no resolvable shipped_in and no scope field for the pre-pass to
    stamp one is now RETAINED, not archived, and carries no shipped_in or
    abandoned stamp at all — this test name is retained (not renamed) for
    git-blame continuity with the pre-FIX-1 test it replaces."""
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-abandoned.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
        # No scope field → _stamp_shipped_in_besteff finds nothing to stamp.
    )
    parent_rel = "state/handoffs/2026-07-22-boot-heir-abandoned.md"
    before_text = parent_path.read_text(encoding="utf-8")
    child_path = boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir-abandoned-child.md"
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-abandoned child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"a heir candidate with no resolvable shipped_in must NOT be "
        f"archived — abandoned is no longer sweep-authorable (FIX 1); "
        f"got archived_ids={archived_ids!r}"
    )
    assert boot_repo.path_exists(parent_rel), "source must remain in state/handoffs/"
    assert parent_path.read_text(encoding="utf-8") == before_text, (
        "a RETAINED candidate must not gain a shipped_in or deployment_state "
        "stamp — never abandoned, never a partial shipped_in write"
    )


def test_heir_pre_pass_skips_h3_promoter_owned_spinoff_roadmap(boot_repo):
    """Finding 1 (P1, 2026-07-22 review): the FIX-1 pre-preview stamp pass
    must mirror archive_handoffs._is_terminal's H3 exclusion (kind ==
    "spinoff-roadmap" AND deliverable_id truthy), not just H1+H2. A
    promoter-owned spinoff-roadmap node with a resolvable scope commit and a
    live succession child (heir-classified, H1+H2 satisfied) must be (a)
    RETAINED — never archived — and (b) left with byte-identical frontmatter
    after the sweep, including no shipped_in write from the pre-pass. Before
    this fix, the pre-pass stamped shipped_in on this exact shape before
    _is_terminal's H3 check retained it, leaving the working tree dirty on a
    record this sweep does not own (H3 belongs to
    promote-shipped-in-flight-stubs.py's separate deliverable-spine join)."""
    scope_path = "docs/plans/2026-07-22-h3-scope-target.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: h3 scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add h3 scope target")

    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-h3.md", "consumed",
        extra_frontmatter=(
            "deployment_state: in_flight\n"
            "kind: spinoff-roadmap\n"
            "deliverable_id: DEL-2026-07-22-001\n"
            f"scope: {scope_path}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-22-boot-heir-h3.md"
    before_text = parent_path.read_text(encoding="utf-8")

    child_path = (
        boot_repo.root / "state" / "handoffs" / "2026-07-22-boot-heir-h3-child.md"
    )
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-h3 child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"H3 promoter-owned spinoff-roadmap+deliverable_id node must be "
        f"RETAINED, not archived; got archived_ids={archived_ids!r}"
    )
    assert not boot_repo.path_exists(
        "archive/handoffs/2026-07/2026-07-22-boot-heir-h3.md"
    ), "H3-excluded handoff must not have moved to archive/"
    assert boot_repo.path_exists(parent_rel), "source must remain in state/handoffs/"
    assert parent_path.read_text(encoding="utf-8") == before_text, (
        "the FIX-1 pre-pass must skip H3 promoter-owned nodes entirely — "
        "frontmatter (including absence of shipped_in) must be byte-identical "
        "to before the sweep ran, even though scope resolves to a real commit"
    )
    assert boot_repo.git_status_clean(), (
        "an H3-excluded record must never leave the working tree dirty"
    )


def test_heir_pre_pass_skips_h3_promoter_owned_roadmap_baton_canonical_kind(boot_repo):
    """C4 anti-regression (baton-kind-vocabulary migration): the SAME H3
    pre-pass exclusion as
    test_heir_pre_pass_skips_h3_promoter_owned_spinoff_roadmap above, but
    using the CANONICAL post-migration `kind: roadmap-baton` spelling
    instead of the retired `spinoff-roadmap` one — this is the live defect
    the migration exposed: migrated live records now carry `roadmap-baton`,
    and a literal `kind == "spinoff-roadmap"` comparison would silently
    stop retaining them. Proves `canonical_kind()` de-aliasing at this call
    site covers the canonical spelling, not only the retired one."""
    scope_path = "docs/plans/2026-07-29-h3-scope-target-canonical.md"
    scope_file = boot_repo.root / scope_path
    scope_file.parent.mkdir(parents=True, exist_ok=True)
    scope_file.write_text("---\ntitle: h3 scope target\n---\n", encoding="utf-8")
    boot_repo._git("add", str(scope_file))
    boot_repo._git("commit", "-m", "add h3 scope target (canonical kind)")

    parent_path = boot_repo.seed_handoff(
        "2026-07-29-boot-heir-h3-canonical.md", "consumed",
        extra_frontmatter=(
            "deployment_state: in_flight\n"
            "kind: roadmap-baton\n"
            "deliverable_id: DEL-2026-07-29-001\n"
            f"scope: {scope_path}"
        ),
    )
    parent_rel = "state/handoffs/2026-07-29-boot-heir-h3-canonical.md"
    before_text = parent_path.read_text(encoding="utf-8")

    child_path = (
        boot_repo.root / "state" / "handoffs" / "2026-07-29-boot-heir-h3-canonical-child.md"
    )
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-h3-canonical child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert parent_rel not in archived_ids, (
        f"H3 promoter-owned roadmap-baton+deliverable_id node must be "
        f"RETAINED, not archived; got archived_ids={archived_ids!r}"
    )
    assert not boot_repo.path_exists(
        "archive/handoffs/2026-07/2026-07-29-boot-heir-h3-canonical.md"
    ), "H3-excluded handoff must not have moved to archive/"
    assert boot_repo.path_exists(parent_rel), "source must remain in state/handoffs/"
    assert parent_path.read_text(encoding="utf-8") == before_text, (
        "the FIX-1 pre-pass must skip H3 promoter-owned nodes entirely — "
        "frontmatter (including absence of shipped_in) must be byte-identical "
        "to before the sweep ran, even though scope resolves to a real commit"
    )
    assert boot_repo.git_status_clean(), (
        "an H3-excluded record must never leave the working tree dirty"
    )


def test_heir_never_archives_with_in_flight_deployment_state(boot_repo):
    """I1 bullet 4: an archived record must NEVER read deployment_state:in_flight.

    FIX 1 revision: since a heir may now only ever resolve to "shipped"
    (never "abandoned"), this test seeds a resolvable shipped_in so the
    candidate is actually archived, then proves the archived record never
    reads in_flight — the direct regression test for the production
    incident: 4 real handoffs were archived by the heir branch (before this
    fix) still reading deployment_state:in_flight because the flip was
    unconditionally suppressed with no substitute terminal stamp."""
    head_sha = boot_repo.git_head_sha()
    parent_path = boot_repo.seed_handoff(
        "2026-07-22-boot-heir-never-inflight.md", "consumed",
        extra_frontmatter=f"deployment_state: in_flight\nshipped_in: {head_sha}",
    )
    child_path = (
        boot_repo.root / "state" / "handoffs"
        / "2026-07-22-boot-heir-never-inflight-child.md"
    )
    child_path.write_text(
        "---\n"
        'title: "Child"\n'
        "status: active\n"
        f"predecessor: {parent_path}\n"
        "created: 2026-01-01\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    boot_repo._git("add", str(child_path))
    boot_repo._git("commit", "-m", "add heir-never-inflight child")

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert "state/handoffs/2026-07-22-boot-heir-never-inflight.md" in archived_ids

    dest = "archive/handoffs/2026-07/2026-07-22-boot-heir-never-inflight.md"
    content = (boot_repo.root / dest).read_text(encoding="utf-8")
    assert "deployment_state: in_flight" not in content, (
        f"archived heir record must never read deployment_state:in_flight "
        f"(I1 production-evidence regression); got content head={content[:500]!r}"
    )
    # Must have a coherent terminal deployment_state — never left unstamped,
    # and (FIX 1) can only ever be "shipped" now.
    assert "deployment_state: shipped" in content, (
        f"archived heir record must carry deployment_state:shipped — "
        f"'abandoned' is no longer producible by the heir branch (FIX 1); "
        f"got content head={content[:500]!r}"
    )
    assert "deployment_state: abandoned" not in content

    assert result["unintegrated_findings"]["failed"] == []


def test_resolve_heir_deployment_state_never_returns_abandoned(boot_repo):
    """Direct unit test (FIX 1): _resolve_heir_deployment_state may ONLY ever
    return "shipped" — never "abandoned". Exercises BOTH the normal path (a
    resolvable shipped_in already present) AND the invariant-violation path
    (no resolvable shipped_in reachable at all, simulating a caller that
    bypassed the H4 eligibility gate) — in both cases the function must not
    produce "abandoned"; the fallback-to-abandoned branch is deleted, not
    merely made unreachable."""
    from coordinator_core.ops.session.boot_sweep import _resolve_heir_deployment_state

    # Normal path: a resolvable shipped_in already present.
    head_sha = boot_repo.git_head_sha()
    resolvable_path = boot_repo.seed_handoff(
        "2026-07-22-resolve-heir-normal.md", "consumed",
        extra_frontmatter=f"shipped_in: {head_sha}",
    )
    state = _run(_resolve_heir_deployment_state(resolvable_path, boot_repo.root))
    assert state == "shipped", f"expected 'shipped' for a resolvable shipped_in; got {state!r}"
    assert state != "abandoned"

    # Invariant-violation path: no scope, no shipped_in, nothing resolvable —
    # the function must still never produce "abandoned".
    unresolvable_path = boot_repo.seed_handoff(
        "2026-07-22-resolve-heir-invariant-violation.md", "consumed",
    )
    state2 = _run(_resolve_heir_deployment_state(unresolvable_path, boot_repo.root))
    assert state2 != "abandoned", (
        f"_resolve_heir_deployment_state must NEVER return 'abandoned' — "
        f"abandoned retirement is fleet-wide coordinator doctrine "
        f"(reaper-scoped precedent, handoff-tracker-system.md:536-540) "
        f"forbids sweep-authored abandoned; got {state2!r}"
    )
    assert state2 == "shipped"


# ---------------------------------------------------------------------------
# (v) DR-084 stop-gap (2026-07-22, C1): no code path in boot_sweep.py writes
#     "abandoned" anywhere — the flip is DELETED, not merely bypassed.
# ---------------------------------------------------------------------------


def test_no_flip_regex_or_symbol_remains_in_boot_sweep_module():
    """Grep-level assertion: the deleted in_flight→abandoned flip mechanism
    (the _DS_IN_FLIGHT_RE regex and the _flip_deployment_state function) is
    absent from coordinator_core/ops/session/boot_sweep.py's source — not
    merely unreachable/dead code. Distinguishes "deleted" from "bypassed":
    a bypassed-but-present flip could be silently re-wired by a future edit;
    a deleted one cannot.

    Scoped to the *mechanism* (regex object name, function name, and the
    literal `.sub(` write of "abandoned"), not to bare mentions of the word
    "abandoned" — comments, docstrings, and negative-spec prose legitimately
    reference the deleted behavior for historical/RAG-bait context (see
    module docstring bullet (b) and the DR-084 negative-spec paragraph).
    """
    import coordinator_core.ops.session.boot_sweep as boot_sweep_mod

    module_source = Path(boot_sweep_mod.__file__).read_text(encoding="utf-8")

    assert "_DS_IN_FLIGHT_RE" not in module_source, (
        "_DS_IN_FLIGHT_RE (the in_flight→abandoned flip regex) must be "
        "DELETED from boot_sweep.py, not merely unreferenced"
    )
    assert "def _flip_deployment_state" not in module_source, (
        "_flip_deployment_state (the in_flight→abandoned flip function) "
        "must be DELETED from boot_sweep.py, not merely unreachable"
    )
    # No regex .sub(...) call writing the literal string "abandoned" —
    # the mechanism-level write, not a bare mention of the word.
    assert '.sub(r"\\1abandoned' not in module_source, (
        "no .sub(...) call writing the literal 'abandoned' replacement may "
        "remain in boot_sweep.py — this was the flip's write mechanism"
    )


def test_non_heir_in_flight_never_produces_abandoned_end_to_end(boot_repo):
    """Behavioral (not just grep-level) assertion: across an end-to-end run
    with BOTH a non-heir in_flight candidate (skip-and-surfaced) and an
    ordinary archived candidate present in the same sweep, "abandoned" never
    appears anywhere on disk — not in state/handoffs/, not in archive/handoffs/,
    not in tasks/orphan-sweep-notes.md.
    """
    boot_repo.seed_handoff(
        "2026-07-22-e2e-in-flight.md", "consumed",
        extra_frontmatter="deployment_state: in_flight",
    )
    boot_repo.seed_handoff(
        "2026-07-22-e2e-plain.md", "consumed",
    )

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] == 0, f"expected exit_code:0; got {result!r}"
    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert "state/handoffs/2026-07-22-e2e-plain.md" in archived_ids
    assert "state/handoffs/2026-07-22-e2e-in-flight.md" not in archived_ids

    skip_map = {s["id"]: s["reason"] for s in result["consumed_handoffs"]["skipped"]}
    assert skip_map.get("state/handoffs/2026-07-22-e2e-in-flight.md") == (
        "awaiting-adjudication-dr084"
    )

    # Sweep every file this sweep could plausibly have written to.
    for rel in (
        "state/handoffs/2026-07-22-e2e-in-flight.md",
        "archive/handoffs/2026-07/2026-07-22-e2e-plain.md",
        "tasks/orphan-sweep-notes.md",
    ):
        p = boot_repo.root / rel
        assert p.exists(), f"expected {rel!r} to exist after the sweep"
        content = p.read_text(encoding="utf-8")
        assert "abandoned" not in content, (
            f"{rel!r} must never contain 'abandoned' (DR-084 stop-gap, "
            f"2026-07-22); got content={content!r}"
        )


# ---------------------------------------------------------------------------
# (k) Handoff-scan-failure (2026-07-22 follow-up): boot_sweep now opts in to
#     _collect_all_handoff_paths' scan_errors out-param (fleet/archive_handoffs.py)
#     and wraps its own heir pre-stamp collect_live_handoff_paths call in
#     try/except OSError — an unreadable state/handoffs/ or archive/handoffs/
#     subtree must degrade safe (WARNING + zero archival for the affected
#     boot), never crash, and never silently misclassify a succession child
#     hiding in the unreadable subtree as childless.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_state_handoffs_skips_heir_prestamp_boot_does_not_crash(boot_repo, caplog):
    """Unreadable state/handoffs/ → heir pre-stamp pass skipped, WARNING surfaced,
    boot completes without raising.

    Seeds a consumed handoff while state/handoffs/ is still readable (so the file
    genuinely exists — this is not a vacuously-empty repo), then chmods the
    directory unreadable before invoking the handler. collect_live_handoff_paths
    raises OSError building the caller's dag_index (fleet/archive_handoffs.py's
    scan_errors out-param); the resulting dag_incomplete short-circuits the heir
    pre-stamp pass before its own (separately-guarded) re-scan ever runs — either
    way, the fix must not let the OSError propagate to the IPC catch-all.

    Sweep 3 (shipped-handoffs, archive_shipped_handoffs._scan_shipped) hits this
    exact same unreadable directory via its own collect_live_handoff_paths call —
    fixed to degrade safe (2026-07-22 follow-up) rather than crash, so no
    isolation patch is needed here any more; this test now exercises the real
    end-to-end boot path across both sweeps.
    """
    boot_repo.seed_handoff("2026-07-22-unreadable-live.md", "consumed")
    cid = "state/handoffs/2026-07-22-unreadable-live.md"

    state_handoffs_dir = boot_repo.root / "state" / "handoffs"
    original_mode = state_handoffs_dir.stat().st_mode
    os.chmod(state_handoffs_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            result = _run(_handler({}, repo_root=boot_repo.common_dir))
    finally:
        os.chmod(state_handoffs_dir, original_mode)

    # Boot must not crash — reaching this line at all is part of the assertion,
    # but also pin the envelope shape: no per-item failures, not a setup error.
    assert result["exit_code"] in (0, 2), f"boot must not raise; got {result!r}"

    # The signal must actually fire — never vacuously empty. Distinguish from a
    # genuinely-empty repo (test_all_four_sweeps_result_shape) by asserting the
    # warning is present AND the seeded handoff was neither archived nor skipped
    # for an unrelated reason (it never got the chance — the scan itself failed).
    assert result["warnings"], (
        f"expected a non-empty warnings list on an unreadable state/handoffs/; "
        f"got {result!r}"
    )
    assert any(
        w.get("scope") == "heir_pre_stamp" for w in result["warnings"]
    ), f"expected a heir_pre_stamp-scoped warning; got warnings={result['warnings']!r}"

    assert result["consumed_handoffs"]["archived"] == [], (
        "nothing can be archived while the live-handoffs scan itself is failing"
    )
    assert cid not in {
        s["id"] for s in result["consumed_handoffs"]["skipped"]
    }, "the seeded handoff never became a candidate — the scan failed before candidacy"

    assert any(
        "dag_index scan incomplete" in r.message or "cannot scan" in r.message
        for r in caplog.records
    ), (
        "expected a logged WARNING naming the scan failure; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_archive_subtree_parent_not_archived_or_reclassified(boot_repo, caplog):
    """Unreadable archive/handoffs/YYYY-MM/ subtree containing a succession child
    → the otherwise-archivable parent handoff is NOT archived/reclassified;
    a structured warning is surfaced.

    Mirrors archive_handoffs.py's test_unreadable_archived_subtree_dry_run_
    returns_zero_candidates: an unreadable archived subtree could be hiding a
    live successor, so dag_incomplete must fail the WHOLE consumed-handoffs
    sweep closed for this boot — not just the heir-classified subset.
    """
    # Genuinely archivable control: same shape as test_deployment_state_absent_
    # field_not_modified, which DOES archive when the scan is clean — proves
    # this handoff is not being skipped for some unrelated reason.
    parent_path = boot_repo.seed_handoff("2026-07-22-scan-gap-parent.md", "consumed")
    cid = "state/handoffs/2026-07-22-scan-gap-parent.md"

    archive_month_dir = boot_repo.root / "archive" / "handoffs" / "2026-07"
    archive_month_dir.mkdir(parents=True, exist_ok=True)
    (archive_month_dir / "2026-07-01-succession-child.md").write_text(
        "unused", encoding="utf-8"
    )

    original_mode = archive_month_dir.stat().st_mode
    os.chmod(archive_month_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING):
            result = _run(_handler({}, repo_root=boot_repo.common_dir))
    finally:
        os.chmod(archive_month_dir, original_mode)

    assert result["exit_code"] in (0, 2), f"boot must not raise; got {result!r}"

    archived_ids = [a["id"] for a in result["consumed_handoffs"]["archived"]]
    assert cid not in archived_ids, (
        f"parent must NOT be archived while an archive/handoffs/ subtree is "
        f"unreadable (could be hiding a succession child); got archived={archived_ids!r}"
    )
    assert parent_path.exists(), (
        "parent handoff must remain in place — never moved on an incomplete dag scan"
    )

    # Signal must fire, not vacuously pass: warnings non-empty AND distinct from
    # the plain-empty-repo shape.
    assert result["warnings"], (
        f"expected a non-empty warnings list on an unreadable archived subtree; "
        f"got {result!r}"
    )
    assert any(
        w.get("scope") == "heir_pre_stamp" for w in result["warnings"]
    ), f"expected a heir_pre_stamp-scoped warning; got warnings={result['warnings']!r}"

    assert any(str(archive_month_dir) in r.message for r in caplog.records), (
        "expected a logged WARNING naming the unreadable archived handoff dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# sat-01b C5 — observed-set-fold sweep (Sweep 7)
#
# AC10: fold runs from session.boot_sweep on a machine that has never folded,
#       with no git hook involved (this whole test file drives the sweep via
#       a direct _handler() call — a JSON-RPC/command-type invocation, never
#       a git hook of any kind).
# AC10b: a repo whose state/sovereign-tracker/ does NOT exist stays untouched
#        — no directory minted, no marker appended, no fold attempted.
# Degrade-safety: a fold that raises does not take the boot sweep down.
# ---------------------------------------------------------------------------


# EVENTS_DIR_RELPATH imported via the fold_observed_set OP module (which
# legitimately re-exports it) rather than the underlying store module
# directly — a direct import here would add a third referencer of the
# underlying store module's own dotted import path to coordinator_core/ops/,
# which the sat-01 substrate's DR-241-affirmed allowlist forbids (exactly two
# referencers are sanctioned: fold_observed_set.py itself and this file).
from coordinator_core.ops.tracker.fold_observed_set import (  # noqa: E402
    EVENTS_DIR_RELPATH,
    EVENTS_SHARD_GLOB,
)


def _observed_set_shard_files(repo_root: Path):
    """Every per-machine shard file currently present under the store dir."""
    return sorted((repo_root / EVENTS_DIR_RELPATH).glob(EVENTS_SHARD_GLOB))


def test_observed_set_fold_absent_store_mints_nothing(boot_repo):
    """AC10b: no state/sovereign-tracker/ directory in this repo -> the sweep
    creates nothing (no directory minted, no marker appended, no fold
    attempted), and reports ran:False, reason:"no_store" in its own envelope."""
    store_dir = boot_repo.root / EVENTS_DIR_RELPATH
    assert not store_dir.exists()

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] in (0, 2)
    assert not store_dir.exists(), (
        "session.boot_sweep must NEVER mint state/sovereign-tracker/ in a repo "
        "that never opted in — DEC-11 confinement / opt-in-by-existence gate"
    )
    assert result["observed_set_fold"] == {
        "ran": False,
        "reason": "no_store",
        "marker": None,
    }


def test_observed_set_fold_runs_on_never_folded_machine(boot_repo):
    """AC10: a repo that HAS opted in (the sovereign tracker store directory
    exists) and has never folded before gets exactly one marker appended by
    the boot sweep — driven entirely through the command-type _handler()
    call, no git hook."""
    store_dir = boot_repo.root / EVENTS_DIR_RELPATH
    store_dir.mkdir(parents=True, exist_ok=True)

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] in (0, 2)
    fold = result["observed_set_fold"]
    assert fold["ran"] is True
    assert fold["reason"] == "appended"
    assert fold["marker"]["kind"] == "observed_set_fold"
    assert len(_observed_set_shard_files(boot_repo.root)) == 1


def test_observed_set_fold_second_boot_is_idempotent_no_op(boot_repo):
    """Two consecutive boot sweeps with an unchanged peer set leave exactly ONE
    marker on the own shard — the second call reports reason:"no_op"."""
    store_dir = boot_repo.root / EVENTS_DIR_RELPATH
    store_dir.mkdir(parents=True, exist_ok=True)

    first = _run(_handler({}, repo_root=boot_repo.common_dir))
    assert first["observed_set_fold"]["reason"] == "appended"

    second = _run(_handler({}, repo_root=boot_repo.common_dir))
    assert second["observed_set_fold"] == {
        "ran": True,
        "reason": "no_op",
        "marker": None,
    }

    shards = _observed_set_shard_files(boot_repo.root)
    assert len(shards) == 1
    lines = [l for l in shards[0].read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 1, f"expected exactly ONE marker line, got {len(lines)}: {lines}"


def test_observed_set_fold_failure_is_degrade_safe(boot_repo, monkeypatch):
    """A fold that raises must never take the boot sweep down — it is caught,
    surfaced as a top-level warning, and the sweep still returns normally."""
    store_dir = boot_repo.root / EVENTS_DIR_RELPATH
    store_dir.mkdir(parents=True, exist_ok=True)

    def _boom(*, repo_root):
        raise RuntimeError("injected observed-set-fold failure")

    monkeypatch.setattr(
        "coordinator_core.ops.session.boot_sweep.run_fold_observed_set", _boom
    )

    result = _run(_handler({}, repo_root=boot_repo.common_dir))

    assert result["exit_code"] in (0, 2), f"boot sweep must not raise; got {result!r}"
    assert result["observed_set_fold"] == {
        "ran": False,
        "reason": "error",
        "marker": None,
    }
    assert any(
        w.get("scope") == "observed_set_fold" for w in result["warnings"]
    ), f"expected an observed_set_fold-scoped warning; got {result['warnings']!r}"
