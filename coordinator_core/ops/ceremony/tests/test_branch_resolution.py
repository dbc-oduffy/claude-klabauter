"""
coordinator_core.ops.ceremony.tests.test_branch_resolution

Tests for the ceremony.branch_resolution branch pre-resolver op.

Coverage:
  (a) missing_sid               — exit_code=1 when 'sid' param absent
  (b) missing_repo_root         — exit_code=1 when repo_root is None
  (c) disposition_single_session — session-shape.json pickup.happened=False → single-session
  (d) disposition_chain_terminal — session-shape.json pickup.happened=True → chain-terminal
  (e) disposition_grep_fallback_terminal — absent session-shape.json → grep fallback → chain-terminal
  (f) disposition_grep_fallback_single   — absent session-shape.json, no consumed_by → single-session
  (g) disposition_absent_pickup_field    — session-shape.json present but 'pickup' field absent → grep fallback
  (h) governing_plan_evidence           — D node step_2a carries candidate plan list as evidence
  (i) nature_classification             — D node step_2.6.4 carries nature from classify_nature
  (j) open_memos_scan                   — D node step_2.65a enumerates open memos
  (k) open_memos_zero                   — step_2.65a fires with open_count=0 when inbox empty
  (l) j_questions_emitted               — all 8 J-nodes present in resolved ctx
  (m) f_slots_emitted                   — all 3 F-nodes present in resolved ctx
  (n) b_node_pre_resolved_evidence      — B1 node has pre_resolved_evidence with brightline keys
  (o) b_node_generic_keys               — B1 uses generic pre_resolved_evidence/em_adjudication, not dispatch_plan/adjudication
  (p) phase1_receipt_written            — emit produces the session-keyed shard state/ceremony/wsc/<sid-short>-...json (phase-1, C3)
  (q) receipt_schema_valid              — emitted receipt passes receipt_schema.validate()
  (r) receipt_graceful_absent           — reading a missing receipt returns NOT_YET_RUN_SENTINEL
  (s) idempotency_guard_no_prior        — idempotency_guard_fired=False when no prior entry
  (t) idempotency_guard_fired           — idempotency_guard_fired=True when prior completion entry exists
  (u) scope_mode_from_session_shape     — scope_mode read from session-shape.json plan.scope_mode
  (v) scope_mode_param_override         — scope_mode param overrides session-shape.json value
  (w) result_exit_code_zero             — happy-path returns exit_code=0
  (x) result_fields_present             — result carries disposition/scope_mode/nature/j_questions/f_slots/b_pre_resolved
  (y) completeness_checklist_branch     — chain-terminal with completeness_checklist in frontmatter → completeness_present=True
  (z) loe_path_branch                   — loe_path resolves chain-terminal to aggregate-chain-loe.sh
  (T1) applicable_node_ids              — single-session omits STEP_2_7/2.75/2.9c, ordered;
                                          chain-terminal includes them, matching full ledger
  (T2) consumed_handoff_archive_scan    — chain-terminal via session-shape with missing
                                          handoff AND the predecessor handoff living in
                                          archive/handoffs/ → ctx.consumed_handoff populated +
                                          predecessor carried
  (T3) consumed_handoff_anchored_match  — a handoff whose BODY prose mentions a sibling sid is
                                          NOT matched by _find_consumed_handoff (anchored,
                                          frontmatter-only)
  (T4) step_1b_step_2_4b_emit_as_d_nodes — Option B F->D reclassification (memo 2026-07-08):
                                          STEP_1B/STEP_2_4B emit as D-nodes with non-empty
                                          resolving_op + disk_first evidence, not the prior F-slots
  (T5) foreign_repo_bleed_absolute      — pickup.handoff ABSOLUTE, escapes worktree_root
                                          to a foreign repo's real handoff (consumed_by: sid
                                          included) — must NOT bind; falls through to in-repo
                                          predecessor.  Defect A regression.
  (T6) foreign_repo_bleed_traversal     — pickup.handoff ../ traversal, escapes
                                          worktree_root to a sibling dir's real handoff —
                                          must NOT bind; reverts to single-session.  Defect A
                                          regression.
  (T7) resolved_state_sid               — resolved_state["sid"] equals the input sid.
                                          Defect B regression (resolved_state.sid = null).
  (T8) session_shape_handoff_path_absent_phantom — in-repo-shaped but ABSENT
                                          handoff + real consumed_by:sid sibling →
                                          falls through to sid-grep. Memo 2026-07-11
                                          regression.
  (C2-a..i) STEP_2_65C/2_65B flip-half + bulk-eligibility coverage (C2) — see the
                                          dedicated section comment above those tests.
  (T9) detector_b_production_path       — Detector B (git-provenance
                                          chain-terminal detection) exercised
                                          end-to-end via resolve_session_branches itself, not
                                          just detect_git_provenance_consumed
                                          directly or wsc_tail.py's separate
                                          lightweight wiring: no
                                          session-shape.json, no live
                                          consumed_by: stamp anywhere, an
                                          archived handoff added by a
                                          Session-Id: <sid>-trailered commit →
                                          chain-terminal with consumed_handoff
                                          populated (positive), and a
                                          malformed B-candidate surfacing
                                          detector_b_warnings on the
                                          WSC_DISPOSITION branch's evidence
                                          without flipping disposition
                                          (rejected-hit). Review: code-reviewer
                                          2026-07-22 slice1 finding #4.

Spec backlink:
  coordinator_core/ops/ceremony/branch_resolution.py
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C2.2
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.node-map.md § Branch Inventory
  docs/plans/2026-07-08-wsc-commit-op-defects.md § Bug-1(i)
  docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.ops.ceremony.branch_resolution import (
    SCOPING_METHOD_AMBIGUOUS,
    SCOPING_METHOD_STARTED_AT_RANGE,
    SCOPING_METHOD_TRAILER,
    ScopingVerdict,
    _detect_foreign_commits,
    _range_is_contiguous_suffix,
    # Review: code-reviewer — _read_session_shape was imported but never used; removed.
    # _session_added_plans is kept — exercised by the direct unit tests below.
    _read_started_at,
    _resolve_in_repo,
    _sanitize_consumed_handoffs,
    _scan_session_scratch,
    _session_added_plans,
    _started_at_candidate_range,
    _trailer_reliable,
    analyze_session_scoping,
    resolve_named_memo_dispositions,
)
from coordinator_core.ops.ceremony.receipt_emit import is_not_yet_run, read_receipt
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _run(coro) -> Any:
    """Run an async handler coroutine synchronously."""
    return asyncio.run(coro)


def _wire_origin_pushing_only_current_head(root: Path, tmp_path: Path) -> None:
    """Add an ``origin`` remote and push ONLY the caller's current HEAD.

    Detector B (``resolver.detect_git_provenance_consumed``) resolves
    ``merge-base origin/main HEAD`` before scanning; the shared ``git_repo``
    fixture wires no remote at all, so Detector B's merge-base would be
    unresolvable for any test that needs it. Call this once, immediately
    after the fixture's initial commit and BEFORE seeding the commit under
    test — pushing a later commit would make ``origin/main == HEAD``,
    collapsing ``merge-base..HEAD`` to empty and hiding the very commit
    Detector B is supposed to see (the gotcha
    ``test_resolver_git_provenance.py``'s ``_commit_unpushed`` docstring
    documents and this helper mirrors).
    """
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=str(root), capture_output=True, check=True,
    )
    push = subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(root), capture_output=True, text=True,
    )
    assert push.returncode == 0, push.stderr


class WscResolveRepo:
    """Lightweight git repo fixture for wsc_resolve tests.

    Provides helpers to:
      - seed session-shape.json at the correct path
      - seed consumed handoffs with consumed_by: <sid>
      - seed open memos in cross-repo/inbox/
      - seed completion archive entries
      - create coordinator.local.md with project_subtypes
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # Create the required subdirectories
        (root / ".git" / "coordinator-sessions").mkdir(parents=True, exist_ok=True)
        (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
        (root / "cross-repo" / "inbox").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        (root / "archive" / "completed").mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
        """Git common dir (= <root>/.git for a standard repo)."""
        return self.root / ".git"

    def seed_session_shape(
        self,
        sid: str,
        *,
        pickup_happened: bool = False,
        handoff: str = "",
        scope_mode: str = "architecture",
        schema_version: int = 1,
        extra_fields: Optional[dict[str, Any]] = None,
    ) -> Path:
        """Write a session-shape.json for the given sid."""
        sid_dir = self.common_dir / "coordinator-sessions" / sid
        sid_dir.mkdir(parents=True, exist_ok=True)
        shape: dict[str, Any] = {
            "schema_version": schema_version,
            "pickup": {
                "happened": pickup_happened,
                "handoff": handoff,
            },
            "actioned_memos": [],
            "plan": {"scope_mode": scope_mode},
            "magnitude": "",
        }
        if extra_fields:
            shape.update(extra_fields)
        path = sid_dir / "session-shape.json"
        path.write_text(json.dumps(shape, indent=2), encoding="utf-8")
        return path

    def seed_handoff(
        self,
        name: str,
        *,
        consumed_by: Optional[str] = None,
        chain: Optional[str] = None,
        completeness_checklist: bool = False,
        status: str = "open",
    ) -> Path:
        """Write a state/handoffs/<name>.md with optional claimed_by.

        Kwarg name kept as ``consumed_by`` (call-site DSL, unchanged) but the
        frontmatter key written is ``claimed_by`` — DR-084 P4 (C7) retired
        ``consumed_by`` corpus-wide and ``coverage._get_handoff_consumed_by``
        is now a single-name ``claimed_by`` read.
        """
        path = self.root / "state" / "handoffs" / name
        lines = [
            f'title: "Test Handoff"',
            f"created: 2026-01-01",
            f"branch: work/test/2026-01-01",
            f"status: {status}",
        ]
        if consumed_by:
            lines.append(f"claimed_by: {consumed_by}")
        if chain:
            lines.append(f"chain: {chain}")
        if completeness_checklist:
            lines.append("completeness_checklist: true")
        fm = "\n".join(lines)
        path.write_text(f"---\n{fm}\n---\n\n# Handoff Body\n", encoding="utf-8")
        return path

    def seed_open_memo(
        self,
        name: str,
        *,
        status: str = "open",
        title: str = "Test Memo",
        kind: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> Path:
        """Write a cross-repo/inbox/<name>.md open memo.

        kind / in_reply_to (C2, AC4a): optional frontmatter fields exercised by
        the bulk-eligibility tests — omitted entirely when None (matching how
        real memos predate these fields).
        """
        path = self.root / "cross-repo" / "inbox" / name
        lines = [f'title: "{title}"', f"status: {status}"]
        if kind is not None:
            lines.append(f"kind: {kind}")
        if in_reply_to is not None:
            lines.append(f"in_reply_to: {in_reply_to}")
        fm = "\n".join(lines)
        path.write_text(f"---\n{fm}\n---\n\nMemo body.\n", encoding="utf-8")
        return path

    def seed_archived_memo(self, name: str) -> Path:
        """Write a cross-repo/archive/<name>.md — a resolved/terminal memo."""
        path = self.root / "cross-repo" / "archive" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\ntitle: "Archived Memo"\nstatus: actioned\n---\n\nMemo body.\n',
            encoding="utf-8",
        )
        return path

    def seed_plan(self, name: str) -> Path:
        """Write a docs/plans/<name>.md plan doc."""
        path = self.root / "docs" / "plans" / name
        path.write_text(f"# Plan\n\nPlan content.\n", encoding="utf-8")
        return path

    def seed_completion_entry(self, name: str, chain: str) -> Path:
        """Write an archive/completed/<name>.md with chain field."""
        path = self.root / "archive" / "completed" / name
        path.write_text(
            f"---\ntitle: Test\nchain: {chain}\n---\n\nEntry.\n",
            encoding="utf-8",
        )
        return path

    def seed_coordinator_local(self, project_subtypes: list[str]) -> Path:
        """Write coordinator.local.md with project_subtypes."""
        path = self.root / "coordinator.local.md"
        subtypes_yaml = "\n".join(f"  - {st}" for st in project_subtypes)
        path.write_text(
            f"---\nproject_subtypes:\n{subtypes_yaml}\n---\n",
            encoding="utf-8",
        )
        return path

    def seed_started_at(self, sid: str, value: str) -> Path:
        """Write a started_at file for the given sid."""
        sid_dir = self.common_dir / "coordinator-sessions" / sid
        sid_dir.mkdir(parents=True, exist_ok=True)
        path = sid_dir / "started_at"
        path.write_text(value + "\n", encoding="utf-8")
        return path

    def seed_tasks_file(
        self,
        rel_path: str,
        *,
        content: str = "scratch\n",
        mtime: Optional[float] = None,
    ) -> Path:
        """Write a file under tasks/<rel_path> with an optional explicit mtime.

        If mtime is provided, sets the file's atime and mtime via os.utime().
        """
        path = self.root / "tasks" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def seed_completeness_mirror(self, sid: str, content: str) -> Path:
        """Write a completeness-checklist.yaml mirror for the given sid."""
        tasks_dir = self.root / "state" / "tasks" / sid
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "completeness-checklist.yaml"
        path.write_text(content, encoding="utf-8")
        return path

@pytest.fixture
def repo(tmp_path) -> WscResolveRepo:
    """Provide a WscResolveRepo for tests."""
    return WscResolveRepo(tmp_path / "repo")


@pytest.fixture
def git_repo(tmp_path) -> WscResolveRepo:
    """Provide a WscResolveRepo whose root is a live git repository.

    Review: code-reviewer F13 — extracted from the duplicated git-init boilerplate
    in tests (e), (f), (g).  Each of those tests required a real git repo for the
    grep-based L1b fallback to work; the setup was copy-pasted three times.

    Yields a WscResolveRepo after running git init, configuring identity, and making
    an initial commit so git grep has a tree to search against.
    """
    repo = WscResolveRepo(tmp_path / "repo")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo.root),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo.root),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo.root),
                   capture_output=True, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo.root),
                   capture_output=True, check=True)
    # Ensure at least one file so the initial commit does not fail on an empty tree
    (repo.root / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo.root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo.root),
                   capture_output=True, check=True)
    return repo


# ---------------------------------------------------------------------------
# (a) missing_sid
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (b) missing_repo_root
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (c) disposition_single_session
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (d) disposition_chain_terminal
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (e) disposition_grep_fallback_terminal — absent session-shape.json → grep finds consumed_by
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (f) disposition_grep_fallback_single — absent session-shape.json, no consumed_by
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# (g) disposition_absent_pickup_field — session-shape.json present but 'pickup' absent
# ---------------------------------------------------------------------------


    # Review: code-reviewer — removed tautological assertion
    # `disposition in ("single-session","chain-terminal")` which was always True
    # regardless of the specific value; the == "single-session" check above is the real gate.




# ---------------------------------------------------------------------------
# (h) governing_plan_evidence
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (i) nature_classification
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (j) open_memos_scan
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (k) open_memos_zero
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (l) j_questions_emitted — all 8 J-nodes present
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (m) f_slots_emitted — all 3 F-nodes present
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (n) b_node_pre_resolved_evidence
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (o) b_node_generic_keys
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (p) phase1_receipt_written
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# (q) receipt_schema_valid
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (r) receipt_graceful_absent
# ---------------------------------------------------------------------------


def test_receipt_graceful_absent(tmp_path):
    """Reading a missing receipt returns NOT_YET_RUN_SENTINEL."""
    missing_path = tmp_path / "state" / "ceremony" / "wsc-receipt.json"
    receipt = read_receipt(missing_path)
    assert is_not_yet_run(receipt), "Absent receipt must return NOT_YET_RUN_SENTINEL"


# ---------------------------------------------------------------------------
# (s) idempotency_guard_no_prior
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (t) idempotency_guard_fired
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (u) scope_mode_from_session_shape
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (v) scope_mode_param_override
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (w) result_exit_code_zero
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (x) result_fields_present
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (y) completeness_checklist_branch
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# (z) loe_path_branch
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# C1 — STEP_2_6_3 chain-slug case-a: _read_started_at unit tests
# ---------------------------------------------------------------------------


def test_read_started_at_present(tmp_path):
    """_read_started_at returns trimmed ISO string when file is present."""
    sid = "sess-sa-unit-001"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("2026-07-06T12:00:25Z\n", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result == "2026-07-06T12:00:25Z"


def test_read_started_at_absent(tmp_path):
    """_read_started_at returns None when the started_at file is absent."""
    sid = "sess-sa-unit-002"
    common_dir = tmp_path / ".git"
    (common_dir / "coordinator-sessions" / sid).mkdir(parents=True)

    result = _read_started_at(common_dir, sid)
    assert result is None


def test_read_started_at_empty(tmp_path):
    """_read_started_at returns None when the started_at file is empty."""
    sid = "sess-sa-unit-003"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result is None


# ---------------------------------------------------------------------------
# C1 — STEP_2_6_3: integration tests (positive / negative / absence)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# C3 — STEP_2_96: _read_completeness_mirror unit tests
# ---------------------------------------------------------------------------

_V1_MIRROR_OPEN = """\
schema: completeness-checklist-mirror-v1
items:
  - title: "Review code"
    state: open
    updated_at: 2026-07-06T10:00:00Z
  - title: "Write tests"
    state: open
    updated_at: 2026-07-06T10:01:00Z
  - title: "Deploy"
    state: done
    updated_at: 2026-07-06T11:00:00Z
"""

_V1_MIRROR_ALL_DONE = """\
schema: completeness-checklist-mirror-v1
items:
  - title: "Step A"
    state: done
    updated_at: 2026-07-06T10:00:00Z
  - title: "Step B"
    state: done
    updated_at: 2026-07-06T10:01:00Z
"""

_V2_MIRROR_MISMATCH = """\
schema: completeness-checklist-mirror-v2
items:
  - title: "Review code"
    state: open
"""

_NO_SCHEMA_CONTENT = """\
items:
  - title: "Orphan"
    state: open
"""














# ---------------------------------------------------------------------------
# C3 — STEP_2_96: integration tests (presence / absent / mismatch)
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Review: code-reviewer — STEP_1B/STEP_2_4B D-node emission integration test
# Finding 1 (P2): reclassified F→D under Option B (memo 2026-07-08); locks the
# D-node type + resolving_op + disk_first evidence shape this diff exists to fix.
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Review: code-reviewer — new direct unit tests for _session_added_plans
# Findings 6, 7, 8, 9 (P2/nit): cover --diff-filter=A ADDED-not-MODIFIED
# semantic, --since temporal boundary, graceful-empty on non-zero git exit,
# and dedup logic — none of these were exercised by the existing positive tests.
# ---------------------------------------------------------------------------




def test_session_added_plans_since_boundary_excludes_old_commit(git_repo):
    """--since=<started_at>: commits before started_at are excluded by the temporal filter.

    Calls _session_added_plans directly with started_at set to a recent time
    AFTER a session-tagged commit was made (commit uses --date to set an old
    author date, and GIT_COMMITTER_DATE to match — ensuring git sees the commit
    as old for both author and committer date checks).  The function must return []
    even though the commit passes --grep and --diff-filter=A.

    The existing positive test uses started_at="2000-01-01" making --since a
    no-op; this test is the complementary gate.

    Review: code-reviewer Finding 7 — real --since temporal boundary test.
    """
    import os

    sid = "sess-sap-since-001"
    git_repo.seed_session_shape(sid)

    # Commit a plan with both author and committer date set to the far past
    plan = git_repo.root / "docs" / "plans" / "past-plan.md"
    plan.write_text("# Past Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    env = os.environ.copy()
    env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00+0000"
    subprocess.run(
        ["git", "commit", "-m", f"add past plan\n\nSession-Id: {sid}",
         "--date", "2000-01-01T00:00:00+0000"],
        cwd=str(git_repo.root), capture_output=True, check=True, env=env,
    )

    # Call _session_added_plans directly with started_at after the commit date
    added = _session_added_plans(git_repo.root, sid, "2026-01-01T00:00:00Z")

    assert added == [], (
        f"Commit with date 2000-01-01 must be excluded by --since=2026-01-01; got {added}"
    )


def test_session_added_plans_graceful_on_nonzero_git(tmp_path):
    """_session_added_plans returns [] gracefully when git returns non-zero.

    Calls _session_added_plans against a non-git directory so git log exits 128.
    Asserts the function returns [] without raising.

    Review: code-reviewer Finding 8 — graceful-empty on non-zero git exit (P2).
    """
    sid = "sess-sap-fail-001"
    non_git_dir = tmp_path / "not-a-git-repo"
    non_git_dir.mkdir()

    # Call with a non-git worktree_root and a valid started_at string;
    # git log will exit 128 (not a git repository) → graceful [] return.
    result = _session_added_plans(non_git_dir, sid, "2026-01-01T00:00:00Z")
    assert result == [], (
        f"_session_added_plans must return [] on git failure; got {result}"
    )




# ---------------------------------------------------------------------------
# Review: code-reviewer — new unit tests for _read_completeness_mirror
# Finding 10 (nit): quoted-scalar (state: "open") and column-0 (state: open)
# anchor cases — the regex must not match either.
# ---------------------------------------------------------------------------


_V1_MIRROR_QUOTED_SCALAR = """\
schema: completeness-checklist-mirror-v1
items:
  - title: "Quoted item"
    state: "open"
    updated_at: 2026-07-06T10:00:00Z
"""

_V1_MIRROR_COLUMN_ZERO = """\
schema: completeness-checklist-mirror-v1
state: open
items:
  - title: "Real item"
    state: done
    updated_at: 2026-07-06T10:00:00Z
"""






# ---------------------------------------------------------------------------
# Review: code-reviewer — new unit test for _read_started_at
# Finding 11 (nit): whitespace-only file should be treated as absent/None.
# ---------------------------------------------------------------------------


def test_read_started_at_whitespace_only(tmp_path):
    """_read_started_at returns None when the started_at file contains only whitespace.

    A file written by a buggy producer as '   \\n   ' is distinct from an empty
    file but must be treated as absent.

    Review: code-reviewer Finding 11 — whitespace-only file → None.
    """
    sid = "sess-sa-ws-001"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("   \n   ", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result is None, (
        f"Whitespace-only started_at must be treated as absent (None); got {result!r}"
    )


# ---------------------------------------------------------------------------
# C1 — STEP_2_67A: _scan_session_scratch unit tests
# ---------------------------------------------------------------------------


def test_scan_session_scratch_graceful_negative_no_started_at(git_repo):
    """_scan_session_scratch returns None when started_at is None (graceful-negative)."""
    result = _scan_session_scratch(git_repo.root, None)
    assert result is None, (
        f"Absent started_at must return None (graceful-negative); got {result!r}"
    )


def test_scan_session_scratch_no_tasks_dir(git_repo):
    """_scan_session_scratch returns 0 when tasks/ dir is absent (started_at present)."""
    started_at = "2026-07-06T12:00:00Z"
    # No tasks/ dir created
    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, f"No tasks/ dir → count=0; got {result!r}"


@contextmanager
def _tz_forced_to_us_pacific():
    """Force the process-local timezone to America/Los_Angeles for the
    duration of the `with` block, to catch a naive-local-time regression
    that would only misbehave under a non-UTC offset (the F2 defect class).

    `time.tzset()` is POSIX-only (AttributeError: module 'time' has no
    attribute 'tzset' on win32) — there is no cross-platform way to change
    the interpreter's notion of "local timezone" at runtime without it, and
    setting the `TZ` env var alone has no effect on Windows (the CRT/Python
    read the OS timezone database, not `TZ`). On Windows this context
    manager degrades to a no-op: the test still runs and still asserts the
    correct count, it just doesn't additionally force a non-UTC skew to
    prove the assertion isn't accidentally UUTC-coincidental on this box.
    That is a real, disclosed reduction in the regression-guard's
    Windows-side specificity, not a weakened assertion — see C7 in
    state/red-baseline-2026-07-20/root-cause-clusters.md.
    """
    if not hasattr(time, "tzset"):
        yield
        return
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    try:
        time.tzset()
        yield
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def test_scan_session_scratch_d_path_untracked_scratch(git_repo):
    """_scan_session_scratch counts an untracked scratch file authored after started_at.

    TZ-PORTABILITY: started_at is authored in UTC ('2026-07-06T12:00:00Z');
    the file mtime is set 30 min later in UTC.  The assertion runs with
    TZ=America/Los_Angeles (POSIX only — see _tz_forced_to_us_pacific) so a
    naive rstrip('Z') parse (which anchors the epoch to local time, skewing
    by +7-8 h) would mis-classify the file as BEFORE the threshold and
    return 0.  The tz-aware parse must return 1.

    This is the CI guard for the F2 fleet-portability defect.
    """
    sid = "sess-67a-unit-dpath-001"
    # started_at: 2026-07-06 12:00:00 UTC — epoch computed tz-aware to avoid hardcode error.
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    # File mtime: 30 min after started_at in UTC = 12:30:00 UTC
    file_mtime = started_epoch_utc + 1800  # 30 min later

    scratch_file = git_repo.root / "tasks" / "my-feature" / "scratch.md"
    scratch_file.parent.mkdir(parents=True, exist_ok=True)
    scratch_file.write_text("scratch notes\n", encoding="utf-8")
    os.utime(scratch_file, (file_mtime, file_mtime))

    with _tz_forced_to_us_pacific():
        # Review: code-reviewer F2 — sid param removed from _scan_session_scratch
        result = _scan_session_scratch(git_repo.root, started_at)

    assert result == 1, (
        f"Untracked scratch file 30 min after started_at must be counted; "
        f"got {result!r} (check tz-aware parse — naive rstrip('Z') skews epoch "
        f"by +7-8h on US-Pacific nodes, making the file appear before threshold)"
    )


def test_scan_session_scratch_keep_list_excluded(git_repo):
    """_scan_session_scratch does NOT count keep-listed files (todo.md, plan.md, etc.)."""
    sid = "sess-67a-unit-keeplist-001"
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    # All of these are keep-listed and must NOT be counted.
    keep_listed_names = ["todo.md", "plan.md", "completion-log.md"]
    mtime_after = started_epoch_utc + 1800  # 30 min after

    for name in keep_listed_names:
        f = git_repo.root / "tasks" / "my-feature" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# {name}\n", encoding="utf-8")
        os.utime(f, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"Keep-listed files (todo.md, plan.md, completion-log.md) must not be counted; "
        f"got {result!r}"
    )


# Review: code-reviewer F4 — add *.plan.md endswith exclusion and .completion
# substring exclusion tests (both branches were uncovered).


def test_scan_session_scratch_plan_md_suffix_excluded(git_repo):
    """_scan_session_scratch does NOT count files ending in .plan.md (suffix exclusion).

    A file named tasks/<feat>/2026-07-06-something.plan.md with mtime after started_at
    must be excluded by the endswith('.plan.md') branch.  A refactor that accidentally
    breaks this (e.g. typo '.planmd') would fail here.

    Review: code-reviewer F4 — endswith('.plan.md') branch coverage gap.
    """
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800  # 30 min after

    plan_file = git_repo.root / "tasks" / "my-feature" / "2026-07-06-something.plan.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\n", encoding="utf-8")
    os.utime(plan_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"*.plan.md file must be excluded by endswith('.plan.md') filter; got {result!r}"
    )


def test_scan_session_scratch_completion_substring_excluded(git_repo):
    """_scan_session_scratch does NOT count files with '.completion' anywhere in the name.

    A file named tasks/<feat>/wsc-2026.completion.md has '.completion' at a non-suffix
    position and must still be excluded by the 'in name' substring filter.

    Review: code-reviewer F4 — .completion substring-match (all positions) coverage gap.
    """
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800  # 30 min after

    # File with .completion at a non-suffix position (mid-name)
    comp_file = git_repo.root / "tasks" / "my-feature" / "wsc-2026.completion.md"
    comp_file.parent.mkdir(parents=True, exist_ok=True)
    comp_file.write_text("# Completion notes\n", encoding="utf-8")
    os.utime(comp_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"File with '.completion' in name (any position) must be excluded; got {result!r}"
    )


# Review: code-reviewer F5 — add ValueError-on-parse graceful-negative test.


def test_scan_session_scratch_graceful_negative_unparseable_started_at(git_repo):
    """_scan_session_scratch returns None when started_at is not parseable as ISO-8601.

    The ValueError-on-parse path (distinct from the None-input path) is the fallback
    for a malformed started_at sentinel produced by a buggy upstream writer.  The
    function must return None without raising.

    Review: code-reviewer F5 — ValueError-on-parse graceful-negative branch coverage gap.
    """
    result = _scan_session_scratch(git_repo.root, "not-a-date")
    assert result is None, (
        f"Unparseable started_at must return None (graceful-negative via ValueError); "
        f"got {result!r}"
    )




def test_scan_session_scratch_zero_path_no_qualifying_files(git_repo):
    """_scan_session_scratch returns 0 when started_at present but no qualifying files.

    Files exist in tasks/ but all are either keep-listed or have mtime <= started_at.
    The D path must still fire (count=0, not None).
    """
    sid = "sess-67a-unit-zero-001"
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    # File with mtime BEFORE started_at — must not be counted.
    old_file = git_repo.root / "tasks" / "old-feature" / "old-scratch.md"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("old scratch\n", encoding="utf-8")
    mtime_before = started_epoch_utc - 3600  # 1 hour before
    os.utime(old_file, (mtime_before, mtime_before))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"No qualifying files → count=0 (D path, not None); got {result!r}"
    )


def test_scan_session_scratch_git_tracked_excluded(git_repo):
    """_scan_session_scratch does NOT count git-tracked files.

    Only UNTRACKED files count as transient scratch.
    """
    sid = "sess-67a-unit-tracked-001"
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800  # 30 min after

    # Create a file, add and commit it (tracked).
    tracked_file = git_repo.root / "tasks" / "some-feature" / "tracked.md"
    tracked_file.parent.mkdir(parents=True, exist_ok=True)
    tracked_file.write_text("tracked content\n", encoding="utf-8")
    subprocess.run(["git", "add", str(tracked_file)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked tasks file"],
                   cwd=str(git_repo.root), capture_output=True, check=True)
    os.utime(tracked_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"Git-tracked file must not be counted as transient scratch; got {result!r}"
    )


# ---------------------------------------------------------------------------
# C1 — STEP_2_67A: integration tests (Branch 11 flip via full handler invoke)
# ---------------------------------------------------------------------------










# ---------------------------------------------------------------------------
# T1 — applicable_node_ids membership (op-spec §3, Option B; plan D2)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# (T2) consumed_handoff_archive_scan — chain-terminal, missing handoff,
# predecessor handoff lives in archive/handoffs/ (swept) → still found
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# (T3) consumed_handoff_anchored_match — body-prose mention of a sibling sid
# is NOT a match (anchored frontmatter-only check)
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# (T4) session_shape_handoff_path_peer_misattribution — session-shape.json
# pickup.handoff points at a temporally-adjacent CONCURRENT session's
# handoff (a peer's, consumed_by != sid), not this session's own predecessor.
# The resolver must reject the peer path and fall through to the anchored
# _find_consumed_handoff recovery scan rather than trusting handoff blindly.
# ---------------------------------------------------------------------------










# Review: code-reviewer F1 — added missing test for the handoff-path-does-not-
# exist-on-disk rejection sub-case (guard's hf_abs.exists() short-circuit was
# untested; both prior tests use handoffs that exist on disk).


# Review: code-reviewer F2 — added missing test for the handoff-exists-but-no-
# consumed_by-field rejection sub-case (distinct code path from the mismatch
# test — _get_handoff_consumed_by returns None here, not a different sid).




# ---------------------------------------------------------------------------
# _resolve_in_repo — direct unit tests
#
# Review: code-reviewer F8 — prior coverage of _resolve_in_repo came only
# through the full resolve_session_branches -> _resolve_branches integration path (the
# traversal/absolute regression tests below).  That proves the end-to-end
# behavior but doesn't pin the helper's own contract, including a case no
# integration test exercises: a `../` traversal that resolves back INSIDE
# the repo must be ACCEPTED, not rejected — a naive "reject any candidate
# containing .." reimplementation would silently break this and nothing in
# the integration suite would catch it.
# ---------------------------------------------------------------------------


def test_resolve_in_repo_relative_in_repo_path_contained(repo):
    """A plain repo-relative candidate resolves and is contained."""
    handoff = repo.root / "state" / "handoffs" / "x.md"
    handoff.write_text("body", encoding="utf-8")

    result = _resolve_in_repo(repo.root, "state/handoffs/x.md")

    assert result == handoff.resolve()


def test_resolve_in_repo_parent_traversal_rejected(repo):
    """A `../` candidate that escapes worktree_root is rejected (returns None)."""
    result = _resolve_in_repo(repo.root, "../outside.md")

    assert result is None


def test_resolve_in_repo_absolute_foreign_path_rejected(repo):
    """An absolute candidate pointing outside worktree_root is rejected."""
    result = _resolve_in_repo(repo.root, "/absolute/foreign/path.md")

    assert result is None


def test_resolve_in_repo_traversal_that_resolves_back_inside_contained(repo):
    """A `../` traversal that nets back INSIDE worktree_root must be ACCEPTED —
    the genuine gap: a naive "reject any candidate containing .." reimplementation
    would wrongly reject this, and only a direct unit test on _resolve_in_repo
    itself (not the full-handler integration tests) pins this contract.
    """
    handoff = repo.root / "state" / "handoffs" / "x.md"
    handoff.write_text("body", encoding="utf-8")

    result = _resolve_in_repo(repo.root, "subdir/../state/handoffs/x.md")

    assert result == handoff.resolve()


def test_resolve_in_repo_dot_is_contained_as_root(repo):
    """The degenerate candidate "." resolves to worktree_root itself, contained."""
    result = _resolve_in_repo(repo.root, ".")

    assert result == repo.root.resolve()


# ---------------------------------------------------------------------------
# Defect A regression — foreign-repo path bleed
#
# pickup.handoff is producer-written and NOT trusted: an absolute path
# (or a ../ traversal) escapes worktree_root via Path.__truediv__, letting the
# primary-path guard validate a file in a DIFFERENT repo (e.g. Example-retrieval-repo)
# whose frontmatter even has consumed_by: <sid> — existence + consumed_by
# alone are insufficient; containment inside worktree_root is the invariant
# _resolve_in_repo asserts.
#
# Spec backlink:
#   docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# Defect A regression (plural array) — foreign-repo bleed into
# consumed_handoff_paths, the 2026-07-13 example-cockpit-repo incident shape.
#
# The per-source scalar guard rejected the foreign pickup.handoff, but the
# incident receipt still carried the foreign ABSOLUTE path AND a relativized
# phantom (foreign basename joined to worktree_root) in the PLURAL
# consumed_handoff_paths array + STEP_2_7 stamp target.  The final-gate
# sanitizer (_sanitize_consumed_handoffs) enforces containment + sid-ownership
# on the MERGED set at the one point every source converges, so no
# foreign/absolute/phantom entry can survive into the receipt or STEP_2_7 even
# if a per-source guard is bypassed by a different source or a future refactor.
#
# Spec backlink:
#   coordinator_core/ops/ceremony/branch_resolution.py :: _sanitize_consumed_handoffs
#   docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
# ---------------------------------------------------------------------------


def test_sanitize_drops_foreign_absolute_from_merged_set(repo, tmp_path):
    """A foreign ABSOLUTE path (consumed_by: sid) that reached the merged set
    is dropped by the final gate and re-expressed as nothing — never survives
    as an absolute path or a relativized phantom.  Directly exercises the
    choke point regardless of which upstream source injected the entry.
    """
    sid = "sess-sanitize-abs-001"

    # Foreign repo handoff outside worktree_root, declaring consumed_by: sid
    # (the trap — existence + consumed_by alone would pass a naive check).
    foreign_repo = tmp_path / "example-cockpit-repo"
    foreign_dir = foreign_repo / "state" / "handoffs"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    foreign_hf = foreign_dir / "2026-07-13_124503_dashboard-placement-rubric-ratify.md"
    foreign_hf.write_text(
        f"---\nstatus: consumed\nconsumed_by: {sid}\npredecessor: sess-foreign-pred\n---\n\nbody\n",
        encoding="utf-8",
    )

    # A local sid-owned handoff that MUST survive alongside the foreign reject.
    local_hf = repo.seed_handoff("real-local.md", consumed_by=sid)

    merged = [
        (str(foreign_hf), {"predecessor": "sess-foreign-pred"}),          # foreign absolute
        ("state/handoffs/real-local.md", {"predecessor": "sess-local"}),  # in-repo, owned
    ]
    kept, rejected = _sanitize_consumed_handoffs(repo.root, sid, merged)

    kept_paths = [p for p, _fm in kept]
    # Foreign absolute path is gone; no relativized phantom of its basename either.
    assert str(foreign_hf) not in kept_paths
    assert not any("dashboard-placement-rubric-ratify" in p for p in kept_paths)
    assert str(foreign_hf) in rejected
    # The in-repo sid-owned handoff survives, as a repo-relative path.
    assert "state/handoffs/real-local.md" in kept_paths
    assert all(not Path(p).is_absolute() for p in kept_paths)
    assert local_hf.exists()  # untouched


def test_sanitize_drops_peer_owned_in_repo_handoff(repo):
    """An IN-REPO handoff owned by a DIFFERENT sid (consumed_by: other) that
    reached the merged set is dropped — containment passes but ownership fails,
    so a temporally-adjacent peer's handoff is never mis-stamped as ours.
    """
    sid = "sess-sanitize-owner-001"
    repo.seed_handoff("mine.md", consumed_by=sid)
    repo.seed_handoff("peer.md", consumed_by="sess-some-peer-999")

    merged = [
        ("state/handoffs/mine.md", {}),
        ("state/handoffs/peer.md", {}),
    ]
    kept, rejected = _sanitize_consumed_handoffs(repo.root, sid, merged)
    kept_paths = [p for p, _fm in kept]
    assert "state/handoffs/mine.md" in kept_paths
    assert "state/handoffs/peer.md" not in kept_paths
    assert "state/handoffs/peer.md" in rejected






# ---------------------------------------------------------------------------
# Defect B regression — resolved_state.sid = null
#
# PipelineContext had no `sid` field, so to_dict() (which becomes the op's
# resolved_state) structurally could not carry the session id.  The handler
# must thread sid through so resolved_state["sid"] == the input sid.
#
# Spec backlink:
#   docs/plans/2026-07-10-wsc-resolve-foreign-repo-bleed-and-sid-null.md
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# C4 (wsc-dag-pickup-n-handoffs) — N-handoff resolve integration + all
# the Staff Engineer-finding regression cases (F0 dedup, F2 STEP_2_7 plural evidence —
# both the STEP_0 evidence dict AND the STEP_2_7 node's own evidence dict,
# folded into test_n_handoffs_both_found_live_and_archived_scalar_is_first
# below, F6 grep-fallback plurality) + C1's F4 divergent-field round-trip.
# ---------------------------------------------------------------------------








# REMOVED 2026-07-29 (kill-list op removal): test_step_2_7_node_carries_plural_evidence_real_read_path
# asserted the STEP_2_7 plural-evidence contract against wsc_commit._read_step_2_7_evidence,
# its only reader. wsc_commit.py was deleted as a dead op module and that function had no
# live counterpart in the single-pass tail, so the contract has no surviving second party.
# The producer side (STEP_2_7 carrying consumed_handoffs_paths) is covered by
# test_n_handoffs_both_found_live_and_archived_scalar_is_first above (Review:
# code-reviewer 2026-07-29 — the original "still covered above" claim here was false;
# no test asserted the STEP_2_7 node's own evidence dict until this fix).



def test_c1_divergent_scalar_field_rejected_by_validate():
    """the Staff Engineer F4: a hand-edited/round-tripped context where consumed_handoff !=
    consumed_handoffs[0] must FAIL validate() — the derived-scalar contract is
    enforced, not merely assigned at from_dict() time."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="",
        disposition="chain-terminal",
        consumed_handoffs=["state/handoffs/real.md"],
        consumed_handoff="state/handoffs/DIVERGENT.md",
        sid="sess-divergent-001",
    )
    errors = ctx.validate()
    assert any("consumed_handoff must equal consumed_handoffs[0]" in e for e in errors), (
        f"divergent scalar/list[0] must be rejected by validate(); got errors: {errors}"
    )


def test_c1_divergent_predecessor_field_rejected_by_validate():
    """the Staff Engineer F4 parallel case: predecessor != predecessors[0] must also fail
    validate()."""
    ctx = PipelineContext(
        ceremony="wsc",
        scope_mode="",
        disposition="chain-terminal",
        consumed_handoffs=["state/handoffs/real.md"],
        consumed_handoff="state/handoffs/real.md",
        predecessors=["sess-real-pred"],
        predecessor="sess-DIVERGENT-pred",
        sid="sess-divergent-002",
    )
    errors = ctx.validate()
    assert any("predecessor must equal predecessors[0]" in e for e in errors), (
        f"divergent predecessor/predecessors[0] must be rejected by validate(); got errors: {errors}"
    )


# ---------------------------------------------------------------------------
# C1 — scoping-analysis helpers (trailer-reliability, started_at range,
# foreign-commit + contiguity detection)
#
# Spec backlink:
#   docs/plans/2026-07-12-wsc-concurrent-tree-safety-hardening.md § Tasks C1
# ---------------------------------------------------------------------------


def _commit(repo_root, message, *, date=None, add=("-A",)):
    """Create a git commit in repo_root with an optional fixed author/commit date."""
    subprocess.run(["git", "add", *add], cwd=str(repo_root), capture_output=True, check=True)
    env = None
    if date is not None:
        import os as _os
        env = dict(_os.environ)
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_root), capture_output=True, check=True, env=env,
    )


def _head_sha(repo_root) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# --- pure-trailer: every session commit carries the trailer ----------------


def test_trailer_reliable_when_session_commits_carry_trailer(git_repo):
    sid = "sess-c1-trailer-001"
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")
    (git_repo.root / "f1.txt").write_text("one\n", encoding="utf-8")
    _commit(git_repo.root, f"work\n\nSession-Id: {sid}")

    assert _trailer_reliable(git_repo.root, sid, "2000-01-01T00:00:00Z") is True


def test_analyze_session_scoping_pure_trailer(git_repo):
    """Trailer present on the session's own commits ⇒ SCOPING_METHOD_TRAILER,
    zero foreign_count, contiguous=True (existing grep scoping stays authoritative)."""
    sid = "sess-c1-trailer-002"
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")
    (git_repo.root / "f1.txt").write_text("one\n", encoding="utf-8")
    _commit(git_repo.root, f"work\n\nSession-Id: {sid}")

    verdict = analyze_session_scoping(git_repo.root, git_repo.common_dir, sid)
    assert verdict.method == SCOPING_METHOD_TRAILER
    assert verdict.foreign_count == 0
    assert verdict.contiguous is True


# --- trailerless-clean: no trailer anywhere, but the range is foreign-free -


def test_trailer_unreliable_when_head_moved_no_trailer(git_repo):
    sid = "sess-c1-trailerless-001"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    (git_repo.root / "f1.txt").write_text("one\n", encoding="utf-8")
    _commit(git_repo.root, "plain commit, no trailer", date="2020-06-01T00:00:00Z")

    assert _trailer_reliable(git_repo.root, sid, started_at) is False


def test_trailer_reliable_when_no_work_since_started_at(git_repo):
    """started_at in the far future ⇒ HEAD has NOT moved since started_at ⇒
    trailer absence is not distinguishable from "no work happened" ⇒ reliable."""
    sid = "sess-c1-trailerless-future"
    started_at = "2099-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    assert _trailer_reliable(git_repo.root, sid, started_at) is True


def test_analyze_session_scoping_trailerless_clean(git_repo):
    """Trailerless session, but every commit in the started_at range touches
    only known-scope paths ⇒ SCOPING_METHOD_STARTED_AT_RANGE, foreign_count=0,
    contiguous=True."""
    sid = "sess-c1-trailerless-clean"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    (git_repo.root / "scoped.txt").write_text("scoped\n", encoding="utf-8")
    _commit(git_repo.root, "trailerless work", date="2020-06-01T00:00:00Z")

    verdict = analyze_session_scoping(
        git_repo.root, git_repo.common_dir, sid,
        # .gitkeep: the git_repo fixture's own init commit falls inside the
        # started_at range (fixture creation time is always after any fixed
        # 2020-era started_at) — treat it as known-scope so this test's
        # signal is the session's own commit, not fixture plumbing.
        known_scope_paths=frozenset({"scoped.txt", ".gitkeep"}),
    )
    assert verdict.method == SCOPING_METHOD_STARTED_AT_RANGE
    assert verdict.foreign_count == 0
    assert verdict.contiguous is True
    assert verdict.candidate_range.endswith("^..HEAD")


# --- trailerless-interleaved: the b15db349 repro ----------------------------
# 8 non-contiguous session commits with foreign commits interleaved, HEAD
# ending on a foreign commit.


def test_detect_foreign_commits_interleaved_repro(git_repo):
    """b15db349 repro shape: alternating session/foreign trailerless commits,
    HEAD ends on a foreign commit — foreign detection must flag the
    out-of-scope commits and never silently attribute them to sid."""
    sid = "sess-c1-interleaved-001"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    session_paths = [
        "coordinator_core/session_a.py",
        "coordinator_core/session_b.py",
        "coordinator_core/session_c.py",
        "coordinator_core/session_d.py",
    ]
    foreign_paths = [
        "state/foreign_a.md",
        "state/foreign_b.md",
        "state/foreign_c.md",
        "state/foreign_d.md",
    ]

    day = 1
    for sp, fp in zip(session_paths, foreign_paths):
        p = git_repo.root / sp
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("session work\n", encoding="utf-8")
        _commit(git_repo.root, f"session commit {sp}", date=f"2020-06-{day:02d}T00:00:00Z")
        day += 1

        fp_path = git_repo.root / fp
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text("foreign work\n", encoding="utf-8")
        _commit(git_repo.root, f"foreign commit {fp}", date=f"2020-06-{day:02d}T00:00:00Z")
        day += 1

    candidate_range = _started_at_candidate_range(git_repo.root, started_at)
    assert candidate_range.endswith("^..HEAD")

    # .gitkeep: the git_repo fixture's own init commit falls inside the
    # started_at range — its 5th foreign-shaped commit is fixture plumbing,
    # not part of the interleaved-repro shape under test; scope it out.
    known_scope = frozenset(session_paths) | frozenset({".gitkeep"})
    foreign_shas = _detect_foreign_commits(git_repo.root, sid, candidate_range, known_scope)
    assert len(foreign_shas) == 4, f"expected 4 foreign commits, got {foreign_shas}"

    contiguous = _range_is_contiguous_suffix(git_repo.root, candidate_range, foreign_shas)
    assert contiguous is False, "interleaved foreign commits must NOT be reported contiguous"


def test_analyze_session_scoping_trailerless_interleaved_is_ambiguous(git_repo):
    """Full pipeline over the interleaved repro shape: trailer unreliable
    (no trailers at all), range foreign-contaminated ⇒ SCOPING_METHOD_AMBIGUOUS
    with foreign_count > 0 — never a silently contaminated range."""
    sid = "sess-c1-interleaved-002"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    session_paths = ["coordinator_core/x1.py", "coordinator_core/x2.py"]
    foreign_paths = ["state/y1.md", "state/y2.md"]

    day = 1
    for sp, fp in zip(session_paths, foreign_paths):
        p = git_repo.root / sp
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("session\n", encoding="utf-8")
        _commit(git_repo.root, f"session {sp}", date=f"2020-06-{day:02d}T00:00:00Z")
        day += 1

        fp_path = git_repo.root / fp
        fp_path.parent.mkdir(parents=True, exist_ok=True)
        fp_path.write_text("foreign\n", encoding="utf-8")
        _commit(git_repo.root, f"foreign {fp}", date=f"2020-06-{day:02d}T00:00:00Z")
        day += 1

    verdict = analyze_session_scoping(
        git_repo.root, git_repo.common_dir, sid,
        known_scope_paths=frozenset(session_paths),
    )
    assert verdict.method == SCOPING_METHOD_AMBIGUOUS
    assert verdict.foreign_count > 0
    assert verdict.contiguous is False


# --- partial-trailer: only some commits carry the trailer ------------------


def test_detect_foreign_commits_partial_trailer_different_sid(git_repo):
    """Partial-trailer case: one commit tagged with a DIFFERENT sid's trailer
    must be flagged foreign regardless of touched paths."""
    sid = "sess-c1-partial-001"
    other_sid = "sess-c1-partial-OTHER"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    (git_repo.root / "own.txt").write_text("own\n", encoding="utf-8")
    _commit(git_repo.root, f"own work\n\nSession-Id: {sid}", date="2020-06-01T00:00:00Z")

    (git_repo.root / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _commit(
        git_repo.root, f"their work\n\nSession-Id: {other_sid}", date="2020-06-02T00:00:00Z",
    )

    candidate_range = _started_at_candidate_range(git_repo.root, started_at)
    # .gitkeep: the git_repo fixture's own init commit falls inside the
    # started_at range; scope it out so the signal under test (the
    # other_sid-trailered commit) is isolated.
    foreign_shas = _detect_foreign_commits(
        git_repo.root, sid, candidate_range, frozenset({"own.txt", ".gitkeep"}),
    )
    assert len(foreign_shas) == 1


def test_analyze_session_scoping_partial_trailer_is_trailer_method(git_repo):
    """At least one commit carries THIS sid's trailer ⇒ trailer is reliable,
    even if a foreign peer's trailer-tagged commit is also present in range —
    existing grep-based scoping (which only matches this sid's trailer) stays
    authoritative and is not contaminated by the foreign commit."""
    sid = "sess-c1-partial-002"
    other_sid = "sess-c1-partial-002-OTHER"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)

    (git_repo.root / "own.txt").write_text("own\n", encoding="utf-8")
    _commit(git_repo.root, f"own work\n\nSession-Id: {sid}", date="2020-06-01T00:00:00Z")

    (git_repo.root / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _commit(
        git_repo.root, f"their work\n\nSession-Id: {other_sid}", date="2020-06-02T00:00:00Z",
    )

    verdict = analyze_session_scoping(git_repo.root, git_repo.common_dir, sid)
    assert verdict.method == SCOPING_METHOD_TRAILER


# --- edge cases --------------------------------------------------------


def test_started_at_candidate_range_absent_started_at(git_repo):
    assert _started_at_candidate_range(git_repo.root, None) == ""


def test_started_at_candidate_range_unparseable_started_at(git_repo):
    assert _started_at_candidate_range(git_repo.root, "not-a-date") == ""


def test_started_at_candidate_range_no_commits_after_started_at(git_repo):
    future = "2099-01-01T00:00:00Z"
    assert _started_at_candidate_range(git_repo.root, future) == ""


def test_detect_foreign_commits_empty_range_graceful(git_repo):
    sid = "sess-c1-empty-range"
    assert _detect_foreign_commits(git_repo.root, sid, "", frozenset()) == []


def test_range_is_contiguous_suffix_no_foreign_commits(git_repo):
    sid = "sess-c1-no-foreign"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    (git_repo.root / "a.txt").write_text("a\n", encoding="utf-8")
    _commit(git_repo.root, f"work\n\nSession-Id: {sid}", date="2020-06-01T00:00:00Z")

    candidate_range = _started_at_candidate_range(git_repo.root, started_at)
    assert _range_is_contiguous_suffix(git_repo.root, candidate_range, []) is True


def test_range_is_contiguous_suffix_foreign_only_at_leading_edge(git_repo):
    """A foreign commit at the OLDEST end of the range, followed only by
    session commits, IS a contiguous suffix (the foreign commit sits before
    the session's true set, not interleaved within it)."""
    sid = "sess-c1-leading-foreign"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    # The git_repo fixture's own init (.gitkeep) commit also falls inside the
    # started_at range and sits before foreign_sha — treat it as part of the
    # tolerated leading run for this test.
    init_sha = _head_sha(git_repo.root)

    (git_repo.root / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _commit(git_repo.root, "foreign leading commit", date="2020-06-01T00:00:00Z")
    foreign_sha = _head_sha(git_repo.root)

    (git_repo.root / "session.txt").write_text("session\n", encoding="utf-8")
    _commit(git_repo.root, f"session work\n\nSession-Id: {sid}", date="2020-06-02T00:00:00Z")

    candidate_range = _started_at_candidate_range(git_repo.root, started_at)
    assert _range_is_contiguous_suffix(
        git_repo.root, candidate_range, [init_sha, foreign_sha]
    ) is True


def test_scoping_verdict_is_a_dataclass_with_expected_fields():
    verdict = ScopingVerdict(
        method=SCOPING_METHOD_AMBIGUOUS,
        foreign_count=2,
        contiguous=False,
        candidate_range="abc123^..HEAD",
    )
    assert verdict.method == SCOPING_METHOD_AMBIGUOUS
    assert verdict.foreign_count == 2
    assert verdict.contiguous is False
    assert verdict.candidate_range == "abc123^..HEAD"


# ---------------------------------------------------------------------------
# C3 — scoping verdict wired into the B-wave scoping site (full-pipeline
# integration, through resolve_session_branches → _resolve_branches → emit_receipt)
#
# Spec backlink:
#   docs/plans/2026-07-12-wsc-concurrent-tree-safety-hardening.md § Tasks C3
# ---------------------------------------------------------------------------












# ---------------------------------------------------------------------------
# Unscannable-subtree hardening (silent-enumeration defect fixes)
#
# Path.glob/.rglob silently swallow PermissionError while walking (an
# unreadable dir/subtree yields an empty iterator, no exception) — see
# coordinator_core/ops/roadmap_dag.py for the reference pattern this mirrors.
# Each of these guards against a scan failure reading as a clean/empty
# result, which is the exact silent-success shape that lets a ceremony
# D-node's verdict come out wrong with no visible signal.
# ---------------------------------------------------------------------------

_SKIP_CHMOD_UNRELIABLE = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)










# ---------------------------------------------------------------------------
# (T9) detector_b_production_path — Detector B via branch_resolution.resolve_session_branches
# ---------------------------------------------------------------------------
# Review: code-reviewer 2026-07-22 slice1 finding #4 — wsc_resolve.py's own
# Detector-B consolidation branch (~:1769-1805), the actual
# /workstream-complete production entry point, had zero end-to-end coverage.
# test_resolver_git_provenance.py exercises detect_git_provenance_consumed
# directly (a-e) and wsc_tail.py's separate lightweight wiring (f); neither
# proves resolve_session_branches's own consolidation branch actually calls it.






# ---------------------------------------------------------------------------
# STEP_2_65C flip half + STEP_2_65B bulk-eligibility evidence (C2)
# ---------------------------------------------------------------------------
# Spec backlink: pln-give-the-memo-disposition-flip-e580c2 § C2
#
# Coverage:
#   (C2-a) step_2_65c_resolving_op_names_resolve — D-node names memo.transition:resolve,
#                                                   no "Edit" instruction in its evidence
#   (C2-b) resolve_in_reply_to_target_open        — in_reply_to target still in inbox -> "open"
#   (C2-c) resolve_in_reply_to_target_closed       — in_reply_to target archived -> "closed"
#   (C2-d) resolve_in_reply_to_target_unresolvable — no match anywhere -> "unresolvable"
#   (C2-e) scan_open_memos_attaches_bulk_eligibility — _scan_open_memos composes
#                                                       classify_bulk_eligibility per memo
#   (C2-f) resolve_named_memo_dispositions_issues_n_calls — N named memos -> N resolve
#                                                            calls with the right dispositions;
#                                                            an unnamed memo stays open
#   (C2-g) resolve_named_memo_dispositions_unknown_memo_refused — a disposition naming a
#                                                                  memo outside open_memos
#                                                                  is refused, no op call issued
#   (C2-h) resolve_named_memo_dispositions_bulk_ineligible_refused — bulk request against a
#                                                                     non-eligible memo refused,
#                                                                     memo stays open on disk
#   (C2-i) resolve_named_memo_dispositions_bulk_eligible_applies — a bulk request against an
#                                                                   eligible fyi memo applies




















def test_resolve_named_memo_dispositions_unknown_memo_refused(git_repo):
    """A dispositions entry naming a memo NOT in open_memos (stale/hallucinated
    judgment answer) is refused fail-loud without ever calling memo.transition."""
    dispositions = [{"memo": "cross-repo/inbox/does-not-exist.md", "actioned_note": "x"}]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, [], dispositions, session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 1
    assert "not one of the open memos" in results[0]["error"]








# ---------------------------------------------------------------------------
# WSC_DISPOSITION / WSC_CONSUMED_HANDOFF escalate-only env override
#
# Spec backlink: coordinator_core.ops.ceremony.wsc_disposition.resolve_env_override
# / normalize_override_handoff, and the resulting Branch 1 override block in
# branch_resolution._resolve_branches. Extends claude-klabauter commit
# 1b07cded (coordinator/bin/wsc-session-disposition.py's own resolve_disposition)
# to this SECOND, independent disposition resolver — the two resolvers must
# behave identically on override reach, even though this module cannot import
# the bin script's implementation (see test_env_override_shared_helper_agrees_
# with_bin_script below for the mechanical drift check).
#
# Every test below explicitly monkeypatch.delenv's both env vars first (even
# though no pre-existing test in this file sets them) so a stray operator
# WSC_DISPOSITION/WSC_CONSUMED_HANDOFF in the ambient test-runner environment
# can never leak into an unrelated case, and so cases in this section cannot
# leak into each other via ambient state.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_wsc_env(monkeypatch):
    """Isolate every test in this module from ambient WSC_DISPOSITION /
    WSC_CONSUMED_HANDOFF — autouse so pre-existing tests above this section
    (none of which anticipated these vars) are equally protected."""
    monkeypatch.delenv("WSC_DISPOSITION", raising=False)
    monkeypatch.delenv("WSC_CONSUMED_HANDOFF", raising=False)
















# ---------------------------------------------------------------------------
# Operator-asserted ownership on the override path (coordinator review, this
# workstream): the override's own WARN strings exist for exactly the cases
# where consumed_by == sid can never hold on disk -- a dead claiming session
# (Detector C ambiguous/indeterminate) or a ship-then-archive handoff with no
# live consume stamp ever written. _sanitize_consumed_handoffs's ownership
# half is bypassable ONLY on the override path (operator_asserted=True);
# containment and existence are never bypassable, for anyone.
# ---------------------------------------------------------------------------














def test_env_override_shared_helper_agrees_with_bin_script(monkeypatch, tmp_path):
    """Mechanical drift check (constraint 2's fallback): coordinator/bin/
    wsc-session-disposition.py is deliberately self-contained and cannot
    import coordinator_core (not pip-installed, and the bin script must keep
    working when percolated somewhere coordinator_core is entirely absent —
    see that script's own module docstring). Its resolve_disposition therefore
    keeps its OWN copy of the override-parsing logic (landed by claude-klabauter
    commit 1b07cded) rather than importing
    coordinator_core.ops.ceremony.wsc_disposition.resolve_env_override. This
    test asserts the two independently-maintained implementations agree on
    the same input matrix, so a future edit to either one that drifts from
    the other is caught here rather than silently diverging in production."""
    import importlib.util
    import sys as _sys

    repo_root = tmp_path / "bin-script-repo"
    repo_root.mkdir()
    bin_path = Path(__file__).resolve().parents[4] / "coordinator" / "bin" / "wsc-session-disposition.py"
    assert bin_path.exists(), f"expected bin script at {bin_path}"
    lib_dir = bin_path.parent / "lib"
    if str(lib_dir) not in _sys.path:
        _sys.path.insert(0, str(lib_dir))
    spec = importlib.util.spec_from_file_location("wsc_session_disposition_dc", bin_path)
    bin_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bin_mod)

    from coordinator_core.ops.ceremony.wsc_disposition import resolve_env_override

    matrix = [
        ("predecessor-consumed", ""),
        ("chain-terminal", ""),
        ("PREDECESSOR-CONSUMED", "state/handoffs/a.md"),
        ("single-session", ""),
        ("banana", ""),
        ("", "state/handoffs/only-handoff-no-disposition.md"),
        ("", ""),
    ]
    for disp_val, handoff_val in matrix:
        monkeypatch.delenv("WSC_DISPOSITION", raising=False)
        monkeypatch.delenv("WSC_CONSUMED_HANDOFF", raising=False)
        if disp_val:
            monkeypatch.setenv("WSC_DISPOSITION", disp_val)
        if handoff_val:
            monkeypatch.setenv("WSC_CONSUMED_HANDOFF", handoff_val)

        shared = resolve_env_override()
        bin_disposition, bin_consumed, bin_diagnostics, _bin_paths = bin_mod.resolve_disposition(
            tmp_path, "sess-drift-check"
        )
        bin_escalated = bin_disposition == "predecessor-consumed" and any(
            "override" in d for d in bin_diagnostics
        )

        assert shared.escalate == bin_escalated, (
            f"escalate disagreement for WSC_DISPOSITION={disp_val!r}: "
            f"shared={shared.escalate} bin={bin_escalated} (bin_diagnostics={bin_diagnostics})"
        )
        if shared.escalate:
            assert shared.consumed_handoff_raw == handoff_val
            assert bin_consumed == handoff_val
