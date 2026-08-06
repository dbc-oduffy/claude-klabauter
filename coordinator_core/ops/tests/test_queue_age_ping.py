"""
coordinator_core.ops.tests.test_queue_age_ping — unit tests for the
``queue.age_ping`` operation (``coordinator_core.ops.queue_age_ping``).

Coverage:
  (a) registry — "queue.age_ping" registered after direct module import.
  (b) three families — an aged ``deferred`` entry in each of improvement-queue,
      debt-backlog, bug-backlog is detected.
  (c) not-yet-aged and non-deferred entries are excluded.
  (d) threshold override — a caller-supplied ``threshold_days`` changes what
      qualifies as aged.
  (e) exclude_paths acknowledgment — a path in the caller-supplied exclusion
      set is omitted even though it otherwise qualifies.
  (f) title always present when the frontmatter has it; severity/why_blocked
      surfaced only when present on the record (never synthesized).
  (g) empty result is an empty list, never None.
  (h) ordering — entries sorted by age_days descending.
  (i) handler direct-invocation (async) with the ratified response envelope.

Spec backlink: docs/plans/2026-07-23-queue-triage-terminus-ops.md § C4
"""

from __future__ import annotations

import asyncio
import subprocess
import textwrap
from datetime import date
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Direct module import fires the @register_op("queue.age_ping") decorator —
# registration-surface files (_registry_map.py, ops/__init__.py, op_scopes.py,
# authz/classification.py) are owned by a later chunk and are NOT edited here.
# ---------------------------------------------------------------------------
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.queue_age_ping import AGING_THRESHOLD_DAYS, _handler, age_ping

assert "queue.age_ping" in _REGISTRY, (
    "import guard failed: 'queue.age_ping' not in _REGISTRY — "
    "coordinator_core.ops.queue_age_ping @register_op did not fire"
)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)


def _seed(root: Path, rel_dir: str, name: str, body: str) -> Path:
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"seed {name}"], cwd=root, check=True, capture_output=True
    )
    return p


_TODAY = date(2026, 7, 23)


def test_all_three_families_detect_an_aged_deferred_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/improvement-queue",
        "2026-06-01-aged-improvement.yaml",
        """
        created: 2026-06-01
        title: Aged improvement
        body: Parked a while ago.
        status: deferred
        surface: coordinator_core/ops/example.py
        proposed_action: do the thing
        from_repo: claude-klabauter
        change_kind: code-edit
        """,
    )
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-06-01-aged-debt.yaml",
        """
        created: 2026-06-01
        title: Aged debt
        body: Some debt.
        status: deferred
        source: review
        risk: it might break
        proposed_action: pay it down
        why_blocked: waiting on an upstream decision
        severity: P2
        """,
    )
    _seed(
        tmp_path,
        "state/bug-backlog",
        "2026-06-01-aged-bug.yaml",
        """
        created: 2026-06-01
        title: Aged bug
        body: Something is broken.
        status: deferred
        surface: coordinator_core/ops/example.py
        severity: P1
        """,
    )

    entries = age_ping(tmp_path, today=_TODAY)
    assert len(entries) == 3
    families = {e["family"] for e in entries}
    assert families == {"improvement-queue", "debt-backlog", "bug-backlog"}
    for entry in entries:
        assert entry["status"] == "deferred"
        assert entry["age_days"] >= AGING_THRESHOLD_DAYS
        assert entry["title"] is not None


def test_not_yet_aged_and_non_deferred_excluded(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-07-20-recent-deferred.yaml",
        """
        created: 2026-07-20
        title: Recently deferred debt
        body: Just parked.
        status: deferred
        source: review
        risk: minor
        proposed_action: pay it down
        why_blocked: not yet triaged
        """,
    )
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-06-01-open-not-deferred.yaml",
        """
        created: 2026-06-01
        title: Old but still open
        body: Not parked.
        status: open
        source: review
        risk: minor
        proposed_action: pay it down
        """,
    )

    entries = age_ping(tmp_path, today=_TODAY)
    assert entries == []


def test_threshold_override_changes_qualification(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-07-15-eight-days-old.yaml",
        """
        created: 2026-07-15
        title: Eight days parked
        body: Moderately aged.
        status: deferred
        source: review
        risk: minor
        proposed_action: pay it down
        why_blocked: waiting
        """,
    )

    assert age_ping(tmp_path, today=_TODAY) == []
    entries = age_ping(tmp_path, today=_TODAY, threshold_days=7)
    assert len(entries) == 1
    assert entries[0]["age_days"] == 8


def test_exclude_paths_omits_already_dispositioned_entry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    seeded = _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-06-01-already-triaged.yaml",
        """
        created: 2026-06-01
        title: Already triaged this cycle
        body: Reviewed already.
        status: deferred
        source: review
        risk: minor
        proposed_action: pay it down
        why_blocked: waiting
        """,
    )
    rel_path = seeded.relative_to(tmp_path).as_posix()

    without_exclusion = age_ping(tmp_path, today=_TODAY)
    assert len(without_exclusion) == 1

    with_exclusion = age_ping(tmp_path, today=_TODAY, exclude_paths=[rel_path])
    assert with_exclusion == []


def test_severity_and_why_blocked_surfaced_only_when_present(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/improvement-queue",
        "2026-06-01-no-severity-no-why-blocked.yaml",
        """
        created: 2026-06-01
        title: Improvement queue has no severity or why_blocked field at all
        body: Per schema, improvement-queue declares neither field.
        status: deferred
        surface: coordinator_core/ops/example.py
        proposed_action: do the thing
        from_repo: claude-klabauter
        change_kind: code-edit
        """,
    )

    entries = age_ping(tmp_path, today=_TODAY, families=["improvement-queue"])
    assert len(entries) == 1
    assert "severity" not in entries[0]
    assert "why_blocked" not in entries[0]


def test_ordering_sorted_by_age_days_descending(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-06-15-younger.yaml",
        """
        created: 2026-06-15
        title: Younger parked entry
        body: Less aged.
        status: deferred
        source: review
        risk: minor
        proposed_action: pay it down
        why_blocked: waiting
        """,
    )
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-05-01-older.yaml",
        """
        created: 2026-05-01
        title: Older parked entry
        body: More aged.
        status: deferred
        source: review
        risk: minor
        proposed_action: pay it down
        why_blocked: waiting
        """,
    )

    entries = age_ping(tmp_path, today=_TODAY)
    assert len(entries) == 2
    assert entries[0]["age_days"] >= entries[1]["age_days"]
    assert entries[0]["title"] == "Older parked entry"


def test_empty_result_is_empty_list_never_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "state" / "debt-backlog").mkdir(parents=True, exist_ok=True)
    entries = age_ping(tmp_path, today=_TODAY)
    assert entries == []
    assert entries is not None


def test_handler_direct_invocation_returns_ratified_envelope(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/bug-backlog",
        "2026-06-01-aged-bug.yaml",
        """
        created: 2026-06-01
        title: Aged bug via handler
        body: Something is broken.
        status: deferred
        surface: coordinator_core/ops/example.py
        severity: P1
        """,
    )

    result = asyncio.run(_handler({}, repo_root=tmp_path))
    assert result["status"] == "ok"
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["path"].endswith("2026-06-01-aged-bug.yaml")
    assert entry["family"] == "bug-backlog"
    assert entry["severity"] == "P1"


def test_handler_missing_repo_root_returns_error(tmp_path: Path) -> None:
    result = asyncio.run(_handler({}, repo_root=None))
    assert result.get("exit_code") == 1
    assert "error" in result


def test_handler_with_git_common_dir_finds_records(tmp_path: Path) -> None:
    """Regression for the 2026-07-23 silent-empty-result bug.

    ``queue.age_ping`` is registered ``"common_dir"`` in
    ``coordinator_core.op_scopes._OP_KEY_SCOPE``, so the IPC engine hands its
    handler ``git_common_dir(caller_worktree)`` (``<worktree>/.git``), never
    the worktree root. This exercises exactly that dispatch shape rather than
    the worktree-root shape every other test in this file uses.
    """
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/bug-backlog",
        "2026-06-01-aged-bug.yaml",
        """
        created: 2026-06-01
        title: Aged bug via handler, common-dir shape
        body: Something is broken.
        status: deferred
        surface: coordinator_core/ops/example.py
        severity: P1
        """,
    )
    common_dir = tmp_path / ".git"
    assert common_dir.is_dir()  # sanity: standard (non-worktree) layout

    result = asyncio.run(_handler({}, repo_root=common_dir))
    assert result["status"] == "ok"
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["path"].endswith("2026-06-01-aged-bug.yaml")
    assert entry["family"] == "bug-backlog"
    assert entry["severity"] == "P1"
