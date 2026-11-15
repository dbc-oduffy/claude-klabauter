"""
coordinator_core.ops.tests.test_handoff_children

Tests for the "handoff.has_live_children" op — focused on the path-containment
guard added to the `candidate` param (op-family path-containment sweep,
2026-07-08). This op has no pre-existing test file; coverage here is scoped to
the containment guard, not a full functional test suite for the op.

Import guard: coordinator_core.ops.handoff_children MUST be imported at module
load time so @register_op("handoff.has_live_children") fires and populates
_REGISTRY.

Coverage:
  (a) registry assertion — op name absent from _REGISTRY after import (killed op)
  (b) traversal-path candidate rejected — exit_code=2 (indeterminate/fail-closed)
  (c) out-of-tree absolute-path candidate rejected — exit_code=2
  (d) in-tree candidate (state/handoffs/) resolves and proceeds past the guard

Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.handoff_children import _handoff_has_live_children
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_OP_NAME = "handoff.has_live_children"
# INVERTED 2026-08-27 (kill ledger K-113). The op was deleted under the 200ms
# sweep; `_handoff_has_live_children` survives UNDECORATED because
# handoff_close_origin_stub._try_close resolves it in-process and needs the
# `children` payload. The tests below
# exercise that compute and stay valuable — what must no longer be true is the
# registration. Asserting the negative keeps this file a guard against the
# decorator being restored rather than a stale import check.
assert _OP_NAME not in _REGISTRY, (
    f"{_OP_NAME!r} is in _REGISTRY — it was killed under the 200ms bar and must "
    "not re-register. Restoring @register_op on handoff_children puts a deleted "
    "op back over the bar."
)


def _run(coro):
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "children-test@claude-klabauter.test")
    _git("config", "user.name", "Children Test")
    _git("config", "commit.gpgsign", "false")
    return repo


def _seed_handoff(repo: Path, subdir: str, name: str) -> Path:
    path = repo / subdir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: \"Test\"\nstatus: open\n---\n\nBody.\n", encoding="utf-8")
    return path


def test_op_not_registered():
    """The killed op stays out of _REGISTRY; the undecorated function remains callable."""
    assert _OP_NAME not in _REGISTRY
    assert callable(_handoff_has_live_children)


def test_candidate_traversal_rejected(tmp_path):
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    secret = repo / "secret.md"
    secret.write_text("---\ntitle: \"Secret\"\n---\n", encoding="utf-8")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": "state/handoffs/../../secret.md"},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] == 2, f"traversal candidate must be rejected; got {result!r}"
    assert "referenced" not in result


def test_candidate_out_of_tree_absolute_rejected(tmp_path):
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text('---\ntitle: "Secret"\n---\n', encoding="utf-8")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(outside)},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] == 2, f"out-of-tree candidate must be rejected; got {result!r}"
    assert "referenced" not in result


def test_candidate_in_state_handoffs_passes_guard(tmp_path):
    repo = _make_git_repo(tmp_path)
    candidate_path = _seed_handoff(repo, "state/handoffs", "candidate.md")
    _seed_handoff(repo, "state/handoffs", "other.md")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(candidate_path)},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] in (0, 1), (
        f"in-tree candidate must pass the containment guard and reach a definite "
        f"verdict, not exit_code=2; got {result!r}"
    )


def test_candidate_in_archive_handoffs_passes_guard(tmp_path):
    """An in-tree candidate under archive/handoffs/ also resolves — this
    COMPUTE_ONLY op's live-set spans both state/handoffs/ and archive/handoffs/."""
    repo = _make_git_repo(tmp_path)
    (repo / "state" / "handoffs").mkdir(parents=True)
    candidate_path = _seed_handoff(repo, "archive/handoffs/2026-01", "old.md")

    with patch(
        "coordinator_core.ops.handoff_children.resolve_live_session_ids",
        return_value=frozenset(),
    ):
        result = _run(
            _handoff_has_live_children(
                {"candidate": str(candidate_path)},
                repo_root=repo / ".git",
            )
        )

    assert result["exit_code"] in (0, 1), (
        f"archive/handoffs/ candidate must pass the containment guard; got {result!r}"
    )


def _write_fm(path: Path, fm: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\nbody\n", encoding="utf-8")
    return path


def test_blocked_by_dependents_many_archived_tri_state_one_pass(tmp_path):
    """Archived candidates classify tri-state in one corpus pass, no spawn,
    and never through the per-file full-parse gate index."""
    from coordinator_core.ops import handoff_children
    from coordinator_core.reconcile import handoff_corpus

    arch = tmp_path / "archive" / "handoffs" / "2026-01"
    held = _write_fm(arch / "held.md", "stub_id: held-1\nstatus: archived\n")
    free = _write_fm(arch / "free.md", "handoff_id: 'free-1'\nstatus: archived\n")
    noid = _write_fm(arch / "noid.md", "status: archived\n")
    _write_fm(
        tmp_path / "state" / "handoffs" / "live.md",
        "stub_id: live-1\nstatus: open\nblocked_by:\n  - held-1\n",
    )

    with patch.object(
        handoff_corpus,
        "_collect_all_handoffs_for_gate_index",
        side_effect=AssertionError("full-parse gate index must not run"),
    ), patch("subprocess.Popen", side_effect=AssertionError("spawned")):
        result = handoff_children.blocked_by_dependents_many(
            [held, free, noid], tmp_path
        )

    assert result[str(held)]["state"] == "dependents"
    assert result[str(free)]["state"] == "none"
    assert result[str(noid)]["state"] == "indeterminate"


def test_blocked_by_dependents_many_unreadable_live_dir_fails_closed(tmp_path):
    from coordinator_core.ops import handoff_children

    cand = _write_fm(
        tmp_path / "archive" / "handoffs" / "2026-01" / "c.md", "stub_id: c-1\n"
    )
    with patch(
        "coordinator_core.ops.fleet._common.collect_live_handoff_paths",
        side_effect=PermissionError("denied"),
    ):
        result = handoff_children.blocked_by_dependents_many([cand], tmp_path)
    assert result[str(cand)]["state"] == "indeterminate"


def test_blocked_by_dependents_many_head_corpus_under_500ms_process_time():
    """Every archived handoff in the HEAD corpus classifies under the 500ms bar."""
    import time

    from coordinator_core.dag import _FRONTMATTER_CACHE
    from coordinator_core.ops.handoff_children import blocked_by_dependents_many

    root = Path(__file__).resolve().parents[3]
    archive = root / "archive" / "handoffs"
    if not archive.is_dir():
        pytest.skip("no archive/handoffs corpus in this checkout")
    paths = sorted(archive.rglob("*.md"))
    _FRONTMATTER_CACHE.clear()
    start = time.process_time()
    result = blocked_by_dependents_many(paths, root)
    elapsed_ms = (time.process_time() - start) * 1000
    assert len(result) == len(paths)
    assert {v["state"] for v in result.values()} <= {"none", "dependents", "indeterminate"}
    assert elapsed_ms < 500, f"{len(paths)} archived handoffs took {elapsed_ms:.0f}ms process time"
