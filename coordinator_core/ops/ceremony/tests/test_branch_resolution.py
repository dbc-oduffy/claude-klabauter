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
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _run(coro) -> Any:
    return asyncio.run(coro)


def _wire_origin_pushing_only_current_head(root: Path, tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        capture_output=True, check=True,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)],
        cwd=str(root), capture_output=True, check=True,
        **no_console_creationflags(),
    )
    push = subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=str(root), capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert push.returncode == 0, push.stderr


class WscResolveRepo:

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / ".git" / "coordinator-sessions").mkdir(parents=True, exist_ok=True)
        (root / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
        (root / "cross-repo" / "inbox").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)
        (root / "archive" / "completed").mkdir(parents=True, exist_ok=True)

    @property
    def common_dir(self) -> Path:
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
        path = self.root / "cross-repo" / "archive" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '---\ntitle: "Archived Memo"\nstatus: actioned\n---\n\nMemo body.\n',
            encoding="utf-8",
        )
        return path

    def seed_plan(self, name: str) -> Path:
        path = self.root / "docs" / "plans" / name
        path.write_text(f"# Plan\n\nPlan content.\n", encoding="utf-8")
        return path

    def seed_completion_entry(self, name: str, chain: str) -> Path:
        path = self.root / "archive" / "completed" / name
        path.write_text(
            f"---\ntitle: Test\nchain: {chain}\n---\n\nEntry.\n",
            encoding="utf-8",
        )
        return path

    def seed_coordinator_local(self, project_subtypes: list[str]) -> Path:
        path = self.root / "coordinator.local.md"
        subtypes_yaml = "\n".join(f"  - {st}" for st in project_subtypes)
        path.write_text(
            f"---\nproject_subtypes:\n{subtypes_yaml}\n---\n",
            encoding="utf-8",
        )
        return path

    def seed_started_at(self, sid: str, value: str) -> Path:
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
        path = self.root / "tasks" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def seed_completeness_mirror(self, sid: str, content: str) -> Path:
        tasks_dir = self.root / "state" / "tasks" / sid
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "completeness-checklist.yaml"
        path.write_text(content, encoding="utf-8")
        return path

@pytest.fixture
def repo(tmp_path) -> WscResolveRepo:
    return WscResolveRepo(tmp_path / "repo")


@pytest.fixture
def git_repo(tmp_path) -> WscResolveRepo:
    repo = WscResolveRepo(tmp_path / "repo")
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    (repo.root / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo.root), capture_output=True, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    return repo


def test_receipt_graceful_absent(tmp_path):
    """Reading a missing receipt returns NOT_YET_RUN_SENTINEL."""
    missing_path = tmp_path / "state" / "ceremony" / "wsc-receipt.json"
    receipt = read_receipt(missing_path)
    assert is_not_yet_run(receipt), "Absent receipt must return NOT_YET_RUN_SENTINEL"


# C1 — STEP_2_6_3 chain-slug case-a: _read_started_at unit tests


def test_read_started_at_present(tmp_path):
    sid = "sess-sa-unit-001"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("2026-07-06T12:00:25Z\n", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result == "2026-07-06T12:00:25Z"


def test_read_started_at_absent(tmp_path):
    sid = "sess-sa-unit-002"
    common_dir = tmp_path / ".git"
    (common_dir / "coordinator-sessions" / sid).mkdir(parents=True)

    result = _read_started_at(common_dir, sid)
    assert result is None


def test_read_started_at_empty(tmp_path):
    sid = "sess-sa-unit-003"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result is None


# C1 — STEP_2_6_3: integration tests (positive / negative / absence)


# C3 — STEP_2_96: _read_completeness_mirror unit tests

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


# C3 — STEP_2_96: integration tests (presence / absent / mismatch)


# STEP_1B/STEP_2_4B D-node emission integration test


# Findings 6, 7, 8, 9 (P2/nit): cover --diff-filter=A ADDED-not-MODIFIED


def test_session_added_plans_since_boundary_excludes_old_commit(git_repo):
    """--since=<started_at>: commits before started_at are excluded by the temporal filter.

    Calls _session_added_plans directly with started_at set to a recent time
    AFTER a session-tagged commit was made (commit uses --date to set an old
    author date, and GIT_COMMITTER_DATE to match — ensuring git sees the commit
    as old for both author and committer date checks).  The function must return []
    even though the commit passes --grep and --diff-filter=A.

    The existing positive test uses started_at="2000-01-01" making --since a
    no-op; this test is the complementary gate.

    Real --since temporal boundary test.
    """
    import os

    sid = "sess-sap-since-001"
    git_repo.seed_session_shape(sid)

    plan = git_repo.root / "docs" / "plans" / "past-plan.md"
    plan.write_text("# Past Plan\n", encoding="utf-8")
    subprocess.run(["git", "add", str(plan)], cwd=str(git_repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    env = os.environ.copy()
    env["GIT_COMMITTER_DATE"] = "2000-01-01T00:00:00+0000"
    subprocess.run(
        ["git", "commit", "-m", f"add past plan\n\nSession-Id: {sid}",
         "--date", "2000-01-01T00:00:00+0000"],
        cwd=str(git_repo.root), capture_output=True, check=True, env=env,
        **no_console_creationflags(),
    )

    added = _session_added_plans(git_repo.root, sid, "2026-01-01T00:00:00Z")

    assert added == [], (
        f"Commit with date 2000-01-01 must be excluded by --since=2026-01-01; got {added}"
    )


def test_session_added_plans_graceful_on_nonzero_git(tmp_path):
    sid = "sess-sap-fail-001"
    non_git_dir = tmp_path / "not-a-git-repo"
    non_git_dir.mkdir()

    result = _session_added_plans(non_git_dir, sid, "2026-01-01T00:00:00Z")
    assert result == [], (
        f"_session_added_plans must return [] on git failure; got {result}"
    )


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


def test_read_started_at_whitespace_only(tmp_path):
    sid = "sess-sa-ws-001"
    common_dir = tmp_path / ".git"
    sid_dir = common_dir / "coordinator-sessions" / sid
    sid_dir.mkdir(parents=True)
    (sid_dir / "started_at").write_text("   \n   ", encoding="utf-8")

    result = _read_started_at(common_dir, sid)
    assert result is None, (
        f"Whitespace-only started_at must be treated as absent (None); got {result!r}"
    )


# C1 — STEP_2_67A: _scan_session_scratch unit tests


def test_scan_session_scratch_graceful_negative_no_started_at(git_repo):
    result = _scan_session_scratch(git_repo.root, None)
    assert result is None, (
        f"Absent started_at must return None (graceful-negative); got {result!r}"
    )


def test_scan_session_scratch_no_tasks_dir(git_repo):
    started_at = "2026-07-06T12:00:00Z"
    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, f"No tasks/ dir → count=0; got {result!r}"


@contextmanager
def _tz_forced_to_us_pacific():
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
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    file_mtime = started_epoch_utc + 1800

    scratch_file = git_repo.root / "tasks" / "my-feature" / "scratch.md"
    scratch_file.parent.mkdir(parents=True, exist_ok=True)
    scratch_file.write_text("scratch notes\n", encoding="utf-8")
    os.utime(scratch_file, (file_mtime, file_mtime))

    with _tz_forced_to_us_pacific():
        result = _scan_session_scratch(git_repo.root, started_at)

    assert result == 1, (
        f"Untracked scratch file 30 min after started_at must be counted; "
        f"got {result!r} (check tz-aware parse — naive rstrip('Z') skews epoch "
        f"by +7-8h on US-Pacific nodes, making the file appear before threshold)"
    )


def test_scan_session_scratch_keep_list_excluded(git_repo):
    sid = "sess-67a-unit-keeplist-001"
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    keep_listed_names = ["todo.md", "plan.md", "completion-log.md"]
    mtime_after = started_epoch_utc + 1800

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


def test_scan_session_scratch_plan_md_suffix_excluded(git_repo):
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800

    plan_file = git_repo.root / "tasks" / "my-feature" / "2026-07-06-something.plan.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\n", encoding="utf-8")
    os.utime(plan_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"*.plan.md file must be excluded by endswith('.plan.md') filter; got {result!r}"
    )


def test_scan_session_scratch_completion_substring_excluded(git_repo):
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()
    mtime_after = started_epoch_utc + 1800

    comp_file = git_repo.root / "tasks" / "my-feature" / "wsc-2026.completion.md"
    comp_file.parent.mkdir(parents=True, exist_ok=True)
    comp_file.write_text("# Completion notes\n", encoding="utf-8")
    os.utime(comp_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"File with '.completion' in name (any position) must be excluded; got {result!r}"
    )


def test_scan_session_scratch_graceful_negative_unparseable_started_at(git_repo):
    """_scan_session_scratch returns None when started_at is not parseable as ISO-8601.

    The ValueError-on-parse path (distinct from the None-input path) is the fallback
    for a malformed started_at sentinel produced by a buggy upstream writer.  The
    function must return None without raising.

    ValueError-on-parse graceful-negative branch coverage gap.
    """
    result = _scan_session_scratch(git_repo.root, "not-a-date")
    assert result is None, (
        f"Unparseable started_at must return None (graceful-negative via ValueError); "
        f"got {result!r}"
    )


def test_scan_session_scratch_zero_path_no_qualifying_files(git_repo):
    sid = "sess-67a-unit-zero-001"
    started_at = "2026-07-06T12:00:00Z"
    started_epoch_utc = datetime.fromisoformat(
        started_at.replace("Z", "+00:00")
    ).timestamp()

    old_file = git_repo.root / "tasks" / "old-feature" / "old-scratch.md"
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text("old scratch\n", encoding="utf-8")
    mtime_before = started_epoch_utc - 3600
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
    mtime_after = started_epoch_utc + 1800

    tracked_file = git_repo.root / "tasks" / "some-feature" / "tracked.md"
    tracked_file.parent.mkdir(parents=True, exist_ok=True)
    tracked_file.write_text("tracked content\n", encoding="utf-8")
    subprocess.run(["git", "add", str(tracked_file)], cwd=str(git_repo.root),
                   capture_output=True, check=True,
                   **no_console_creationflags(),)
    subprocess.run(["git", "commit", "-m", "add tracked tasks file"],
                   cwd=str(git_repo.root), capture_output=True, check=True,
                   **no_console_creationflags(),)
    os.utime(tracked_file, (mtime_after, mtime_after))

    result = _scan_session_scratch(git_repo.root, started_at)
    assert result == 0, (
        f"Git-tracked file must not be counted as transient scratch; got {result!r}"
    )


# C1 — STEP_2_67A: integration tests (Branch 11 flip via full handler invoke)


# pickup.handoff points at a temporally-adjacent CONCURRENT session's


# the repo must be ACCEPTED, not rejected — a naive "reject any candidate


def test_resolve_in_repo_relative_in_repo_path_contained(repo):
    handoff = repo.root / "state" / "handoffs" / "x.md"
    handoff.write_text("body", encoding="utf-8")

    result = _resolve_in_repo(repo.root, "state/handoffs/x.md")

    assert result == handoff.resolve()


def test_resolve_in_repo_parent_traversal_rejected(repo):
    result = _resolve_in_repo(repo.root, "../outside.md")

    assert result is None


def test_resolve_in_repo_absolute_foreign_path_rejected(repo):
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
    result = _resolve_in_repo(repo.root, ".")

    assert result == repo.root.resolve()


# primary-path guard validate a file in a DIFFERENT repo (e.g. Example-retrieval-repo)


# incident receipt still carried the foreign ABSOLUTE path AND a relativized
# consumed_handoff_paths array + STEP_2_7 stamp target.  The final-gate
# foreign/absolute/phantom entry can survive into the receipt or STEP_2_7 even


def test_sanitize_drops_foreign_absolute_from_merged_set(repo, tmp_path):
    """A foreign ABSOLUTE path (consumed_by: sid) that reached the merged set
    is dropped by the final gate and re-expressed as nothing — never survives
    as an absolute path or a relativized phantom.  Directly exercises the
    choke point regardless of which upstream source injected the entry.
    """
    sid = "sess-sanitize-abs-001"

    foreign_repo = tmp_path / "example-cockpit-repo"
    foreign_dir = foreign_repo / "state" / "handoffs"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    foreign_hf = foreign_dir / "2026-07-13_124503_dashboard-placement-rubric-ratify.md"
    foreign_hf.write_text(
        f"---\nstatus: consumed\nconsumed_by: {sid}\npredecessor: sess-foreign-pred\n---\n\nbody\n",
        encoding="utf-8",
    )

    local_hf = repo.seed_handoff("real-local.md", consumed_by=sid)

    merged = [
        (str(foreign_hf), {"predecessor": "sess-foreign-pred"}),
        ("state/handoffs/real-local.md", {"predecessor": "sess-local"}),
    ]
    kept, rejected = _sanitize_consumed_handoffs(repo.root, sid, merged)

    kept_paths = [p for p, _fm in kept]
    assert str(foreign_hf) not in kept_paths
    assert not any("dashboard-placement-rubric-ratify" in p for p in kept_paths)
    assert str(foreign_hf) in rejected
    assert "state/handoffs/real-local.md" in kept_paths
    assert all(not Path(p).is_absolute() for p in kept_paths)
    assert local_hf.exists()


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


# the Staff Engineer-finding regression cases (F0 dedup, F2 STEP_2_7 plural evidence —
# both the STEP_0 evidence dict AND the STEP_2_7 node's own evidence dict,


# asserted the STEP_2_7 plural-evidence contract against wsc_commit._read_step_2_7_evidence,
# The producer side (STEP_2_7 carrying consumed_handoffs_paths) is covered by
# no test asserted the STEP_2_7 node's own evidence dict until this fix).


def test_c1_divergent_scalar_field_rejected_by_validate():
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


def _commit(repo_root, message, *, date=None, add=("-A",)):
    subprocess.run(["git", "add", *add], cwd=str(repo_root), capture_output=True, check=True, **no_console_creationflags())
    env = None
    if date is not None:
        import os as _os
        env = dict(_os.environ)
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(repo_root), capture_output=True, check=True, env=env,
        **no_console_creationflags(),
    )


def _head_sha(repo_root) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


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


def test_trailer_unreliable_when_head_moved_no_trailer(git_repo):
    sid = "sess-c1-trailerless-001"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
    (git_repo.root / "f1.txt").write_text("one\n", encoding="utf-8")
    _commit(git_repo.root, "plain commit, no trailer", date="2020-06-01T00:00:00Z")

    assert _trailer_reliable(git_repo.root, sid, started_at) is False


def test_trailer_reliable_when_no_work_since_started_at(git_repo):
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
        known_scope_paths=frozenset({"scoped.txt", ".gitkeep"}),
    )
    assert verdict.method == SCOPING_METHOD_STARTED_AT_RANGE
    assert verdict.foreign_count == 0
    assert verdict.contiguous is True
    assert verdict.candidate_range.endswith("^..HEAD")


def test_detect_foreign_commits_interleaved_repro(git_repo):
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
    foreign_shas = _detect_foreign_commits(
        git_repo.root, sid, candidate_range, frozenset({"own.txt", ".gitkeep"}),
    )
    assert len(foreign_shas) == 1


def test_analyze_session_scoping_partial_trailer_is_trailer_method(git_repo):
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
    sid = "sess-c1-leading-foreign"
    started_at = "2020-01-01T00:00:00Z"
    git_repo.seed_started_at(sid, started_at)
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


_SKIP_CHMOD_UNRELIABLE = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)


# STEP_2_65C flip half + STEP_2_65B bulk-eligibility evidence (C2)


def test_resolve_named_memo_dispositions_unknown_memo_refused(git_repo):
    dispositions = [{"memo": "cross-repo/inbox/does-not-exist.md", "actioned_note": "x"}]

    results = _run(resolve_named_memo_dispositions(
        git_repo.root, [], dispositions, session_id="s", at="2026-07-26T00:00:00Z",
    ))

    assert results[0]["exit_code"] == 1
    assert "not one of the open memos" in results[0]["error"]


# WSC_DISPOSITION / WSC_CONSUMED_HANDOFF escalate-only env override
# WSC_DISPOSITION/WSC_CONSUMED_HANDOFF in the ambient test-runner environment


@pytest.fixture(autouse=True)
def _clean_wsc_env(monkeypatch):
    """Isolate every test in this module from ambient WSC_DISPOSITION /
    WSC_CONSUMED_HANDOFF — autouse so pre-existing tests above this section
    (none of which anticipated these vars) are equally protected."""
    monkeypatch.delenv("WSC_DISPOSITION", raising=False)
    monkeypatch.delenv("WSC_CONSUMED_HANDOFF", raising=False)


def test_env_override_shared_helper_agrees_with_bin_script(monkeypatch, tmp_path):
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
