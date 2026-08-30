"""
coordinator_core.ops.ceremony.tests.test_commit_authored_content_edges

The edge matrix for `git_native.commit_authored_content()` (C2's in-process
form-4 committer, `_commit_via_head_spine`) that the zero-spawn spike never
proved: every named row of chunk C5's own dispatch brief (§ Known-unproven,
`docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md`), each carrying
git's own oracle (AC2: `git status --porcelain` empty AND `git fsck --strict`
rc=0) and an explicit assertion of WHICH arm ran -- the in-process fast path
(`_commit_via_head_spine` returns non-`None`) or the ladder fall-back
(returns `None`, the pre-existing `read-tree`/`update-index`/`write-tree`/
`commit-tree`/`update-ref` sequence runs instead). A test that only checks
the commit landed, without pinning the arm, would pass unchanged if a
regression silently routed every case to the ladder -- exactly the failure
mode this file exists to catch (see the dispatch brief's own framing).

Coverage (one section per named edge):
  - unborn branch                              -- loud refusal, no arm runs
  - detached HEAD                               -- in-process
  - ref only in packed-refs (no loose file)     -- ladder
  - linked worktree (`git worktree add`)        -- in-process
  - index v3 / v4 present                       -- in-process (index is
                                                    never read by this
                                                    entrypoint at all, so its
                                                    format cannot force a
                                                    fall-back)
  - split index present                         -- in-process (same reason)
  - packed HEAD commit + packed tree objects
    (`git gc --aggressive`)                     -- in-process (C2's reader)
  - delta-chain-exceeds-depth-guard simulation  -- ladder, never a partial
                                                    tree
  - `filter.*.clean` / `core.autocrlf=true`     -- both in-process, both
                                                    byte-identical to what
                                                    `git add` produces
  - CAS window (AC6): peer moves the ref between this committer's ref read
    and its ref write -- loser refused, no orphaned commit
  - reflog (AC8): survives through either arm
  - tree-object count (AC17): O(path depth), asserted as a positive
    property, never a repo-relative constant

Spec backlink: docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md,
chunk C5; dispatch brief state/dispatch-briefs/2026-08-22-a-commit-is-one-
spawn-not-eleven/C5.md.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.git import git_objects
from coordinator_core.ops.ceremony import git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

# Real-git spawn is load-bearing throughout: every edge here is a property
# of git's own on-disk formats (packfiles, packed-refs, split index, linked
# worktrees) that a mocked git has no way to exhibit.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
        **kwargs,
    )


def _write_msg(tmp_path: Path, text: str = "authored content commit\n") -> Path:
    msg_file = tmp_path / "msg.txt"
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def _porcelain(repo: Path) -> list[str]:
    result = _git(["status", "--porcelain"], repo)
    return [line for line in result.stdout.splitlines() if line]


def _assert_ac2_oracle(repo: Path) -> None:
    """AC2's oracle, asserted verbatim: `git status --porcelain` empty AND
    `git fsck --strict` rc=0 -- every test in this file must call this,
    never merely check the commit's own SHA/content."""
    assert _porcelain(repo) == [], "git status --porcelain is not empty"
    fsck = subprocess.run(
        ["git", "fsck", "--strict"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert fsck.returncode == 0, f"git fsck --strict failed: {fsck.stdout}\n{fsck.stderr}"


def _committed_content_at_head(repo: Path, rel: str, ref: str = "HEAD") -> str:
    result = _git(["show", f"{ref}:{rel}"], repo)
    return result.stdout


def _head_sha(repo: Path, ref: str = "HEAD") -> str:
    return _git(["rev-parse", ref], repo).stdout.strip()


def _sync_worktree(repo: Path, rel: str, content: str) -> None:
    """Writes `content` to `rel` on disk BEFORE the commit call -- mirrors a
    realistic `commit_authored_content` caller (e.g. `locked_rmw`'s own
    writer) that has already synced the file to disk and is committing the
    SAME bytes it just wrote, rather than a caller whose worktree
    deliberately diverges (that shape is `test_commit_scoped.py`'s own
    `test_ac3_new_entrypoint_never_absorbs_foreign_unstaged_worktree_edit`).
    Needed for AC2's oracle: this entrypoint never writes the worktree
    itself (by design -- see its own docstring bound 2), so a caller whose
    on-disk copy does not already match the authored content leaves `git
    status --porcelain` reporting a real, expected `M` for that path."""
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content, encoding="utf-8")


def _run_authored_commit(
    repo: Path,
    path: str,
    content: str,
    msg_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    **kwargs,
):
    """Calls `commit_authored_content` while spying on
    `_commit_via_head_spine` so the caller can assert WHICH arm ran --
    non-`None` means the in-process fast path landed (or failed) the
    commit; `None` means the ladder fall-back ran instead. Returns
    `(result, fast_result)`.
    """
    real_fn = git_native._commit_via_head_spine
    captured: dict = {}

    def _spy(*args, **kw):
        captured["result"] = real_fn(*args, **kw)
        return captured["result"]

    monkeypatch.setattr(git_native, "_commit_via_head_spine", _spy)

    result = git_native.commit_authored_content(path, content, msg_file, repo, **kwargs)
    return result, captured.get("result")


def _assert_in_process(fast_result) -> None:
    assert fast_result is not None, "expected the in-process fast path to run, but the ladder ran"


def _assert_ladder(fast_result) -> None:
    assert fast_result is None, "expected the ladder fall-back to run, but the in-process path ran"


# ---------------------------------------------------------------------------
# unborn branch -- loud refusal, no arm runs at all
# ---------------------------------------------------------------------------


def test_unborn_branch_refuses_loud(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    # No commits at all -- HEAD is unborn.
    msg_file = _write_msg(tmp_path)

    result, fast_result = _run_authored_commit(repo, "file.txt", "content\n", msg_file, monkeypatch)

    assert not result.ok
    assert "no resolvable commit" in result.stderr
    assert fast_result is None  # never reached the fast path's own precondition check
    # An unborn repo still gives a clean status; nothing was written.
    assert _porcelain(repo) == []


# ---------------------------------------------------------------------------
# detached HEAD -- in-process
# ---------------------------------------------------------------------------


def test_detached_head_commits_via_in_process_arm(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    _git(["checkout", "-q", "--detach", "HEAD"], repo)
    old_head = _head_sha(repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    assert _committed_content_at_head(repo, "file.txt") == "NEW\n"
    assert _head_sha(repo) != old_head
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# ref only in packed-refs (no loose file) -- ladder
# ---------------------------------------------------------------------------


def test_ref_only_in_packed_refs_falls_back_to_ladder(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    # Pack the branch ref -- removes the loose refs/heads/<branch> file,
    # leaving only a packed-refs entry. `_resolve_cas_ref_target` refuses
    # to CAS against a non-loose ref (its own docstring), so this must take
    # the ladder.
    _git(["pack-refs", "--all"], repo)
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()
    loose_ref = repo / ".git" / "refs" / "heads" / branch
    assert not loose_ref.exists()
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_ladder(fast_result)
    assert _committed_content_at_head(repo, "file.txt") == "NEW\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# linked worktree -- in-process
# ---------------------------------------------------------------------------


def test_linked_worktree_commits_via_in_process_arm(tmp_path, monkeypatch):
    main_repo = real_git_repo(tmp_path)
    (main_repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], main_repo)
    _git(["commit", "-q", "-m", "baseline"], main_repo)

    linked = tmp_path / "linked-worktree"
    _git(["worktree", "add", "-q", "-b", "linked-branch", str(linked), "HEAD"], main_repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(linked, "file.txt", "FROM LINKED\n")

    result, fast_result = _run_authored_commit(linked, "file.txt", "FROM LINKED\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    assert _committed_content_at_head(linked, "file.txt") == "FROM LINKED\n"
    _assert_ac2_oracle(linked)
    # The main worktree's own checkout is untouched.
    assert _committed_content_at_head(main_repo, "file.txt") == "orig\n"


# ---------------------------------------------------------------------------
# index v3 / v4 / split index present -- in-process, unaffected, since this
# entrypoint never reads the index at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index_version", [3, 4])
def test_index_version_present_does_not_force_ladder(tmp_path, monkeypatch, index_version):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    _git(["update-index", f"--index-version={index_version}"], repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    assert _committed_content_at_head(repo, "file.txt") == "NEW\n"
    _assert_ac2_oracle(repo)


def test_split_index_present_does_not_force_ladder(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    _git(["update-index", "--split-index"], repo)
    assert any((repo / ".git").glob("sharedindex.*"))
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    assert _committed_content_at_head(repo, "file.txt") == "NEW\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# packed HEAD commit + packed tree objects -- in-process, C2's reader
# ---------------------------------------------------------------------------


def test_packed_head_commit_and_tree_objects_take_in_process_path(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    # `git gc` defaults `gc.packRefs` to true, which would ALSO pack the
    # branch ref as a side effect -- that is a different named edge (§ ref
    # only in packed-refs, above) and would make this test's own arm
    # assertion ambiguous about which cause forced the ladder. Disabled so
    # only the commit/tree objects are packed, isolating this edge.
    _git(["config", "gc.packRefs", "false"], repo)
    # Aggressively pack + prune: HEAD's commit object and every tree object
    # along its spine are now pack-only, no loose copies left.
    _git(["gc", "--aggressive", "--prune=now"], repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "a/b/file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "a/b/file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    assert _committed_content_at_head(repo, "a/b/file.txt") == "NEW\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# delta chain deeper than _MAX_DELTA_DEPTH -- ladder fall-back, never a
# partial tree. A genuine >200-deep chain is impractical to construct
# through git's own porcelain (git's pack.depth default caps at 50); this
# simulates the guard tripping by forcing the pack reader to behave exactly
# as it would past the depth ceiling (raise _GitReadModelError) against a
# repo whose loose copies have already been pruned -- the same downstream
# effect (`read_tree_spine`/`head_tree_sha` return None) as a genuine
# depth-exceeded chain, without needing 200 real delta hops.
# ---------------------------------------------------------------------------


def test_delta_chain_exceeding_depth_guard_falls_back_to_ladder_never_partial_tree(
    tmp_path, monkeypatch
):
    repo = real_git_repo(tmp_path)
    (repo / "a" / "b").mkdir(parents=True)
    (repo / "a" / "b" / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    # See the packed-HEAD test above for why `gc.packRefs` is disabled
    # here too -- this edge is about the OBJECT reader, not the ref.
    _git(["config", "gc.packRefs", "false"], repo)
    # Pack + prune loose copies so the pack reader is the ONLY read path for
    # HEAD's existing tree objects -- the loose fallback must not mask the
    # simulated depth-guard trip below.
    _git(["gc", "--aggressive", "--prune=now"], repo)

    def _always_exceeds_depth(*args, **kwargs):
        raise git_objects._GitReadModelError(
            "delta chain exceeds max depth (simulated depth-guard trip)"
        )

    monkeypatch.setattr(git_objects, "_read_pack_object_at", _always_exceeds_depth)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "a/b/file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "a/b/file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_ladder(fast_result)
    # The ladder's `write-tree` rebuilds the FULL tree from a private index
    # seeded off HEAD -- never a partial tree missing untouched siblings.
    assert _committed_content_at_head(repo, "a/b/file.txt") == "NEW\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# AC5 -- filter.*.clean and core.autocrlf=true both take the in-process
# path via `hash-object -w --path=`, and both are byte-identical to what
# `git add` produces. No per-path detection exists (or is tested) -- only
# the property that EVERY worktree-attribute mechanism is honoured because
# `--path=` is always passed, never `--no-filters`.
# ---------------------------------------------------------------------------


def test_filter_clean_driver_is_byte_identical_to_git_add(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / ".gitattributes").write_text("*.txt filter=upper\n", encoding="utf-8")
    _git(["config", "filter.upper.clean", "tr a-z A-Z"], repo)
    (repo / "upper.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    # Independent oracle: what `git add` produces for the SAME bytes,
    # through the SAME filter pattern, via a scratch path that is staged
    # then fully un-staged again -- so this oracle capture never dirties
    # the tree the AC2 check below runs against.
    (repo / "scratch_probe.txt").write_text("world\n", encoding="utf-8")
    _git(["add", "--", "scratch_probe.txt"], repo)
    oracle_sha = _git(["rev-parse", ":scratch_probe.txt"], repo).stdout.strip()
    _git(["rm", "--cached", "-q", "--", "scratch_probe.txt"], repo)
    (repo / "scratch_probe.txt").unlink()
    assert _porcelain(repo) == []

    msg_file = _write_msg(tmp_path)
    # The caller's on-disk copy already carries the RAW (pre-filter) bytes
    # it authored -- exactly like `upper.txt`'s own baseline write above --
    # matching a realistic caller that writes then commits the same bytes.
    _sync_worktree(repo, "upper.txt", "world\n")
    result, fast_result = _run_authored_commit(repo, "upper.txt", "world\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    committed_sha = _git(["rev-parse", "HEAD:upper.txt"], repo).stdout.strip()
    assert committed_sha == oracle_sha
    assert _committed_content_at_head(repo, "upper.txt") == "WORLD\n"
    _assert_ac2_oracle(repo)


def test_core_autocrlf_true_no_attribute_is_byte_identical_to_git_add(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    _git(["config", "core.autocrlf", "true"], repo)
    (repo / "crlf.txt").write_bytes(b"line1\nline2\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)

    scratch = repo / "scratch_probe2.txt"
    scratch.write_bytes(b"line1\r\nline2\r\n")
    _git(["add", "--", "scratch_probe2.txt"], repo)
    oracle_sha = _git(["rev-parse", ":scratch_probe2.txt"], repo).stdout.strip()
    _git(["rm", "--cached", "-q", "--", "scratch_probe2.txt"], repo)
    scratch.unlink()
    assert _porcelain(repo) == []

    msg_file = _write_msg(tmp_path)
    result, fast_result = _run_authored_commit(
        repo, "crlf.txt", "line1\r\nline2\r\n", msg_file, monkeypatch
    )

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    committed_sha = _git(["rev-parse", "HEAD:crlf.txt"], repo).stdout.strip()
    assert committed_sha == oracle_sha
    # autocrlf's clean direction normalizes CRLF -> LF in the object.
    assert _committed_content_at_head(repo, "crlf.txt") == "line1\nline2\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# AC6 -- CAS window: a peer ref move lands BETWEEN this committer's ref
# read and its ref write. The loser is refused with the existing
# compare-and-swap diagnostic; no silent retry; no orphaned commit reachable
# from any ref.
# ---------------------------------------------------------------------------


def test_cas_window_peer_move_between_read_and_write_is_refused(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    msg_file = _write_msg(tmp_path)

    real_cas_ref = git_native.cas_ref

    def _peer_moves_ref_then_cas(*args, **kwargs):
        # Simulates a peer session landing its own commit in the exact
        # window between this committer's HEAD read (already captured as
        # `old_head` by the time `_commit_via_head_spine` reaches this
        # call) and its own ref write below.
        _git(["commit", "-q", "--allow-empty", "-m", "peer landed"], repo)
        return real_cas_ref(*args, **kwargs)

    monkeypatch.setattr(git_native, "cas_ref", _peer_moves_ref_then_cas)

    peer_sha_holder: dict = {}

    result, fast_result = _run_authored_commit(repo, "file.txt", "LOSER\n", msg_file, monkeypatch)
    peer_sha_holder["sha"] = _head_sha(repo)

    assert not result.ok
    assert "compare-and-swap failed" in result.stderr
    _assert_in_process(fast_result)  # reached the fast path; refused inside it
    # The peer's commit is the one that landed -- ours never overwrote it.
    assert _head_sha(repo) == peer_sha_holder["sha"]
    assert _committed_content_at_head(repo, "file.txt") == "orig\n"
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# AC8 -- reflog survives through either arm.
# ---------------------------------------------------------------------------


_REFLOG_LINE_RE = re.compile(
    r"^[0-9a-f]{40} [0-9a-f]{40} .+ <[^>]*> \d+ [+-]\d{4}\t.+$"
)


def _reflog_lines(repo: Path, ref: str = "HEAD") -> list[str]:
    log_path = repo / ".git" / "logs" / ref
    return log_path.read_text(encoding="utf-8").splitlines()


@pytest.mark.designed_red
def test_reflog_survives_in_process_arm(tmp_path, monkeypatch):
    """AC8, literal: "After a commit through either entrypoint, `git
    reflog` shows the new commit and `logs/HEAD` has grown." Measured
    against a REAL checked-out (non-detached) branch, this is currently
    FALSE for the in-process arm: `_commit_via_head_spine` -> `cas_ref`
    (`coordinator_core/git/git_objects.py`) appends the reflog entry to
    `logs/<ref_relpath>` only -- for a normal branch that is
    `logs/refs/heads/<branch>`, never `logs/HEAD` -- because `cas_ref`
    operates on exactly the one physical ref path `_resolve_cas_ref_target`
    hands it and has no notion of the symref HEAD points through. A real
    `git commit`/`git update-ref -m ... HEAD ...` updates BOTH logs (see
    this file's own `test_reflog_survives_ladder_arm`, which passes,
    because the LADDER calls `update-ref` with the literal ref name
    `HEAD` and git resolves the symref/dual-log write itself). Only the
    DETACHED-HEAD case is unaffected (there `ref_relpath` IS `"HEAD"`
    itself). `designed_red`: this test's failure output is the worklist
    for a fix in `_commit_via_head_spine`/`cas_ref`, out of this test-only
    chunk's file scope -- reported to the EM, not fixed here.
    """
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    before = _reflog_lines(repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    after = _reflog_lines(repo)
    assert len(after) == len(before) + 1
    new_line = after[-1]
    assert _REFLOG_LINE_RE.match(new_line), f"reflog line has unexpected shape: {new_line!r}"
    assert new_line.split()[1] == _head_sha(repo)
    _assert_ac2_oracle(repo)


def test_reflog_survives_ladder_arm(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    # Force the ladder the same way the packed-refs test above does.
    _git(["pack-refs", "--all"], repo)
    before = _reflog_lines(repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_ladder(fast_result)
    after = _reflog_lines(repo)
    assert len(after) == len(before) + 1
    new_line = after[-1]
    assert _REFLOG_LINE_RE.match(new_line), f"reflog line has unexpected shape: {new_line!r}"
    assert new_line.split()[1] == _head_sha(repo)
    _assert_ac2_oracle(repo)


def test_reflog_survives_in_process_arm_detached_head(tmp_path, monkeypatch):
    """The detached-HEAD counterpart to the `designed_red` case above --
    here `ref_relpath` IS `"HEAD"` itself (`_resolve_cas_ref_target`'s own
    documented detached-HEAD branch), so `cas_ref` appends directly to
    `logs/HEAD` and this one is NOT affected by the gap; kept GREEN to
    show the defect is specific to a checked-out branch, not to the
    in-process arm as a whole."""
    repo = real_git_repo(tmp_path)
    (repo / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "--", "file.txt"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    _git(["checkout", "-q", "--detach", "HEAD"], repo)
    before = _reflog_lines(repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "file.txt", "NEW\n")

    result, fast_result = _run_authored_commit(repo, "file.txt", "NEW\n", msg_file, monkeypatch)

    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    after = _reflog_lines(repo)
    assert len(after) == len(before) + 1
    new_line = after[-1]
    assert _REFLOG_LINE_RE.match(new_line), f"reflog line has unexpected shape: {new_line!r}"
    assert new_line.split()[1] == _head_sha(repo)
    _assert_ac2_oracle(repo)


# ---------------------------------------------------------------------------
# AC17 -- tree-object count is O(path depth), asserted as a positive
# property (never a repo-relative constant).
# ---------------------------------------------------------------------------


def _loose_object_shas(repo: Path) -> set[str]:
    objects_dir = repo / ".git" / "objects"
    shas: set[str] = set()
    for sub in objects_dir.iterdir():
        if not sub.is_dir() or sub.name in ("pack", "info"):
            continue
        for obj in sub.iterdir():
            shas.add(sub.name + obj.name)
    return shas


def _object_type(repo: Path, sha: str) -> str:
    return _git(["cat-file", "-t", sha], repo).stdout.strip()


def test_tree_object_count_is_path_depth_not_repo_relative(tmp_path, monkeypatch):
    repo = real_git_repo(tmp_path)
    (repo / "a" / "b" / "c").mkdir(parents=True)
    (repo / "a" / "b" / "c" / "file.txt").write_text("orig\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    msg_file = _write_msg(tmp_path)
    _sync_worktree(repo, "a/b/c/file.txt", "NEW\n")

    before = _loose_object_shas(repo)
    result, fast_result = _run_authored_commit(
        repo, "a/b/c/file.txt", "NEW\n", msg_file, monkeypatch
    )
    assert result.ok, result.stderr
    _assert_in_process(fast_result)
    after = _loose_object_shas(repo)

    new_shas = after - before
    new_tree_shas = [sha for sha in new_shas if _object_type(repo, sha) == "tree"]

    # The positive property (AC17): rewriting a leaf at depth N emits
    # exactly one new tree object per ancestor directory INCLUDING the
    # root -- "a/b/c/file.txt" has ancestor dirs "", "a", "a/b", "a/b/c".
    # Computed from the committed path itself, never a repo-relative
    # constant that would rot as this repo's own tree grows.
    committed_path = "a/b/c/file.txt"
    expected_tree_count = committed_path.count("/")  # dir segments, root implicit +1 below
    expected_tree_count += 1  # the root tree itself is always rewritten too
    assert len(new_tree_shas) == expected_tree_count
    _assert_ac2_oracle(repo)
