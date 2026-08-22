"""Revert-row coverage for the ``commit_closures`` section porter (C3, DR-318 §D4/D8).

Section-scoped (this module's own ``collect()``/``CommitClosure`` shape), not
whole-envelope — kept out of ``test_emit_parity.py`` deliberately: that file is
cross-section (whole-envelope) parity scope, already carries bespoke, named
``commit_closures`` coverage behind its own ``_NO_GOLDEN_ORACLE_SECTIONS`` set, and
folding revert-row coverage in there would create write-overlap with any other chunk
or sibling plan touching whole-envelope parity (review finding G12).

Reuses ``test_emit_parity.py``'s fixture-commit helper *shape*
(``_closure_test_ctx`` / ``_init_closure_test_repo`` / ``_commit_with_message``,
including the ``update-ref refs/remotes/origin/main`` trick — without it
``collect()``'s ``git log origin/main HEAD`` does not resolve in a fixture repo at
all) rather than re-deriving a throwaway git repo from scratch.

Spec backlink: docs/plans/2026-08-18-sat-07-tier-a-wiring.md § Chunk C3, DR-318 §D4,
§D8, AC9, AC16, AC17.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinator_core.contract.cockpit_schema.entities.commit_closure import CommitClosure
from coordinator_core.ops.emit.sections import commit_closures

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# --------------------------------------------------------------------------- fixture helpers
# Shape reused from test_emit_parity.py's _init_closure_test_repo / _commit_with_message /
# _closure_test_ctx (G12) — not imported cross-test-file (breaks pytest collection
# isolation, per that module's own F2 review note), re-derived locally instead.


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


def _revert_commit(repo_root: Path, sha: str) -> str:
    """Run a real ``git revert``, producing git's own auto-generated body linkage line."""
    _run_git_or_raise(repo_root, "revert", "--no-edit", sha)
    return _run_git_or_raise(repo_root, "rev-parse", "HEAD")


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
    """A real ``git revert`` of a Closes:-trailer commit yields TWO rows: the original close
    row (``reverts_sha`` null) and a revert row carrying the SAME item_id, the revert
    commit's OWN sha, and ``reverts_sha`` set to the reverted commit's sha (AC9, D4/D8)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-42\n", "content-1\n"
    )
    revert_sha = _revert_commit(tmp_path, closure_sha)
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", revert_sha)

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
    """A commit whose subject looks like a revert but carries NO auto-generated
    'This reverts commit <sha>' body line produces no revert row — fails safe, never an
    error (D4's measured ~50% coverage limit, AC16)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-7\n", "content-2\n"
    )
    hand_revert_sha = _commit_with_message(
        tmp_path,
        f"Revert \"fix: close an item\"\n\nManually reversed, no auto linkage line here.\n",
        "content-2-reverted-by-hand\n",
    )
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", hand_revert_sha)

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


# --------------------------------------------------------------------------- AC5/G13: pair-walk stride
def test_pair_walk_stride_matches_widened_three_field_format(tmp_path: Path) -> None:
    """A malformed-SHA record followed by a well-formed record, in the WIDENED three-field
    (sha, trailers, body) format, must not desynchronize the pair-walk (G13) — the
    malformed-row quarantine's stride correction (``i += 3``) is exercised alongside the
    happy-path stride."""
    _init_closure_test_repo(tmp_path)
    ctx = _closure_test_ctx(tmp_path)

    fake_stdout = (
        "\x00" + "not-a-valid-sha" + "\x00" + "RECS-99\n" + "\x00"
        + "\x00" + ("b" * 40) + "\x00" + "RECS-1\n" + "\x00This reverts commit " + ("c" * 40)
    )
    fake_result = subprocess.CompletedProcess(
        args=["git", "log"], returncode=0, stdout=fake_stdout, stderr=""
    )

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.subprocess.run",
        return_value=fake_result,
    ):
        records, malformed = commit_closures.collect(ctx)

    assert malformed == [
        {
            "sha": "not-a-valid-sha",
            "reason": "git-log record failed 40-char lowercase-hex SHA validation",
        }
    ], f"malformed bucket did not quarantine correctly under the widened stride: {malformed!r}"
    assert len(records) == 1, f"the well-formed record must still be emitted intact: {records!r}"
    assert records[0]["sha"] == "b" * 40
    assert records[0]["item_id"] == "RECS-1"
    assert records[0]["reverts_sha"] is None, (
        "this record's trailer block ('RECS-1\\n') must not be mistaken for its body — the "
        "actual body ('This reverts commit ' + 'c'*40) never resolves to a revert because no "
        "commit in this fixture has sha 'c'*40, so the (sha, trailer_block, body) triple must "
        "not desynchronize under a malformed-row stride correction"
    )


# --------------------------------------------------------------------------- AC5: single subprocess call
def test_revert_arm_adds_no_second_subprocess_call(tmp_path: Path) -> None:
    """The revert arm is a pure post-processing pass over the already-captured commits list —
    collect() still performs EXACTLY ONE subprocess call even when a revert row is produced
    (amplification-gate invariant, test_no_unbatched_per_item_git_spawn.py)."""
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-3\n", "content-4\n"
    )
    revert_sha = _revert_commit(tmp_path, closure_sha)
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", revert_sha)

    ctx = _closure_test_ctx(tmp_path)

    with patch(
        "coordinator_core.ops.emit.sections.commit_closures.subprocess.run",
        wraps=subprocess.run,
    ) as spy:
        records, _malformed = commit_closures.collect(ctx)

    assert spy.call_count == 1, (
        f"collect() must spawn exactly one subprocess even with a revert row present; "
        f"got {spy.call_count} calls: {spy.call_args_list!r}"
    )
    assert len(records) == 2


# --------------------------------------------------------------------------- G5: marker pre-filter
def test_marker_pre_filter_keeps_the_record_set_and_drops_the_rest(tmp_path: Path) -> None:
    """The ``--grep`` disjunction is a pre-filter, not a narrowing: every commit that
    could contribute a row still does, and commits carrying neither marker never reach
    Python at all.

    The unfiltered form read 15.9 MB of ``%B`` bodies for 23,446 commits on this repo to
    produce 3 contributing ones (hitlist § G5). Filtering server-side is safe BY
    CONSTRUCTION — collect() skips any commit with no trailer values and any body
    ``_REVERT_LINE_RE`` does not match — so this pins the construction rather than a
    measurement: a close row, a revert row, and a noise commit that must be filtered out.
    """
    _init_closure_test_repo(tmp_path)
    closure_sha = _commit_with_message(
        tmp_path, "fix: close an item\n\nCloses: RECS-9\n", "content-5\n"
    )
    revert_sha = _revert_commit(tmp_path, closure_sha)
    _commit_with_message(tmp_path, "chore: neither marker here\n", "content-6\n")
    noise_sha = _run_git_or_raise(tmp_path, "rev-parse", "HEAD").strip()
    _run_git_or_raise(tmp_path, "update-ref", "refs/remotes/origin/main", noise_sha)

    ctx = _closure_test_ctx(tmp_path)
    commits, malformed = commit_closures._extract_closure_commits(ctx)

    assert malformed == []
    walked = {sha for sha, _tv, _body in commits}
    assert walked == {closure_sha, revert_sha}, (
        "the pre-filter must admit exactly the two marker-carrying commits"
    )
    assert noise_sha not in walked, "a commit with neither marker must not reach Python"

    records, _ = commit_closures.collect(ctx)
    assert {r["sha"] for r in records} == {closure_sha, revert_sha}
