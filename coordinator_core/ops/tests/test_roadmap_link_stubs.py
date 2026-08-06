"""
coordinator_core.ops.tests.test_roadmap_link_stubs

Tests for the roadmap.link_stubs op (C4,
docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md).

Import guard: coordinator_core.ops.roadmap_link_stubs MUST be imported at
module load time so @register_op("roadmap.link_stubs") fires and populates
_REGISTRY — mirrors coordinator_core/ops/tests/test_handoff_correct_body.py's
own import-guard convention.

Coverage:
  (a) registration — op name present in _REGISTRY.
  (b) AC6 round-trip — after a successful link write, blocked_by/blocks are
      an ARRAY (not a single-quoted string) per the 2026-07-13 lesson
      (state/lessons/2026-07-13-claude-klabauter-frontmatter-write-list-fields-via-
      51feff6964c3.yaml) — asserted via a schema_validate pass on the
      re-parsed frontmatter, not by eyeballing the raw text.
  (c) idempotent re-link — linking the same pair twice is a no-op the second
      time, exit_code 0, applied False.
  (d) refusal on a non-existent endpoint.
  (e) REPAIR of a half-present edge — one direction already written, the
      other missing — writes the missing side rather than skipping.
  (f) write when the blocked_by key is ABSENT entirely from frontmatter
      (exercises _insert_fm_array_field, not _replace_fm_array_field).
  (g) F8 refusals — archived-only endpoint, self-edge, cross-roadmap_id
      endpoints.
  (h) cycle refusal.
  (i) mandatory non-empty reason — absent/empty refused.
  (j) no git commit — HEAD and index unchanged after a successful write
      (DR-212 Invariant 4 / DR-247 § (vi)).

Spec backlink: coordinator_core/ops/roadmap_link_stubs.py
               docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md, chunk C4/C6b
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.roadmap_link_stubs  # noqa: F401 — fires @register_op

from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.roadmap_link_stubs import _SCHEMA_PATH, _handler, _run_link_stubs

_OP_NAME = "roadmap.link_stubs"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.roadmap_link_stubs @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        check=True,
        text=True,
    )


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root.

    Mirrors test_handoff_correct_body.py's own `_make_git_repo` exactly — a
    state/handoffs/ skeleton, committed. Returns repo_root (the main
    worktree root, NOT the .git dir); pass repo_root / ".git" as `repo_root`
    to the handler.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "roadmap-link-stubs-test@claude-klabauter.test")
    _git(repo, "config", "user.name", "Roadmap Link Stubs Test")
    _git(repo, "config", "commit.gpgsign", "false")

    (repo / "state" / "handoffs").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: initial skeleton")

    return repo


def _seed_baton(
    repo: Path,
    name: str,
    *,
    roadmap_id: str,
    stub_id: str,
    wave: int = 1,
    kind: str = "roadmap-baton",
    blocked_by: Optional[list] = None,
    blocks: Optional[list] = None,
    omit_blocked_by: bool = False,
    omit_blocks: bool = False,
    archived: bool = False,
) -> Path:
    """Write a minimal, schema-valid `kind: roadmap-baton` stub.

    `omit_blocked_by`/`omit_blocks` leave the corresponding key OUT of the
    frontmatter entirely (rather than writing an empty list) — exercises
    the _insert_fm_array_field branch in roadmap_link_stubs._write_edge_field
    (present-key path only exercises _replace_fm_array_field).
    """
    if archived:
        path = repo / "archive" / "handoffs" / "2026-07" / name
    else:
        path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        f'title: "Test stub {stub_id}"',
        "created: 2026-08-05",
        "branch: work/roadmap-link-stubs-test",
        "status: open",
        "predecessor: none",
        "category: roadmap",
        f'summary: "Test roadmap-baton stub {stub_id}"',
        f"kind: {kind}",
        f"roadmap_id: {roadmap_id}",
        f"stub_id: {stub_id}",
        f"wave: {wave}",
    ]
    if not omit_blocked_by:
        if blocked_by:
            lines.append("blocked_by:")
            for dep in blocked_by:
                lines.append(f"  - {dep}")
        else:
            lines.append("blocked_by: []")
    if not omit_blocks:
        if blocks:
            lines.append("blocks:")
            for dep in blocks:
                lines.append(f"  - {dep}")
        else:
            lines.append("blocks: []")
    lines.append("---")
    lines.append("")
    lines.append(f"# {stub_id}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _fm_dict(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    fm_text = text.split("---", 2)[1]
    return yaml.safe_load(fm_text) or {}


# ---------------------------------------------------------------------------
# (a) Registration
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# (b) AC6 round-trip — blocked_by/blocks are ARRAYS, not quoted strings
# ---------------------------------------------------------------------------


def test_ac6_written_fields_are_arrays_not_strings(tmp_path, monkeypatch):
    repo = _make_git_repo(tmp_path)
    dependent = _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    dependency = _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "AC6 round-trip check",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    dependent_fm = _fm_dict(dependent)
    dependency_fm = _fm_dict(dependency)

    assert isinstance(dependent_fm["blocked_by"], list), (
        f"blocked_by must parse as a YAML array, got {type(dependent_fm['blocked_by'])}: "
        f"{dependent_fm['blocked_by']!r}"
    )
    assert dependent_fm["blocked_by"] == ["dependency"]
    assert isinstance(dependency_fm["blocks"], list), (
        f"blocks must parse as a YAML array, got {type(dependency_fm['blocks'])}: "
        f"{dependency_fm['blocks']!r}"
    )
    assert dependency_fm["blocks"] == ["dependent"]

    # Full round-trip through schema_validate — this is the 2026-07-13
    # lesson's exact failure mode: a quoted-string array value passes a bare
    # isinstance(list) check on the WRITE side's own in-memory value, but a
    # fresh parse of the on-disk bytes would come back as a string.
    dependent_errors = validate_frontmatter(dependent_fm, _SCHEMA_PATH)
    assert dependent_errors == [], dependent_errors
    dependency_errors = validate_frontmatter(dependency_fm, _SCHEMA_PATH)
    assert dependency_errors == [], dependency_errors


# ---------------------------------------------------------------------------
# (c) Idempotent re-link
# ---------------------------------------------------------------------------


def test_idempotent_relink_is_noop_exit_0(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")

    params = {
        "roadmap_id": "rm1",
        "dependent_stub_id": "dependent",
        "dependency_stub_id": "dependency",
        "reason": "first link",
    }

    first = _run(_handler(dict(params), repo_root=repo / ".git"))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True

    second = _run(_handler(dict(params, reason="second link, same pair"), repo_root=repo / ".git"))
    assert second["exit_code"] == 0, second
    assert second["applied"] is False
    assert second["applied_blocked_by"] is False
    assert second["applied_blocks"] is False


# ---------------------------------------------------------------------------
# (d) Refusal on a non-existent endpoint
# ---------------------------------------------------------------------------


def test_refuses_nonexistent_endpoint(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "does-not-exist",
            "reason": "test",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "does-not-exist" in result["error"]


# ---------------------------------------------------------------------------
# (e) REPAIR of a half-present edge
# ---------------------------------------------------------------------------


def test_repairs_half_present_edge_writes_missing_side(tmp_path):
    repo = _make_git_repo(tmp_path)
    # dependent already carries blocked_by: [dependency] (one direction
    # already written), but dependency does NOT yet carry blocks —
    # simulating a crashed/interrupted prior invocation.
    dependent = _seed_baton(
        repo, "dependent.md", roadmap_id="rm1", stub_id="dependent",
        blocked_by=["dependency"],
    )
    dependency = _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "repair half-present edge",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result["applied_blocked_by"] is False, "dependent side was already present"
    assert result["applied_blocks"] is True, "dependency side was missing and must be repaired"

    assert _fm_dict(dependent)["blocked_by"] == ["dependency"]
    assert _fm_dict(dependency)["blocks"] == ["dependent"]


# ---------------------------------------------------------------------------
# (f) Write when the key is ABSENT entirely (_insert_fm_array_field branch)
# ---------------------------------------------------------------------------


def test_writes_via_insert_when_blocked_by_key_absent(tmp_path):
    repo = _make_git_repo(tmp_path)
    # dependent's frontmatter omits blocked_by entirely (blocks is present,
    # serving as the insertion anchor) — forces _write_edge_field down the
    # _insert_fm_array_field path rather than _replace_fm_array_field.
    dependent = _seed_baton(
        repo, "dependent.md", roadmap_id="rm1", stub_id="dependent",
        omit_blocked_by=True,
    )
    dependency = _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")

    before_text = dependent.read_text(encoding="utf-8")
    assert "blocked_by" not in before_text

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "insert absent key",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    dependent_fm = _fm_dict(dependent)
    assert dependent_fm["blocked_by"] == ["dependency"]
    assert isinstance(dependent_fm["blocked_by"], list)

    errors = validate_frontmatter(dependent_fm, _SCHEMA_PATH)
    assert errors == [], errors
    assert _fm_dict(dependency)["blocks"] == ["dependent"]


# ---------------------------------------------------------------------------
# (g) F8 refusals
# ---------------------------------------------------------------------------


def test_refuses_archived_only_endpoint(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    _seed_baton(
        repo, "archived-dependency.md", roadmap_id="rm1", stub_id="dependency",
        archived=True,
    )

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "test",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "archived" in result["error"].lower()


def test_refuses_self_edge(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "solo.md", roadmap_id="rm1", stub_id="solo")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "solo",
            "dependency_stub_id": "solo",
            "reason": "test",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "self-edge" in result["error"] or "itself" in result["error"]


def test_refuses_cross_roadmap_id_endpoints(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    # dependency exists, but under a DIFFERENT roadmap_id than the one named
    # in the invocation.
    _seed_baton(repo, "dependency.md", roadmap_id="rm2", stub_id="dependency")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "test",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "roadmap_id" in result["error"]


# ---------------------------------------------------------------------------
# (h) Cycle refusal
# ---------------------------------------------------------------------------


def test_refuses_cycle(tmp_path):
    repo = _make_git_repo(tmp_path)
    # a blocked_by b already; linking b blocked_by a would close a cycle.
    a = _seed_baton(repo, "a.md", roadmap_id="rm1", stub_id="a", blocked_by=["b"])
    b = _seed_baton(repo, "b.md", roadmap_id="rm1", stub_id="b", blocks=["a"])
    before_a = a.read_text(encoding="utf-8")
    before_b = b.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "b",
            "dependency_stub_id": "a",
            "reason": "would create a cycle",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "cycle" in result["error"]
    assert a.read_text(encoding="utf-8") == before_a
    assert b.read_text(encoding="utf-8") == before_b


# ---------------------------------------------------------------------------
# (h2) Finding 2 regression — concurrent opposite-direction edges cannot
# both land, i.e. the roadmap_id-scoped lock actually serializes the
# read-check-write sequence rather than only each individual file write.
# ---------------------------------------------------------------------------


def test_concurrent_opposite_direction_edges_cannot_create_a_cycle(tmp_path):
    """Two real threads race to link a<-blocked_by-b and b<-blocked_by-a for
    the same pair, synchronized to start together via a Barrier. Before the
    Finding 2 fix, each thread's per-file locked_rmw calls only ever
    serialized the two INDIVIDUAL writes, not the read-corpus -> cycle-check
    sequence that precedes them — both could observe the pre-race snapshot,
    pass their own cycle check, and both write, landing a real cycle on
    disk. With _run_link_stubs's roadmap_id-scoped lock, whichever thread
    acquires it second must re-read the corpus, see the first thread's
    completed edge, and get refused by the cycle check — so exactly one of
    the two calls succeeds and the other is refused as a cycle, every run.
    """
    repo = _make_git_repo(tmp_path)
    a = _seed_baton(repo, "a.md", roadmap_id="rm1", stub_id="a")
    b = _seed_baton(repo, "b.md", roadmap_id="rm1", stub_id="b")

    barrier = threading.Barrier(2)
    results: dict = {}

    def _call(label: str, dependent: str, dependency: str) -> None:
        barrier.wait()
        results[label] = _run_link_stubs(
            roadmap_id="rm1",
            dependent_stub_id=dependent,
            dependency_stub_id=dependency,
            reason=f"race test {label}",
            repo_root=repo / ".git",
            worktree=repo,
        )

    t1 = threading.Thread(target=_call, args=("a_blocked_by_b", "a", "b"))
    t2 = threading.Thread(target=_call, args=("b_blocked_by_a", "b", "a"))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    assert not t1.is_alive() and not t2.is_alive(), "a thread hung — lock likely deadlocked"
    assert len(results) == 2, results

    exit_codes = sorted(r["exit_code"] for r in results.values())
    assert exit_codes == [0, 1], (
        f"exactly one call must succeed and one must be refused, got: {results}"
    )
    refused = next(r for r in results.values() if r["exit_code"] == 1)
    assert "cycle" in refused["error"], (
        f"the losing call must be refused specifically as a cycle "
        f"(proving it re-read the winner's write), got: {refused}"
    )

    a_fm = _fm_dict(a)
    b_fm = _fm_dict(b)
    a_blocked_by_b = "b" in (a_fm.get("blocked_by") or [])
    b_blocked_by_a = "a" in (b_fm.get("blocked_by") or [])
    # Never both — that IS the cycle the refusal set exists to prevent.
    assert a_blocked_by_b != b_blocked_by_a, (
        f"a reciprocal cycle landed on disk: a.blocked_by={a_fm.get('blocked_by')!r}, "
        f"b.blocked_by={b_fm.get('blocked_by')!r}"
    )


# ---------------------------------------------------------------------------
# (i) Mandatory non-empty reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_reason", [None, "", "   "])
def test_refuses_missing_or_empty_reason(tmp_path, bad_reason):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")

    params = {
        "roadmap_id": "rm1",
        "dependent_stub_id": "dependent",
        "dependency_stub_id": "dependency",
    }
    if bad_reason is not None:
        params["reason"] = bad_reason

    result = _run(_handler(params, repo_root=repo / ".git"))

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "reason" in result["error"]


# ---------------------------------------------------------------------------
# (j) No git commit — HEAD and index unchanged after a successful write
# ---------------------------------------------------------------------------


def test_no_git_commit_head_and_index_unchanged(tmp_path):
    repo = _make_git_repo(tmp_path)
    _seed_baton(repo, "dependent.md", roadmap_id="rm1", stub_id="dependent")
    _seed_baton(repo, "dependency.md", roadmap_id="rm1", stub_id="dependency")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "chore: seed batons")

    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()
    index_before = _git(repo, "diff", "--cached", "--name-only").stdout

    result = _run(_handler(
        {
            "roadmap_id": "rm1",
            "dependent_stub_id": "dependent",
            "dependency_stub_id": "dependency",
            "reason": "assert no self-commit",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    index_after = _git(repo, "diff", "--cached", "--name-only").stdout
    status_porcelain = _git(repo, "status", "--porcelain").stdout

    assert head_after == head_before, "roadmap.link_stubs must never issue a git commit (DR-212 Invariant 4)"
    assert index_after == index_before, "roadmap.link_stubs must never stage changes into the git index"
    # The two written files must appear as UNSTAGED modifications (" M"),
    # never staged ("M ") — confirms the write touched the working tree only.
    assert " M state/handoffs/dependent.md" in status_porcelain
    assert " M state/handoffs/dependency.md" in status_porcelain
