"""Revert-row coverage for the ``commit_closures`` section porter (C3, DR-318 §D4/D8).

Section-scoped (this module's own ``collect()``/``CommitClosure`` shape), not
whole-envelope — kept out of ``test_emit_parity.py`` deliberately: that file is
cross-section (whole-envelope) parity scope, already carries bespoke, named
``commit_closures`` coverage behind its own ``_NO_GOLDEN_ORACLE_SECTIONS`` set, and
folding revert-row coverage in there would create write-overlap with any other chunk
or sibling plan touching whole-envelope parity (review finding G12).

**Ledger-backed since C2 (commit-closure-pipe-carries-rows plan).** ``collect()`` no
longer scans commit messages or git history for the closure/revert facts — C1 stamps
``closes``/``reverts_sha`` at commit time and this porter reads them back off the
commit ledger (see ``coordinator_core/ops/emit/sections/commit_closures.py``'s own
docstring). Fixture helpers below mirror
``test_commit_closures_from_ledger.py``'s pattern — real throwaway git commits (so
the one reachability ``git rev-list origin/main`` spawn resolves against real commit
objects) with the closure/revert facts written directly into the ledger via
``coordinator_core.commit_ledger.store.append_entry``, rather than authoring real
``Closes:``-trailer/``git revert`` messages for ``collect()`` to parse — it never
parses commit messages any more.

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Chunk C3, DR-318 §D4,
§D8, AC9, AC16, AC17; C2 ledger migration per
docs/plans/2026-08-22-the-commit-closure-pipe-carries-rows.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.commit_ledger import store as ledger_store
from coordinator_core.contract.cockpit_schema.entities.commit_closure import CommitClosure
from coordinator_core.ops.emit.sections import commit_closures

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# --------------------------------------------------------------------------- fixture helpers
# Shape reused from test_commit_closures_from_ledger.py's ledger-backed fixture pattern
# (_init_repo / _commit / _mark_origin_main / _append / _closure_test_ctx) — not imported
# cross-test-file (breaks pytest collection isolation, per F2 review note on the prior
# git-scan-era version of this file), re-derived locally instead.


def _run_git_or_raise(repo_root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_closure_test_repo(repo_root: Path) -> None:
    """Init a throwaway git repo with a local identity (no reliance on global git config)."""
    _run_git_or_raise(repo_root, "init", "-q")
    _run_git_or_raise(repo_root, "config", "user.email", "test@example.com")
    _run_git_or_raise(repo_root, "config", "user.name", "Test User")
    _run_git_or_raise(repo_root, "config", "commit.gpgsign", "false")


def _commit_with_message(repo_root: Path, message: str, content: str) -> str:
    """Write unique ``content`` to a tracked file and commit ``message``; return the new SHA."""
    (repo_root / "file.txt").write_text(content)
    _run_git_or_raise(repo_root, "add", "-A")
    _run_git_or_raise(repo_root, "commit", "-q", "-m", message)
    return _run_git_or_raise(repo_root, "rev-parse", "HEAD")


def _mark_origin_main(repo_root: Path, sha: str) -> None:
    _run_git_or_raise(repo_root, "update-ref", "refs/remotes/origin/main", sha)


def _revert_commit(repo_root: Path, sha: str) -> str:
    """Run a real ``git revert``, producing git's own auto-generated body linkage line.

    Kept for ``test_revert_of_untracked_commit_yields_no_revert_row`` (out of this
    dispatch's scope, still passing unmodified) -- it only needs a real revert commit
    object to exist, never a ledger entry, since its expectation (no rows at all) holds
    on an empty ledger regardless of mechanism.
    """
    _run_git_or_raise(repo_root, "revert", "--no-edit", sha)
    return _run_git_or_raise(repo_root, "rev-parse", "HEAD")


def _append(repo_root: Path, handoff_id: str, sha: str, **kwargs) -> None:
    ok = ledger_store.append_entry(handoff_id, sha, "code", cwd=str(repo_root), **kwargs)
    assert ok, f"fixture append_entry failed for sha={sha}"


def _closure_test_ctx(repo_root: Path):
    from coordinator_core.ops.emit.context import EmitContext

    return EmitContext(
        repo_root=repo_root,
        coordinator_root=repo_root,
        central_state_root=repo_root / "state",
        git_branch="main",
        git_sha="a" * 40,
        git_sha_short="aaaaaaaa",
        observed_at="2026-08-18T00:00:00Z",
        hostname="test-host",
        repo_name="test/repo",
    )


# --------------------------------------------------------------------------- AC9: revert row
def test_git_revert_yields_marked_revert_row(tmp_path: Path) -> None:
    """A revert commit whose ledger entry carries ``reverts_sha`` (stamped at write time,
    C1, off git's own auto-generated 'This reverts commit <sha>' body line) yields TWO rows:
    the original close row (``reverts_sha`` null) and a revert row carrying the SAME item_id,
    the revert commit's OWN sha, and ``reverts_sha`` set to the reverted commit's sha
    (AC9, D4/D8)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(tmp_path, "fix: close an item", "content-1\n")
    revert_sha = _commit_with_message(tmp_path, "revert: undo it", "content-1-reverted\n")
    _mark_origin_main(tmp_path, revert_sha)
    _append(tmp_path, "hnd-a", closure_sha, closes=["RECS-42"])
    _append(tmp_path, "hnd-b", revert_sha, reverts_sha=closure_sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 2, f"expected one close row + one revert row, got {records!r}"

    by_sha = {r["sha"]: r for r in records}
    close_row = by_sha[closure_sha]
    revert_row = by_sha[revert_sha]

    assert close_row["item_id"] == "RECS-42"
    assert close_row["reverts_sha"] is None

    assert revert_row["item_id"] == "RECS-42"
    assert revert_row["sha"] == revert_sha, "revert row must carry the REVERT commit's own sha"
    assert revert_row["reverts_sha"] == closure_sha, (
        "reverts_sha must name the reverted (closure) commit, not the revert commit itself"
    )

    # Both rows must validate against the CommitClosure entity (extra="forbid").
    CommitClosure.model_validate(close_row)
    CommitClosure.model_validate(revert_row)


# --------------------------------------------------------------------------- AC16: hand-authored revert
def test_hand_authored_revert_message_yields_no_revert_row(tmp_path: Path) -> None:
    """A hand-authored revert message has no auto-generated 'This reverts commit <sha>' body
    line, so C1's ``extract_closure_facts_from_text`` never stamps ``reverts_sha`` for it at
    write time — its ledger entry carries no ``reverts_sha`` at all. Such a commit therefore
    produces no revert row — fails safe, never an error (D4's measured coverage limit,
    AC16)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(tmp_path, "fix: close an item", "content-2\n")
    hand_revert_sha = _commit_with_message(
        tmp_path,
        "Revert \"fix: close an item\"\n\nManually reversed, no auto linkage line here.\n",
        "content-2-reverted-by-hand\n",
    )
    _mark_origin_main(tmp_path, hand_revert_sha)
    _append(tmp_path, "hnd-a", closure_sha, closes=["RECS-7"])
    # No reverts_sha on hand_revert_sha's entry -- nothing was stamped at write time.
    _append(tmp_path, "hnd-a", hand_revert_sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert len(records) == 1, f"only the original close row should be emitted: {records!r}"
    assert records[0]["sha"] == closure_sha
    assert records[0]["reverts_sha"] is None
    assert not any(r["sha"] == hand_revert_sha for r in records), (
        "the hand-authored 'revert' commit must not produce a row of its own"
    )


# --------------------------------------------------------------------------- AC17: unmatched reverted sha
def test_revert_of_untracked_commit_yields_no_revert_row(tmp_path: Path) -> None:
    """A real ``git revert`` whose reverted sha names no existing closure row (the reverted
    commit carried no Closes: trailer) produces no revert row and no close row — the join
    key matches nothing (AC17)."""
    _init_closure_test_repo(tmp_path)
    plain_sha = _commit_with_message(tmp_path, "chore: unrelated change", "content-3\n")
    revert_sha = _revert_commit(tmp_path, plain_sha)
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", revert_sha)

    ctx = _closure_test_ctx(tmp_path)
    records, malformed = commit_closures.collect(ctx)

    assert malformed == []
    assert records == [], f"reverted sha matches no closure row; expected no rows: {records!r}"


# NOTE test_pair_walk_stride_matches_widened_three_field_format (formerly here, AC5/G13) was
# DELETED (2026-08-23, C2 test-retirement pass): it pinned the git-log NUL-delimited pair-walk
# stride and the ``i += 3`` correction over a fake ``subprocess.run`` stdout -- both artifacts
# of the retired git-log scan mechanism (no ``_LOG_FORMAT``, no pair-walk, no fake-stdout
# parsing exist in collect() any more). The malformed-sha-quarantine property it also touched
# survives and stays covered: test_malformed_sha_shape_is_quarantined_not_emitted in
# test_commit_closures_from_ledger.py.


# --------------------------------------------------------------------------- AC5: single subprocess call
def test_revert_arm_adds_no_second_subprocess_call(tmp_path: Path) -> None:
    """The revert arm is a pure post-processing pass over the already-read ledger entries --
    collect() still performs EXACTLY ONE subprocess call (the reachability ``git rev-list``)
    even when a revert row is produced (amplification-gate invariant,
    test_no_unbatched_per_item_git_spawn.py)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(tmp_path, "fix: close an item", "content-4\n")
    revert_sha = _commit_with_message(tmp_path, "revert: undo it", "content-4-reverted\n")
    _mark_origin_main(tmp_path, revert_sha)
    _append(tmp_path, "hnd-a", closure_sha, closes=["RECS-3"])
    _append(tmp_path, "hnd-b", revert_sha, reverts_sha=closure_sha)

    ctx = _closure_test_ctx(tmp_path)

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.run_git",
        wraps=commit_closures.run_git,
    ) as spy:
        records, _malformed = commit_closures.collect(ctx)

    assert spy.call_count == 1, (
        f"collect() must spawn exactly one subprocess even with a revert row present; "
        f"got {spy.call_args_list!r}"
    )
    assert len(records) == 2


# NOTE test_marker_pre_filter_keeps_the_record_set_and_drops_the_rest (formerly here, G5) was
# DELETED (2026-08-23, C2 test-retirement pass): it called ``commit_closures._extract_closure_
# commits``, which no longer exists -- the ``--grep`` server-side pre-filter it pinned was part
# of the retired git-log scan and has no ledger-backed analogue (a ledger read has no "noise
# commit" to filter: only entries carrying ``closes``/``reverts_sha`` are ever written). No
# durable property survives this one to re-express.
