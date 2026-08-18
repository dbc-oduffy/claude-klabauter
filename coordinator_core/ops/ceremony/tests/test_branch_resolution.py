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
import logging
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.ops.ceremony.branch_resolution import (
    SCOPING_METHOD_AMBIGUOUS,
    SCOPING_METHOD_STARTED_AT_RANGE,
    SCOPING_METHOD_TRAILER,
    ScopingVerdict,
    _detect_foreign_commits,
    _find_consumed_handoff,
    resolve_session_branches,
    _range_is_contiguous_suffix,
    _read_completeness_mirror,
    # Review: code-reviewer — _read_session_shape was imported but never used; removed.
    # _session_added_plans is kept — exercised by the direct unit tests below.
    _read_started_at,
    _resolve_in_repo,
    _resolve_in_reply_to_target,
    _sanitize_consumed_handoffs,
    _scan_open_memos,
    _scan_session_scratch,
    _session_added_plans,
    _started_at_candidate_range,
    _trailer_reliable,
    analyze_session_scoping,
    resolve_named_memo_dispositions,
)
from coordinator_core.ops.ceremony.receipt_emit import is_not_yet_run, read_receipt
from coordinator_core.ops.ceremony.receipt_schema import validate
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.ops.ceremony.node_handlers import (
    J_QUESTIONS,
    STEP_0,
    STEP_1B,
    STEP_2_4B,
    STEP_2_6_3,
    STEP_2_6_4,
    STEP_2_65A,
    STEP_2_65B,
    STEP_2_6_3A,
    STEP_2_67A,
    STEP_2_7,
    STEP_2_75,
    STEP_2_96,
    STEP_2_9A,
    STEP_2_9C,
    STEP_B1,
)

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

    def invoke(self, sid: str, extra_params: Optional[dict] = None) -> dict:
        """Invoke resolve_session_branches with the given sid and this repo's common_dir."""
        params: dict[str, Any] = {"sid": sid}
        if extra_params:
            params.update(extra_params)
        return _run(resolve_session_branches(params, repo_root=self.common_dir))


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


def test_missing_sid(repo):
    result = _run(resolve_session_branches({}, repo_root=repo.common_dir))
    assert result["exit_code"] == 1
    assert "sid" in result["error"]


# ---------------------------------------------------------------------------
# (b) missing_repo_root
# ---------------------------------------------------------------------------


def test_missing_repo_root():
    result = _run(resolve_session_branches({"sid": "abc123"}, repo_root=None))
    assert result["exit_code"] == 1
    assert "repo_root" in result["error"]


# ---------------------------------------------------------------------------
# (c) disposition_single_session
# ---------------------------------------------------------------------------


def test_disposition_single_session(repo):
    sid = "sess-single-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"


# ---------------------------------------------------------------------------
# (d) disposition_chain_terminal
# ---------------------------------------------------------------------------


def test_disposition_chain_terminal(repo):
    sid = "sess-chain-001"
    repo.seed_handoff("consumed.md", consumed_by=sid)
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/consumed.md",
    )
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"


# ---------------------------------------------------------------------------
# (e) disposition_grep_fallback_terminal — absent session-shape.json → grep finds consumed_by
# ---------------------------------------------------------------------------


def test_disposition_grep_fallback_terminal(git_repo):
    """When session-shape.json absent, fallback to grep; grep finds consumed_by."""
    # Review: code-reviewer F13 — uses shared git_repo fixture (replaces duplicated git-init)
    sid = "sess-no-shape-001"
    # No session-shape.json written; seed a handoff with consumed_by: sid
    git_repo.seed_handoff("consumed-fallback.md", consumed_by=sid)
    # Re-stage the new handoff file so git grep can find it
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add handoff"], cwd=str(git_repo.root),
                   capture_output=True, check=True)

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"


# ---------------------------------------------------------------------------
# (f) disposition_grep_fallback_single — absent session-shape.json, no consumed_by
# ---------------------------------------------------------------------------


def test_disposition_grep_fallback_single(git_repo):
    """When session-shape.json absent and grep finds no consumed_by → single-session."""
    # Review: code-reviewer F13 — uses shared git_repo fixture (replaces duplicated git-init)
    sid = "sess-no-shape-002"
    # No session-shape.json, no handoff with consumed_by

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"


def test_disposition_grep_fallback_single_ignores_sibling_consumed_handoff(git_repo):
    """L1b grep fallback must NOT anchor on a sibling session's consumed handoff.

    Reported: cross-repo/inbox/2026-07-19-claude-central-em-route-7-doe-bugs-to-
    claude-klabauter-engine.md (doe_id 2026-07-09-wsc-resolve-mis-anchors-on-a-sibling-
    session) — "when no state/handoffs/ entry matches the current sid, wsc_resolve
    falls back to the newest-consumed non-sid handoff — anchoring the resolve on
    a sibling session's work." session-shape.json is absent (forcing the L1b grep
    path via _grep_disposition), and the ONLY handoff on disk is a sibling
    session's, already consumed. The current sid owns no consumed predecessor, so
    disposition must resolve single-session — never chain-terminal onto the
    sibling's handoff.

    Substantively already fixed by _grep_disposition routing through the
    sid-anchored _find_all_consumed_handoffs (55d17869, 5751c90e) rather than an
    unanchored newest-consumed scan — see test_session_shape_own_handoff_
    prearchived_with_sibling_decoy_in_state for the sibling-decoy pin on the
    session-shape.pickup path. This test is the L1b-grep-fallback-path pin — no
    prior test combines an absent session-shape.json with a sibling's consumed
    handoff present.
    """
    sid = "sess-no-shape-003"
    sibling_sid = "sess-sibling-newer-777"

    # No session-shape.json for sid — forces the L1b grep-fallback path.
    # The only handoff on disk belongs to a DIFFERENT, already-consumed session.
    git_repo.seed_handoff("sibling-consumed.md", consumed_by=sibling_sid)
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add sibling handoff"], cwd=str(git_repo.root),
                   capture_output=True, check=True)

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == ""
    assert "state/handoffs/sibling-consumed.md" not in resolved_state.get("consumed_handoffs", [])


# ---------------------------------------------------------------------------
# (g) disposition_absent_pickup_field — session-shape.json present but 'pickup' absent
# ---------------------------------------------------------------------------


def test_disposition_absent_pickup_field(git_repo):
    """session-shape.json present but 'pickup' field absent → grep fallback."""
    # Review: code-reviewer F13 — uses shared git_repo fixture (replaces duplicated git-init)
    sid = "sess-no-pickup-001"
    # Seed session-shape.json WITHOUT the pickup field
    sid_dir = git_repo.common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True, exist_ok=True)
    shape_no_pickup = {
        "schema_version": 1,
        # pickup field intentionally absent
        "actioned_memos": [],
        "plan": {"scope_mode": "architecture"},
        "magnitude": "",
    }
    (sid_dir / "session-shape.json").write_text(
        json.dumps(shape_no_pickup), encoding="utf-8"
    )

    # No handoff with consumed_by — expect single-session from grep
    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    # Without consumed_by in any handoff → single-session
    assert result["disposition"] == "single-session"
    # Review: code-reviewer — removed tautological assertion
    # `disposition in ("single-session","chain-terminal")` which was always True
    # regardless of the specific value; the == "single-session" check above is the real gate.


def test_disposition_absent_pickup_field_with_consumed_handoff(git_repo):
    """session-shape.json present but 'pickup' absent (L1b grep-fallback path),
    AND a real consumed_by: <sid> handoff exists — the combination that surfaces
    Finding 1's asymmetry (grep-fallback bypassing the anchored frontmatter match
    and archive-scan the primary path uses).  Also pins the anchored-vs-substring
    distinction via a decoy handoff whose BODY prose mentions a DIFFERENT sid's
    consumed_by string.

    Review: code-reviewer F5 — this combination (pickup absent + real match) was
    previously untested; test (e) covers grep-fallback-with-match only when
    session-shape.json is entirely absent, and test (g) covers pickup-absent only
    for the negative (no match) case.
    """
    sid = "sess-no-pickup-002"
    other_sid = "sess-other-999"

    # Seed session-shape.json WITHOUT the pickup field (forces L1b grep fallback).
    sid_dir = git_repo.common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True, exist_ok=True)
    shape_no_pickup = {
        "schema_version": 1,
        # pickup field intentionally absent
        "actioned_memos": [],
        "plan": {"scope_mode": "architecture"},
        "magnitude": "",
    }
    (sid_dir / "session-shape.json").write_text(
        json.dumps(shape_no_pickup), encoding="utf-8"
    )

    # Decoy: frontmatter consumed_by names a DIFFERENT sid, but body prose
    # mentions this sid's consumed_by string — must NOT be treated as a match.
    decoy = git_repo.root / "state" / "handoffs" / "decoy.md"
    decoy.write_text(
        "---\n"
        'title: "Decoy Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: consumed\n"
        f"claimed_by: {other_sid}\n"
        "---\n\n"
        f"# Handoff Body\n\nSee also claimed_by: {sid} mentioned here in prose, "
        "not frontmatter.\n",
        encoding="utf-8",
    )

    # Real match: frontmatter consumed_by names this sid, with a predecessor field.
    real_path = git_repo.root / "state" / "handoffs" / "real-consumed.md"
    real_path.write_text(
        "---\n"
        'title: "Real Handoff"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "predecessor: sess-predecessor-777\n"
        "---\n\n# Handoff Body\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add handoffs"], cwd=str(git_repo.root),
                   capture_output=True, check=True)

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-consumed.md"
    assert resolved_state["predecessor"] == "sess-predecessor-777"


# ---------------------------------------------------------------------------
# (h) governing_plan_evidence
# ---------------------------------------------------------------------------


def test_governing_plan_evidence(repo):
    """D node step_2a carries candidate plan list as evidence."""
    sid = "sess-gov-plan-001"
    repo.seed_session_shape(sid)
    repo.seed_plan("2026-07-06-test-plan.md")
    repo.seed_plan("2026-07-05-other-plan.md")

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    step_2a = nodes.get("step_2a")
    assert step_2a is not None
    assert step_2a["type"] == "D"
    evidence = step_2a.get("evidence", {})
    assert "candidate_count" in evidence
    assert evidence["candidate_count"] == 2


# ---------------------------------------------------------------------------
# (i) nature_classification
# ---------------------------------------------------------------------------


def test_nature_classification(repo):
    """D node step_2.6.4 carries nature from classify_nature."""
    sid = "sess-nature-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["nature"] in ("roadmap", "bugfix", "tech-debt", "infra")

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node_2_6_4 = nodes.get("step_2.6.4")
    assert node_2_6_4 is not None
    assert node_2_6_4["type"] == "D"
    assert node_2_6_4["evidence"]["nature"] in ("roadmap", "bugfix", "tech-debt", "infra")


# ---------------------------------------------------------------------------
# (j) open_memos_scan
# ---------------------------------------------------------------------------


def test_open_memos_scan(repo):
    """D node step_2.65a enumerates open memos when present."""
    sid = "sess-memos-001"
    repo.seed_session_shape(sid)
    repo.seed_open_memo("2026-07-06-test-memo.md", title="Test Memo")
    repo.seed_open_memo("2026-07-05-other-memo.md", title="Other Memo")

    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["open_memos_count"] == 2

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node_65a = nodes.get("step_2.65a")
    assert node_65a is not None
    assert node_65a["type"] == "D"
    assert node_65a["evidence"]["open_count"] == 2


# ---------------------------------------------------------------------------
# (k) open_memos_zero
# ---------------------------------------------------------------------------


def test_open_memos_zero(repo):
    """step_2.65a fires with open_count=0 when inbox empty."""
    sid = "sess-memos-zero-001"
    repo.seed_session_shape(sid)
    # No memos seeded

    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["open_memos_count"] == 0


# ---------------------------------------------------------------------------
# (l) j_questions_emitted — all 8 J-nodes present
# ---------------------------------------------------------------------------


def test_j_questions_emitted(repo):
    """All 8 J-nodes present in resolved ctx."""
    sid = "sess-j-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    j_questions = result["j_questions"]
    assert len(j_questions) == 8, (
        f"Expected 8 J-questions; got {len(j_questions)}: "
        f"{[q['id'] for q in j_questions]}"
    )
    # All J nodes have non-empty question text
    for jq in j_questions:
        assert jq["question"], f"J-node {jq['id']} has empty question"


# ---------------------------------------------------------------------------
# (m) f_slots_emitted — all 3 F-nodes present
# ---------------------------------------------------------------------------


def test_f_slots_emitted(repo):
    """All 3 F-nodes present in resolved ctx."""
    sid = "sess-f-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    f_slots = result["f_slots"]
    assert len(f_slots) == 3, (
        f"Expected 3 F-slots; got {len(f_slots)}: "
        f"{[s['id'] for s in f_slots]}"
    )
    # All F nodes have non-empty slot description
    for fs in f_slots:
        assert fs["slot"], f"F-node {fs['id']} has empty slot"


# ---------------------------------------------------------------------------
# (n) b_node_pre_resolved_evidence
# ---------------------------------------------------------------------------


def test_b_node_pre_resolved_evidence(repo):
    """B1 node has pre_resolved_evidence with brightline keys."""
    sid = "sess-b-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    b_pre = result["b_pre_resolved"]
    assert "brightline_verdict" in b_pre
    assert b_pre["brightline_verdict"] in ("single-reviewer-ok", "PARTITION-MANDATORY")
    assert "slice_count" in b_pre
    assert "docs_checker_inclusion" in b_pre
    assert "partition_boundaries" in b_pre

    # Also check B1 node in the ledger
    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    b1_node = nodes.get("step_B1")
    assert b1_node is not None
    assert b1_node["type"] == "B"
    assert "pre_resolved_evidence" in b1_node
    assert "em_adjudication" in b1_node
    assert b1_node["em_adjudication"] == {}  # empty in phase-1


# ---------------------------------------------------------------------------
# (o) b_node_generic_keys
# ---------------------------------------------------------------------------


def test_b_node_generic_keys(repo):
    """B1 uses generic pre_resolved_evidence/em_adjudication, not dispatch_plan/adjudication."""
    sid = "sess-b-generic-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    b1_node = nodes.get("step_B1")
    assert b1_node is not None
    # Verify generic key names (not wsc-specific names)
    assert "pre_resolved_evidence" in b1_node, "B1 must use generic 'pre_resolved_evidence' key"
    assert "em_adjudication" in b1_node, "B1 must use generic 'em_adjudication' key"
    # Negative-spec: these wsc-specific names must NOT appear as direct keys
    assert "dispatch_plan" not in b1_node, "B1 must NOT use wsc-specific 'dispatch_plan' key"
    assert "adjudication" not in b1_node, "B1 must NOT use wsc-specific 'adjudication' key"
    assert "review_verdict" not in b1_node, "B1 must NOT use wsc-specific 'review_verdict' key"


# ---------------------------------------------------------------------------
# (p) phase1_receipt_written
# ---------------------------------------------------------------------------


def test_phase1_receipt_written(repo):
    """emit produces the session-keyed shard state/ceremony/wsc/<sid-short>-...json (phase-1, C3)."""
    sid = "sess-receipt-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    receipt_rel = result["receipt_path"]
    receipt_abs = repo.root / receipt_rel
    assert receipt_abs.exists(), f"Phase-1 receipt not written at {receipt_abs}"
    assert receipt_rel.startswith("state/ceremony/wsc/"), (
        f"expected the session-keyed shard dir, not the old singleton path; got {receipt_rel!r}"
    )
    assert receipt_rel.endswith(".json")
    # The sid-short filename prefix must be derived from THIS session's sid.
    from coordinator_core.ops.ceremony.receipt_emit import _sid_short
    assert Path(receipt_rel).name.startswith(_sid_short(sid) + "-"), (
        f"shard filename must be keyed on this session's sid; got {receipt_rel!r}"
    )

    # File is valid JSON
    data = json.loads(receipt_abs.read_text(encoding="utf-8"))
    assert data["ceremony"] == "wsc"
    assert data["phase"] == "phase-1"


def test_concurrent_sessions_never_read_each_others_receipt(repo):
    """C3 / AC5: two concurrent ceremonies (distinct sid) each read back only their own.

    Simulates two overlapping /workstream-complete invocations against the SAME
    worktree by running two phase-1 resolves with distinct sids, then asserting
    each session's receipt_path is a DIFFERENT shard, and that
    resolve_latest_receipt_path(sid=A) never resolves to B's shard (and vice
    versa) — the concurrency defect this chunk fixes (the prior singleton
    state/ceremony/wsc-receipt.json would have had the second invoke clobber
    the first).
    """
    from coordinator_core.ops.ceremony.receipt_emit import resolve_latest_receipt_path

    # NOTE: sids differ in their first _SID_SHORT_LEN (12) chars — real coordinator
    # session ids are high-entropy and diverge immediately; a shared long common
    # prefix (e.g. "sess-concurrent-aaa" vs "...-bbb") would collide on the
    # filename-legibility truncation (see receipt_emit._SID_SHORT_LEN docstring).
    sid_a = "aaa-session-001"
    sid_b = "bbb-session-002"
    repo.seed_session_shape(sid_a, scope_mode="architecture")
    repo.seed_session_shape(sid_b, scope_mode="deep-dive")

    result_a = repo.invoke(sid_a)
    result_b = repo.invoke(sid_b)
    assert result_a["exit_code"] == 0
    assert result_b["exit_code"] == 0

    receipt_path_a = repo.root / result_a["receipt_path"]
    receipt_path_b = repo.root / result_b["receipt_path"]

    # Distinct shards — no clobbering.
    assert receipt_path_a != receipt_path_b
    assert receipt_path_a.exists()
    assert receipt_path_b.exists()

    # sid-keyed resolution never crosses sessions.
    resolved_for_a = resolve_latest_receipt_path(repo.root, "wsc", sid_a)
    resolved_for_b = resolve_latest_receipt_path(repo.root, "wsc", sid_b)
    assert resolved_for_a == receipt_path_a
    assert resolved_for_b == receipt_path_b
    assert resolved_for_a != resolved_for_b

    # Each receipt's own body reflects its own session's inputs, not the peer's.
    data_a = json.loads(receipt_path_a.read_text(encoding="utf-8"))
    data_b = json.loads(receipt_path_b.read_text(encoding="utf-8"))
    assert data_a["scope_mode"] == "architecture"
    assert data_b["scope_mode"] == "deep-dive"


# ---------------------------------------------------------------------------
# (q) receipt_schema_valid
# ---------------------------------------------------------------------------


def test_receipt_schema_valid(repo):
    """Emitted receipt passes receipt_schema.validate()."""
    sid = "sess-schema-valid-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    receipt_abs = repo.root / result["receipt_path"]
    data = json.loads(receipt_abs.read_text(encoding="utf-8"))
    errors = validate(data)
    assert not errors, f"Receipt schema errors: {errors}"


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


def test_idempotency_guard_no_prior(repo):
    """idempotency_guard_fired=False when no prior completion entry."""
    sid = "sess-idem-001"
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/h.md")
    repo.seed_handoff("h.md", consumed_by=sid, chain="my-plan-slug")
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["idempotency_guard_fired"] is False


# ---------------------------------------------------------------------------
# (t) idempotency_guard_fired
# ---------------------------------------------------------------------------


def test_idempotency_guard_fired(repo):
    """idempotency_guard_fired=True when prior completion entry exists."""
    sid = "sess-idem-002"
    slug = "my-special-plan-slug"
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/h.md")
    repo.seed_handoff("h.md", consumed_by=sid, chain=slug)
    # Seed a prior completion entry with the same chain slug
    repo.seed_completion_entry("2026-01-01-prior.md", chain=slug)

    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["idempotency_guard_fired"] is True


# ---------------------------------------------------------------------------
# (u) scope_mode_from_session_shape
# ---------------------------------------------------------------------------


def test_scope_mode_from_session_shape(repo):
    """scope_mode is read from session-shape.json plan.scope_mode field."""
    sid = "sess-scope-001"
    repo.seed_session_shape(sid, scope_mode="spike")
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["scope_mode"] == "spike"


# ---------------------------------------------------------------------------
# (v) scope_mode_param_override
# ---------------------------------------------------------------------------


def test_scope_mode_param_override(repo):
    """scope_mode param overrides session-shape.json value."""
    sid = "sess-scope-002"
    repo.seed_session_shape(sid, scope_mode="architecture")
    result = repo.invoke(sid, extra_params={"scope_mode": "spike"})
    assert result["exit_code"] == 0
    assert result["scope_mode"] == "spike"


# ---------------------------------------------------------------------------
# (w) result_exit_code_zero
# ---------------------------------------------------------------------------


def test_result_exit_code_zero(repo):
    """Happy-path returns exit_code=0."""
    sid = "sess-happy-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0


# ---------------------------------------------------------------------------
# (x) result_fields_present
# ---------------------------------------------------------------------------


def test_result_fields_present(repo):
    """Result carries all expected top-level fields."""
    sid = "sess-fields-001"
    repo.seed_session_shape(sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    required_fields = [
        "exit_code", "disposition", "scope_mode", "nature",
        "resolved_state", "receipt_path", "j_questions", "f_slots",
        "b_pre_resolved", "idempotency_guard_fired", "open_memos_count",
    ]
    for field in required_fields:
        assert field in result, f"Result missing field: {field!r}"


# ---------------------------------------------------------------------------
# (y) completeness_checklist_branch
# ---------------------------------------------------------------------------


def test_completeness_checklist_branch(repo):
    """chain-terminal with completeness_checklist in frontmatter → completeness_present."""
    sid = "sess-checklist-001"
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/h-checklist.md",
    )
    repo.seed_handoff(
        "h-checklist.md",
        consumed_by=sid,
        completeness_checklist=True,
    )
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"

    # The completeness_checklist branch resolution should be present
    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    cc_branch = branches.get("completeness_checklist_present")
    assert cc_branch is not None
    assert cc_branch["signal_read"] is True


# ---------------------------------------------------------------------------
# (z) loe_path_branch
# ---------------------------------------------------------------------------


def test_loe_path_chain_terminal(repo):
    """loe_path resolves chain-terminal to aggregate-chain-loe.sh."""
    sid = "sess-loe-001"
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/loe-test.md",
    )
    repo.seed_handoff("loe-test.md", consumed_by=sid)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"

    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    loe_branch = branches.get("loe_path")
    assert loe_branch is not None
    assert loe_branch["signal_read"] == "aggregate-chain-loe.sh"


def test_loe_path_single_session(repo):
    """loe_path resolves single-session to coordinator-session-loe.sh."""
    sid = "sess-loe-002"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"

    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    loe_branch = branches.get("loe_path")
    assert loe_branch is not None
    assert loe_branch["signal_read"] == "coordinator-session-loe.sh"


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


def test_step_2_6_3_positive_plan_added(git_repo):
    """STEP_2_6_3 emits D with added plans when started_at present and plan added this session."""
    sid = "sess-c1-pos-001"
    git_repo.seed_session_shape(sid)
    # Use an old started_at so --since catches all commits
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")

    # Add a plan file in a commit tagged with Session-Id: <sid>
    plan_path = git_repo.root / "docs" / "plans" / "2026-07-06-c1-test.md"
    plan_path.write_text("# Test Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan_path)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"add plan\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_6_3)
    assert node is not None
    assert node["type"] == "D", f"STEP_2_6_3 must emit D, got {node['type']}"
    evidence = node.get("evidence", {})
    added = evidence.get("added_plans", [])
    assert any("2026-07-06-c1-test.md" in p for p in added), (
        f"Expected added plan in evidence; got added_plans={added}"
    )


def test_step_2_6_3_negative_no_plan_added(git_repo):
    """STEP_2_6_3 emits D with empty signal when started_at present but no plan added this session."""
    sid = "sess-c1-neg-001"
    git_repo.seed_session_shape(sid)
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")
    # No plan commit with matching Session-Id

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_6_3)
    assert node is not None
    assert node["type"] == "D", f"STEP_2_6_3 must emit D, got {node['type']}"
    evidence = node.get("evidence", {})
    assert evidence.get("added_plans") == [], (
        f"Expected empty added_plans; got {evidence.get('added_plans')}"
    )


def test_step_2_6_3_absence_started_at_missing(repo):
    """STEP_2_6_3 emits D with graceful-negative signal when started_at is absent."""
    sid = "sess-c1-abs-001"
    repo.seed_session_shape(sid)
    # No started_at file seeded

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_6_3)
    assert node is not None
    assert node["type"] == "D", f"STEP_2_6_3 must emit D even when started_at absent"
    evidence = node.get("evidence", {})
    assert evidence.get("started_at") is None
    assert evidence.get("added_plans") == []
    assert "absent" in evidence.get("signal", "").lower()


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


def test_read_completeness_mirror_present_open_items(tmp_path):
    """_read_completeness_mirror returns open/total counts for a v1 mirror with open items."""
    sid = "sess-c3-unit-001"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _V1_MIRROR_OPEN, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    assert result is not None
    assert result.get("open_count") == 2
    assert result.get("total_count") == 3


def test_read_completeness_mirror_all_done(tmp_path):
    """_read_completeness_mirror returns open_count=0 when all items are done."""
    sid = "sess-c3-unit-002"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _V1_MIRROR_ALL_DONE, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    assert result is not None
    assert result.get("open_count") == 0
    assert result.get("total_count") == 2


def test_read_completeness_mirror_absent(tmp_path):
    """_read_completeness_mirror returns None when file is absent (normal case)."""
    sid = "sess-c3-unit-003"
    result = _read_completeness_mirror(tmp_path, sid)
    assert result is None


def test_read_completeness_mirror_schema_mismatch(tmp_path):
    """_read_completeness_mirror returns {schema_mismatch, open_count: None} for non-v1 schema."""
    sid = "sess-c3-unit-004"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _V2_MIRROR_MISMATCH, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    assert result is not None
    assert "schema_mismatch" in result, f"Expected schema_mismatch key; got {result}"
    assert result["schema_mismatch"] == "completeness-checklist-mirror-v2"
    assert result["open_count"] is None


def test_read_completeness_mirror_no_schema_line(tmp_path):
    """_read_completeness_mirror returns None when file has no schema: line and no v1 tag."""
    sid = "sess-c3-unit-005"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _NO_SCHEMA_CONTENT, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    assert result is None


def test_read_completeness_mirror_no_state_lines(tmp_path):
    """_read_completeness_mirror returns None for a v1-tagged file with no state: lines."""
    sid = "sess-c3-unit-006"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    content = "schema: completeness-checklist-mirror-v1\nitems: []\n"
    (mirror_dir / "completeness-checklist.yaml").write_text(content, encoding="utf-8")

    result = _read_completeness_mirror(tmp_path, sid)
    assert result is None


# ---------------------------------------------------------------------------
# C3 — STEP_2_96: integration tests (presence / absent / mismatch)
# ---------------------------------------------------------------------------


def test_step_2_96_mirror_present_open_items(repo):
    """STEP_2_96 emits D with open_count>0 when mirror present with open items."""
    sid = "sess-c3-int-001"
    repo.seed_session_shape(sid)
    repo.seed_completeness_mirror(sid, _V1_MIRROR_OPEN)

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_96)
    assert node is not None
    assert node["type"] == "D", f"STEP_2_96 must emit D, got {node['type']}"
    evidence = node.get("evidence", {})
    assert evidence.get("open_count") == 2
    assert evidence.get("total_count") == 3


def test_step_2_96_mirror_present_all_done(repo):
    """STEP_2_96 emits D with open_count=0 when all checklist items are done."""
    sid = "sess-c3-int-002"
    repo.seed_session_shape(sid)
    repo.seed_completeness_mirror(sid, _V1_MIRROR_ALL_DONE)

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_96)
    assert node is not None
    assert node["type"] == "D"
    assert node["evidence"].get("open_count") == 0
    assert node["evidence"].get("total_count") == 2


def test_step_2_96_mirror_absent(repo):
    """STEP_2_96 emits D with zero/negative signal when mirror is absent (normal case)."""
    sid = "sess-c3-int-003"
    repo.seed_session_shape(sid)
    # No mirror file seeded

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_96)
    assert node is not None
    assert node["type"] == "D", f"STEP_2_96 must emit D even when mirror absent"
    evidence = node.get("evidence", {})
    assert evidence.get("open_count") == 0
    assert evidence.get("total_count") == 0


def test_step_2_96_schema_mismatch(repo):
    """STEP_2_96 emits D with schema_mismatch evidence when mirror has wrong schema."""
    sid = "sess-c3-int-004"
    repo.seed_session_shape(sid)
    repo.seed_completeness_mirror(sid, _V2_MIRROR_MISMATCH)

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_96)
    assert node is not None
    assert node["type"] == "D"
    evidence = node.get("evidence", {})
    assert "schema_mismatch" in evidence, (
        f"Expected schema_mismatch in evidence; got {evidence}"
    )
    assert evidence["open_count"] is None


def test_step_2_96_no_state_lines_graceful(repo):
    """STEP_2_96 emits D with zero signal for a v1-tagged file with no state: lines."""
    sid = "sess-c3-int-005"
    repo.seed_session_shape(sid)
    repo.seed_completeness_mirror(
        sid, "schema: completeness-checklist-mirror-v1\nitems: []\n"
    )

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_96)
    assert node is not None
    assert node["type"] == "D"
    # No state: lines → graceful zero signal
    evidence = node.get("evidence", {})
    assert evidence.get("open_count") == 0
    assert evidence.get("total_count") == 0


# ---------------------------------------------------------------------------
# Review: code-reviewer — STEP_1B/STEP_2_4B D-node emission integration test
# Finding 1 (P2): reclassified F→D under Option B (memo 2026-07-08); locks the
# D-node type + resolving_op + disk_first evidence shape this diff exists to fix.
# ---------------------------------------------------------------------------


def test_step_1b_step_2_4b_emit_as_d_nodes(repo):
    """STEP_1B/STEP_2_4B emit as D-nodes (Option B), not the prior empty F-slots.

    Locks the Option B reclassification: both nodes must be provenance-only
    D-nodes with a non-empty resolving_op and a structured disk_first evidence
    key, so a future refactor that silently drops the add_node(handle_d(...))
    calls (rather than re-adding them as F) is caught here rather than only by
    the weaker f_slot-count checks.
    """
    sid = "sess-optionb-int-001"
    repo.seed_session_shape(sid)

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}

    step_1b_node = nodes.get(STEP_1B)
    assert step_1b_node is not None
    assert step_1b_node["type"] == "D", f"STEP_1B must emit D, got {step_1b_node['type']}"
    assert step_1b_node.get("resolving_op") == "disk-first:coordinator-lesson-add"
    evidence_1b = step_1b_node.get("evidence", {})
    assert evidence_1b.get("disk_first") is True
    assert "disk-first" in evidence_1b.get("note", "")

    step_2_4b_node = nodes.get(STEP_2_4B)
    assert step_2_4b_node is not None
    assert step_2_4b_node["type"] == "D", f"STEP_2_4B must emit D, got {step_2_4b_node['type']}"
    assert step_2_4b_node.get("resolving_op") == "disk-first:allowlist-edit"
    evidence_2_4b = step_2_4b_node.get("evidence", {})
    assert evidence_2_4b.get("disk_first") is True
    assert "provenance" in evidence_2_4b.get("note", "")


# ---------------------------------------------------------------------------
# Review: code-reviewer — new direct unit tests for _session_added_plans
# Findings 6, 7, 8, 9 (P2/nit): cover --diff-filter=A ADDED-not-MODIFIED
# semantic, --since temporal boundary, graceful-empty on non-zero git exit,
# and dedup logic — none of these were exercised by the existing positive tests.
# ---------------------------------------------------------------------------


def test_session_added_plans_modified_plan_not_returned(git_repo):
    """--diff-filter=A: a plan MODIFIED (not added) in a session commit is absent.

    Pins the ADDED-not-touched semantic: only plans authored (ADDED) this
    session should appear; modifying a pre-existing plan must be excluded.

    Review: code-reviewer Finding 6 — negative test for --diff-filter=A predicate.
    """
    sid = "sess-sap-mod-001"
    git_repo.seed_session_shape(sid)
    # started_at in the past so --since includes all commits
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")

    # Seed a pre-existing plan in the initial repo state (not this session)
    plan_path = git_repo.root / "docs" / "plans" / "pre-existing-plan.md"
    plan_path.write_text("# Pre-existing Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan_path)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add pre-existing plan (no session tag)"],
                   cwd=str(git_repo.root), capture_output=True, check=True)

    # Now MODIFY the pre-existing plan in a session-tagged commit
    plan_path.write_text("# Pre-existing Plan\n\nModified this session.\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan_path)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"update pre-existing plan\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_6_3)
    assert node is not None
    evidence = node.get("evidence", {})
    added = evidence.get("added_plans", [])
    assert added == [], (
        f"Modified (not added) plan must NOT appear in added_plans; got {added}"
    )


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


def test_session_added_plans_dedup(git_repo):
    """_session_added_plans returns each plan path only once across two session commits.

    Creates two session-tagged commits that each add a different plan doc, then
    verifies each appears exactly once and neither is duplicated.  The seen-set
    in _session_added_plans deduplicates output lines from --name-only across commits.

    Note: a true same-path dedup would require two commits that each ADD the same
    file — git normally prevents re-adding an already-tracked path with --diff-filter=A,
    so same-path dedup is not directly testable here.  This test validates that two
    different plans across two commits are each counted exactly once, which is the
    primary invariant the seen-set enforces.

    Review: code-reviewer F6 — strengthened from single commit to two commits so
    the test actually exercises the two-commit scenario described in its docstring.
    """
    sid = "sess-sap-dedup-001"
    git_repo.seed_session_shape(sid)
    git_repo.seed_started_at(sid, "2000-01-01T00:00:00Z")

    # First session-tagged commit: add plan-alpha.md
    plan_alpha = git_repo.root / "docs" / "plans" / "dedup-plan-alpha.md"
    plan_alpha.write_text("# Alpha Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan_alpha)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"add alpha plan\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    # Second session-tagged commit: add plan-beta.md
    plan_beta = git_repo.root / "docs" / "plans" / "dedup-plan-beta.md"
    plan_beta.write_text("# Beta Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan_beta)], cwd=str(git_repo.root),
                   capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"add beta plan\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_6_3)
    assert node is not None
    evidence = node.get("evidence", {})
    added = evidence.get("added_plans", [])

    # Each plan must appear exactly once (no duplicates from two --name-only commits)
    alpha_entries = [p for p in added if "dedup-plan-alpha.md" in p]
    beta_entries = [p for p in added if "dedup-plan-beta.md" in p]
    assert len(alpha_entries) == 1, (
        f"dedup-plan-alpha.md must appear exactly once; got {alpha_entries}"
    )
    assert len(beta_entries) == 1, (
        f"dedup-plan-beta.md must appear exactly once; got {beta_entries}"
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


def test_read_completeness_mirror_quoted_scalar_not_counted(tmp_path):
    """_read_completeness_mirror does NOT count quoted state: \"open\" in open_count.

    The line-pattern regex ``^\\s+state:\\s*open\\b`` requires an unquoted scalar
    value.  The quoted form ``state: "open"`` is counted in total_count (it IS an
    indented state: line) but must NOT contribute to open_count.  A future regex
    change that accidentally starts matching the quoted form would be caught here.

    Review: code-reviewer Finding 10 — quoted-scalar negative case.
    """
    sid = "sess-c3-quoted-001"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _V1_MIRROR_QUOTED_SCALAR, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    # The indented ``state: "open"`` line is counted in total_count (prefix match)
    # but the quoted value must NOT inflate open_count.
    assert result is not None, (
        "v1-tagged file with indented state: line must not return None; got None"
    )
    assert result.get("open_count") == 0, (
        "Quoted-scalar state: \"open\" must NOT be counted in open_count; got {!r}".format(result)
    )


def test_read_completeness_mirror_column_zero_state_not_counted(tmp_path):
    """_read_completeness_mirror does NOT count a col-0 'state: open' line.

    The regex requires at least one leading space (^\\s+) so a top-level YAML
    key at column 0 must not contribute to open_count or total_count.

    Review: code-reviewer Finding 10 — column-0 anchor exclusion case.
    """
    sid = "sess-c3-col0-001"
    mirror_dir = tmp_path / "state" / "tasks" / sid
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "completeness-checklist.yaml").write_text(
        _V1_MIRROR_COLUMN_ZERO, encoding="utf-8"
    )

    result = _read_completeness_mirror(tmp_path, sid)
    # The col-0 "state: open" is excluded; the indented item is "done", so
    # open_count=0 and total_count=1 (one indented state: line at item level).
    assert result is not None, (
        f"v1-tagged file with indented state: done must not be None; got {result}"
    )
    assert result.get("open_count") == 0, (
        f"Col-0 state: open must not inflate open_count; got {result}"
    )


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


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_scan_session_scratch_unreadable_subdir_forces_graceful_negative(git_repo, caplog):
    """Review: code-reviewer Finding 2 regression — a permission-denied
    subdirectory under tasks/ must not silently undercount scratch_count;
    it must force the existing None graceful-negative contract (an X-node
    upstream) rather than reading as a clean, possibly-undercounted int.
    Mirrors the chmod round-trip pattern used for _scan_open_memos /
    _check_idempotency / find_all_consumed_handoffs elsewhere in this diff.
    """
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    hidden_dir = git_repo.root / "tasks" / "locked-feature"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    hidden_file = hidden_dir / "scratch.md"
    hidden_file.write_text("scratch\n", encoding="utf-8")
    mtime_after = started_epoch_utc + 3600
    os.utime(hidden_file, (mtime_after, mtime_after))

    original_mode = hidden_dir.stat().st_mode
    os.chmod(hidden_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.branch_resolution"):
            result = _scan_session_scratch(git_repo.root, started_at)
    finally:
        os.chmod(hidden_dir, original_mode)

    assert result is None, (
        "a permission-denied subdirectory under tasks/ must force the "
        f"graceful-negative None contract, not a possibly-undercounted int; got {result!r}"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(hidden_dir) in r.message for r in warnings), (
        f"expected a WARNING naming the unreadable subdir; got {[r.message for r in warnings]}"
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


def test_step_2_67a_d_path_scratch_present(git_repo):
    """Branch 11 resolves D when started_at present and an untracked scratch file exists.

    TZ-PORTABILITY (F1 regression guard): started_at in UTC, file mtime 30 min
    later in UTC, asserted under TZ=America/Los_Angeles.  Naive rstrip('Z') parse
    skews the epoch by +7-8 h and would classify the file as before-threshold,
    returning count=0 instead of 1.  The tz-aware parse must return count=1 and
    node_type='D'.
    """
    sid = "sess-67a-int-d-001"
    git_repo.seed_session_shape(sid)
    git_repo.seed_started_at(sid, "2026-07-06T12:00:00Z")

    started_at_str = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at_str.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800  # 30 min after

    scratch_file = git_repo.root / "tasks" / "wip-feature" / "notes.md"
    scratch_file.parent.mkdir(parents=True, exist_ok=True)
    scratch_file.write_text("work in progress\n", encoding="utf-8")
    os.utime(scratch_file, (mtime_after, mtime_after))

    with _tz_forced_to_us_pacific():
        result = git_repo.invoke(sid)

    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_67A)
    assert node is not None
    assert node["type"] == "D", (
        f"Branch 11 must resolve D when started_at present and scratch file exists; "
        f"got {node['type']!r} — check tz-aware parse (naive rstrip skews US-Pacific by +7-8h)"
    )
    evidence = node.get("evidence", {})
    assert evidence.get("candidate_count") == 1, (
        f"candidate_count must be 1; got {evidence.get('candidate_count')!r}"
    )

    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    branch = branches.get("session_authored_transient_files")
    assert branch is not None
    assert branch["legible"] is True
    assert branch["node_type"] == "D"
    assert branch["signal_read"] == 1


def test_step_2_67a_keep_list_not_counted(git_repo):
    """Branch 11 does NOT count keep-listed tasks/feat/todo.md as a scratch file."""
    sid = "sess-67a-int-keeplist-001"
    git_repo.seed_session_shape(sid)
    git_repo.seed_started_at(sid, "2026-07-06T12:00:00Z")

    started_at_str = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at_str.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800

    # Keep-listed file — must NOT be counted.
    keep_file = git_repo.root / "tasks" / "some-feat" / "todo.md"
    keep_file.parent.mkdir(parents=True, exist_ok=True)
    keep_file.write_text("- [ ] task\n", encoding="utf-8")
    os.utime(keep_file, (mtime_after, mtime_after))

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_67A)
    assert node is not None
    assert node["type"] == "D", f"Branch 11 must be D; got {node['type']!r}"
    evidence = node.get("evidence", {})
    assert evidence.get("candidate_count") == 0, (
        f"todo.md must be keep-list excluded; candidate_count must be 0, "
        f"got {evidence.get('candidate_count')!r}"
    )


def test_step_2_67a_zero_path_no_qualifying_files(git_repo):
    """Branch 11 resolves D with count=0 when started_at present but no qualifying files."""
    sid = "sess-67a-int-zero-001"
    git_repo.seed_session_shape(sid)
    git_repo.seed_started_at(sid, "2026-07-06T12:00:00Z")
    # No tasks/ dir and no scratch files seeded.

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_67A)
    assert node is not None
    assert node["type"] == "D", f"Branch 11 must be D on zero-count; got {node['type']!r}"
    evidence = node.get("evidence", {})
    assert evidence.get("candidate_count") == 0


def test_step_2_67a_graceful_negative_absent_started_at(repo):
    """Branch 11 degrades to X-shaped no-signal when started_at is absent.

    Deliberate divergence from canonical self-clean first-commit fallback:
    first-commit is not reliably session start (no-commit / rebase cases).
    """
    sid = "sess-67a-int-gn-001"
    repo.seed_session_shape(sid)
    # No started_at file seeded — graceful-negative path.

    result = repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node = nodes.get(STEP_2_67A)
    assert node is not None
    assert node["type"] == "X", (
        f"Branch 11 must degrade to X when started_at absent; got {node['type']!r}"
    )

    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    branch = branches.get("session_authored_transient_files")
    assert branch is not None
    assert branch["legible"] is False
    assert branch["node_type"] == "X"
    assert branch["signal_read"] is None


# ---------------------------------------------------------------------------
# T1 — applicable_node_ids membership (op-spec §3, Option B; plan D2)
# ---------------------------------------------------------------------------


def test_applicable_node_ids_omits_chain_terminal_steps_single_session(repo):
    """Single-session (non-chain-terminal) disposition omits the chain-terminal-only
    D-nodes from applicable_node_ids, and the field is present + ordered (canonical
    step order, matching resolved_state.nodes).

    STEP_2_96 is included in the exclusion set alongside STEP_2_7/2_75/2_9C: it
    reads a completeness-checklist mirror that only exists post-pickup, so it is a
    guaranteed no-op in single-session even though it carries no evidence.chain_terminal
    flag of its own (op-spec §3 Branch-12-and-friends declared-membership coverage).
    """
    sid = "sess-applic-single-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"

    applicable = result["applicable_node_ids"]
    assert applicable, "applicable_node_ids must be present and non-empty"
    assert STEP_2_7 not in applicable
    assert STEP_2_75 not in applicable
    assert STEP_2_9C not in applicable
    assert STEP_2_96 not in applicable

    all_node_ids = [n["id"] for n in result["resolved_state"]["nodes"]]
    excluded = (STEP_2_7, STEP_2_75, STEP_2_9C, STEP_2_96)
    expected = [nid for nid in all_node_ids if nid not in excluded]
    assert applicable == expected, (
        "applicable_node_ids must preserve canonical step order pruned to membership"
    )


def test_applicable_node_ids_includes_chain_terminal_steps_chain_terminal(repo):
    """Chain-terminal disposition includes both remaining chain-terminal-only
    D-nodes in applicable_node_ids, matching the full node ledger 1:1.

    STEP_2_75 (the render-handoff-tracker D-node) was removed 2026-08-14 --
    see docs/plans/2026-08-14-retire-the-handoff-tracker-and-project-tracker-
    renders.md § C2 -- so it is no longer part of the node ledger at all and
    is not asserted on here.
    """
    sid = "sess-applic-chain-001"
    repo.seed_handoff("consumed-applic.md", consumed_by=sid)
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/consumed-applic.md",
    )
    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"

    applicable = result["applicable_node_ids"]
    assert STEP_2_7 in applicable
    assert STEP_2_9C in applicable

    all_node_ids = [n["id"] for n in result["resolved_state"]["nodes"]]
    assert applicable == all_node_ids, (
        "chain-terminal disposition must include every node in applicable_node_ids"
    )


# ---------------------------------------------------------------------------
# (T2) consumed_handoff_archive_scan — chain-terminal, missing handoff,
# predecessor handoff lives in archive/handoffs/ (swept) → still found
# ---------------------------------------------------------------------------


def test_consumed_handoff_found_in_archive_handoffs(repo):
    """session-shape pickup.happened=True with an EMPTY handoff triggers the
    _find_consumed_handoff scan; the predecessor handoff has already been swept
    to archive/handoffs/ (not state/handoffs/) — the scan must still find it and
    populate ctx.consumed_handoff + ctx.predecessor.
    """
    sid = "sess-archive-scan-001"
    archive_dir = repo.root / "archive" / "handoffs" / "2026-07"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_handoff = archive_dir / "consumed-archived.md"
    archived_handoff.write_text(
        "---\n"
        'title: "Test Handoff"\n'
        "created: 2026-07-01\n"
        "branch: work/test/2026-07-01\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "predecessor: sess-predecessor-999\n"
        "---\n\n# Handoff Body\n",
        encoding="utf-8",
    )
    # pickup.happened=True but handoff deliberately empty — forces the
    # _find_consumed_handoff scan fallback inside _resolve_branches.
    repo.seed_session_shape(sid, pickup_happened=True, handoff="")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == "archive/handoffs/2026-07/consumed-archived.md"
    assert resolved_state["predecessor"] == "sess-predecessor-999"


def test_find_consumed_handoff_scans_archive_dir(tmp_path):
    """_find_consumed_handoff scans archive/handoffs/ in addition to state/handoffs/."""
    sid = "sess-direct-archive-001"
    (tmp_path / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    archive_dir = tmp_path / "archive" / "handoffs" / "2026-06"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived_handoff = archive_dir / "old-consumed.md"
    archived_handoff.write_text(
        "---\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "predecessor: sess-pred-abc\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )

    path, fm = _find_consumed_handoff(tmp_path, sid)

    assert path == "archive/handoffs/2026-06/old-consumed.md"
    assert fm.get("predecessor") == "sess-pred-abc"


# ---------------------------------------------------------------------------
# (T3) consumed_handoff_anchored_match — body-prose mention of a sibling sid
# is NOT a match (anchored frontmatter-only check)
# ---------------------------------------------------------------------------


def test_find_consumed_handoff_ignores_body_prose_mention(tmp_path):
    """A handoff whose frontmatter consumed_by names a DIFFERENT session, but whose
    BODY prose happens to mention the target sid, must NOT match — the anchored
    frontmatter check (_get_handoff_consumed_by) only reads the frontmatter field,
    never body text.
    """
    target_sid = "sess-target-001"
    other_sid = "sess-other-002"
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    decoy = handoffs_dir / "decoy.md"
    decoy.write_text(
        "---\n"
        "status: consumed\n"
        f"consumed_by: {other_sid}\n"
        "---\n\n"
        f"# Handoff Body\n\nThis session was picked up alongside consumed_by: {target_sid} "
        "mentioned here in prose, not frontmatter.\n",
        encoding="utf-8",
    )

    path, fm = _find_consumed_handoff(tmp_path, target_sid)

    assert path == "", (
        "body-prose mention of consumed_by: <target_sid> must NOT match — "
        "only the frontmatter field is anchored-checked"
    )
    assert fm == {}


def test_find_consumed_handoff_matches_only_frontmatter(tmp_path):
    """Sanity check: a handoff whose frontmatter DOES name the target sid is found,
    even when a decoy handoff with a body-prose mention of the same sid is present.
    """
    target_sid = "sess-target-003"
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)

    decoy = handoffs_dir / "a-decoy.md"
    decoy.write_text(
        "---\nstatus: consumed\nconsumed_by: sess-unrelated\n---\n\n"
        f"Body prose mentions consumed_by: {target_sid} but is not frontmatter.\n",
        encoding="utf-8",
    )
    real = handoffs_dir / "b-real.md"
    real.write_text(
        f"---\nstatus: consumed\nclaimed_by: {target_sid}\npredecessor: sess-pred-real\n---\n\nBody.\n",
        encoding="utf-8",
    )

    path, fm = _find_consumed_handoff(tmp_path, target_sid)

    assert path == "state/handoffs/b-real.md"
    assert fm.get("predecessor") == "sess-pred-real"


# ---------------------------------------------------------------------------
# (T4) session_shape_handoff_path_peer_misattribution — session-shape.json
# pickup.handoff points at a temporally-adjacent CONCURRENT session's
# handoff (a peer's, consumed_by != sid), not this session's own predecessor.
# The resolver must reject the peer path and fall through to the anchored
# _find_consumed_handoff recovery scan rather than trusting handoff blindly.
# ---------------------------------------------------------------------------


def test_session_shape_handoff_path_peer_misattribution_rejected(repo):
    """pickup.handoff names a peer's in-flight handoff (consumed_by is a
    DIFFERENT sid, deployment_state: in_flight) — the resolver must NOT trust
    it.  The session's real predecessor handoff (consumed_by: sid) lives
    elsewhere on disk and must be found via the recovery scan instead.

    Originating incident: DoE-claude 2026-07-09 — a /workstream-complete
    nearly stamped a live peer workstream's handoff as shipped because the
    session-shape.json pickup.handoff pointed at the peer's handoff.
    """
    sid = "sess-owner-001"
    peer_sid = "sess-peer-002"

    # Peer's in-flight handoff — this is what pickup.handoff (wrongly)
    # points at.  consumed_by names the PEER, not this session.
    peer_handoff = repo.root / "state" / "handoffs" / "peer-in-flight.md"
    peer_handoff.write_text(
        "---\n"
        'title: "Peer In-Flight Handoff"\n'
        "created: 2026-07-09\n"
        "branch: work/peer/2026-07-09\n"
        "status: active\n"
        f"consumed_by: {peer_sid}\n"
        "deployment_state: in_flight\n"
        "predecessor: sess-peer-predecessor\n"
        "---\n\n# Peer Handoff Body\n",
        encoding="utf-8",
    )

    # This session's REAL predecessor handoff — consumed_by: sid, discoverable
    # only via the anchored recovery scan.
    repo.seed_handoff("real-predecessor.md", consumed_by=sid)

    # session-shape.json for sid: pickup.happened=True, handoff points at
    # the PEER's handoff (the misattribution bug's trigger condition).
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/peer-in-flight.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # The peer's path must NOT survive as the consumed handoff.
    assert resolved_state["consumed_handoff"] != "state/handoffs/peer-in-flight.md"
    # The resolver must have fallen through to the sid-owned handoff instead.
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-predecessor.md"

    # Bonus: step0 evidence records the rejection of the peer path for the
    # paper trail (receipt reader can see the misattribution was caught).
    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == "state/handoffs/peer-in-flight.md"
    )


def test_session_shape_handoff_path_matches_sid_trusted_directly(repo):
    """Happy path: session-shape.json pickup.handoff names a handoff whose
    consumed_by DOES match sid — the resolver trusts it directly (no fallback
    scan needed), and no rejection is recorded in step0 evidence.
    """
    sid = "sess-owner-direct-001"
    repo.seed_handoff(
        "owned-handoff.md",
        consumed_by=sid,
    )
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/owned-handoff.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == "state/handoffs/owned-handoff.md"

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert "session_shape_handoff_rejected" not in disposition_branch["evidence"]


def test_session_shape_handoff_path_absent_phantom_falls_through(repo):
    """session-shape.json pickup.handoff names an in-repo-shaped path that
    does NOT exist on disk at all (a phantom/stale chain field), while this
    session's real predecessor handoff (consumed_by: sid) lives elsewhere on
    disk under a different filename.  The resolver must not trust the absent
    path and must fall through to the anchored _find_consumed_handoff recovery
    scan, exactly as it does for the peer-misattribution case (T4) above.

    Originating incident: cross-repo/inbox/2026-07-11-example-retrieval-repo-em-wsc-resolve-
    stale-consumed-handoff-path.md — a chain-terminal /workstream-complete
    mis-shipped a phantom handoff because consumed_handoff_path pointed at an
    in-repo-shaped but ABSENT file, while the real consumed_by:<sid> handoff
    was left unshipped.  Locks the stale/phantom-chain-field-beats-current-sid-
    grep regression; the resolution fix this test locks is commit b606e4b.
    """
    sid = "sess-owner-phantom-001"

    # This session's REAL predecessor handoff — consumed_by: sid, discoverable
    # only via the anchored recovery scan.
    repo.seed_handoff("real-predecessor.md", consumed_by=sid)

    # session-shape.json for sid: pickup.happened=True, handoff points at
    # an in-repo-shaped path that is NEVER WRITTEN to disk (the memo's exact
    # phantom shape).
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/2026-07-11_223816_phantom.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # The phantom path must NOT survive as the consumed handoff.
    assert resolved_state["consumed_handoff"] != "state/handoffs/2026-07-11_223816_phantom.md"
    # The resolver must have fallen through to the sid-owned handoff instead.
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-predecessor.md"

    # Bonus: step0 evidence records the rejection of the phantom path for the
    # paper trail (receipt reader can see the phantom was caught).
    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == "state/handoffs/2026-07-11_223816_phantom.md"
    )


def test_session_shape_chain_terminal_reverts_to_single_session_when_no_sid_handoff(repo):
    """session_shape.pickup.happened hypothesizes chain-terminal, but the
    handoff it points at is a FOREIGN peer's (rejected by the sid-verified
    guard) and the anchored recovery scan finds NO consumed_by == sid handoff
    anywhere on disk either.  On a shared concurrent-EM work/* branch this
    session owns no consumed predecessor at all — the disposition must revert
    to single-session rather than staying stuck at chain-terminal with an
    empty consumed_handoff_path, which would otherwise produce a spurious
    wsc_commit STEP_2_7 "evidence gap" op_tail failure on what is really a
    single-session close.
    """
    sid = "sess-owner-no-predecessor-001"
    peer_sid = "sess-peer-in-flight-002"

    # Peer's in-flight handoff — this is what pickup.handoff (wrongly)
    # points at.  consumed_by names the PEER, not this session.
    peer_handoff = repo.root / "state" / "handoffs" / "peer-in-flight.md"
    peer_handoff.write_text(
        "---\n"
        'title: "Peer In-Flight Handoff"\n'
        "created: 2026-07-09\n"
        "branch: work/peer/2026-07-09\n"
        "status: consumed\n"
        f"consumed_by: {peer_sid}\n"
        "deployment_state: in_flight\n"
        "predecessor: sess-peer-predecessor\n"
        "---\n\n# Peer Handoff Body\n",
        encoding="utf-8",
    )

    # No handoff owned by sid exists anywhere — the recovery scan will also
    # come up empty.

    # session-shape.json for sid: pickup.happened=True, handoff points at
    # the PEER's handoff (rejected by the sid-verified guard).
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/peer-in-flight.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == ""

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    evidence = disposition_branch["evidence"]
    assert evidence.get("session_shape_handoff_rejected") == "state/handoffs/peer-in-flight.md"
    assert evidence.get("disposition_reverted_to_single_session") is True


# Review: code-reviewer F1 — added missing test for the handoff-path-does-not-
# exist-on-disk rejection sub-case (guard's hf_abs.exists() short-circuit was
# untested; both prior tests use handoffs that exist on disk).
def test_session_shape_handoff_path_missing_file_rejected(repo):
    """pickup.handoff names a file that does not exist on disk (swept,
    archived, or renamed between producer-write and resolver-read) — the
    guard's hf_abs.exists() check must reject it and the resolver must fall
    through to the anchored recovery scan for the session's real predecessor.
    """
    sid = "sess-owner-vanished-001"

    # This session's REAL predecessor handoff — consumed_by: sid, discoverable
    # only via the anchored recovery scan.
    repo.seed_handoff("real-predecessor.md", consumed_by=sid)

    # session-shape.json for sid: pickup.happened=True, handoff points at
    # a path that was never written (or has since vanished).
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/vanished.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # The vanished path must NOT survive as the consumed handoff.
    assert resolved_state["consumed_handoff"] != "state/handoffs/vanished.md"
    # The resolver must have fallen through to the sid-owned handoff instead.
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-predecessor.md"

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == "state/handoffs/vanished.md"
    )


# Review: code-reviewer F2 — added missing test for the handoff-exists-but-no-
# consumed_by-field rejection sub-case (distinct code path from the mismatch
# test — _get_handoff_consumed_by returns None here, not a different sid).
def test_session_shape_handoff_path_unconsumed_no_consumed_by_rejected(repo):
    """pickup.handoff names a handoff that exists on disk but has NO
    consumed_by field at all (status: active, unconsumed — the not-yet-picked-
    up race shape).  _get_handoff_consumed_by returns None for it, so
    None == sid is False and the guard must reject, falling through to the
    anchored recovery scan for the session's real predecessor.
    """
    sid = "sess-owner-unconsumed-001"

    # Decoy handoff — exists on disk, status: active, no consumed_by field.
    decoy_handoff = repo.root / "state" / "handoffs" / "unconsumed-decoy.md"
    decoy_handoff.write_text(
        "---\n"
        'title: "Unconsumed Decoy Handoff"\n'
        "created: 2026-07-09\n"
        "branch: work/peer/2026-07-09\n"
        "status: active\n"
        "---\n\n# Decoy Handoff Body\n",
        encoding="utf-8",
    )

    # This session's REAL predecessor handoff — consumed_by: sid, discoverable
    # only via the anchored recovery scan.
    repo.seed_handoff("real-predecessor.md", consumed_by=sid)

    # session-shape.json for sid: pickup.happened=True, handoff points at
    # the unconsumed decoy.
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/unconsumed-decoy.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # The decoy path must NOT survive as the consumed handoff.
    assert resolved_state["consumed_handoff"] != "state/handoffs/unconsumed-decoy.md"
    # The resolver must have fallen through to the sid-owned handoff instead.
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-predecessor.md"

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == "state/handoffs/unconsumed-decoy.md"
    )


def test_session_shape_own_handoff_prearchived_with_sibling_decoy_in_state(repo):
    """Fleet-sweep pre-archive + sibling-decoy mis-anchor (the exact reported
    shape).  pickup.handoff points at THIS session's own handoff at its
    original state/handoffs/ location, but a concurrent fleet sweep has already
    moved that handoff to archive/handoffs/ (consumed_by: sid intact, still
    deployment_state: in_flight).  Meanwhile a DIFFERENT session's newer consumed
    handoff sits in state/handoffs/ as the "newest remaining consumed" decoy.

    The resolver must:
      - reject the now-vanished state/ path (hf_abs.exists() is False there),
      - recovery-scan and anchor on THIS session's own handoff in archive/
        (consumed_by == sid), NOT the newer sibling in state/,
      - stay chain-terminal with the archived own path as consumed_handoff.

    Guards against the mis-anchor where wsc_resolve, finding no state/handoffs/
    entry for the current sid, falls back to the newest *remaining* consumed
    handoff (a sibling's) — which wsc_commit's sid-guarded archive step then
    fail-loud's on, halting the ceremony tail.  The sid-filtered recovery scan
    (archive-inclusive) is what prevents it.

    Reported: cross-repo/inbox/2026-07-09-claude-central-em-wsc-resolve-mis-anchor-
    prearchived-handoff.md (claude-central-em).  Substantively fixed upstream by
    357f60c (sid gate) + 5ee6216 (revert) + 979d08b (archive scan); this test is
    the exact-scenario regression pin — no prior test combines a pre-archived own
    handoff with a newer sibling decoy in state/.
    """
    sid = "sess-owner-prearchived-001"
    sibling_sid = "sess-sibling-newer-002"

    # THIS session's own handoff — already swept to archive/ by a concurrent
    # fleet sweep; consumed_by == sid intact, still in_flight.
    own_archived = (
        repo.root / "archive" / "handoffs" / "2026-07" / "own-160010.md"
    )
    own_archived.parent.mkdir(parents=True, exist_ok=True)
    own_archived.write_text(
        "---\n"
        'title: "Own Pre-Archived Handoff"\n'
        "created: 2026-07-09\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "deployment_state: in_flight\n"
        "predecessor: sess-own-predecessor\n"
        "---\n\n# Own Handoff Body\n",
        encoding="utf-8",
    )

    # A DIFFERENT session's newer consumed handoff — the "newest remaining"
    # decoy in state/handoffs/ that a non-sid-scoped fallback would mis-anchor on.
    sibling_decoy = repo.root / "state" / "handoffs" / "sibling-163651.md"
    sibling_decoy.write_text(
        "---\n"
        'title: "Sibling Newer Consumed Handoff"\n'
        "created: 2026-07-09\n"
        "status: consumed\n"
        f"claimed_by: {sibling_sid}\n"
        "deployment_state: in_flight\n"
        "predecessor: sess-sibling-predecessor\n"
        "---\n\n# Sibling Handoff Body\n",
        encoding="utf-8",
    )

    # session-shape.json for sid: pickup.happened=True, handoff points at the
    # PRE-ARCHIVE state/ location (now vanished — the handoff lives in archive/).
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff="state/handoffs/own-160010.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # Must anchor on the OWN archived handoff — never the sibling decoy.
    assert (
        resolved_state["consumed_handoff"]
        == "archive/handoffs/2026-07/own-160010.md"
    )
    assert resolved_state["consumed_handoff"] != "state/handoffs/sibling-163651.md"
    assert resolved_state["predecessor"] == "sess-own-predecessor"

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == "state/handoffs/own-160010.md"
    )


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


def test_session_shape_handoff_path_absolute_foreign_repo_rejected(repo, tmp_path):
    """pickup.handoff is an ABSOLUTE path pointing OUTSIDE worktree_root,
    at a real handoff file in a separate tmp dir (simulating a foreign repo,
    e.g. Example-retrieval-repo) whose frontmatter even has consumed_by: <sid> — proving
    existence+consumed_by alone would wrongly pass without the containment
    guard.  The resolver must NOT bind the foreign path; it must fall through
    to this session's real in-repo handoff instead.
    """
    sid = "sess-owner-foreign-abs-001"

    # A foreign repo's handoff, entirely outside repo.root, with consumed_by
    # matching sid (the trap: existence + consumed_by == sid alone would pass).
    foreign_repo_root = tmp_path / "foreign-repo"
    foreign_handoffs_dir = foreign_repo_root / "state" / "handoffs"
    foreign_handoffs_dir.mkdir(parents=True, exist_ok=True)
    foreign_handoff = foreign_handoffs_dir / "foreign-consumed.md"
    foreign_handoff.write_text(
        "---\n"
        'title: "Foreign Repo Handoff"\n'
        "created: 2026-07-10\n"
        "branch: work/foreign/2026-07-10\n"
        "status: consumed\n"
        f"consumed_by: {sid}\n"
        "predecessor: sess-foreign-predecessor\n"
        "---\n\n# Foreign Repo Handoff Body\n",
        encoding="utf-8",
    )

    # This session's REAL in-repo predecessor handoff — consumed_by: sid,
    # discoverable via the anchored recovery scan once the foreign path rejects.
    repo.seed_handoff("real-predecessor.md", consumed_by=sid)

    # session-shape.json for sid: pickup.happened=True, handoff is the
    # ABSOLUTE foreign path — the foreign-repo bleed trigger condition.
    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff=str(foreign_handoff),
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]

    # The foreign path must NOT survive as the consumed handoff.
    assert resolved_state["consumed_handoff"] != str(foreign_handoff)
    # The resolver must have fallen through to the sid-owned in-repo handoff.
    assert resolved_state["consumed_handoff"] == "state/handoffs/real-predecessor.md"

    # The escape must be distinguishable in the receipt from the generic
    # rejection case (peer misattribution / missing file / consumed_by mismatch).
    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert (
        disposition_branch["evidence"].get("consumed_handoff_outside_repo")
        == str(foreign_handoff)
    )
    assert (
        disposition_branch["evidence"].get("session_shape_handoff_rejected")
        == str(foreign_handoff)
    )


def test_session_shape_handoff_path_traversal_foreign_repo_rejected(repo, tmp_path):
    """pickup.handoff is a RELATIVE path using ../ traversal to escape
    worktree_root, at a real handoff file (with consumed_by: <sid>) sitting
    just outside repo.root — the second foreign-repo bleed vector alongside
    the absolute-path case.  No sid-owned handoff exists in-repo, so the
    resolver must reject the escaping path and revert to single-session
    rather than binding the foreign file.
    """
    sid = "sess-owner-foreign-traversal-001"

    # A sibling directory outside repo.root, reachable via ../ traversal from
    # worktree_root.  repo.root is tmp_path / "repo" (see the `repo` fixture).
    sibling_dir = repo.root.parent / "sibling-foreign-repo"
    sibling_dir.mkdir(parents=True, exist_ok=True)
    foreign_handoff = sibling_dir / "escaped-consumed.md"
    foreign_handoff.write_text(
        "---\n"
        'title: "Traversal-Escaped Foreign Handoff"\n'
        "created: 2026-07-10\n"
        "branch: work/foreign/2026-07-10\n"
        "status: consumed\n"
        f"consumed_by: {sid}\n"
        "predecessor: sess-foreign-predecessor\n"
        "---\n\n# Escaped Handoff Body\n",
        encoding="utf-8",
    )

    # ../ traversal from worktree_root (repo.root) to the sibling dir.
    traversal_path = "../sibling-foreign-repo/escaped-consumed.md"

    repo.seed_session_shape(
        sid,
        pickup_happened=True,
        handoff=traversal_path,
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    # No sid-owned handoff exists anywhere inside the repo — must revert to
    # single-session rather than binding the escaped foreign path.
    assert result["disposition"] == "single-session"
    resolved_state = result["resolved_state"]
    assert resolved_state["consumed_handoff"] == ""
    assert resolved_state["consumed_handoff"] != str(foreign_handoff)

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    evidence = disposition_branch["evidence"]
    assert evidence.get("consumed_handoff_outside_repo") == traversal_path
    assert evidence.get("session_shape_handoff_rejected") == traversal_path
    assert evidence.get("disposition_reverted_to_single_session") is True


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


def test_foreign_absolute_never_in_plural_paths_end_to_end(repo, tmp_path):
    """End-to-end incident shape: session-shape pickup.handoff is an ABSOLUTE
    foreign path (consumed_by: sid) AND a real local sid-owned predecessor
    exists.  The resolver must resolve the LOCAL handoff and the plural
    consumed_handoff_paths (receipt + STEP_2_7) must contain ONLY the in-repo,
    sid-owned, repo-relative path — never the foreign absolute path, never a
    relativized phantom.  This is the assertion the pre-existing T5 test omits
    (it checked only the scalar consumed_handoff).
    """
    sid = "sess-e2e-foreign-plural-001"

    foreign_repo = tmp_path / "example-cockpit-repo"
    foreign_dir = foreign_repo / "state" / "handoffs"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    basename = "2026-07-13_124503_dashboard-placement-rubric-ratify.md"
    foreign_hf = foreign_dir / basename
    foreign_hf.write_text(
        f"---\nstatus: consumed\nconsumed_by: {sid}\n---\n\nbody\n", encoding="utf-8"
    )

    repo.seed_handoff("real-local-predecessor.md", consumed_by=sid)
    repo.seed_session_shape(sid, pickup_happened=True, handoff=str(foreign_hf))

    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    rs = result["resolved_state"]

    plural = rs.get("consumed_handoffs") or []
    # ONLY the in-repo sid-owned handoff, repo-relative.
    assert plural == ["state/handoffs/real-local-predecessor.md"]
    assert str(foreign_hf) not in plural
    assert not any(basename in p for p in plural)

    branches = {b["branch_id"]: b for b in rs["resolved_branches"]}
    ev = branches["WSC_DISPOSITION"]["evidence"]
    ev_plural = ev.get("consumed_handoff_paths") or []
    assert str(foreign_hf) not in ev_plural
    assert not any(basename in p for p in ev_plural)
    # The receipt scalar is consistent with the sanitized plural.
    assert ev.get("consumed_handoff_path") == "state/handoffs/real-local-predecessor.md"


def test_foreign_only_pickup_reverts_single_session_plural_empty(repo, tmp_path):
    """Incident's no-local-fallthrough variant: pickup.handoff is a foreign
    ABSOLUTE path (consumed_by: sid) and NO sid-owned handoff exists anywhere
    in-repo.  Disposition must revert to single-session and the plural
    consumed_handoff_paths must be EMPTY — never left stuck at chain-terminal
    with a foreign/phantom entry (the STEP_2_7 mis-stamp trigger).
    """
    sid = "sess-e2e-foreign-empty-001"

    foreign_repo = tmp_path / "example-cockpit-repo"
    foreign_dir = foreign_repo / "state" / "handoffs"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    foreign_hf = foreign_dir / "foreign-only.md"
    foreign_hf.write_text(
        f"---\nstatus: consumed\nconsumed_by: {sid}\n---\n\nbody\n", encoding="utf-8"
    )

    repo.seed_session_shape(sid, pickup_happened=True, handoff=str(foreign_hf))

    result = repo.invoke(sid)
    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    rs = result["resolved_state"]
    assert (rs.get("consumed_handoffs") or []) == []
    assert rs.get("consumed_handoff") == ""


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


def test_resolved_state_sid_matches_input_sid(repo):
    """resolved_state["sid"] must equal the sid passed into the handler —
    closes the resolved_state.sid = null defect (Defect B)."""
    sid = "sess-sid-threading-001"
    repo.seed_session_shape(sid, pickup_happened=False)

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["resolved_state"]["sid"] == sid


# ---------------------------------------------------------------------------
# C4 (wsc-dag-pickup-n-handoffs) — N-handoff resolve integration + all
# the Staff Engineer-finding regression cases (F0 dedup, F2 STEP_2_7 plural evidence —
# both the STEP_0 evidence dict AND the STEP_2_7 node's own evidence dict,
# folded into test_n_handoffs_both_found_live_and_archived_scalar_is_first
# below, F6 grep-fallback plurality) + C1's F4 divergent-field round-trip.
# ---------------------------------------------------------------------------


def test_n_handoffs_both_found_live_and_archived_scalar_is_first(repo):
    """Two handoffs consumed_by: <sid> — one live in state/handoffs/, one already
    swept to archive/handoffs/ — both land in ctx.consumed_handoffs, and the
    scalar consumed_handoff fallback equals the first element (pickup-path-first
    ordering, unchanged from today's scalar-fallback contract)."""
    sid = "sess-n-handoff-both-001"

    live = repo.root / "state" / "handoffs" / "live-consumed.md"
    live.write_text(
        "---\n"
        'title: "Live Handoff"\n'
        "created: 2026-07-12\n"
        "branch: work/test/2026-07-12\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "predecessor: sess-pred-live\n"
        "---\n\n# Handoff Body\n",
        encoding="utf-8",
    )

    archive_dir = repo.root / "archive" / "handoffs" / "2026-07"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived = archive_dir / "archived-consumed.md"
    archived.write_text(
        "---\n"
        'title: "Archived Handoff"\n'
        "created: 2026-07-01\n"
        "branch: work/test/2026-07-01\n"
        "status: consumed\n"
        f"claimed_by: {sid}\n"
        "predecessor: sess-pred-archived\n"
        "---\n\n# Handoff Body\n",
        encoding="utf-8",
    )

    repo.seed_session_shape(sid, pickup_happened=True, handoff="")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved_state = result["resolved_state"]
    consumed_handoffs = resolved_state["consumed_handoffs"]
    assert len(consumed_handoffs) == 2
    assert "state/handoffs/live-consumed.md" in consumed_handoffs
    assert "archive/handoffs/2026-07/archived-consumed.md" in consumed_handoffs
    # Scalar fallback stays green — equals the FIRST element.
    assert resolved_state["consumed_handoff"] == consumed_handoffs[0]

    # the Staff Engineer F2 producer side: the STEP_2_7 node's own evidence dict must carry
    # the plural key too (not just STEP_0's) — this is chain-terminal, so the
    # "consumed_handoffs_paths" branch_resolution.py:2655-2656 guard fires.
    nodes = {n["id"]: n for n in resolved_state["nodes"]}
    step_2_7_node = nodes[STEP_2_7]
    assert step_2_7_node["evidence"]["consumed_handoffs_paths"] == consumed_handoffs


def test_n_handoffs_peer_poison_only_owned_survives(repo):
    """One owned (consumed_by: sid) + one foreign (consumed_by: other-sid) handoff
    — only the owned handoff survives into ctx.consumed_handoffs; the foreign
    handoff never poisons the set (per-element rejection, not list-wide)."""
    sid = "sess-n-handoff-poison-001"
    other_sid = "sess-peer-999"

    repo.seed_handoff("owned.md", consumed_by=sid)
    repo.seed_handoff("foreign.md", consumed_by=other_sid)
    repo.seed_session_shape(sid, pickup_happened=True, handoff="")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    consumed_handoffs = result["resolved_state"]["consumed_handoffs"]
    assert consumed_handoffs == ["state/handoffs/owned.md"]


def test_n_handoffs_dedup_pickup_named_handoff_also_found_by_scan(repo):
    """the Staff Engineer F0: session-shape.json pickup.handoff names a handoff that the
    archive-aware scan ALSO independently finds (same file, both sources own-sid
    verified) — the union must dedup to exactly ONE entry, pickup-path-first."""
    sid = "sess-n-handoff-dedup-001"

    repo.seed_handoff(
        "shared.md", consumed_by=sid,
    )
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/shared.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    consumed_handoffs = result["resolved_state"]["consumed_handoffs"]
    assert consumed_handoffs == ["state/handoffs/shared.md"], (
        f"dedup must yield exactly ONE entry (pickup-path-first), not a double-count; "
        f"got: {consumed_handoffs}"
    )


# REMOVED 2026-07-29 (kill-list op removal): test_step_2_7_node_carries_plural_evidence_real_read_path
# asserted the STEP_2_7 plural-evidence contract against wsc_commit._read_step_2_7_evidence,
# its only reader. wsc_commit.py was deleted as a dead op module and that function had no
# live counterpart in the single-pass tail, so the contract has no surviving second party.
# The producer side (STEP_2_7 carrying consumed_handoffs_paths) is covered by
# test_n_handoffs_both_found_live_and_archived_scalar_is_first above (Review:
# code-reviewer 2026-07-29 — the original "still covered above" claim here was false;
# no test asserted the STEP_2_7 node's own evidence dict until this fix).

def test_grep_fallback_returns_all_matches_not_just_first(git_repo):
    """the Staff Engineer F6: with session-shape.json absent entirely (triggering
    _grep_disposition), seed >=2 anchored consumed_by: <sid> handoffs — the
    fallback path must also return ALL matches, not just grep_paths[0]."""
    sid = "sess-grep-fallback-n-001"

    git_repo.seed_handoff("grep-first.md", consumed_by=sid)
    git_repo.seed_handoff("grep-second.md", consumed_by=sid)
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add handoffs"], cwd=str(git_repo.root),
                   capture_output=True, check=True)

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    consumed_handoffs = result["resolved_state"]["consumed_handoffs"]
    assert len(consumed_handoffs) == 2
    assert "state/handoffs/grep-first.md" in consumed_handoffs
    assert "state/handoffs/grep-second.md" in consumed_handoffs


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


def test_c3_interleaved_repro_yields_x_node_never_contaminated_scope(git_repo):
    """b15db349 shape: alternating session/foreign trailerless commits, HEAD
    ending on a foreign commit — the B-wave scoping site must emit an X-node
    (SCOPING_METHOD_AMBIGUOUS) with foreign_commit_count > 0, and must NEVER
    thread a contaminated sha_range/partition_boundaries into the B1
    dispatch-plan pre_resolved_evidence."""
    sid = "sess-c3-interleaved-001"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    git_repo.seed_session_shape(sid, pickup_happened=False)

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

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved_state = result["resolved_state"]
    assert resolved_state["scoping_method"] == SCOPING_METHOD_AMBIGUOUS
    assert resolved_state["foreign_commit_count"] > 0

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    review_wave_branch = branches["review_wave_scale"]
    assert review_wave_branch["node_type"] == "X"
    assert review_wave_branch["evidence"]["foreign_commit_count"] > 0

    nodes = {n["id"]: n for n in resolved_state["nodes"]}
    assert nodes[STEP_2_9A]["type"] == "X"
    assert nodes[STEP_2_9A]["missing_signal"] == "SESSION_COMMIT_SCOPE"

    b_pre_resolved = result["b_pre_resolved"]
    assert b_pre_resolved["brightline_verdict"] == "AMBIGUOUS-SCOPE"
    assert b_pre_resolved["sha_range"] is None
    assert b_pre_resolved["partition_boundaries"] == []
    assert b_pre_resolved["scoping_method"] == SCOPING_METHOD_AMBIGUOUS
    assert b_pre_resolved["foreign_commit_count"] > 0

    receipt_path = git_repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["scoping_method"] == SCOPING_METHOD_AMBIGUOUS
    assert receipt["foreign_commit_count"] > 0
    errors = validate(receipt)
    assert errors == [], f"receipt failed validate(): {errors}"


def test_c3_trailerless_clean_resolves_via_started_at_range(git_repo):
    """Trailerless session whose started_at range is entirely foreign-free
    and contiguous must resolve via SCOPING_METHOD_STARTED_AT_RANGE — never
    a silent empty scope (sha_range absent, scoping_method unset) — and must
    carry a non-empty sha_range in the B1 pre_resolved_evidence.  diff_loc/
    commit_count are recomputed over the recovered candidate_range (not via
    the trailer-grep helpers, which would stay 0 for this trailerless commit
    — see test_c3_trailerless_large_range_scores_partition_mandatory for the
    size-flips-the-verdict regression case) — sha_range is still the primary
    signal that proves this session's true scope was recovered.

    In the full pipeline, known_scope_paths is this sid's OWN trailer-scoped
    touched_paths (empty for a trailerless commit) UNIONED with its consumed-
    handoff paths — unlike C1's direct analyze_session_scoping() unit tests,
    a test here cannot inject an arbitrary known_scope_paths override.  So
    this scenario uses a chain-terminal session whose trailerless commit
    touches its OWN consumed handoff path — the one real path the wiring
    puts into known_scope_paths — keeping the started_at range foreign-free.
    started_at is pinned to just AFTER the git_repo fixture's own real-time
    init commit so the fixture's own init commit falls OUTSIDE the range."""
    sid = "sess-c3-trailerless-clean-001"
    # 1.1s buffer after the git_repo fixture's own init commit — git commit
    # timestamps have 1-second granularity and `git log --since` is
    # inclusive-equal, so a started_at captured in the same second as the
    # init commit would (correctly, per the wiring) count that commit as
    # in-range and foreign — the wiring's job is exactly to catch that; this
    # test wants a clean range, so it buffers past it.
    time.sleep(1.1)
    started_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    git_repo.seed_started_at(sid, started_at)
    handoff_path = git_repo.seed_handoff("consumed-clean.md", consumed_by=sid)
    git_repo.seed_session_shape(
        sid, pickup_happened=True, handoff="state/handoffs/consumed-clean.md",
    )
    time.sleep(1.1)
    handoff_path.write_text(
        handoff_path.read_text(encoding="utf-8") + "\nUpdated body.\n",
        encoding="utf-8",
    )
    _commit(git_repo.root, "trailerless work, no Session-Id trailer")

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved_state = result["resolved_state"]
    assert resolved_state["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    assert resolved_state["foreign_commit_count"] == 0

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    review_wave_branch = branches["review_wave_scale"]
    assert review_wave_branch["node_type"] == "D"

    b_pre_resolved = result["b_pre_resolved"]
    assert b_pre_resolved["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    assert b_pre_resolved["foreign_commit_count"] == 0
    # Not a silent empty scope — the recovered started_at range IS the scope
    # (sha_range non-empty), even though the trailer-grep commit_count stays
    # 0 (the commit carries no Session-Id trailer for grep to match — that's
    # exactly the gap SCOPING_METHOD_STARTED_AT_RANGE recovers from).
    assert b_pre_resolved["sha_range"]
    assert b_pre_resolved["sha_range"].endswith("^..HEAD")

    receipt_path = git_repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    assert receipt["foreign_commit_count"] == 0
    errors = validate(receipt)
    assert errors == [], f"receipt failed validate(): {errors}"


def test_c3_trailerless_large_range_scores_partition_mandatory(git_repo):
    """Review: code-reviewer Finding 3 — a LARGE trailerless-clean session
    recovered via SCOPING_METHOD_STARTED_AT_RANGE must be scored
    PARTITION-MANDATORY, not single-reviewer-ok.

    Regression for the trailer-grep-vs-range-recompute bug: SCOPING_METHOD_
    STARTED_AT_RANGE is reached exactly when the Session-Id trailer is
    unreliable, so diff_loc/commit_count computed via the trailer-grep
    helpers (_session_diff_loc/session_commit_count_attributed) return 0 BY
    CONSTRUCTION on this branch — silently under-scoring a large trailerless
    session as single-reviewer-ok regardless of true size.  This test's
    single trailerless commit alone exceeds _BRIGHTLINE_LOC (500), so the
    fix's range-scoped recompute (git rev-list/log --stat over
    candidate_range) must flip the verdict to PARTITION-MANDATORY where the
    old trailer-grep wiring would report diff_loc=0/commit_count=0 and stay
    single-reviewer-ok.
    """
    sid = "sess-c3-trailerless-large-001"
    time.sleep(1.1)
    started_at = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    git_repo.seed_started_at(sid, started_at)
    handoff_path = git_repo.seed_handoff("consumed-large.md", consumed_by=sid)
    git_repo.seed_session_shape(
        sid, pickup_happened=True, handoff="state/handoffs/consumed-large.md",
    )
    time.sleep(1.1)
    # Large trailerless diff — >_BRIGHTLINE_LOC (500) insertions, touching
    # the session's own consumed-handoff path so the started_at range stays
    # foreign-free (known_scope_paths membership, mirrors the sibling test).
    large_body = "\n".join(f"line {i}" for i in range(600))
    handoff_path.write_text(
        handoff_path.read_text(encoding="utf-8") + "\n" + large_body + "\n",
        encoding="utf-8",
    )
    _commit(git_repo.root, "large trailerless work, no Session-Id trailer")

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved_state = result["resolved_state"]
    assert resolved_state["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    assert resolved_state["foreign_commit_count"] == 0

    b_pre_resolved = result["b_pre_resolved"]
    assert b_pre_resolved["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    assert b_pre_resolved["diff_loc"] >= 500
    assert b_pre_resolved["brightline_verdict"] == "PARTITION-MANDATORY"

    receipt_path = git_repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["scoping_method"] == SCOPING_METHOD_STARTED_AT_RANGE
    errors = validate(receipt)
    assert errors == [], f"receipt failed validate(): {errors}"


def test_c3_clean_trailer_session_keeps_session_id_trailer_method(git_repo):
    """A session whose commits carry a reliable Session-Id trailer keeps the
    existing grep-based scoping — SCOPING_METHOD_TRAILER — as the existing
    B-wave computation is left as a no-op (C1's documented behavior)."""
    sid = "sess-c3-trailer-001"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    git_repo.seed_session_shape(sid, pickup_happened=False)

    p = git_repo.root / "coordinator_core" / "trailer_work.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("trailer-tagged work\n", encoding="utf-8")
    _commit(git_repo.root, f"trailer work\n\nSession-Id: {sid}", date="2020-06-01T00:00:00Z")

    result = git_repo.invoke(sid)
    assert result["exit_code"] == 0

    resolved_state = result["resolved_state"]
    assert resolved_state["scoping_method"] == SCOPING_METHOD_TRAILER
    assert resolved_state["foreign_commit_count"] == 0

    branches = {b["branch_id"]: b for b in resolved_state["resolved_branches"]}
    review_wave_branch = branches["review_wave_scale"]
    assert review_wave_branch["node_type"] == "D"

    b_pre_resolved = result["b_pre_resolved"]
    assert b_pre_resolved["scoping_method"] == SCOPING_METHOD_TRAILER
    assert b_pre_resolved["sha_range"] == ""
    assert b_pre_resolved["commit_count"] >= 1

    receipt_path = git_repo.root / result["receipt_path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["scoping_method"] == SCOPING_METHOD_TRAILER
    errors = validate(receipt)
    assert errors == [], f"receipt failed validate(): {errors}"


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


@_SKIP_CHMOD_UNRELIABLE
def test_open_memos_unreadable_inbox_not_clean_empty(repo, caplog):
    """An unreadable cross-repo/inbox/ must not read as open_count=0/clean.

    open_count: 0 is ambiguous between "no open memos" and "could not scan" —
    a permission-denied inbox must surface a distinct scan_error and force
    the D-node's signal_read True, rather than the D-node proceeding as if
    the inbox were genuinely empty.
    """
    sid = "sess-memos-unreadable-001"
    repo.seed_session_shape(sid)
    repo.seed_open_memo("2026-07-06-hidden-memo.md", title="Hidden Memo")

    inbox = repo.root / "cross-repo" / "inbox"
    original_mode = inbox.stat().st_mode
    os.chmod(inbox, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.branch_resolution"):
            result = repo.invoke(sid)
    finally:
        os.chmod(inbox, original_mode)

    assert result["exit_code"] == 0
    assert result["open_memos_count"] == 0, (
        "open_count reflects zero SUCCESSFULLY enumerated memos, not a claim "
        "that the inbox is empty — scan_error carries the distinguishing signal"
    )

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node_65a = nodes.get("step_2.65a")
    assert node_65a is not None
    assert node_65a["evidence"].get("scan_error"), (
        "scan_error must be populated for an unreadable inbox — "
        f"got evidence={node_65a['evidence']!r}"
    )

    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    open_memos_branch = branches["open_memos_exist"]
    assert open_memos_branch["signal_read"] is True, (
        "signal_read must be forced True on a scan failure — a genuinely "
        "clean/empty inbox must not be indistinguishable from an unreadable one"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(inbox) in r.message for r in warnings), (
        f"expected a WARNING naming the unreadable inbox; got {[r.message for r in warnings]}"
    )


@_SKIP_CHMOD_UNRELIABLE
def test_idempotency_guard_unreadable_subtree_forces_conservative_fire(repo, caplog):
    """A completion entry hidden behind an unreadable archive/completed/
    subdir must not silently look like "no prior completion entry found" —
    idempotency_guard_fired must be forced True (conservative) rather than
    incorrectly proceeding to re-complete an already-recorded session.
    """
    sid = "sess-idem-unreadable-001"
    slug = "hidden-plan-slug"
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/h.md")
    repo.seed_handoff("h.md", consumed_by=sid, chain=slug)

    hidden_dir = repo.root / "archive" / "completed" / "2020-01-hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    (hidden_dir / "prior.md").write_text(
        f"---\ntitle: Test\nchain: {slug}\n---\n\nEntry.\n", encoding="utf-8",
    )

    original_mode = hidden_dir.stat().st_mode
    os.chmod(hidden_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.branch_resolution"):
            result = repo.invoke(sid)
    finally:
        os.chmod(hidden_dir, original_mode)

    assert result["exit_code"] == 0
    assert result["idempotency_guard_fired"] is True, (
        "an unreadable archive/completed subtree must force the guard to "
        "fire conservatively rather than silently reporting 'no prior entry'"
    )

    resolved = result["resolved_state"]
    nodes = {n["id"]: n for n in resolved["nodes"]}
    node_263a = nodes.get("step_2.6.3a")
    assert node_263a is not None
    evidence = node_263a["evidence"]
    assert evidence.get("scan_errors"), "scan_errors must be non-empty on an unscannable subdir"
    assert evidence.get("fired_raw_match") is False, (
        "the raw scan genuinely could not find the match (it's behind the "
        "chmod'd dir) — fired_raw_match preserves that distinction from the "
        f"forced conservative 'fired' value; got evidence={evidence!r}"
    )
    assert "scan incomplete" in evidence.get("detail", ""), (
        f"detail must be distinguishable from the clean-negative message; got {evidence.get('detail')!r}"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(hidden_dir) in r.message for r in warnings), (
        f"expected a WARNING naming the unreadable subdir; got {[r.message for r in warnings]}"
    )


@_SKIP_CHMOD_UNRELIABLE
def test_disposition_unreadable_handoffs_subtree_not_silently_reverted(repo, caplog):
    """A predecessor handoff hidden behind an unreadable archive/handoffs/
    subtree must not silently revert WSC_DISPOSITION from chain-terminal to
    single-session — a scan gap means "no consumed_by match found" is NOT a
    confirmed negative, and letting it revert would mis-dispose a real chain
    continuation as a clean single-session close.
    """
    sid = "sess-disp-unreadable-001"
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/missing.md")

    hidden_dir = repo.root / "archive" / "handoffs" / "2020-01-hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    (hidden_dir / "predecessor.md").write_text(
        f"---\ntitle: Predecessor\nconsumed_by: {sid}\n---\n\nBody.\n",
        encoding="utf-8",
    )

    original_mode = hidden_dir.stat().st_mode
    os.chmod(hidden_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.resolver"):
            result = repo.invoke(sid)
    finally:
        os.chmod(hidden_dir, original_mode)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed", (
        "an incomplete scan must not silently revert disposition to "
        "single-session — that would let ceremony treat a real chain "
        f"continuation as a clean single-session close; got {result['disposition']!r}"
    )

    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    assert disposition_branch["evidence"].get("scan_errors"), (
        "scan_errors must be non-empty on an unscannable handoffs subtree — "
        f"got evidence={disposition_branch['evidence']!r}"
    )
    assert not disposition_branch["evidence"].get("disposition_reverted_to_single_session"), (
        "must NOT claim a (false) clean revert-to-single-session happened"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(hidden_dir) in r.message for r in warnings), (
        f"expected a WARNING naming the unreadable subtree; got {[r.message for r in warnings]}"
    )


@_SKIP_CHMOD_UNRELIABLE
def test_l1b_disposition_unreadable_handoffs_subtree_not_silently_single_session(repo, caplog):
    """Review: code-reviewer Finding 1 regression — the L1b grep-fallback
    disposition path (session-shape.json absent) must not silently resolve
    WSC_DISPOSITION=single-session when a real predecessor handoff is hidden
    behind an unreadable archive/handoffs/ subtree.  Mirrors
    test_disposition_unreadable_handoffs_subtree_not_silently_reverted, but
    with NO session-shape.json seeded — forcing _grep_disposition (the L1b
    path) rather than the primary session-shape.pickup path.
    """
    sid = "sess-l1b-disp-unreadable-001"
    # Deliberately do NOT seed_session_shape — shape_source == "absent" forces
    # use_grep_fallback == True (the L1b path under test).

    hidden_dir = repo.root / "archive" / "handoffs" / "2020-01-hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    (hidden_dir / "predecessor.md").write_text(
        f"---\ntitle: Predecessor\nconsumed_by: {sid}\n---\n\nBody.\n",
        encoding="utf-8",
    )

    original_mode = hidden_dir.stat().st_mode
    os.chmod(hidden_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.resolver"):
            result = repo.invoke(sid)
    finally:
        os.chmod(hidden_dir, original_mode)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed", (
        "an incomplete L1b scan must not silently read as single-session — "
        "a real predecessor handoff could be hidden behind the unreadable "
        f"subtree; got {result['disposition']!r}"
    )

    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    disposition_branch = branches["WSC_DISPOSITION"]
    evidence = disposition_branch["evidence"]
    assert evidence.get("scan_errors"), (
        "scan_errors must be non-empty on an unscannable handoffs subtree — "
        f"got evidence={evidence!r}"
    )
    assert evidence.get("disposition_forced_chain_terminal_scan_incomplete") is True, (
        f"expected the L1b conservative-fire flag; got evidence={evidence!r}"
    )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any(str(hidden_dir) in r.message for r in warnings), (
        f"expected a WARNING naming the unreadable subtree; got {[r.message for r in warnings]}"
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


def test_disposition_detector_b_production_path(git_repo, tmp_path):
    """No session-shape.json, no live consumed_by: stamp anywhere -- an
    archived handoff added by a Session-Id: <sid>-trailered commit must still
    resolve chain-terminal through resolve_session_branches itself (not just the resolver
    unit function or wsc_tail's separate wiring)."""
    sid = "sess-detb-prod-001"
    _wire_origin_pushing_only_current_head(git_repo.root, tmp_path)

    handoff_path = git_repo.root / "archive" / "handoffs" / "detb-shipped.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        '---\ntitle: "Test Archived Handoff"\ncreated: 2026-01-01\n'
        'status: archived\npredecessor: null\n---\n\n# Handoff Body\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"archive: ship handoff\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == "archive/handoffs/detb-shipped.md"

    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence.get("disposition_source_detector_b") is True, (
        f"expected the Detector B provenance flag; got evidence={evidence!r}"
    )


def test_disposition_detector_b_rejected_hit_warnings_surfaced(git_repo, tmp_path):
    """A B-candidate that fails the well-formedness guard (no predecessor:
    line) must not flip disposition to chain-terminal, but its rejection
    MUST still surface as a detector_b_warnings entry on the WSC_DISPOSITION
    branch's evidence -- a fail-loud rejected-hit, never a silent drop."""
    sid = "sess-detb-rejected-001"
    _wire_origin_pushing_only_current_head(git_repo.root, tmp_path)

    handoff_path = git_repo.root / "archive" / "handoffs" / "detb-malformed.md"
    handoff_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_path.write_text(
        '---\ntitle: "Test Archived Handoff"\ncreated: 2026-01-01\n'
        'status: archived\n---\n\n# Handoff Body\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-A"], cwd=str(git_repo.root), capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"archive: ship malformed handoff\n\nSession-Id: {sid}"],
        cwd=str(git_repo.root), capture_output=True, check=True,
    )

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"

    branches = {b["branch_id"]: b for b in result["resolved_state"]["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    warnings = evidence.get("detector_b_warnings") or []
    assert any("detb-malformed.md" in w and "predecessor" in w for w in warnings), (
        f"expected a well-formedness rejection naming the malformed candidate; got {warnings!r}"
    )


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


def test_step_2_65c_resolving_op_names_resolve(repo):
    """AC3: STEP_2_65C's D-node names memo.transition:resolve and carries no
    instruction to Edit memo frontmatter by hand."""
    sid = "sess-265c-resolving-op"
    repo.seed_session_shape(sid)

    result = repo.invoke(sid)
    nodes = {n["id"]: n for n in result["resolved_state"]["nodes"]}
    node = nodes["step_2.65c"]

    assert "memo.transition:resolve" in node["resolving_op"]
    assert "Edit" not in node["evidence"]["note"]


def test_resolve_in_reply_to_target_open(git_repo):
    """in_reply_to naming a memo still sitting in cross-repo/inbox/ -> "open"."""
    git_repo.seed_open_memo("2026-07-20-ask.md")
    assert _resolve_in_reply_to_target(git_repo.root, "2026-07-20-ask.md") == "open"


def test_resolve_in_reply_to_target_closed(git_repo):
    """in_reply_to naming a memo archived to cross-repo/archive/ -> "closed"."""
    git_repo.seed_archived_memo("2026-07-15-old-ask.md")
    assert _resolve_in_reply_to_target(git_repo.root, "2026-07-15-old-ask.md") == "closed"


def test_resolve_in_reply_to_target_unresolvable(git_repo):
    """in_reply_to matching nothing in inbox or archive -> "unresolvable" (fail-closed input)."""
    assert _resolve_in_reply_to_target(git_repo.root, "2026-07-01-missing.md") == "unresolvable"


def test_resolve_in_reply_to_target_glob_metachars_do_not_spuriously_match(git_repo):
    """A malformed/corrupted in_reply_to containing glob metacharacters must NOT
    be interpreted as a pattern against cross-repo/archive/ — that would let an
    unrelated archived file spuriously resolve as "closed" instead of the
    fail-CLOSED "unresolvable" AC4a requires (fail-OPEN bug: rglob() takes its
    argument as a glob pattern, not a literal filename)."""
    git_repo.seed_archived_memo("2026-07-20-ask.md")
    git_repo.seed_archived_memo("2026-07-25-ask.md")

    # "2026-07-2[0-5]-ask.md" is not a real filename but, interpreted as a glob
    # pattern, matches both archived memos above.
    assert _resolve_in_reply_to_target(
        git_repo.root, "2026-07-2[0-5]-ask.md",
    ) == "unresolvable"

    # A bare "*.md" would match every archived memo if treated as a pattern.
    assert _resolve_in_reply_to_target(git_repo.root, "*.md") == "unresolvable"


def test_resolve_in_reply_to_target_directory_traversal_value_cannot_escape_archive_dir(git_repo):
    """Review: code-reviewer (slice 2, nit) — mirrors
    test_memo_send.py::TestInReplyToExistenceGate::test_unit_directory_traversal_value_cannot_escape_archive_dir.
    _resolve_in_reply_to_target shares `_normalize_in_reply_to` with
    memo_send's gate but had no traversal regression test of its own at this
    call site. A value carrying directory separators must be normalized to
    its bare basename before being joined onto inbox/archive dirs — never
    allowed to escape them. A secret file living OUTSIDE the worktree must
    not be found via a traversal in_reply_to."""
    secret = git_repo.root.parent / "secret.md"
    secret.write_text("classified", encoding="utf-8")
    assert _resolve_in_reply_to_target(
        git_repo.root, "../../../../../../../secret.md",
    ) == "unresolvable"


def test_resolve_in_reply_to_target_closed_one_level_shard(git_repo):
    """A one-level-sharded archive layout (cross-repo/archive/<shard>/<name>) —
    not today's real flat layout, but the defensive fallback this narrowing
    keeps — still resolves "closed", not "unresolvable"."""
    sharded = git_repo.root / "cross-repo" / "archive" / "2026-07"
    sharded.mkdir(parents=True, exist_ok=True)
    (sharded / "2026-07-18-sharded-ask.md").write_text(
        '---\ntitle: "Archived Memo"\nstatus: actioned\n---\n\nMemo body.\n',
        encoding="utf-8",
    )
    assert _resolve_in_reply_to_target(
        git_repo.root, "2026-07-18-sharded-ask.md",
    ) == "closed"


def test_scan_open_memos_attaches_bulk_eligibility(git_repo):
    """_scan_open_memos composes classify_bulk_eligibility per memo, once, using the
    SAME in_reply_to resolution resolve_named_memo_dispositions later gates on."""
    git_repo.seed_open_memo("2026-07-20-plain-fyi.md", kind="fyi")
    git_repo.seed_open_memo(
        "2026-07-20-reply-to-open.md", kind="fyi",
        in_reply_to="2026-07-20-plain-fyi.md",
    )
    git_repo.seed_archived_memo("2026-07-10-resolved-ask.md")
    git_repo.seed_open_memo(
        "2026-07-20-reply-to-closed.md", kind="fyi",
        in_reply_to="2026-07-10-resolved-ask.md",
    )
    git_repo.seed_open_memo("2026-07-20-consult.md", kind="consult")

    open_memos, scan_error = _scan_open_memos(git_repo.root)
    assert scan_error is None
    by_name = {Path(m["path"]).name: m for m in open_memos}

    assert by_name["2026-07-20-plain-fyi.md"]["bulk_eligible"] is True
    assert by_name["2026-07-20-reply-to-open.md"]["bulk_eligible"] is False
    assert by_name["2026-07-20-reply-to-closed.md"]["bulk_eligible"] is True
    assert by_name["2026-07-20-consult.md"]["bulk_eligible"] is False


def test_resolve_named_memo_dispositions_issues_n_calls(git_repo):
    """N named memos -> N memo.transition resolve calls carrying the right
    dispositions; a memo the judgment did NOT name is left untouched at status open."""
    git_repo.seed_open_memo("2026-07-20-memo-a.md", title="Memo A")
    git_repo.seed_open_memo("2026-07-20-memo-b.md", title="Memo B", kind="fyi")
    git_repo.seed_open_memo("2026-07-20-memo-c.md", title="Memo C — unnamed")
    # A real memo is committed by cross-repo-memo's _commit_delivered_memo
    # before any transition verb runs -- memo.transition resolve refuses a
    # path absent from HEAD, so the fixture must land these on HEAD first.
    _commit(git_repo.root, "seed open memos a/b/c")

    open_memos, scan_error = _scan_open_memos(git_repo.root)
    assert scan_error is None
    assert len(open_memos) == 3

    dispositions = [
        {
            "memo": "cross-repo/inbox/2026-07-20-memo-a.md",
            "decision": "accepted",
            "realized_by": "inline",
        },
        {
            "memo": "cross-repo/inbox/2026-07-20-memo-b.md",
            "actioned_note": "no objection",
        },
    ]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, open_memos, dispositions,
        session_id="sess-resolve-001", at="2026-07-26T00:00:00Z",
    ))

    assert len(results) == 2
    for r in results:
        assert r["exit_code"] == 0, r
        assert r["applied"] is True

    memo_a_text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-memo-a.md").read_text()
    assert "status: actioned" in memo_a_text
    assert "decision: accepted" in memo_a_text

    memo_b_text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-memo-b.md").read_text()
    assert "status: actioned" in memo_b_text

    memo_c_text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-memo-c.md").read_text()
    assert "status: open" in memo_c_text


def test_resolve_named_memo_dispositions_unknown_memo_refused(git_repo):
    """A dispositions entry naming a memo NOT in open_memos (stale/hallucinated
    judgment answer) is refused fail-loud without ever calling memo.transition."""
    dispositions = [{"memo": "cross-repo/inbox/does-not-exist.md", "actioned_note": "x"}]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, [], dispositions, session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 1
    assert "not one of the open memos" in results[0]["error"]


def test_resolve_named_memo_dispositions_bulk_ineligible_refused(git_repo):
    """A bulk disposition request against an ineligible (non-fyi) memo is refused
    fail-loud; the memo stays open on disk — memo.transition is never reached."""
    git_repo.seed_open_memo("2026-07-20-consult.md", kind="consult")
    open_memos, _ = _scan_open_memos(git_repo.root)
    dispositions = [{
        "memo": "cross-repo/inbox/2026-07-20-consult.md",
        "bulk": True,
        "actioned_note": "ack",
    }]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, open_memos, dispositions, session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 1
    assert "bulk disposition refused" in results[0]["error"]
    text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-consult.md").read_text()
    assert "status: open" in text


def test_resolve_named_memo_dispositions_bulk_eligible_applies(git_repo):
    """A bulk request against an eligible fyi memo (no in_reply_to) applies."""
    git_repo.seed_open_memo("2026-07-20-ack.md", kind="fyi")
    # Real memos are committed by cross-repo-memo before a transition verb
    # ever runs -- match that production shape here.
    _commit(git_repo.root, "seed open memo ack")
    open_memos, _ = _scan_open_memos(git_repo.root)
    dispositions = [{
        "memo": "cross-repo/inbox/2026-07-20-ack.md",
        "bulk": True,
        "actioned_note": "no action needed",
    }]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, open_memos, dispositions, session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 0
    assert results[0]["applied"] is True
    text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-ack.md").read_text()
    assert "status: actioned" in text


def test_step_2_65b_answer_shape_key_matches_disposition_parser(git_repo):
    """STEP_2_65B's EM-facing ANSWER SHAPE example must key each disposition
    by the SAME field resolve_named_memo_dispositions actually reads
    ("memo") — a prompt/parser divergence here means an EM following the
    documented contract literally gets every disposition refused as "not one
    of the open memos", silently no-opping the entire flip feature in
    production. Drives the EXACT prompt text through the real JSON-decode +
    resolve (resolve_named_memo_dispositions) pipeline, rather than asserting
    the key name in isolation, so a future divergence between the two sides
    fails here."""
    git_repo.seed_open_memo("2026-07-20-memo-a.md", title="Memo A")
    # Real memos are committed by cross-repo-memo before a transition verb
    # ever runs -- match that production shape here.
    _commit(git_repo.root, "seed open memo a")

    match = re.search(r"ANSWER SHAPE:.*?(\[\{.*\}\])", J_QUESTIONS[STEP_2_65B], re.DOTALL)
    assert match, "STEP_2_65B prompt must document an ANSWER SHAPE JSON example"
    example = json.loads(
        match.group(1).replace("<memo>.md", "cross-repo/inbox/2026-07-20-memo-a.md")
    )
    # Exercise the first ("decision") example entry only.
    answer = json.dumps([{**example[0], "realized_by": "inline"}])

    # Decoded inline: wsc_commit._parse_memo_dispositions_answer was deleted with
    # its module (kill-list op removal, 2026-07-29). The contract under test is
    # prompt-example-key vs. resolver-read-key, which does not need that helper.
    dispositions = json.loads(answer)

    open_memos, scan_error = _scan_open_memos(git_repo.root)
    assert scan_error is None

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, open_memos, dispositions,
        session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 0, results[0]
    assert results[0]["applied"] is True
    text = (git_repo.root / "cross-repo" / "inbox" / "2026-07-20-memo-a.md").read_text()
    assert "status: actioned" in text


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


def test_env_override_wins_over_session_shape_primary_single_session(repo, monkeypatch):
    """WSC_DISPOSITION=predecessor-consumed wins outright over a session-shape.json
    primary result that says single-session — the exact escape-hatch scenario
    the override exists for."""
    sid = "sess-override-primary-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    # No WSC_CONSUMED_HANDOFF given alongside the override — the resolved
    # handoff must stay empty, not silently inherit anything from the
    # session-shape.json the override overrode.
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence["method"] == "env_override"
    assert any("override" in d for d in evidence.get("env_override_diagnostics", ()))


def test_env_override_wins_over_l1b_grep_fallback(git_repo, monkeypatch):
    """WSC_DISPOSITION=predecessor-consumed wins outright over the L1b grep
    fallback (session-shape.json absent) when grep would otherwise resolve
    single-session."""
    sid = "sess-override-l1b-001"
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")

    common_dir = git_repo.root / ".git"
    result = _run(resolve_session_branches({"sid": sid}, repo_root=common_dir))

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence["method"] == "env_override"


def test_env_override_legacy_alias_normalised_to_canonical(repo, monkeypatch):
    """WSC_DISPOSITION=chain-terminal (the permanently-recognised legacy
    alias) resolves to the canonical predecessor-consumed token, same as the
    canonical spelling."""
    sid = "sess-override-legacy-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "chain-terminal")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    assert result["resolved_state"]["consumed_handoff"] == ""


def test_env_override_single_session_downgrade_refused(repo, monkeypatch):
    """WSC_DISPOSITION=single-session cannot downgrade a positively-detected
    consume — the detector chain runs normally (still predecessor-consumed)
    and a WARN records the refusal."""
    sid = "sess-override-refused-001"
    repo.seed_handoff("consumed.md", consumed_by=sid)
    repo.seed_session_shape(
        sid, pickup_happened=True, handoff="state/handoffs/consumed.md",
    )
    monkeypatch.setenv("WSC_DISPOSITION", "single-session")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    # A refused downgrade must not disturb the disposition chain's own
    # (unrelated) real handoff resolution.
    assert resolved["consumed_handoff"] == "state/handoffs/consumed.md"
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence["method"] == "session_shape"
    diag = evidence.get("env_override_diagnostics", ())
    assert any("WARN" in d and "cannot downgrade" in d for d in diag)


def test_env_override_unrecognised_value_ignored_with_warn(repo, monkeypatch):
    """An unrecognised WSC_DISPOSITION value is ignored (WARN naming the
    accepted values); the detector chain runs normally."""
    sid = "sess-override-unrecognised-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "banana")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    diag = evidence.get("env_override_diagnostics", ())
    assert any("WARN" in d and "unrecognised" in d for d in diag)


def test_env_override_consumed_handoff_alone_does_not_flip_disposition(repo, monkeypatch):
    """WSC_CONSUMED_HANDOFF set without a positive WSC_DISPOSITION does not
    flip disposition — NOTE and ignore, detector chain runs normally."""
    sid = "sess-override-handoff-alone-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", "state/handoffs/anything.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "single-session"
    resolved = result["resolved_state"]
    # The named handoff must never leak into consumed_handoff — the whole
    # point of this case is that it does not flip anything.
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    diag = evidence.get("env_override_diagnostics", ())
    assert any("NOTE" in d and "cannot flip disposition" in d for d in diag)


@_SKIP_CHMOD_UNRELIABLE
def test_env_override_absent_scan_incomplete_fence_still_fires(repo, caplog):
    """With NO override set, the pre-existing scan-incomplete Tier-2 fence
    (an unreadable archive/handoffs/ subtree must not silently revert
    predecessor-consumed to single-session) still fires exactly as before —
    the new override machinery must not have disturbed it."""
    sid = "sess-no-override-fence-001"
    repo.seed_session_shape(sid, pickup_happened=True, handoff="state/handoffs/missing.md")

    hidden_dir = repo.root / "archive" / "handoffs" / "2020-01-hidden"
    hidden_dir.mkdir(parents=True, exist_ok=True)
    (hidden_dir / "predecessor.md").write_text(
        f"---\ntitle: Predecessor\nconsumed_by: {sid}\n---\n\nBody.\n",
        encoding="utf-8",
    )

    original_mode = hidden_dir.stat().st_mode
    os.chmod(hidden_dir, 0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.ceremony.resolver"):
            result = repo.invoke(sid)
    finally:
        os.chmod(hidden_dir, original_mode)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed", (
        "scan-incomplete fence must still fire with no env override present"
    )
    resolved = result["resolved_state"]
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence.get("scan_errors")
    assert not evidence.get("disposition_reverted_to_single_session")


# ---------------------------------------------------------------------------
# Operator-asserted ownership on the override path (coordinator review, this
# workstream): the override's own WARN strings exist for exactly the cases
# where consumed_by == sid can never hold on disk -- a dead claiming session
# (Detector C ambiguous/indeterminate) or a ship-then-archive handoff with no
# live consume stamp ever written. _sanitize_consumed_handoffs's ownership
# half is bypassable ONLY on the override path (operator_asserted=True);
# containment and existence are never bypassable, for anyone.
# ---------------------------------------------------------------------------


def test_env_override_handoff_owned_by_different_dead_session_kept(repo, monkeypatch):
    """Mirrors the Detector-C-ambiguous real case: several stale batons, each
    claimed_by a DIFFERENT, dead session -- none equal to sid. The operator
    names one directly via WSC_CONSUMED_HANDOFF; disposition escalates AND
    the named path is kept (not silently dropped by the ownership check) AND
    the operator-assertion is recorded loudly in the receipt."""
    sid = "sess-override-dead-owner-001"
    repo.seed_handoff("stale-baton-a.md", consumed_by="sess-dead-peer-a-001")
    repo.seed_handoff("stale-baton-b.md", consumed_by="sess-dead-peer-b-002")
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", "state/handoffs/stale-baton-a.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == "state/handoffs/stale-baton-a.md"
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence.get("consumed_handoff_ownership_operator_asserted") is True
    diag = evidence.get("env_override_diagnostics", ())
    assert any(
        "operator" in d and "stale-baton-a.md" in d and "bypassed" in d for d in diag
    )
    assert not evidence.get("consumed_handoff_paths_rejected")


def test_env_override_archived_handoff_no_live_stamp_kept(repo, monkeypatch):
    """Ship-then-archive: the handoff lives under archive/handoffs/ with no
    live consume stamp anywhere -- the exact case the Detector B WARN in the
    override's own remedy text describes. Operator names it directly;
    disposition escalates AND the archived path is kept via operator
    assertion, not silently dropped."""
    sid = "sess-override-archived-001"
    archive_dir = repo.root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "shipped-predecessor.md").write_text(
        "---\ntitle: Predecessor\nstatus: archived\n---\n\nBody.\n",
        encoding="utf-8",
    )
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", "archive/handoffs/shipped-predecessor.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == "archive/handoffs/shipped-predecessor.md"
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert evidence.get("consumed_handoff_ownership_operator_asserted") is True


def test_env_override_absolute_path_outside_worktree_dropped(repo, tmp_path, monkeypatch):
    """WSC_CONSUMED_HANDOFF pointing at an ABSOLUTE path outside the worktree
    is dropped -- containment is never bypassable, even on the override path.
    Disposition still escalates; the rejection is visible in the receipt."""
    sid = "sess-override-foreign-abs-001"
    foreign = tmp_path / "foreign-repo" / "handoffs" / "real.md"
    foreign.parent.mkdir(parents=True, exist_ok=True)
    foreign.write_text(
        f"---\ntitle: Foreign\nclaimed_by: {sid}\n---\n\nBody.\n", encoding="utf-8",
    )
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", str(foreign))

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert not evidence.get("consumed_handoff_ownership_operator_asserted")
    assert evidence.get("consumed_handoff_paths_rejected")


def test_env_override_traversal_path_outside_worktree_dropped(repo, monkeypatch):
    """WSC_CONSUMED_HANDOFF using ../ traversal to escape worktree_root is
    dropped -- containment is never bypassable. Disposition still escalates;
    the rejection is visible in the receipt."""
    sid = "sess-override-traversal-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", "../outside-repo/handoffs/real.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert not evidence.get("consumed_handoff_ownership_operator_asserted")
    assert evidence.get("consumed_handoff_paths_rejected")


def test_env_override_nonexistent_in_repo_path_dropped(repo, monkeypatch):
    """WSC_CONSUMED_HANDOFF naming an in-repo path that does not exist on
    disk is dropped -- existence is never bypassable, even on the override
    path. Disposition still escalates; a NOTE (from normalize_override_handoff)
    is present alongside the drop."""
    sid = "sess-override-nonexistent-001"
    repo.seed_session_shape(sid, pickup_happened=False)
    monkeypatch.setenv("WSC_DISPOSITION", "predecessor-consumed")
    monkeypatch.setenv("WSC_CONSUMED_HANDOFF", "state/handoffs/does-not-exist.md")

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    assert result["disposition"] == "predecessor-consumed"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert not evidence.get("consumed_handoff_ownership_operator_asserted")
    diag = evidence.get("env_override_diagnostics", ())
    assert any("does not exist on disk" in d for d in diag)


def test_non_override_peer_ownership_check_unchanged(repo):
    """A non-override run still gets FULL ownership enforcement -- no
    regression on the pre-existing peer-misattribution defense. Mirrors
    test_session_shape_handoff_path_matches_sid_trusted_directly /
    test_session_shape_pickup_handoff_misattributes_peer_handoff (unedited,
    still green) but pinned here explicitly against the sanitize call site
    this workstream touched."""
    sid = "sess-no-override-ownership-001"
    peer_sid = "sess-peer-owner-002"
    repo.seed_handoff("peer-owned.md", consumed_by=peer_sid)
    repo.seed_session_shape(
        sid, pickup_happened=True, handoff="state/handoffs/peer-owned.md",
    )

    result = repo.invoke(sid)

    assert result["exit_code"] == 0
    # No sid-owned predecessor anywhere on disk -> reverts to single-session,
    # exactly the pre-existing concurrency-correctness revert.
    assert result["disposition"] == "single-session"
    resolved = result["resolved_state"]
    assert resolved["consumed_handoff"] == ""
    branches = {b["branch_id"]: b for b in resolved["resolved_branches"]}
    evidence = branches["WSC_DISPOSITION"]["evidence"]
    assert not evidence.get("consumed_handoff_ownership_operator_asserted")


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
