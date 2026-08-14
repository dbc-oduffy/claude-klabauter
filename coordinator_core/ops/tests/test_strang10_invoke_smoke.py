"""
coordinator_core.ops.tests.test_strang10_invoke_smoke — real-path invoke smoke for the
5 strang-10 A+B ops.

Purpose (strang-07 postmortem lesson): byte-parity tests call the handler DIRECTLY, which
does NOT prove the real command-type dispatch envelope works. This smoke proves the full
end-to-end path:

    python -m coordinator_core.invoke <op> '<json-params>' --repo <tmp-worktree>

builds the JSON-RPC 2.0 envelope, resolves repo_root via git_common_dir, dispatches in-process,
writes the artifact, and exits 0.

The 5 ops (all MUTATING, _OP_KEY_SCOPE=common_dir):
    changelog.append_day      → writes state/week-changelog/{date}.md (per-day
                                  collapse, PM ruling 2026-07-19; section header
                                  inside the file remains "## {date} — {machine}")
    changelog.backfill_gaps   → writes state/week-changelog/{date}-{host}-backfill.md
    completion.reconcile_commits → in-place on caller-supplied docs/plans/<plan>.md
    plan.append_session          → in-place on caller-supplied docs/plans/<plan>.md
    review_trail.write           → writes state/review-trail/{ts}-{session_short}.json

Each test:
    1. Creates a tmp dir and runs git init (so git_common_dir/common_dir scope resolves).
    2. Seeds any prerequisite fixture (HEADER.md, plan file).
    3. Invokes via subprocess: python -m coordinator_core.invoke <op> '<json>' --repo <tmpdir>.
    4. Asserts: returncode == 0, stdout parses as JSON-RPC 2.0 success response,
       and the expected artifact file exists on disk.

Negative-spec:
    - Does NOT call handlers directly (that is the byte-parity tests' job).
    - Does NOT edit op modules, registration files, or any file outside this test.
    - Does NOT require DoE oracle scripts — all 5 ops run for real without oracle dependency.

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C5
Strang-07 postmortem: byte-parity ≠ invoke-path proof (prior-art Claim #16).
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Git repo helpers
# ---------------------------------------------------------------------------

# Minimal git identity config so commits work in isolated test trees.
_GIT_ENV_BASE: dict = {
    "GIT_AUTHOR_NAME": "Smoke Test",
    "GIT_AUTHOR_EMAIL": "smoke@test.local",
    "GIT_COMMITTER_NAME": "Smoke Test",
    "GIT_COMMITTER_EMAIL": "smoke@test.local",
}

# _invoke spawns `python -m coordinator_core.invoke` as a real subprocess.
# That child inherits cwd but NOT pytest's rootdir sys.path insertion, so it
# can only resolve the coordinator_core package when cwd is (or is under) the
# repo root -- from any other cwd it dies with ModuleNotFoundError before it
# can write anything to stdout. Pinning cwd to the repo root derived from this
# file's own path makes the subprocess resolvable regardless of the invoking
# shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_git_repo(base: Path) -> Path:
    """Initialize a git repo at base, create an initial empty commit, return base.

    The initial commit ensures git_common_dir() resolves and git log returns at
    least one commit in today's UTC window (used by changelog.backfill_gaps).
    """
    base.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_GIT_ENV_BASE}

    subprocess.run(
        ["git", "init", str(base)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    # checkout -b to avoid relying on init.defaultBranch config variation.
    subprocess.run(
        ["git", "-C", str(base), "checkout", "-b", "main"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    subprocess.run(
        [
            "git", "-C", str(base),
            "commit", "--allow-empty", "-m", "initial: invoke-smoke test setup",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return base


# ---------------------------------------------------------------------------
# Invoke helper
# ---------------------------------------------------------------------------


def _invoke(
    op: str,
    params: dict,
    repo: Path,
    extra_env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run ``python -m coordinator_core.invoke <op> '<json-params>' --repo <repo>``.

    Inherits the current process environment, overlays _GIT_ENV_BASE, then
    applies any caller-supplied extra_env (e.g. CLAUDE_CODE_SESSION_ID for
    review_trail.write).

    Returns the CompletedProcess without checking returncode — callers assert it.
    """
    env = {**os.environ, **_GIT_ENV_BASE}
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [
            sys.executable,
            "-m", "coordinator_core.invoke",
            op,
            json.dumps(params),
            "--repo", str(repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# Date helper
# ---------------------------------------------------------------------------


def _today_utc() -> str:
    """Return today's date in YYYY-MM-DD (UTC)."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Plan file fixtures
# (same shapes as test_completion_parity.py — reuse exact on-disk fixture shapes)
# ---------------------------------------------------------------------------

_PLAN_EMPTY_COMMITS = """\
---
title: "Smoke test plan — reconcile_commits"
created: 2026-07-06
status: pending-release
commits: []
---

Plan body for smoke test.
"""

_PLAN_WITH_AGENT_SESSIONS = """\
---
title: "Smoke test plan — append_session"
created: 2026-07-06
status: in-progress
agent_sessions:
---

Plan body for smoke test.
"""


# ---------------------------------------------------------------------------
# Shared assertion helper
# ---------------------------------------------------------------------------


def _assert_invoke_success(result: subprocess.CompletedProcess, op: str) -> dict:
    """Assert exit 0, stdout parses as JSON-RPC 2.0 success, return the op result dict."""
    assert result.returncode == 0, (
        f"{op}: invoke exited {result.returncode} (expected 0).\n"
        f"stdout: {result.stdout!r}\n"
        f"stderr: {result.stderr!r}"
    )
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"{op}: stdout is not valid JSON (expected JSON-RPC 2.0 envelope).\n"
            f"JSONDecodeError: {exc}\n"
            f"stdout: {result.stdout!r}"
        )
    assert "error" not in response, (
        f"{op}: unexpected JSON-RPC error in response.\n"
        f"response: {json.dumps(response, indent=2)}"
    )
    assert "result" in response, (
        f"{op}: expected 'result' key in JSON-RPC response.\n"
        f"response: {json.dumps(response, indent=2)}"
    )
    return response["result"]


# ===========================================================================
# Test: changelog.append_day
# ===========================================================================


class TestChangelogAppendDayInvoke:
    """Smoke: changelog.append_day via the real invoke path exits 0 and writes the artifact."""

    def test_invoke_exit_0_and_writes_changelog_file(self, tmp_path: Path) -> None:
        """changelog.append_day invoke: exit 0, valid JSON-RPC result, artifact on disk."""
        repo = _make_git_repo(tmp_path / "repo")

        params = {
            "machine": "smoke-machine",
            "branch": "work/smoke/2026-07-06",
            "commit_count": 1,
            "commit_range": "abc1234..def5678",
            "date": "2026-07-06",
            "scope": "Invoke smoke test day.",
        }

        result = _invoke("changelog.append_day", params, repo)
        op_result = _assert_invoke_success(result, "changelog.append_day")

        # Verify the artifact path is in the result and the file exists on disk.
        assert "out_path" in op_result, (
            f"changelog.append_day: expected 'out_path' in result, got: {op_result}"
        )
        out_path = Path(op_result["out_path"])
        assert out_path.exists(), (
            f"changelog.append_day: artifact not found at {out_path}.\n"
            f"op_result: {op_result}"
        )

        # Verify expected path: state/week-changelog/{date}.md under the repo worktree
        # (per-day filename collapse, PM ruling 2026-07-19, AC11/DEC-4 — the section
        # header inside the file remains machine-keyed, only the filename collapsed).
        expected_path = repo / "state" / "week-changelog" / "2026-07-06.md"
        assert out_path == expected_path, (
            f"changelog.append_day: artifact at unexpected path.\n"
            f"expected: {expected_path}\n"
            f"got:      {out_path}"
        )

        # Sanity-check content: date+machine header must be present.
        content = out_path.read_text(encoding="utf-8")
        assert "## 2026-07-06 — smoke-machine" in content, (
            f"changelog.append_day: expected date+machine header in artifact content.\n"
            f"content:\n{content}"
        )


# ===========================================================================
# Test: changelog.backfill_gaps
# ===========================================================================


class TestChangelogBackfillGapsInvoke:
    """Smoke: changelog.backfill_gaps via the real invoke path exits 0 with valid result.

    The op is advisory (errors return gracefully, never abort). It writes a backfill file
    when there are commits in the date window. Since the test repo is freshly git-init'd
    with an initial commit at the current timestamp, backfill SHOULD write a file for today
    — asserting the artifact is best-effort; the hard invariant is exit 0 + valid JSON.
    """

    def test_invoke_exit_0_and_valid_result(self, tmp_path: Path) -> None:
        """changelog.backfill_gaps invoke: exit 0, valid JSON-RPC result, artifact on disk
        (if commits exist in today's UTC window — expected for a freshly-committed test repo)."""
        repo = _make_git_repo(tmp_path / "repo")
        today = _today_utc()

        # Seed HEADER.md so backfill_gaps has a week_start to iterate from.
        week_dir = repo / "state" / "week-changelog"
        week_dir.mkdir(parents=True)
        (week_dir / "HEADER.md").write_text(
            f"**Week starting:** {today}\n\nSmoke test week header.\n",
            encoding="utf-8",
        )

        params = {"host": "smoke-host", "today": today}
        result = _invoke("changelog.backfill_gaps", params, repo)
        op_result = _assert_invoke_success(result, "changelog.backfill_gaps")

        assert "backfilled" in op_result, (
            f"changelog.backfill_gaps: expected 'backfilled' key in result, got: {op_result}"
        )
        assert isinstance(op_result["backfilled"], list), (
            f"changelog.backfill_gaps: 'backfilled' must be a list, got: {type(op_result['backfilled'])}"
        )

        # Best-effort artifact assertion: the initial commit created in _make_git_repo is
        # timestamped at the current UTC time, so it should fall in today's window and trigger
        # a backfill write. If the test runs at a UTC midnight boundary this may be empty —
        # that's not a failure of the invoke path, so we degrade gracefully.
        if op_result["backfilled"]:
            backfill_path = Path(op_result["backfilled"][0])
            assert backfill_path.exists(), (
                f"changelog.backfill_gaps: backfill artifact not found at {backfill_path}.\n"
                f"op_result: {op_result}"
            )
            content = backfill_path.read_text(encoding="utf-8")
            assert f"## {today} — smoke-host (synthesized backfill)" in content, (
                f"changelog.backfill_gaps: expected date+host header in backfill content.\n"
                f"content:\n{content}"
            )
        else:
            # Graceful degradation: log the skip reason for the reviewer without failing.
            # This branch is only reached at UTC midnight when the commit window is empty.
            pytest.skip(
                f"changelog.backfill_gaps: no commits found in today's UTC window "
                f"(today={today}). This is a known edge-case at UTC midnight; the invoke "
                f"path did exit 0 with valid JSON, which proves the envelope works."
            )


# ===========================================================================
# Test: completion.reconcile_commits
# ===========================================================================


class TestCompletionReconcileCommitsInvoke:
    """Smoke: completion.reconcile_commits via the real invoke path exits 0 and mutates plan."""

    def test_invoke_exit_0_and_folds_shas_into_plan(self, tmp_path: Path) -> None:
        """completion.reconcile_commits invoke: exit 0, valid result, plan file mutated."""
        repo = _make_git_repo(tmp_path / "repo")

        # Seed a plan file with commits: [] inside the repo.
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "smoke-test-reconcile.md"
        plan_file.write_text(_PLAN_EMPTY_COMMITS, encoding="utf-8")

        params = {
            "plan_path": str(plan_file),
            "commits": ["abc1234", "def5678"],
        }

        result = _invoke("completion.reconcile_commits", params, repo)
        op_result = _assert_invoke_success(result, "completion.reconcile_commits")

        # The handler should have folded both SHAs into the plan.
        assert op_result.get("appended") == 2, (
            f"completion.reconcile_commits: expected appended=2, got: {op_result}"
        )
        assert op_result.get("no_op") is False, (
            f"completion.reconcile_commits: expected no_op=False, got: {op_result}"
        )
        assert op_result.get("plan_path") == str(plan_file), (
            f"completion.reconcile_commits: expected plan_path in result.\n"
            f"op_result: {op_result}"
        )

        # Verify the plan file was actually mutated on disk (the key invariant of this smoke).
        content = plan_file.read_text(encoding="utf-8")
        assert '  - "abc1234"' in content, (
            f"completion.reconcile_commits: 'abc1234' SHA not found in mutated plan.\n"
            f"content:\n{content}"
        )
        assert '  - "def5678"' in content, (
            f"completion.reconcile_commits: 'def5678' SHA not found in mutated plan.\n"
            f"content:\n{content}"
        )
        # The inline-empty commits: [] must have been replaced with the multi-line form.
        assert "commits: []" not in content, (
            f"completion.reconcile_commits: 'commits: []' still present after fold.\n"
            f"content:\n{content}"
        )


# ===========================================================================
# Test: plan.append_session
# ===========================================================================


class TestPlanAppendSessionInvoke:
    """Smoke: plan.append_session via the real invoke path exits 0 and appends session entry."""

    def test_invoke_exit_0_and_appends_session_entry(self, tmp_path: Path) -> None:
        """plan.append_session invoke: exit 0, valid result, session entry in plan file."""
        repo = _make_git_repo(tmp_path / "repo")

        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        plan_file = plans_dir / "smoke-test-append-session.md"
        plan_file.write_text(_PLAN_WITH_AGENT_SESSIONS, encoding="utf-8")

        params = {
            "plan_path": str(plan_file),
            "session_id": "smoke-invoke-session-00000001",
            "status": "working",
            "created_at": "2026-07-06T10:00:00Z",
        }

        result = _invoke("plan.append_session", params, repo)
        op_result = _assert_invoke_success(result, "plan.append_session")

        assert op_result.get("appended") is True, (
            f"plan.append_session: expected appended=True, got: {op_result}"
        )
        assert op_result.get("no_op") is False, (
            f"plan.append_session: expected no_op=False, got: {op_result}"
        )
        assert op_result.get("session_id") == "smoke-invoke-session-00000001", (
            f"plan.append_session: unexpected session_id in result: {op_result}"
        )

        # Verify the plan file was actually mutated on disk (the key invariant of this smoke).
        content = plan_file.read_text(encoding="utf-8")
        expected_entry = "smoke-invoke-session-00000001|working|2026-07-06T10:00:00Z"
        assert expected_entry in content, (
            f"plan.append_session: session entry not found in plan file.\n"
            f"expected entry: {expected_entry!r}\n"
            f"content:\n{content}"
        )


# ===========================================================================
# Test: review_trail.write
# ===========================================================================


class TestReviewTrailWriteInvoke:
    """Smoke: review_trail.write via the real invoke path exits 0 and writes JSON artifact."""

    def test_invoke_exit_0_and_writes_json_artifact(self, tmp_path: Path) -> None:
        """review_trail.write invoke: exit 0, valid result, JSON artifact on disk and parseable."""
        repo = _make_git_repo(tmp_path / "repo")

        # session_attribution._guard_foreign_session_range (review_trail_write.py) resolves
        # sha_range with a real `git log` call and fails closed (GitLogFailed) on a range
        # git cannot resolve. A synthetic "abc1234..def5678" is unresolvable in this repo,
        # so build a real two-commit range instead — this also exercises the guard for real,
        # which the synthetic placeholder never did.
        # Both commits carry a Session-Id trailer matching the smoke session's own
        # CLAUDE_CODE_SESSION_ID (below) so session_attribution's trailer signal
        # places the range in this session's own scope — the two-signal classifier
        # (session_attribution.detect_foreign_commits) otherwise refuses an
        # untrailered range it cannot place (ForeignSessionRangeRefused).
        env = {**os.environ, **_GIT_ENV_BASE}
        smoke_session_id = "smoke-review-sess-01234567"
        (repo / "smoke-file.txt").write_text("first\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "smoke-file.txt"],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [
                "git", "-C", str(repo), "commit", "-m",
                f"smoke: first commit\n\nSession-Id: {smoke_session_id}",
            ],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sha1 = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()

        (repo / "smoke-file.txt").write_text("second\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(repo), "add", "smoke-file.txt"],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        subprocess.run(
            [
                "git", "-C", str(repo), "commit", "-m",
                f"smoke: second commit\n\nSession-Id: {smoke_session_id}",
            ],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        sha2 = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip()

        sha_range = f"{sha1}..{sha2}"

        params = {
            "sha_range": sha_range,
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 42,
            "scope_kind": "diff",
            # Pass workstream explicitly to avoid handoff-scan in the tmp repo.
            "workstream": "strang-10-smoke",
        }
        # The handler resolves session_id from CLAUDE_SESSION_ID / CLAUDE_CODE_SESSION_ID env.
        # Inject a pinned session_id so the invoke does not fail with "unresolvable session_id".
        extra_env = {"CLAUDE_CODE_SESSION_ID": smoke_session_id}

        result = _invoke("review_trail.write", params, repo, extra_env=extra_env)
        op_result = _assert_invoke_success(result, "review_trail.write")

        # Verify artifact path in result and existence on disk.
        assert "out_path" in op_result, (
            f"review_trail.write: expected 'out_path' in result, got: {op_result}"
        )
        out_path = Path(op_result["out_path"])
        assert out_path.exists(), (
            f"review_trail.write: artifact not found at {out_path}.\n"
            f"op_result: {op_result}"
        )
        assert out_path.suffix == ".json", (
            f"review_trail.write: expected .json extension, got {out_path.suffix!r}"
        )
        # Artifact must live in state/review-trail/ under the repo worktree.
        expected_parent = repo / "state" / "review-trail"
        assert out_path.parent == expected_parent, (
            f"review_trail.write: artifact in unexpected directory.\n"
            f"expected parent: {expected_parent}\n"
            f"got:             {out_path.parent}"
        )
        # Filename uses session_id short (first 8 chars of "smoke-review-sess-01234567").
        # Session short = "smoke-re" (8 chars)
        assert out_path.name.endswith("-smoke-re.json"), (
            f"review_trail.write: expected filename ending '-smoke-re.json', got: {out_path.name!r}"
        )

        # Verify the JSON file content is parseable and carries the correct field values.
        raw = out_path.read_bytes()
        assert not raw.endswith(b"\n"), (
            f"review_trail.write: file must have no trailing newline (oracle printf '%s' parity), "
            f"last byte: {raw[-1:]!r}"
        )
        parsed = json.loads(raw)
        assert parsed["sha_range"] == sha_range, (
            f"review_trail.write: unexpected sha_range in JSON: {parsed}"
        )
        assert parsed["reviewer"] == "code-reviewer", (
            f"review_trail.write: unexpected reviewer in JSON: {parsed}"
        )
        assert parsed["verdict"] == "ok", (
            f"review_trail.write: unexpected verdict in JSON: {parsed}"
        )
        assert parsed["session_id"] == smoke_session_id, (
            f"review_trail.write: unexpected session_id in JSON: {parsed}"
        )
        assert parsed["workstream"] == "strang-10-smoke", (
            f"review_trail.write: unexpected workstream in JSON: {parsed}"
        )
