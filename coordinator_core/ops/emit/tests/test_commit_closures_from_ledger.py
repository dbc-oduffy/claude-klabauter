"""Ledger-sourced coverage for the ``commit_closures`` section porter (C2,
commit-closure-pipe-carries-rows plan).

Section-scoped (this module's own ``collect()``/``CommitClosure`` shape), not
whole-envelope. Fixture shape: a REAL throwaway git repo (for the one reachability
subprocess ``collect()`` still spawns) whose commit-ledger entries are written directly via
``coordinator_core.commit_ledger.store.append_entry`` -- collect() no longer scans commit
message text or git history for the closure/revert facts themselves (C1 stamps those at
write time), so the fixture writes the ALREADY-NORMALIZED ``closes``/``reverts_sha`` fields
straight into the ledger rather than authoring a real ``Closes:``-trailer commit message.

Spec backlink: state/dispatch-briefs/2026-08-22-the-commit-closure-pipe-carries-rows/C2.md,
AC4, AC5, AC6, AC9, AC11, AC16, AC17; DR-318 §D4, §D8.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.contract.cockpit_schema.entities.commit_closure import CommitClosure
from coordinator_core.ops.emit.sections import commit_closures
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# --------------------------------------------------------------------------- fixture helpers


def _run_git_or_raise(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return result.stdout.strip()


def _init_repo(repo_root: Path) -> None:
    _run_git_or_raise(repo_root, "init", "-q")
    _run_git_or_raise(repo_root, "config", "user.email", "test@example.com")
    _run_git_or_raise(repo_root, "config", "user.name", "Test User")
    _run_git_or_raise(repo_root, "config", "commit.gpgsign", "false")


def _commit(repo_root: Path, message: str, content: str) -> str:
    (repo_root / "file.txt").write_text(content)
    _run_git_or_raise(repo_root, "add", "-A")
    _run_git_or_raise(repo_root, "commit", "-q", "-m", message)
    return _run_git_or_raise(repo_root, "rev-parse", "HEAD")


def _mark_origin_main(repo_root: Path, sha: str) -> None:
    _run_git_or_raise(repo_root, "update-ref", "refs/remotes/origin/main", sha)


def _closure_test_ctx(repo_root: Path):
    from coordinator_core.ops.emit.context import EmitContext

    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="main",
        git_sha="a" * 40,
        git_sha_short="aaaaaaaa",
        observed_at="2026-08-22T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


def _append(repo_root: Path, handoff_id: str, sha: str, **kwargs) -> None:
    ok = ledger_store.append_entry(handoff_id, sha, "code", cwd=str(repo_root), **kwargs)
    assert ok, f"fixture append_entry failed for sha={sha}"


# --------------------------------------------------------------------------- close row


def test_close_row_from_ledger_entry(tmp_path: Path) -> None:
    """A ledger entry carrying ``closes`` yields one CLOSE row per item_id, with
    ``reachable_on_default_branch`` resolved True when the sha is an origin/main ancestor."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    _mark_origin_main(tmp_path, sha)
    _append(tmp_path, "hnd-a", sha, closes=["RECS-42"])

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    row = records[0]
    assert row["sha"] == sha
    assert row["item_id"] == "RECS-42"
    assert row["reverts_sha"] is None
    assert row["reachable_on_default_branch"] is True
    CommitClosure.model_validate(row)


def test_multiple_item_ids_yield_multiple_close_rows(tmp_path: Path) -> None:
    """A ledger entry's ``closes`` list with more than one item_id yields one row per
    (sha, item_id) pair -- no cross-item dedup (DECISION-4)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "fix: close two items", "content-1\n")
    _mark_origin_main(tmp_path, sha)
    _append(tmp_path, "hnd-a", sha, closes=["RECS-1", "RECS-2"])

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert {(r["sha"], r["item_id"]) for r in records} == {(sha, "RECS-1"), (sha, "RECS-2")}


# --------------------------------------------------------------------------- revert row (AC9)


def test_revert_row_joins_across_ledger_files(tmp_path: Path) -> None:
    """A revert entry's ``reverts_sha`` joins against ANOTHER entry's ``closes`` list even
    when the two are recorded under different handoff_id ledger files (AC9, D4/D8) --
    the join is over the whole-corpus glob, not a single file."""
    _init_repo(tmp_path)
    closure_sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    revert_sha = _commit(tmp_path, "revert: undo it", "content-2\n")
    _mark_origin_main(tmp_path, revert_sha)
    _append(tmp_path, "hnd-a", closure_sha, closes=["RECS-42"])
    _append(tmp_path, "hnd-b", revert_sha, reverts_sha=closure_sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 2
    by_sha = {r["sha"]: r for r in records}

    close_row = by_sha[closure_sha]
    assert close_row["item_id"] == "RECS-42"
    assert close_row["reverts_sha"] is None

    revert_row = by_sha[revert_sha]
    assert revert_row["item_id"] == "RECS-42"
    assert revert_row["reverts_sha"] == closure_sha

    CommitClosure.model_validate(close_row)
    CommitClosure.model_validate(revert_row)


def test_revert_of_untracked_sha_yields_no_revert_row(tmp_path: Path) -> None:
    """A ``reverts_sha`` naming a sha with no matching ``closes`` row anywhere in the ledger
    produces no revert row (AC17) -- fails safe, never an error."""
    _init_repo(tmp_path)
    revert_sha = _commit(tmp_path, "revert: nothing tracked", "content-1\n")
    _mark_origin_main(tmp_path, revert_sha)
    _append(tmp_path, "hnd-a", revert_sha, reverts_sha="f" * 40)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert records == []


# --------------------------------------------------------------------------- reachability tri-state (AC4)


def test_reachability_false_when_sha_not_on_origin_main(tmp_path: Path) -> None:
    """A close row whose sha is NOT an ancestor of origin/main resolves False, not null."""
    _init_repo(tmp_path)
    main_sha = _commit(tmp_path, "chore: base", "content-1\n")
    _mark_origin_main(tmp_path, main_sha)
    unmerged_sha = _commit(tmp_path, "fix: close an item, not yet merged", "content-2\n")
    _append(tmp_path, "hnd-a", unmerged_sha, closes=["RECS-9"])

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["reachable_on_default_branch"] is False


def test_reachability_null_when_origin_main_unresolvable(tmp_path: Path) -> None:
    """No ``refs/remotes/origin/main`` at all degrades EVERY row to null -- never coerced to
    False (DECISION-1)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    # deliberately no _mark_origin_main call
    _append(tmp_path, "hnd-a", sha, closes=["RECS-9"])

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 1
    assert records[0]["reachable_on_default_branch"] is None


# --------------------------------------------------------------------------- AC6: no history scan


def test_collect_issues_exactly_one_reachability_spawn_and_no_history_scan(tmp_path: Path) -> None:
    """``collect()`` issues exactly ONE subprocess -- the bounded ``rev-list origin/main``
    reachability call -- never a ``git log`` history scan (AC6, pinned by test)."""
    _init_repo(tmp_path)
    sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    _mark_origin_main(tmp_path, sha)
    _append(tmp_path, "hnd-a", sha, closes=["RECS-1"])

    ctx = _closure_test_ctx(tmp_path)

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.run_git",
        wraps=commit_closures.run_git,
    ) as spy:
        records, _malformed = commit_closures.collect(ctx)

    assert spy.call_count == 1, f"expected exactly one git spawn, got {spy.call_args_list!r}"
    (call_args, call_kwargs) = spy.call_args
    assert call_args[0] == ["rev-list", "origin/main"]
    assert "log" not in call_args[0]
    assert len(records) == 1


def test_empty_ledger_returns_empty_lists_not_raising(tmp_path: Path) -> None:
    """No ledger files at all (fresh repo, or one with no ledger-wired commits) returns
    ``([], [])`` -- never raises."""
    _init_repo(tmp_path)
    ctx = _closure_test_ctx(tmp_path)

    records, malformed = commit_closures.collect(ctx)

    assert records == []
    assert malformed == []


# --------------------------------------------------------------------------- AC11: per-repo scoping


def test_every_row_repo_matches_ctx_repo_name(tmp_path: Path) -> None:
    """Every emitted row's ``repo`` equals ``ctx.repo_name`` -- per-repo scoped by
    construction (AC11), asserted rather than assumed."""
    _init_repo(tmp_path)
    closure_sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    revert_sha = _commit(tmp_path, "revert: undo it", "content-2\n")
    _mark_origin_main(tmp_path, revert_sha)
    _append(tmp_path, "hnd-a", closure_sha, closes=["RECS-1"])
    _append(tmp_path, "hnd-b", revert_sha, reverts_sha=closure_sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 2
    assert all(r["repo"] == ctx.repo_name for r in records)


# --------------------------------------------------------------------------- malformed sha shape


def test_malformed_sha_shape_is_quarantined_not_emitted(tmp_path: Path) -> None:
    """A ledger entry whose sha fails the 40-lowercase-hex shape check is quarantined into
    ``malformed`` rather than emitted with a corrupt identity key."""
    _init_repo(tmp_path)
    good_sha = _commit(tmp_path, "fix: close an item", "content-1\n")
    _mark_origin_main(tmp_path, good_sha)
    _append(tmp_path, "hnd-a", good_sha, closes=["RECS-1"])
    _append(tmp_path, "hnd-a", "not-a-real-sha", closes=["RECS-2"])

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert len(records) == 1
    assert records[0]["sha"] == good_sha
    assert malformed == [
        {
            "sha": "not-a-real-sha",
            "reason": "commit-ledger entry failed 40-char lowercase-hex SHA validation",
        }
    ]
