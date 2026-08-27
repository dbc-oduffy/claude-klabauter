"""
coordinator_core.ops.tests.test_deliverable_rollup

Tests for the deliverable.rollup COMPUTE_ONLY op (AC7).

Coverage:
  (i)   resolvable_single_artifact  — one plan with deliverable_id + non-null initiative FK
                                      resolving to a real state/initiatives/<id>.yaml entry;
                                      advances_initiatives has that initiative.
  (ii)  multi_artifact_aggregate    — two plans share the same deliverable_id; each carries a
                                      (possibly different) initiative FK; advances_initiatives is
                                      the UNION/aggregate across both (NOT omit). Per AC3 the
                                      expected grain is one-deliverable→many-artifacts; omit-on-multi
                                      is a category error here.
  (iii) edge_unresolvable_omitted   — one artifact has a null initiative FK; that edge is omitted;
                                      advances_initiatives is empty (safe-null for that artifact).
  (iv)  unknown_deliverable         — deliverable_id present in params but no artifact carries it;
                                      artifacts_matched=0, advances_initiatives=[], no error.
  (v)   malformed_deliverable_id    — '../', absolute path ('/etc/passwd'), embedded null byte;
                                      the wire token is NEVER used as a filesystem path component;
                                      returns safe-empty, NOT an error.
  (vi)  compute_only_no_write       — invoking the handler on a fixture worktree writes no file
                                      anywhere under the worktree; the tmp_path tree is unchanged
                                      after the call.

Fixture shape: production-shaped YAML-fenced Markdown files (---...---) mirroring the real
plan/handoff frontmatter format.  Initiatives are minimal YAML files (label + status).

Spec backlink: pln-claude-klabauter-deliverable-spine-fact--cd004e § AC7
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import unittest.mock
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

import coordinator_core.ops.deliverable_rollup as _rollup_mod
from coordinator_core.ops.deliverable_rollup import _handler, _scan_artifacts_by_deliverable_id

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# RollupRepo — lightweight fixture class (no git ops needed by the handler,
# but git init is provided so common_dir matches the standard-layout convention
# used by main_worktree_root: common_dir.parent == worktree root).
# ---------------------------------------------------------------------------


class RollupRepo:
    """Wrapper around a temporary git repository for deliverable.rollup op tests.

    The handler is COMPUTE_ONLY and performs zero git operations, so this repo
    needs only:
      - a .git directory (so common_dir.parent == worktree root)
      - docs/plans/, state/handoffs/, state/initiatives/ as needed

    Usage::

        def test_something(rollup_repo):
            rollup_repo.write_plan("p1.md", deliverable_id="dlv-x", initiative="init-1")
            rollup_repo.write_initiative("init-1", label="Initiative One", status="active")
            result = _handler({"deliverable_id": "dlv-x"}, repo_root=rollup_repo.common_dir)
            assert result["artifacts_matched"] == 1
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def common_dir(self) -> Path:
        """Absolute path to the git common dir (.git for a non-worktree repo).

        Handlers receive this as their repo_root arg; main_worktree_root(repo_root)
        returns common_dir.parent == self.root (the actual worktree).
        """
        return (self.root / ".git").resolve()

    # ------------------------------------------------------------------
    # Helpers for writing fixture content
    # ------------------------------------------------------------------

    def write_plan(
        self,
        name: str,
        *,
        deliverable_id: Optional[str] = None,
        initiative: Optional[str] = None,
        title: str = "Test Plan",
    ) -> Path:
        """Write a production-shaped plan file to docs/plans/<name>.

        Returns the absolute path to the created file.
        """
        path = self.root / "docs" / "plans" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f'title: "{title}"',
            "created: 2026-07-06",
            "status: draft",
        ]
        if deliverable_id is not None:
            lines.append(f"deliverable_id: {deliverable_id}")
        if initiative is not None:
            # YAML null for Python None — emit bare null; emit the id as a bare scalar otherwise.
            if initiative == "null":
                lines.append("initiative: null")
            else:
                lines.append(f"initiative: {initiative}")
        else:
            lines.append("initiative: null")

        fm_body = "\n".join(lines)
        content = f"---\n{fm_body}\n---\n\n# {title}\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        return path

    def write_handoff(
        self,
        name: str,
        *,
        deliverable_id: Optional[str] = None,
        initiative: Optional[str] = None,
        title: str = "Test Handoff",
    ) -> Path:
        """Write a stub handoff file to state/handoffs/<name>."""
        path = self.root / "state" / "handoffs" / name
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f'title: "{title}"',
            "created: 2026-07-06",
            "branch: work/test/2026-07-06",
            "status: open",
        ]
        if deliverable_id is not None:
            lines.append(f"deliverable_id: {deliverable_id}")
        if initiative is not None:
            if initiative == "null":
                lines.append("initiative: null")
            else:
                lines.append(f"initiative: {initiative}")
        else:
            lines.append("initiative: null")

        fm_body = "\n".join(lines)
        content = f"---\n{fm_body}\n---\n\n# {title}\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        return path

    def write_archive_handoff(
        self,
        subdir: str,
        name: str,
        *,
        deliverable_id: Optional[str] = None,
        initiative: Optional[str] = None,
        title: str = "Test Archive Handoff",
    ) -> Path:
        """Write a stub handoff file to archive/handoffs/<subdir>/<name>.

        Purpose: exercises the recursive archive/handoffs/**/*.md scan path.
        """
        # Review: code-reviewer — add archive-handoff writer to exercise archive/handoffs/**/*.md
        # scan glob (F4); the recursive pattern differs structurally from the flat *.md paths.
        path = self.root / "archive" / "handoffs" / subdir / name
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f'title: "{title}"',
            "created: 2026-07-01",
            "branch: work/test/2026-07-01",
            "status: archived",
        ]
        if deliverable_id is not None:
            lines.append(f"deliverable_id: {deliverable_id}")
        if initiative is not None:
            if initiative == "null":
                lines.append("initiative: null")
            else:
                lines.append(f"initiative: {initiative}")
        else:
            lines.append("initiative: null")

        fm_body = "\n".join(lines)
        content = f"---\n{fm_body}\n---\n\n# {title}\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        return path

    def write_archive_spec(
        self,
        subdir: str,
        name: str,
        *,
        deliverable_id: Optional[str] = None,
        initiative: Optional[str] = None,
        title: str = "Test Archive Spec",
    ) -> Path:
        """Write a plan-shaped file to archive/specs/<subdir>/<name>.

        Purpose: exercises the recursive archive/specs/**/*.md scan path (C1).
        `fleet.archive_completed_plans` moves a plan here from docs/plans/ the
        instant its status flips terminal.
        """
        path = self.root / "archive" / "specs" / subdir / name
        path.parent.mkdir(parents=True, exist_ok=True)

        lines = [
            f'title: "{title}"',
            "created: 2026-07-01",
            "status: implemented",
        ]
        if deliverable_id is not None:
            lines.append(f"deliverable_id: {deliverable_id}")
        if initiative is not None:
            if initiative == "null":
                lines.append("initiative: null")
            else:
                lines.append(f"initiative: {initiative}")
        else:
            lines.append("initiative: null")

        fm_body = "\n".join(lines)
        content = f"---\n{fm_body}\n---\n\n# {title}\n\nBody.\n"
        path.write_text(content, encoding="utf-8")
        return path

    def write_initiative(
        self,
        initiative_id: str,
        *,
        label: str = "Test Initiative",
        status: str = "active",
    ) -> Path:
        """Write a minimal state/initiatives/<initiative_id>.yaml."""
        path = self.root / "state" / "initiatives" / f"{initiative_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = f"label: {label!r}\nstatus: {status!r}\n"
        path.write_text(content, encoding="utf-8")
        return path

    def write_sizing(
        self,
        name: str,
        *,
        deliverable_id: Optional[str] = None,
        plan_id: Optional[str] = None,
        intent: str = "Test sizing intent.",
    ) -> Path:
        """Write a whole-document YAML sizing object to state/sizings/<name>.

        Purpose: exercises the flat state/sizings/*.yaml scan path (C10 leg
        (a)). Sizings have NO `---` frontmatter fence — unlike every other
        write_* helper on this fixture, this is a bare YAML document.
        """
        path = self.root / "state" / "sizings" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f'intent: "{intent}"', "schema: sizing-object"]
        if deliverable_id is not None:
            lines.append(f"deliverable_id: {deliverable_id}")
        if plan_id is not None:
            lines.append(f"plan_id: {plan_id}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def snapshot_paths(self) -> set:
        """Return the set of all file paths currently present under self.root.

        Used by the COMPUTE_ONLY no-write assertion (case vi): call before and
        after the handler and assert the sets are equal.
        """
        return {str(p) for p in self.root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def rollup_repo(tmp_path) -> RollupRepo:
    """Provide a temporary git-init'd repository for deliverable.rollup op tests.

    The repo has:
      - git config user.email / user.name / commit.gpgsign=false set
      - A .git directory (standard layout) so common_dir.parent == worktree root
      - No initial commit required (handler performs zero git operations)

    Usage::

        def test_something(rollup_repo):
            rollup_repo.write_plan("p.md", deliverable_id="dlv-x", initiative="init-y")
            rollup_repo.write_initiative("init-y", label="Y", status="active")
            result = _handler({"deliverable_id": "dlv-x"}, repo_root=rollup_repo.common_dir)
            assert result["artifacts_matched"] == 1
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
    _git("config", "user.email", "rollup-test@claude-klabauter.test")
    _git("config", "user.name", "Rollup Test")
    _git("config", "commit.gpgsign", "false")

    return RollupRepo(repo_root)


# ---------------------------------------------------------------------------
# Autouse reset — prevent module-scope memo state leaking between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_central_root_memo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level memoization globals before (and after) each test.

    Without this fixture, a test that resolves the central root would cache the result
    in _RESOLVED_CENTRAL_ROOT / _CENTRAL_ROOT_RESOLVED, causing subsequent tests to skip
    the resolution branch entirely — producing false passes or false failures depending
    on fixture order.

    The fixture also:
    - Unsets CLAUDE_KLABAUTER_ROOT so existing tests use worktree-local fallback by default
      (matching the pre-C1 behaviour: tests that write initiatives into rollup_repo.root
      must resolve from there, not from a stale or ambient CLAUDE_KLABAUTER_ROOT).
    - Patches _machine_local_get to return None, suppressing the registry subprocess
      for tests that do not need central resolution.  Individual tests that need a
      non-None registry result override this with their own monkeypatch/mock.
    """
    _rollup_mod._reset_central_root_cache()

    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
    monkeypatch.setattr(_rollup_mod, "_machine_local_get", lambda key: None)

    yield

    # Post-test cleanup (safety net for tests that mutate globals without monkeypatch).
    _rollup_mod._reset_central_root_cache()


# ---------------------------------------------------------------------------
# Helper — assert the payload schema is well-formed
# ---------------------------------------------------------------------------


def _assert_schema(result: dict, deliverable_id: str) -> None:
    """Assert the result satisfies the pinned return-field schema (C0 findings)."""
    assert result["deliverable_id"] == deliverable_id
    assert result["resolution_mode"] == "direct"
    assert isinstance(result["artifacts_matched"], int)
    assert isinstance(result["advances_initiatives"], list)
    for entry in result["advances_initiatives"]:
        assert "id" in entry
        assert "label" in entry
        assert "status" in entry
    assert isinstance(result["scan_incomplete"], bool)


# ---------------------------------------------------------------------------
# (i) resolvable single artifact
# ---------------------------------------------------------------------------


def test_resolvable_single_artifact(rollup_repo: RollupRepo) -> None:
    """A plan carrying deliverable_id + a non-null initiative FK resolves to a real
    initiatives file → advances_initiatives contains that initiative."""
    rollup_repo.write_plan(
        "2026-07-06-my-feature.md",
        deliverable_id="dlv-single-a",
        initiative="claude-klabauter-strangler",
    )
    rollup_repo.write_initiative(
        "claude-klabauter-strangler",
        label="Claude-Klabauter Strangler Initiative",
        status="active",
    )

    result = _handler(
        {"deliverable_id": "dlv-single-a"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-single-a")
    assert result["artifacts_matched"] == 1
    assert len(result["advances_initiatives"]) == 1
    entry = result["advances_initiatives"][0]
    assert entry["id"] == "claude-klabauter-strangler"
    assert entry["label"] == "Claude-Klabauter Strangler Initiative"
    assert entry["status"] == "active"


# ---------------------------------------------------------------------------
# (ii) multi-artifact-per-deliverable → UNION/aggregate, deduped by id
# ---------------------------------------------------------------------------


def test_multi_artifact_aggregate_union(rollup_repo: RollupRepo) -> None:
    """Two plans share the same deliverable_id; each carries a different initiative FK.
    advances_initiatives is the UNION of both, deduped by id.  AC3: aggregate, NOT omit."""
    rollup_repo.write_plan(
        "2026-07-06-feature-plan.md",
        deliverable_id="dlv-multi-b",
        initiative="init-alpha",
    )
    rollup_repo.write_plan(
        "2026-07-06-feature-handoff.md",
        deliverable_id="dlv-multi-b",
        initiative="init-beta",
    )
    rollup_repo.write_initiative("init-alpha", label="Alpha Initiative", status="active")
    rollup_repo.write_initiative("init-beta", label="Beta Initiative", status="planned")

    result = _handler(
        {"deliverable_id": "dlv-multi-b"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-multi-b")
    assert result["artifacts_matched"] == 2
    initiative_ids = {e["id"] for e in result["advances_initiatives"]}
    assert initiative_ids == {"init-alpha", "init-beta"}


def test_multi_artifact_deduplication(rollup_repo: RollupRepo) -> None:
    """Three plans share a deliverable_id; two carry the same initiative FK.
    advances_initiatives deduplicates by id — the repeated FK appears once only."""
    for suffix in ("a", "b", "c"):
        rollup_repo.write_plan(
            f"2026-07-06-plan-{suffix}.md",
            deliverable_id="dlv-dedup-c",
            initiative="shared-init",
        )
    rollup_repo.write_initiative("shared-init", label="Shared Initiative", status="active")

    result = _handler(
        {"deliverable_id": "dlv-dedup-c"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-dedup-c")
    assert result["artifacts_matched"] == 3
    # Three artifacts, same FK — deduped to one entry.
    assert len(result["advances_initiatives"]) == 1
    assert result["advances_initiatives"][0]["id"] == "shared-init"


# ---------------------------------------------------------------------------
# (iii) null/absent initiative FK → that edge omitted, safe-empty for that artifact
# ---------------------------------------------------------------------------


def test_null_initiative_fk_omitted(rollup_repo: RollupRepo) -> None:
    """An artifact with a null initiative FK contributes to artifacts_matched but
    produces no entry in advances_initiatives.  Empty list is the safe null."""
    rollup_repo.write_plan(
        "2026-07-06-no-initiative.md",
        deliverable_id="dlv-null-fk-d",
        initiative="null",  # explicit null in YAML
    )

    result = _handler(
        {"deliverable_id": "dlv-null-fk-d"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-null-fk-d")
    assert result["artifacts_matched"] == 1
    assert result["advances_initiatives"] == []


def test_unresolvable_fk_omitted(rollup_repo: RollupRepo) -> None:
    """An artifact carries a non-null initiative FK, but no state/initiatives/<id>.yaml
    exists.  Precision-over-recall: that edge is omitted, advances_initiatives is empty."""
    rollup_repo.write_plan(
        "2026-07-06-dangling-init.md",
        deliverable_id="dlv-dangling-e",
        initiative="nonexistent-init",
    )
    # Intentionally do NOT write a state/initiatives/nonexistent-init.yaml file.

    result = _handler(
        {"deliverable_id": "dlv-dangling-e"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-dangling-e")
    assert result["artifacts_matched"] == 1
    assert result["advances_initiatives"] == []


def test_mixed_null_and_resolvable_fks(rollup_repo: RollupRepo) -> None:
    """Two artifacts: one null FK (omitted), one resolvable FK (included).
    Precision-over-recall governs at the edge level, not the deliverable level."""
    rollup_repo.write_plan(
        "2026-07-06-plan-null.md",
        deliverable_id="dlv-mixed-f",
        initiative="null",
    )
    rollup_repo.write_plan(
        "2026-07-06-plan-real.md",
        deliverable_id="dlv-mixed-f",
        initiative="real-init",
    )
    rollup_repo.write_initiative("real-init", label="Real Initiative", status="active")

    result = _handler(
        {"deliverable_id": "dlv-mixed-f"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-mixed-f")
    assert result["artifacts_matched"] == 2
    assert len(result["advances_initiatives"]) == 1
    assert result["advances_initiatives"][0]["id"] == "real-init"


# ---------------------------------------------------------------------------
# (iv) unknown / absent deliverable_id → safe-empty, no error
# ---------------------------------------------------------------------------


def test_unknown_deliverable_id(rollup_repo: RollupRepo) -> None:
    """A deliverable_id that no artifact carries returns artifacts_matched=0 and
    an empty advances_initiatives.  No exception is raised."""
    # Write a plan with a different deliverable_id to confirm the scan is running.
    rollup_repo.write_plan(
        "2026-07-06-other.md",
        deliverable_id="dlv-other",
        initiative="init-other",
    )
    rollup_repo.write_initiative("init-other", label="Other", status="active")

    result = _handler(
        {"deliverable_id": "dlv-completely-unknown"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-completely-unknown")
    assert result["artifacts_matched"] == 0
    assert result["advances_initiatives"] == []


def test_absent_deliverable_id_param(rollup_repo: RollupRepo) -> None:
    """deliverable_id param absent from the wire dict → empty payload, no error."""
    result = _handler({}, repo_root=rollup_repo.common_dir)

    # When deliverable_id is absent/empty the handler returns an empty payload.
    # Review: code-reviewer — pin echoed deliverable_id="" to confirm the producer contract
    # guarantees deliverable_id is present in every response, including the absent-param path.
    assert result["deliverable_id"] == ""
    assert result["resolution_mode"] == "direct"
    assert result["artifacts_matched"] == 0
    assert result["advances_initiatives"] == []


def test_none_repo_root_returns_safe_empty() -> None:
    """repo_root=None → safe-empty payload; no filesystem access attempted."""
    result = _handler({"deliverable_id": "dlv-no-repo"}, repo_root=None)

    _assert_schema(result, "dlv-no-repo")
    assert result["artifacts_matched"] == 0
    assert result["advances_initiatives"] == []


# ---------------------------------------------------------------------------
# (v) malformed / injected deliverable_id → safe-empty, NOT an error
#     The wire token is NEVER used as a filesystem path component.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "malformed_id",
    [
        "../../../etc/passwd",
        "../../state/initiatives/sneaky",
        "/etc/passwd",
        "/absolute/path/attempt",
        "../",
        # embedded null byte — cannot appear in a real frontmatter value
        "dlv-null\x00byte",
        # path traversal mixed with a plausible deliverable fragment
        "../dlv-real",
        # blank / whitespace-only (treated as absent)
        "",
        "   ",
    ],
)
def test_malformed_deliverable_id_safe_empty(
    rollup_repo: RollupRepo, malformed_id: str
) -> None:
    """Malformed or injected deliverable_id values never produce a filesystem path.
    The token is used only as a frontmatter filter VALUE (string equality).
    All malformed inputs return safe-empty; no exception is raised."""
    result = _handler(
        {"deliverable_id": malformed_id},
        repo_root=rollup_repo.common_dir,
    )

    # Schema keys must be present.
    assert result["resolution_mode"] == "direct"
    # Review: code-reviewer — assert value, not just type; a bug matching traversal tokens
    # would still pass advances_initiatives==[] but produce non-zero artifacts_matched.
    assert result["artifacts_matched"] == 0
    assert isinstance(result["advances_initiatives"], list)
    # No artifacts should ever match a path-traversal token.
    assert result["advances_initiatives"] == []
    # No error propagated — safe-empty is the response.


# ---------------------------------------------------------------------------
# (vi) COMPUTE_ONLY no-write assertion
# ---------------------------------------------------------------------------


def test_compute_only_no_write(rollup_repo: RollupRepo) -> None:
    """Invoking the handler writes no file anywhere under the worktree.
    The set of files before and after the call must be identical."""
    rollup_repo.write_plan(
        "2026-07-06-compute-only.md",
        deliverable_id="dlv-compute-g",
        initiative="init-g",
    )
    rollup_repo.write_initiative("init-g", label="G Initiative", status="active")

    before = rollup_repo.snapshot_paths()

    result = _handler(
        {"deliverable_id": "dlv-compute-g"},
        repo_root=rollup_repo.common_dir,
    )

    after = rollup_repo.snapshot_paths()

    # Handler must have returned a valid result (not failed silently).
    assert result["artifacts_matched"] == 1
    assert len(result["advances_initiatives"]) == 1

    # No new files written, no existing files deleted.
    assert after == before, (
        f"Handler wrote unexpected files: {after - before}; "
        f"missing files: {before - after}"
    )


def test_compute_only_no_write_empty_result(rollup_repo: RollupRepo) -> None:
    """Even when the deliverable_id is unknown (safe-empty path), no file is written."""
    # Write something so the worktree is not completely empty.
    rollup_repo.write_plan("2026-07-06-background.md", deliverable_id="dlv-bg")

    before = rollup_repo.snapshot_paths()

    _handler(
        {"deliverable_id": "dlv-not-present"},
        repo_root=rollup_repo.common_dir,
    )

    after = rollup_repo.snapshot_paths()
    assert after == before


# ---------------------------------------------------------------------------
# (vii) state/handoffs scan path — exercises secondary scan surface
# ---------------------------------------------------------------------------


def test_handoff_scan_path(rollup_repo: RollupRepo) -> None:
    """A stub handoff carrying deliverable_id + non-null initiative FK is found via
    the state/handoffs/*.md scan glob.

    Review: code-reviewer (F3) — state/handoffs/*.md scan path was untested;
    write_handoff() existed but was never called. Confirms multi-surface scan, not just docs/plans.
    """
    rollup_repo.write_handoff(
        "2026-07-06-handoff-scan-test.md",
        deliverable_id="dlv-handoff-scan",
        initiative="init-handoff-h",
    )
    rollup_repo.write_initiative("init-handoff-h", label="Handoff Initiative", status="active")

    result = _handler(
        {"deliverable_id": "dlv-handoff-scan"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-handoff-scan")
    assert result["artifacts_matched"] == 1  # Review: code-reviewer — each test writes exactly one artifact; >= 1 would miss inflation bugs
    initiative_ids = {e["id"] for e in result["advances_initiatives"]}
    assert "init-handoff-h" in initiative_ids


# ---------------------------------------------------------------------------
# (viii) archive/handoffs scan path — exercises tertiary recursive scan surface
# ---------------------------------------------------------------------------


def test_archive_handoff_scan_path(rollup_repo: RollupRepo) -> None:
    """A stub handoff in archive/handoffs/<subdir>/ is found via the recursive
    archive/handoffs/**/*.md scan glob.

    Review: code-reviewer (F4) — archive/handoffs/**/*.md scan path was untested;
    no archive fixture existed. The recursive **/*.md pattern differs structurally
    from the flat *.md patterns for the other two surfaces and was completely dark.
    """
    rollup_repo.write_archive_handoff(
        "2026-07/",
        "2026-07-01-archived-handoff.md",
        deliverable_id="dlv-archive-scan",
        initiative="init-archive-i",
    )
    rollup_repo.write_initiative("init-archive-i", label="Archive Initiative", status="done")

    result = _handler(
        {"deliverable_id": "dlv-archive-scan"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-archive-scan")
    assert result["artifacts_matched"] == 1  # Review: code-reviewer — each test writes exactly one artifact; >= 1 would miss inflation bugs
    initiative_ids = {e["id"] for e in result["advances_initiatives"]}
    assert "init-archive-i" in initiative_ids


# ---------------------------------------------------------------------------
# (viii-b) C1/AC4 — archive/specs scan path — fourth recursive scan root
# ---------------------------------------------------------------------------


def test_archive_specs_scan_path_deliverable_id_only_under_archive_specs(
    rollup_repo: RollupRepo,
) -> None:
    """AC4: a deliverable_id carried ONLY by a file under
    archive/specs/<YYYY-MM>/ resolves — the post-archival commit that, before
    C1, was refused now succeeds. No file with this deliverable_id exists
    under docs/plans (or any other root), so this proves the new root itself
    is scanned, not that some other root already covered it."""
    rollup_repo.write_archive_spec(
        "2026-08",
        "2026-08-01-archived-plan.md",
        deliverable_id="dlv-archived-spec-only",
        initiative="init-archived-spec",
    )
    rollup_repo.write_initiative(
        "init-archived-spec", label="Archived Spec Initiative", status="active"
    )

    result = _handler(
        {"deliverable_id": "dlv-archived-spec-only"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-archived-spec-only")
    assert result["artifacts_matched"] == 1
    initiative_ids = {e["id"] for e in result["advances_initiatives"]}
    assert "init-archived-spec" in initiative_ids


# ---------------------------------------------------------------------------
# (ix) traversal guard in initiative_id — malformed frontmatter initiative FK
# ---------------------------------------------------------------------------


def test_traversal_guard_in_initiative_id(rollup_repo: RollupRepo) -> None:
    """An artifact whose frontmatter contains a path-traversal initiative FK
    (e.g. '../../evil') resolves to no initiative entry — the traversal guard in
    _resolve_initiative rejects it before any path construction.

    Review: code-reviewer (F2) — initiative_id from artifact frontmatter was used
    in path construction without a traversal guard; an accidental '../../other' value
    could silently read the wrong YAML. The guard now rejects such ids pre-path-join.
    """
    # Write a plan whose initiative FK contains a traversal sequence.
    plan_path = rollup_repo.root / "docs" / "plans" / "2026-07-06-traversal-test.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "---\n"
        "title: traversal test\n"
        "created: 2026-07-06\n"
        "status: draft\n"
        "deliverable_id: dlv-traversal-guard\n"
        "initiative: ../../evil\n"
        "---\n\n# traversal test\n\nBody.\n"
    )
    plan_path.write_text(content, encoding="utf-8")

    # Do NOT write any evil.yaml file — if traversal succeeded it would escape state/initiatives/.

    result = _handler(
        {"deliverable_id": "dlv-traversal-guard"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-traversal-guard")
    # Artifact is found (deliverable_id matched), but the traversal initiative FK is rejected.
    assert result["artifacts_matched"] == 1
    assert result["advances_initiatives"] == []


# ---------------------------------------------------------------------------
# (x) AC1 — central resolution via CLAUDE_KLABAUTER_ROOT env
# ---------------------------------------------------------------------------


def test_ac1_central_resolve_via_claude_klabauter_root_env(
    rollup_repo: RollupRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1: CLAUDE_KLABAUTER_ROOT set to a tree with state/initiatives/<fk>.yaml; DoE-style scan
    worktree has NO local state/initiatives/ — advances_initiatives still resolves the FK
    from the central (CLAUDE_KLABAUTER_ROOT) tree.

    This is the primary failure mode fixed by C1: DoE deliverables with a complete FK
    population return advances_initiatives=[] when the entity lives only centrally.
    """
    # --- Central (claude-klabauter) tree: holds the initiative entity ---
    central_root = tmp_path / "claude-klabauter-central"
    central_initiatives = central_root / "state" / "initiatives"
    central_initiatives.mkdir(parents=True)
    (central_initiatives / "fleet-deliverable-spine.yaml").write_text(
        "label: 'Fleet Deliverable Spine'\nstatus: 'active'\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(central_root))

    # --- DoE-style scan worktree: plan carries FK, NO local state/initiatives/ ---
    rollup_repo.write_plan(
        "2026-07-06-doe-deliverable.md",
        deliverable_id="dlv-doe-central-ac1",
        initiative="fleet-deliverable-spine",
    )
    # Intentionally do NOT call rollup_repo.write_initiative(...) —
    # the initiative entity lives only in the central (CLAUDE_KLABAUTER_ROOT) tree.

    result = _handler(
        {"deliverable_id": "dlv-doe-central-ac1"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-doe-central-ac1")
    assert result["artifacts_matched"] == 1
    assert len(result["advances_initiatives"]) == 1
    entry = result["advances_initiatives"][0]
    assert entry["id"] == "fleet-deliverable-spine"
    assert entry["label"] == "Fleet Deliverable Spine"
    assert entry["status"] == "active"


# ---------------------------------------------------------------------------
# (xi) AC2 — dual-gate fallback: CLAUDE_KLABAUTER_ROOT unset AND registry returns None
# ---------------------------------------------------------------------------


def test_ac2_fallback_to_worktree_local_dual_gate(
    rollup_repo: RollupRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2: CLAUDE_KLABAUTER_ROOT unset AND _machine_local_get mocked to None → resolution falls
    back to worktree-local state/initiatives/ — byte-identical to pre-C1 behaviour.

    Both gates are independently suppressed (AC2 test-seam lesson 2026-07-02-a):
    merely unsetting CLAUDE_KLABAUTER_ROOT on a configured machine would still resolve centrally
    via the machine-local registry and silently miss the fallback branch.
    Patching the exact module-local _machine_local_get (not queue_append's copy) is
    the correct seam; patching queue_append's copy is a no-op for this op.
    """
    # autouse fixture already: unsets CLAUDE_KLABAUTER_ROOT, patches _machine_local_get → None.
    # Explicitly patch again to assert the exact target and make the test self-documenting.
    with patch("coordinator_core.ops.deliverable_rollup._machine_local_get", return_value=None):
        rollup_repo.write_plan(
            "2026-07-06-local-fallback.md",
            deliverable_id="dlv-local-fallback-ac2",
            initiative="local-fallback-init",
        )
        rollup_repo.write_initiative(
            "local-fallback-init",
            label="Local Fallback Initiative",
            status="active",
        )

        result = _handler(
            {"deliverable_id": "dlv-local-fallback-ac2"},
            repo_root=rollup_repo.common_dir,
        )

    _assert_schema(result, "dlv-local-fallback-ac2")
    assert result["artifacts_matched"] == 1
    assert len(result["advances_initiatives"]) == 1
    assert result["advances_initiatives"][0]["id"] == "local-fallback-init"


# ---------------------------------------------------------------------------
# (xii) AC3 — coincident-dir case: CLAUDE_KLABAUTER_ROOT == scan worktree root
# ---------------------------------------------------------------------------


def test_ac3_coincident_dir_realpath_equivalence(
    rollup_repo: RollupRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: When CLAUDE_KLABAUTER_ROOT equals the scan worktree root (claude-klabauter's own invocation),
    the resolved initiatives_dir and worktree_root/"state"/"initiatives" compare EQUAL
    under Path.resolve() — asserting the coincident-dir claim in § Fix shape.

    This is NOT merely "an FK resolves" (which could succeed via either branch);
    it asserts the resolved *path* is the same physical directory as the worktree-local one.
    """
    from coordinator_core.ops.deliverable_rollup import _central_initiatives_dir
    from coordinator_core.ops.fleet._common import main_worktree_root

    # Point COORDINATOR_ENGINE_ROOT at the scan worktree root — the coincident case.
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(rollup_repo.root))

    worktree_root = main_worktree_root(rollup_repo.common_dir)

    # Call _central_initiatives_dir directly to inspect the resolved path.
    resolved_dir = _central_initiatives_dir(worktree_root)
    expected_dir = worktree_root / "state" / "initiatives"

    assert resolved_dir.resolve() == expected_dir.resolve(), (
        f"Coincident-dir claim violated: resolved={resolved_dir!r}, "
        f"expected={expected_dir!r} (under Path.resolve())"
    )

    # Also confirm an FK resolves end-to-end via the handler.
    rollup_repo.write_plan(
        "2026-07-06-own-worktree.md",
        deliverable_id="dlv-own-worktree-ac3",
        initiative="own-initiative-ac3",
    )
    rollup_repo.write_initiative(
        "own-initiative-ac3",
        label="Own Worktree Initiative",
        status="active",
    )

    result = _handler(
        {"deliverable_id": "dlv-own-worktree-ac3"},
        repo_root=rollup_repo.common_dir,
    )

    _assert_schema(result, "dlv-own-worktree-ac3")
    assert result["artifacts_matched"] == 1
    assert len(result["advances_initiatives"]) == 1
    assert result["advances_initiatives"][0]["id"] == "own-initiative-ac3"


# ---------------------------------------------------------------------------
# (xiii) AC4 — resolve-once: _central_initiatives_dir called once per handler call
# ---------------------------------------------------------------------------


def test_ac4_initiatives_dir_resolved_once_per_handler_call(
    rollup_repo: RollupRepo,
) -> None:
    """AC4: With N>1 matching artifacts, _central_initiatives_dir is invoked exactly
    once per handler call — NOT once per artifact (not inside the FK loop).

    The handler computes initiatives_dir = _central_initiatives_dir(worktree_root) once
    before the loop and passes the cached Path into _resolve_initiative per-edge.
    """
    # Write 4 artifacts sharing the same deliverable_id, each with a distinct initiative FK.
    for i in range(4):
        rollup_repo.write_plan(
            f"2026-07-06-ac4-plan-{i}.md",
            deliverable_id="dlv-resolve-once-ac4",
            initiative=f"init-once-{i}",
        )
        rollup_repo.write_initiative(
            f"init-once-{i}",
            label=f"Initiative {i}",
            status="active",
        )

    with patch(
        "coordinator_core.ops.deliverable_rollup._central_initiatives_dir",
        wraps=_rollup_mod._central_initiatives_dir,
    ) as spy:
        result = _handler(
            {"deliverable_id": "dlv-resolve-once-ac4"},
            repo_root=rollup_repo.common_dir,
        )

    assert result["artifacts_matched"] == 4
    assert spy.call_count == 1, (
        f"_central_initiatives_dir was called {spy.call_count} times for 4 artifacts; "
        "expected exactly 1 call per handler invocation (resolve-once invariant)"
    )


# ---------------------------------------------------------------------------
# (xiv) AC9 — WARN emitted on unresolvable fallback; silent on coincident case
# ---------------------------------------------------------------------------


def test_ac9_warn_emitted_on_unresolvable_fallback(
    rollup_repo: RollupRepo, caplog: pytest.LogCaptureFixture
) -> None:
    """AC9 (part 1): A WARN is logged (once) when the claude-klabauter central root is
    unresolvable (CLAUDE_KLABAUTER_ROOT unset AND registry returns None) and resolution
    falls back to worktree-local.  The WARN makes systemic misconfiguration observable.
    """
    # autouse fixture: CLAUDE_KLABAUTER_ROOT unset, _machine_local_get → None → fallback path.
    rollup_repo.write_plan(
        "2026-07-06-ac9-warn.md",
        deliverable_id="dlv-ac9-warn",
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"):
        _handler({"deliverable_id": "dlv-ac9-warn"}, repo_root=rollup_repo.common_dir)

    warn_messages = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "coordinator_core.ops.deliverable_rollup"
    ]
    assert warn_messages, "Expected at least one WARNING from deliverable_rollup; got none"
    assert any(
        "unresolvable" in m or "CLAUDE_KLABAUTER_ROOT" in m
        for m in warn_messages
    ), (
        f"Expected a WARN mentioning 'unresolvable' or 'CLAUDE_KLABAUTER_ROOT'; got: {warn_messages}"
    )


def test_ac9_no_warn_on_coincident_case(
    rollup_repo: RollupRepo, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC9 (part 2): No 'unresolvable' WARN is emitted when CLAUDE_KLABAUTER_ROOT is set (coincident
    case — central root resolves successfully, even though it happens to equal the worktree).

    The WARN is guarded to the unresolvable-root fallback branch only; a successful
    central resolution (step 1 or 2 in the precedence chain) must not trigger it.
    """
    # Coincident case: point COORDINATOR_ENGINE_ROOT at the scan worktree root.
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(rollup_repo.root))

    rollup_repo.write_plan(
        "2026-07-06-ac9-no-warn.md",
        deliverable_id="dlv-ac9-no-warn",
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"):
        _handler({"deliverable_id": "dlv-ac9-no-warn"}, repo_root=rollup_repo.common_dir)

    unresolvable_warns = [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and r.name == "coordinator_core.ops.deliverable_rollup"
        and ("unresolvable" in r.getMessage() or "machine-local registry" in r.getMessage())
    ]
    assert not unresolvable_warns, (
        f"Unexpected 'unresolvable' WARN on coincident-dir case: {unresolvable_warns}"
    )


# ---------------------------------------------------------------------------
# (xv) AC10 — memoization: _machine_local_get fires at most once across calls
# ---------------------------------------------------------------------------


def test_ac10_machine_local_get_memoized_across_handler_calls(
    rollup_repo: RollupRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC10: The resolved central root is memoized at module scope.  Across two handler
    calls, _machine_local_get fires at most once total — the second call hits the memo
    and skips the registry subprocess entirely.

    CLAUDE_KLABAUTER_ROOT is unset so the env fast-path is inactive and the registry path fires
    on the first call; the second call must return the cached result without re-invoking
    the registry subprocess.
    """
    # Override the autouse lambda with a MagicMock so we can count calls.
    # Returns the rollup_repo root so central resolution succeeds (no fallback, no WARN).
    mock_get = MagicMock(return_value=str(rollup_repo.root))
    monkeypatch.setattr(_rollup_mod, "_machine_local_get", mock_get)

    rollup_repo.write_plan(
        "2026-07-06-ac10-memo.md",
        deliverable_id="dlv-ac10-memo",
        initiative="memo-initiative",
    )
    rollup_repo.write_initiative(
        "memo-initiative",
        label="Memo Initiative",
        status="active",
    )

    # First call — resolves centrally, populates the memo.
    result1 = _handler({"deliverable_id": "dlv-ac10-memo"}, repo_root=rollup_repo.common_dir)
    # Second call — must use the memoized value without re-invoking _machine_local_get.
    result2 = _handler({"deliverable_id": "dlv-ac10-memo"}, repo_root=rollup_repo.common_dir)

    assert result1["advances_initiatives"][0]["id"] == "memo-initiative"
    assert result2["advances_initiatives"][0]["id"] == "memo-initiative"

    # Review: code-reviewer — == 1 not <= 1: call_count==0 would mean memoization skipped
    # resolution entirely (incorrect pass — AC10 would be unverified).
    assert mock_get.call_count == 1, (
        f"_machine_local_get was called {mock_get.call_count} times across two handler "
        "invocations; expected exactly 1 (first call resolves; second call uses memo — AC10)"
    )


# ---------------------------------------------------------------------------
# (xvi) AC9 WARN-once — sentinel fires exactly once across sequential handler calls
# ---------------------------------------------------------------------------


def test_ac9_warn_fires_exactly_once_across_sequential_calls(
    rollup_repo: RollupRepo, caplog: pytest.LogCaptureFixture
) -> None:
    """AC9 (once-per-process invariant): two sequential handler calls with the
    unresolvable-root fallback active must produce exactly ONE WARN from
    deliverable_rollup — not two.

    The autouse fixture resets _CENTRAL_ROOT_WARNED before this test (pre-call-1),
    but we do NOT reset it between call-1 and call-2.  That's the invariant under test:
    _CENTRAL_ROOT_WARNED=True set by call-1 must suppress the WARN in call-2.

    Note on concurrent-first-call window: two asyncio.to_thread threads can both read
    _CENTRAL_ROOT_WARNED=False before either sets it True, potentially causing two WARNs
    at startup. Under CPython's GIL this is benign; the once-per-process invariant is
    sequential (best-effort for the concurrent case). See _central_initiatives_dir comment.
    """
    # autouse fixture: CLAUDE_KLABAUTER_ROOT unset, _machine_local_get → None → fallback path.
    rollup_repo.write_plan(
        "2026-07-06-ac9-once-call1.md",
        deliverable_id="dlv-ac9-once-1",
    )
    rollup_repo.write_plan(
        "2026-07-06-ac9-once-call2.md",
        deliverable_id="dlv-ac9-once-2",
    )

    with caplog.at_level(logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"):
        # Call 1: _CENTRAL_ROOT_WARNED is False → WARN fires, sentinel set to True.
        _handler({"deliverable_id": "dlv-ac9-once-1"}, repo_root=rollup_repo.common_dir)
        # DO NOT reset _rollup_mod._CENTRAL_ROOT_WARNED here — that's the point.
        # Call 2: _CENTRAL_ROOT_WARNED is True → WARN must NOT fire again.
        _handler({"deliverable_id": "dlv-ac9-once-2"}, repo_root=rollup_repo.common_dir)

    warn_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "coordinator_core.ops.deliverable_rollup"
    ]
    assert len(warn_records) == 1, (
        f"Expected exactly 1 WARN from deliverable_rollup across two sequential calls "
        f"with unresolvable fallback; got {len(warn_records)}: "
        f"{[r.getMessage() for r in warn_records]}"
    )


# ---------------------------------------------------------------------------
# _machine_local_impl — settings-home repoint (AC3, C3)
#
# Spec backlink: pln-repoint-coordinator-core-claud-56d805 § C3
# ---------------------------------------------------------------------------


class TestMachineLocalImplSettingsHomeRepoint:
    """_machine_local_impl() prefers <settings-home>/bin/_machine_local.py, falling
    back to the legacy ~/.claude/bin path only when the settings-home impl is absent.
    """

    def test_prefers_settings_home_impl_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
        settings_home_root = tmp_path / "settings_home"
        (settings_home_root / "bin").mkdir(parents=True)
        expected_impl = settings_home_root / "bin" / "_machine_local.py"
        expected_impl.write_text("# stub\n")
        monkeypatch.setattr(
            _rollup_mod, "settings_home", lambda: settings_home_root
        )

        result = _rollup_mod._machine_local_impl()

        assert result == str(expected_impl)

    def test_falls_back_to_claude_home_when_settings_home_impl_absent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
        settings_home_root = tmp_path / "settings_home_missing"
        monkeypatch.setattr(
            _rollup_mod, "settings_home", lambda: settings_home_root
        )
        claude_home_root = tmp_path / "dummy_claude_home"
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_root))

        result = _rollup_mod._machine_local_impl()

        assert result == str(claude_home_root / "bin" / "_machine_local.py")


# ---------------------------------------------------------------------------
# _scan_artifacts_by_deliverable_id — blocked scan root vs genuinely-empty deliverable
#
# A blocked scan root must never roll up to "deliverable has no artifacts" — that is
# indistinguishable from the genuinely-empty case unless the scan itself signals
# partial/failed coverage. Mirrors roadmap_dag.py's scan_incomplete idiom.
#
# scan_incomplete is on the emitted payload as of DoE's be8b5d88 reader-widen
# (coordinator_core/contract/deliverable-rollup-producer-contract.md § 5.2
# reader-widen-before-writer-flips protocol — DoE's render layer now reads the
# field and appends " (partial scan)" per rendered line when it is set). These
# tests assert the internal signal + the logged WARNING, and separately pin that
# the wire shape carries the field through to the handler payload.
# ---------------------------------------------------------------------------


_SKIP_CHMOD_UNRELIABLE = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)


def test_unreadable_flat_scan_root_sets_internal_scan_incomplete_signal(
    rollup_repo: RollupRepo, caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable state/handoffs/ dir (a flat scan root) logs a WARNING and sets
    the internal scan_incomplete signal — a blocked scan root must not silently
    roll up to 'deliverable has no artifacts', which is exactly what glob("*.md")'s
    PermissionError-swallowing selector would otherwise produce. Asserted directly
    against _scan_artifacts_by_deliverable_id — the wire shape is checked separately
    (test_handler_payload_wire_shape_includes_scan_incomplete_true below).

    Exercises the production contract directly (Path.iterdir() raising OSError,
    caught by the try/except around base_dir.iterdir() in the flat-root loop) rather
    than provoking it via chmod 0o000, which is unreliable on Windows/as root. This
    runs on every platform."""
    # A plan under docs/plans/ (unaffected scan root) so the result would otherwise
    # look non-trivially resolved — this is NOT vacuously empty for an unrelated reason.
    rollup_repo.write_plan(
        "2026-07-06-unaffected.md", deliverable_id="dlv-blocked", initiative="init-unaffected"
    )

    handoffs_dir = rollup_repo.root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "2026-07-01-unreachable.md").write_text(
        "---\ndeliverable_id: dlv-blocked\ninitiative: init-blocked\n---\nBody.\n",
        encoding="utf-8",
    )

    original_iterdir = Path.iterdir
    resolved_handoffs_dir = handoffs_dir.resolve()

    def _fake_iterdir(self: Path):
        if self.resolve() == resolved_handoffs_dir:
            raise OSError(13, "Permission denied", str(self))
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _fake_iterdir)

    with caplog.at_level(
        logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"
    ):
        matches, scan_incomplete = _scan_artifacts_by_deliverable_id(
            rollup_repo.root, "dlv-blocked"
        )

    assert scan_incomplete is True, (
        "internal scan_incomplete signal must be True when a flat scan root "
        f"cannot be enumerated — got {scan_incomplete!r}"
    )
    # The unaffected docs/plans/ artifact is still visible — failure is scoped to
    # the blocked subtree, not fatal to the whole scan.
    assert len(matches) == 1

    dir_warnings = [
        r
        for r in caplog.records
        if str(handoffs_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable handoffs dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_unreadable_recursive_scan_root_sets_internal_scan_incomplete_signal(
    rollup_repo: RollupRepo, caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable archive/handoffs/ dir (the recursive scan root) logs a WARNING
    and sets the internal scan_incomplete signal, mirroring the flat-root case above.

    Exercises the production contract directly (os.walk(onerror=...) invoking its
    error callback with an OSError) rather than provoking it via chmod 0o000, which
    is unreliable on Windows/as root. This runs on every platform."""
    archive_dir = rollup_repo.root / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "2026-07-01-unreachable.md").write_text(
        "---\ndeliverable_id: dlv-archive-blocked\ninitiative: init-x\n---\nBody.\n",
        encoding="utf-8",
    )

    original_walk = os.walk
    resolved_archive_dir = archive_dir.resolve()

    def _fake_walk(top, onerror=None, **kwargs):
        top_path = Path(top)
        if top_path.resolve() == resolved_archive_dir:
            if onerror is not None:
                onerror(OSError(13, "Permission denied", str(top_path)))
            return iter([])
        return original_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(_rollup_mod.os, "walk", _fake_walk)

    with caplog.at_level(
        logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"
    ):
        matches, scan_incomplete = _scan_artifacts_by_deliverable_id(
            rollup_repo.root, "dlv-archive-blocked"
        )

    assert scan_incomplete is True, (
        "internal scan_incomplete signal must be True when the recursive "
        f"archive/handoffs/ scan root cannot be enumerated — got {scan_incomplete!r}"
    )
    assert matches == []

    dir_warnings = [
        r
        for r in caplog.records
        if str(archive_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable archive dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_unreadable_archive_specs_scan_root_sets_internal_scan_incomplete_signal(
    rollup_repo: RollupRepo, caplog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5 (C1): an unreadable archive/specs/ dir (the fourth, recursive scan
    root) logs a WARNING and sets the internal scan_incomplete signal,
    mirroring the archive/handoffs case above — a blocked archive/specs
    subtree must never silently roll up to 'deliverable has no artifacts'.

    Exercises the production contract directly (os.walk(onerror=...) invoking its
    error callback with an OSError) rather than provoking it via chmod 0o000, which
    is unreliable on Windows/as root. This runs on every platform. Together with
    the archive/handoffs case above, this pins that EITHER of the two recursive
    roots — walked in the same per-root loop, each re-initialising walk_errors and
    setting scan_incomplete independently — propagates the blocked signal."""
    specs_dir = rollup_repo.root / "archive" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    (specs_dir / "2026-08-01-unreachable.md").write_text(
        "---\ndeliverable_id: dlv-specs-blocked\ninitiative: init-x\n---\nBody.\n",
        encoding="utf-8",
    )

    original_walk = os.walk
    resolved_specs_dir = specs_dir.resolve()

    def _fake_walk(top, onerror=None, **kwargs):
        top_path = Path(top)
        if top_path.resolve() == resolved_specs_dir:
            if onerror is not None:
                onerror(OSError(13, "Permission denied", str(top_path)))
            return iter([])
        return original_walk(top, onerror=onerror, **kwargs)

    monkeypatch.setattr(_rollup_mod.os, "walk", _fake_walk)

    with caplog.at_level(
        logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"
    ):
        matches, scan_incomplete = _scan_artifacts_by_deliverable_id(
            rollup_repo.root, "dlv-specs-blocked"
        )

    assert scan_incomplete is True, (
        "internal scan_incomplete signal must be True when the recursive "
        f"archive/specs/ scan root cannot be enumerated — got {scan_incomplete!r}"
    )
    assert matches == []

    dir_warnings = [
        r
        for r in caplog.records
        if str(specs_dir) in r.message and r.levelno == logging.WARNING
    ]
    assert dir_warnings, (
        "expected a logged WARNING naming the unreadable archive/specs dir; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_scan_incomplete_false_on_clean_scan(rollup_repo: RollupRepo) -> None:
    """The common case: a fully-readable tree yields the internal
    scan_incomplete=False signal."""
    rollup_repo.write_plan(
        "2026-07-06-clean.md", deliverable_id="dlv-clean", initiative="init-clean"
    )

    matches, scan_incomplete = _scan_artifacts_by_deliverable_id(rollup_repo.root, "dlv-clean")

    assert scan_incomplete is False
    assert len(matches) == 1


@_SKIP_CHMOD_UNRELIABLE
def test_handler_payload_wire_shape_includes_scan_incomplete_true(
    rollup_repo: RollupRepo, caplog
) -> None:
    """Contract compliance pin: when a scan root is blocked and the internal
    scan_incomplete signal is True, the emitted payload carries
    'scan_incomplete': True — on the wire as of DoE's be8b5d88 reader-widen
    (contract § 5.2)."""
    handoffs_dir = rollup_repo.root / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "2026-07-01-unreachable.md").write_text(
        "---\ndeliverable_id: dlv-wire-shape\ninitiative: init-x\n---\nBody.\n",
        encoding="utf-8",
    )

    original_mode = handoffs_dir.stat().st_mode
    os.chmod(handoffs_dir, 0o000)
    try:
        with caplog.at_level(
            logging.WARNING, logger="coordinator_core.ops.deliverable_rollup"
        ):
            result = _handler(
                {"deliverable_id": "dlv-wire-shape"}, repo_root=rollup_repo.common_dir
            )
    finally:
        os.chmod(handoffs_dir, original_mode)

    assert result["scan_incomplete"] is True, (
        "the wire payload must carry scan_incomplete=True when a scan root was "
        f"blocked — got {result.get('scan_incomplete')!r}"
    )
    _assert_schema(result, "dlv-wire-shape")

    # The deliverable_id-keyed WARNING logged by the handler itself (in addition to
    # the per-scan-root WARNING) is still observable operationally, alongside the
    # wire signal.
    id_warnings = [
        r
        for r in caplog.records
        if "dlv-wire-shape" in r.message and r.levelno == logging.WARNING
    ]
    assert id_warnings, (
        "expected a logged WARNING naming the affected deliverable_id; "
        f"none found in: {[r.message for r in caplog.records]}"
    )


def test_handler_payload_wire_shape_includes_scan_incomplete_false(
    rollup_repo: RollupRepo,
) -> None:
    """Companion to the True case: a fully-readable tree yields
    'scan_incomplete': False on the wire — pins that the flip is meaningful in
    both directions, not just the incomplete-scan branch."""
    rollup_repo.write_plan(
        "2026-07-06-clean-wire.md", deliverable_id="dlv-clean-wire", initiative="init-clean-wire"
    )
    rollup_repo.write_initiative("init-clean-wire", label="Clean Wire Initiative", status="active")

    result = _handler({"deliverable_id": "dlv-clean-wire"}, repo_root=rollup_repo.common_dir)

    assert result["scan_incomplete"] is False, (
        f"expected scan_incomplete=False for a fully-readable scan — got {result.get('scan_incomplete')!r}"
    )
    _assert_schema(result, "dlv-clean-wire")


# ---------------------------------------------------------------------------
# (vii) no fork-equivalence join — F-1 collapse: raw ids never merge
# ---------------------------------------------------------------------------


def test_fork_equivalence_absent_entry_does_not_silently_merge(rollup_repo: RollupRepo) -> None:
    """Two genuinely-unrelated ids must never be merged — there is no
    equivalence-map mechanism left to consult (F-1 collapse)."""
    rollup_repo.write_plan("2026-07-06-a.md", deliverable_id="dlv-alpha", initiative=None)
    rollup_repo.write_plan("2026-07-06-b.md", deliverable_id="dlv-beta", initiative=None)

    result = _handler({"deliverable_id": "dlv-alpha"}, repo_root=rollup_repo.common_dir)

    assert result["artifacts_matched"] == 1

    # Review: coordinatorcode-reviewer-67ffaa7e Finding 1 — the duplicate line above was
    # a copy-paste slip; strengthened to assert both legs independently, matching the
    # rest of the suite's evidence-of-both-directions style.
    beta_result = _handler({"deliverable_id": "dlv-beta"}, repo_root=rollup_repo.common_dir)
    assert beta_result["artifacts_matched"] == 1


# ---------------------------------------------------------------------------
# C10b (docs/plans/2026-08-13-spec-backlinks-cite-a-stable-deliverable-id.md):
# plan_id match arm + the shared resolvable-root surface with
# spec_backlink_resolve.
# ---------------------------------------------------------------------------


def test_scan_plan_id_match_arm(rollup_repo: RollupRepo) -> None:
    """The scanner's plan_id match arm (leg (c)): a query id that itself
    carries the `pln-` shape resolves against an artifact's own `plan_id`
    frontmatter field, not just `deliverable_id`."""
    path = rollup_repo.root / "docs" / "plans" / "2026-08-13-a.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nplan_id: pln-widget-abc123\ntitle: \"A\"\ncreated: 2026-08-13\n"
        "status: draft\ninitiative: null\n---\n\n# A\n",
        encoding="utf-8",
    )

    matches, scan_incomplete = _scan_artifacts_by_deliverable_id(
        rollup_repo.root, "pln-widget-abc123"
    )

    assert scan_incomplete is False
    assert len(matches) == 1
    assert matches[0]["plan_id"] == "pln-widget-abc123"


def test_dlv_query_artifacts_matched_invariant_under_plan_id_carrying_records(
    rollup_repo: RollupRepo,
) -> None:
    """Invariance proof for the C10b ungated ruling: a `deliverable.rollup`
    query for a `dlv-` id is UNCHANGED by the presence of `plan_id`-carrying
    records in the corpus, because the `pln-`/`dlv-` mint prefixes are
    disjoint by construction. This is the evidence leg (c)'s plan_id match
    arm is value-disjoint from every DoE-reachable input (a `dlv-` query),
    not merely an assertion."""
    rollup_repo.write_plan(
        "2026-08-13-dlv.md", deliverable_id="dlv-widget-xyz789", initiative=None
    )
    baseline = _handler(
        {"deliverable_id": "dlv-widget-xyz789"}, repo_root=rollup_repo.common_dir
    )
    assert baseline["artifacts_matched"] == 1

    # Add plan_id-carrying records (none of which carry this deliverable_id).
    plan_id_path = rollup_repo.root / "docs" / "plans" / "2026-08-13-pln.md"
    plan_id_path.write_text(
        "---\nplan_id: pln-widget-xyz789\ntitle: \"Pln\"\ncreated: 2026-08-13\n"
        "status: draft\ninitiative: null\n---\n\n# Pln\n",
        encoding="utf-8",
    )
    rollup_repo.write_handoff(
        "2026-08-13-h-pln.md", deliverable_id=None, initiative=None
    )
    handoff_path = rollup_repo.root / "state" / "handoffs" / "2026-08-13-h-pln.md"
    handoff_path.write_text(
        "---\nplan_id: pln-another-widget-def456\ntitle: \"H\"\ncreated: 2026-08-13\n"
        "branch: work/test/2026-08-13\nstatus: open\ninitiative: null\n---\n\n# H\n",
        encoding="utf-8",
    )

    after = _handler(
        {"deliverable_id": "dlv-widget-xyz789"}, repo_root=rollup_repo.common_dir
    )

    assert after["artifacts_matched"] == baseline["artifacts_matched"] == 1


def test_resolvable_root_predicate_shared_constant_shape() -> None:
    """`RESOLVABLE_ARTIFACT_ROOTS`/`SIZINGS_ONLY_ROOT` are the single source
    of truth both `deliverable_rollup._scan_artifacts_by_deliverable_id` and
    `spec_backlink_resolve.build_index` import — pins the shape so neither
    caller can silently re-fork a hard-coded root list.

    C10 leg (a) cleared (cross-repo/inbox/2026-08-13-doe-claude-em-spec-
    backlink-id-form-ruled-and-rollup-cleared.md): `SIZINGS_ONLY_ROOT` is now
    folded directly into `RESOLVABLE_ARTIFACT_ROOTS` (five entries), not a
    separate root the rollup scanner omits."""
    assert _rollup_mod.SIZINGS_ONLY_ROOT == (("state", "sizings"), "flat")
    assert _rollup_mod.RESOLVABLE_ARTIFACT_ROOTS == (
        (("docs", "plans"), "flat"),
        (("state", "handoffs"), "flat"),
        (("archive", "handoffs"), "recursive"),
        (("archive", "specs"), "recursive"),
        _rollup_mod.SIZINGS_ONLY_ROOT,
    )

    import coordinator_core.ops.spec_backlink_resolve as _resolver_mod

    assert _resolver_mod.RESOLVABLE_ARTIFACT_ROOTS is _rollup_mod.RESOLVABLE_ARTIFACT_ROOTS
    assert _resolver_mod.SIZINGS_ONLY_ROOT is _rollup_mod.SIZINGS_ONLY_ROOT


def test_ac10_resolvable_root_sets_are_equal() -> None:
    """AC10: the two callers' resolvable-root SETS are equal, with no duplicate root.

    Was `test_ac10_resolvable_root_sets_are_equal_pending_gate`, red BY
    DESIGN pending leg (a) of C10. The gate cleared
    (cross-repo/inbox/2026-08-13-doe-claude-em-spec-backlink-id-form-ruled-
    and-rollup-cleared.md): the reader (`coordinator_render_rollup.py`) is
    count-agnostic over `artifacts_matched` and claude-klabauter-resident — no DoE-side
    reader change was required. `SIZINGS_ONLY_ROOT` is now folded into
    `RESOLVABLE_ARTIFACT_ROOTS` as its own entry — both callers import the
    SAME tuple, so `SIZINGS_ONLY_ROOT` must appear in it EXACTLY once. A
    naive `frozenset(RESOLVABLE_ARTIFACT_ROOTS + (SIZINGS_ONLY_ROOT,))`
    comparison would silently dedupe a reintroduced double-add of
    `SIZINGS_ONLY_ROOT` via `frozenset` and pass either way — this assertion
    is structured to catch that instead (Review: code-reviewer P2)."""
    import coordinator_core.ops.spec_backlink_resolve as _resolver_mod

    # Both modules must be looking at the literal same tuple object (or an
    # equal one) — not two independently-maintained root lists.
    assert _resolver_mod.RESOLVABLE_ARTIFACT_ROOTS == _rollup_mod.RESOLVABLE_ARTIFACT_ROOTS
    assert _resolver_mod.SIZINGS_ONLY_ROOT == _rollup_mod.SIZINGS_ONLY_ROOT

    # SIZINGS_ONLY_ROOT must occur exactly once in the shared tuple — a
    # duplicate (the double-scan bug) would inflate this count to 2 without
    # `frozenset` masking it away.
    assert _rollup_mod.RESOLVABLE_ARTIFACT_ROOTS.count(_rollup_mod.SIZINGS_ONLY_ROOT) == 1

    rollup_roots = frozenset(_rollup_mod.RESOLVABLE_ARTIFACT_ROOTS)
    resolver_roots = frozenset(_resolver_mod.RESOLVABLE_ARTIFACT_ROOTS)

    assert rollup_roots == resolver_roots


# ---------------------------------------------------------------------------
# C10 leg (a) — sizings root evidence (P2 scenario, 61750c0fec61)
# ---------------------------------------------------------------------------


def test_dlv_query_resolves_via_sizing_object_only(rollup_repo: RollupRepo) -> None:
    """P2 scenario (61750c0fec61): a commit staging ONLY a state/sizings/*.yaml
    file resolves via `--deliverable-id` — no docs/plans, no handoff, no
    archive artifact carries the id at all. This is the acceptance evidence
    for the whole C10 fold: the sizings root was previously invisible to
    `deliverable.rollup` even when it was the ONLY artifact carrying the id."""
    rollup_repo.write_sizing(
        "2026-08-13-only-a-sizing.yaml", deliverable_id="dlv-sizing-only-abc123"
    )

    result = _handler(
        {"deliverable_id": "dlv-sizing-only-abc123"}, repo_root=rollup_repo.common_dir
    )

    assert result["artifacts_matched"] == 1
    assert result["scan_incomplete"] is False


def test_scanner_finds_sizing_deliverable_id_directly(rollup_repo: RollupRepo) -> None:
    """A sizing's `deliverable_id` is found by
    `_scan_artifacts_by_deliverable_id` — the matched dict is the parsed
    whole-document YAML (not a frontmatter dict, since sizings have none)."""
    rollup_repo.write_sizing(
        "2026-08-13-sizing-two.yaml", deliverable_id="dlv-sizing-two-def456"
    )

    matches, scan_incomplete = _scan_artifacts_by_deliverable_id(
        rollup_repo.root, "dlv-sizing-two-def456"
    )

    assert scan_incomplete is False
    assert len(matches) == 1
    assert matches[0]["deliverable_id"] == "dlv-sizing-two-def456"
    assert matches[0]["schema"] == "sizing-object"


def test_sizing_bogus_prefix_id_is_still_rejected(rollup_repo: RollupRepo) -> None:
    """A queried id that does not match the sizing's own `deliverable_id`
    (a bogus/unrelated prefix or value) is still rejected — the widened scan
    does not loosen the equality match into a prefix or substring check."""
    rollup_repo.write_sizing(
        "2026-08-13-sizing-three.yaml", deliverable_id="dlv-sizing-three-ghi789"
    )

    result = _handler(
        {"deliverable_id": "bogus-prefix-not-a-real-id"}, repo_root=rollup_repo.common_dir
    )

    assert result["artifacts_matched"] == 0


def test_read_sizing_yaml_malformed_degrades_to_empty(tmp_path: Path) -> None:
    """`_read_sizing_yaml`'s bare `except Exception` (matching the existing
    `_resolve_initiative` convention in this file) degrades malformed YAML
    to `{}` rather than raising. The OSError and PyYAML-ImportError branches
    are already exercised elsewhere; this pins the parse-error branch, which
    this diff's sizing reader introduced without test coverage."""
    path = tmp_path / "malformed.yaml"
    # Unbalanced flow-mapping brace — a YAML scanner/parser error, not merely
    # an OSError or an ImportError.
    path.write_text("deliverable_id: dlv-x\nbad: [unterminated\n", encoding="utf-8")

    result = _rollup_mod._read_sizing_yaml(path)

    assert result == {}


def test_scan_ignores_sizing_file_with_malformed_yaml(rollup_repo: RollupRepo) -> None:
    """A malformed `state/sizings/*.yaml` file is silently skipped by the
    scanner (via `_read_sizing_yaml`'s degrade-to-empty), not raised — a
    sibling well-formed sizing in the same directory still resolves."""
    bad_path = rollup_repo.root / "state" / "sizings" / "2026-08-13-broken.yaml"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text("deliverable_id: dlv-broken\nbad: [unterminated\n", encoding="utf-8")
    rollup_repo.write_sizing(
        "2026-08-13-sizing-ok.yaml", deliverable_id="dlv-sizing-ok-jkl012"
    )

    result = _handler(
        {"deliverable_id": "dlv-sizing-ok-jkl012"}, repo_root=rollup_repo.common_dir
    )

    assert result["artifacts_matched"] == 1
    assert result["scan_incomplete"] is False
