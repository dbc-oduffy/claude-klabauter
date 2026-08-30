"""Regression coverage for plan_status_transition's writer-side commit
ownership (2026-08-05 chunk C14).

Purpose: pins that `_stamp_implemented` now COMMITS its own terminal
``status: implemented`` write, scoped to exactly the plan path, immediately
after the flip lands -- the same writer-commits shape
`coordinator_core.ops.ceremony.consumed_handoff_stamp.
post_commit_stamp_and_ship` already proves ("the op never exits with the
stamp left as an unswept dirty working-tree edit"). Also pins the surviving
half of the module's former byte-parity obligation: the shared frontmatter
primitives' emitted bytes and this CLI's stdout lines are unperturbed by the
added commit (2026-08-04 PM ratification retired the repo-wide byte-parity
obligation to the node oracle itself -- see plan_status_transition.py's
module docstring -- but the primitives/stdout half survives and is what
this file actually asserts against real emitted bytes and real stdout, not
a description of them).

Spec backlink: docs/decisions/DR-211-fleet-op-substrate-write-boundary.md D1
(category precedent for writer-side commit ownership -- DR-211 itself
ratifies `fleet.*` specifically, and this op is not covered by it).

Negative-spec:
    - Does NOT build a two-branch "byte-parity forbids committing" gate --
      that premise was retired 2026-08-04 (PM-ratified); a git commit of an
      already-written plan file perturbs neither the primitives' emitted
      bytes nor this CLI's stdout lines, which is exactly what the tests
      below assert directly against real bytes/output.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.plan_status_transition import _PROG, main
from coordinator_core.win_portability import no_console_creationflags

# Pins that `_stamp_implemented` commits its own terminal status flip — the
# entire point of the suite is asserting a REAL commit lands (HEAD SHA moves,
# porcelain status clears), which a mocked git cannot demonstrate since the
# assertion IS the real commit-ownership side effect.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git_env() -> dict:
    return {**os.environ, **_GIT_ENV_KEYS}


def _ensure_git_repo(tmp_path: Path) -> None:
    if (tmp_path / ".git").exists():
        return
    subprocess.run(
        ["git", "init"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )


def _write(tmp_path: Path, name: str, body: str) -> Path:
    _ensure_git_repo(tmp_path)
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def _write_and_commit(tmp_path: Path, name: str, body: str) -> Path:
    # `commit_authored_content` (C2, DR-272) is deliberately built for
    # in-place mutation of an EXISTING reserved-noun file (its own
    # containment guard fails loud if `path` does not yet exist in HEAD --
    # "this entrypoint is built for in-place mutation ... not for creating
    # a new one"). A plan document's real lifecycle (draft -> reviewed ->
    # approved -> ... -> stamp-implemented) always has the plan file already
    # tracked/committed by the time stamp-implemented ever runs -- earlier
    # lifecycle stages commit it. So the realistic fixture for exercising
    # this op's commit path is a plan file already committed once at its
    # PRE-flip status, mirroring production, not a bare uncommitted worktree
    # file in a zero-commit repo.
    p = _write(tmp_path, name, body)
    subprocess.run(["git", "add", "--", name], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    return p


def _head_sha(tmp_path: Path) -> str | None:
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tmp_path), capture_output=True, text=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _porcelain(tmp_path: Path, relpath: str) -> str:
    r = subprocess.run(
        ["git", "status", "--porcelain", "--", relpath],
        cwd=str(tmp_path), capture_output=True, text=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    return r.stdout


def _show_head_blob(tmp_path: Path, relpath: str) -> str:
    r = subprocess.run(
        ["git", "show", f"HEAD:{relpath}"],
        cwd=str(tmp_path), capture_output=True, text=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---------------------------------------------------------------------------
# Commit ownership: a real flip is committed, scoped to the plan path.
# ---------------------------------------------------------------------------


def test_flip_commits_the_plan_path_and_leaves_a_clean_tree(tmp_path, capsys):
    p = _write_and_commit(tmp_path, "p.md", "---\ntitle: T\nstatus: draft\nowner: x\n---\n\nBody.\n")

    before_sha = _head_sha(tmp_path)  # the pre-flip commit -- see _write_and_commit
    assert before_sha is not None

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 0
    after_sha = _head_sha(tmp_path)
    assert after_sha is not None and after_sha != before_sha, "expected a real commit to land after the flip"
    # Working tree is clean on the plan path -- the flip's write is fully
    # captured by the commit, not left as an unswept dirty edit.
    assert _porcelain(tmp_path, "p.md") == ""


def test_committed_blob_is_byte_identical_to_the_working_tree_write(tmp_path, capsys):
    # Pins that the commit captures EXACTLY what the shared frontmatter
    # rebuild primitives emitted to disk -- not a re-derivation, not a
    # re-serialization through some other path.
    p = _write_and_commit(
        tmp_path, "p.md",
        "---\nstatus: executing  # authorized 2026-07-21 (body-sha ffc19fc)\n---\n\nBody.\n",
    )
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0

    on_disk = p.read_text(encoding="utf-8")
    assert on_disk == (
        "---\nstatus: implemented  # authorized 2026-07-21 (body-sha ffc19fc)\n---\n\nBody.\n"
    )
    committed = _show_head_blob(tmp_path, "p.md")
    assert committed == on_disk


def test_no_op_flip_does_not_commit(tmp_path, capsys):
    # A frozen status (idempotent re-run / terminal state) never mutates the
    # file -- locked_rmw skips the write, so there is nothing for this op to
    # commit either. No commit should land.
    p = _write(tmp_path, "p.md", "---\nstatus: implemented\n---\n\nBody.\n")

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 0
    assert _head_sha(tmp_path) is None, "no-op flip must not create a commit"
    out = capsys.readouterr().out
    assert "is terminal/deferred — no-op" in out


def test_second_flip_on_an_already_implemented_plan_is_a_second_no_op(tmp_path, capsys):
    p = _write_and_commit(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    rc1 = main(["stamp-implemented", "--plan", str(p)])
    assert rc1 == 0
    first_sha = _head_sha(tmp_path)
    assert first_sha is not None
    capsys.readouterr()

    rc2 = main(["stamp-implemented", "--plan", str(p)])
    assert rc2 == 0
    assert _head_sha(tmp_path) == first_sha, "idempotent re-run must not add a second commit"


# ---------------------------------------------------------------------------
# Byte-parity survivors: stdout lines + primitives' emitted bytes are
# unperturbed by the added commit (2026-08-04 PM-ratified retirement of the
# BROADER node-oracle byte-parity obligation does NOT touch this half).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["draft", "reviewed", "approved", "executing"])
def test_success_stdout_line_unperturbed_by_the_added_commit(tmp_path, capsys, status):
    p = _write_and_commit(tmp_path, "p.md", f"---\ntitle: T\nstatus: {status}\nowner: x\n---\n\nBody.\n")
    rc = main(["stamp-implemented", "--plan", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    # Exact stdout line the node oracle / pre-commit-ownership port emitted --
    # a real commit now lands as a side effect, but this line's text and
    # position (sole stdout line on the success path) are unchanged.
    assert out == f'{_PROG}: {p} status "{status}" → implemented\n'


def test_second_invocation_resumes_a_stranded_uncommitted_flip(tmp_path, capsys, monkeypatch):
    # code-reviewer Finding 1 (c6169575 review): if the commit after a real
    # flip fails, the write is already on disk -- a naive "did THIS
    # invocation flip it" gate would make every later invocation read the
    # now-frozen `implemented` status and silently no-op forever, never
    # recovering the stranded write. This pins the resume path: the SECOND
    # invocation must notice the plan path is dirty in git and commit it.
    p = _write_and_commit(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    initial_sha = _head_sha(tmp_path)

    import coordinator_core.ops.plan_status_transition as pst
    from coordinator_core.ops.ceremony.git_native import GitResult

    def _fail_after_staging(path, content, msg_file, cwd, **_kw):
        # Simulate a REAL stranded-commit failure shape: staging happened
        # (e.g. a post-stage commit-hook rejection), not a failure that
        # never touches the index at all.
        subprocess.run(
            ["git", "add", "--", path], cwd=str(cwd), capture_output=True,
            env=_git_env(), timeout=15,
            **no_console_creationflags(),
        )
        return GitResult(returncode=1, stdout="", stderr="simulated post-stage commit failure")

    monkeypatch.setattr(pst.git_native, "commit_authored_content", _fail_after_staging)

    rc1 = main(["stamp-implemented", "--plan", str(p)])
    assert rc1 == 1
    assert _head_sha(tmp_path) == initial_sha, "no commit should have landed on the failed first run"
    assert "status: implemented" in p.read_text(encoding="utf-8"), "the flip itself already landed on disk"
    capsys.readouterr()

    monkeypatch.undo()  # restore the real commit_authored_content for the resume attempt

    rc2 = main(["stamp-implemented", "--plan", str(p)])
    assert rc2 == 0, "the resume attempt should succeed once commit_authored_content works again"
    assert _head_sha(tmp_path) is not None and _head_sha(tmp_path) != initial_sha, (
        "the stranded write must get committed on resume"
    )
    assert _porcelain(tmp_path, "p.md") == ""
    out = capsys.readouterr().out
    assert "stranded" in out.lower()
    assert "committed the stranded write now" in out


def test_untracked_frozen_plan_is_still_a_genuine_no_op(tmp_path, capsys):
    # A plan that was NEVER touched by this CLI and simply arrives on disk
    # already at a frozen status (e.g. a fixture) is untracked (`??`), not a
    # stranded write this CLI left behind -- it must still take the genuine
    # terminal no-op branch, never attempt a commit.
    p = _write(tmp_path, "p.md", "---\nstatus: abandoned\n---\n\nBody.\n")

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 0
    assert _head_sha(tmp_path) is None
    out = capsys.readouterr().out
    assert "is terminal/deferred — no-op" in out
    assert "stranded" not in out.lower()


def test_ac2_ac3_commit_call_carries_the_locked_rmw_authored_bytes_not_a_worktree_reread(
    tmp_path, capsys, monkeypatch
):
    # 2026-08-06 (C3, DR-272 Defect 1/AC2/AC3): pins the plumbing that closes
    # the DEFINITELY-MULTI-WRITER vector this chunk exists for. Previously
    # (`commit_scoped`), a foreign edit landing on the plan's worktree path
    # between the write landing and the commit being issued could get
    # silently absorbed into this op's own commit (the AGREE branch reads
    # and stages whatever is currently on the worktree at COMMIT time).
    # `commit_authored_content` closes this by construction -- it commits
    # exactly the `content` argument it is handed, never re-reading `path`.
    # This test proves THIS call site passes the bytes `locked_rmw` itself
    # returned as that argument: the fake commit function below corrupts the
    # worktree file the instant it is invoked (simulating a foreign write
    # racing the commit), and the captured `content` must be unaffected --
    # if this call site instead re-read the worktree to build the commit,
    # the corruption would leak into what was captured.
    p = _write_and_commit(tmp_path, "p.md", "---\nstatus: draft\nowner: x\n---\n\nBody.\n")

    import coordinator_core.ops.plan_status_transition as pst
    from coordinator_core.ops.ceremony.git_native import GitResult

    captured: dict = {}

    def _capture_then_corrupt_worktree(path, content, msg_file, cwd, **kw):
        captured["path"] = path
        captured["content"] = content
        captured["deliverable_id"] = kw.get("deliverable_id")
        (Path(cwd) / path).write_text(
            "---\nstatus: CORRUPTED-BY-FOREIGN-WRITE\n---\n\nForeign.\n", encoding="utf-8"
        )
        return GitResult(returncode=0, stdout="f" * 40, stderr="")

    monkeypatch.setattr(pst.git_native, "commit_authored_content", _capture_then_corrupt_worktree)

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 0
    assert captured["path"] == "p.md"
    assert "status: implemented" in captured["content"]
    assert "owner: x" in captured["content"]
    assert "CORRUPTED-BY-FOREIGN-WRITE" not in captured["content"]


def test_commit_failure_surfaces_loud_and_never_prints_the_success_line(tmp_path, capsys, monkeypatch):
    # A git-commit failure after a real flip must fail loud (exit 1) and must
    # NOT print the success "-> implemented" stdout line -- that line is a
    # promise the write AND its commit both landed; the plan's on-disk
    # frontmatter has already flipped by this point (locked_rmw already
    # returned), so this only pins the CLI's own reporting contract, not a
    # rollback of the write itself.
    # Must use the committed fixture: an untracked plan in a zero-commit repo now
    # short-circuits to the skip-commit branch and never reaches the commit call,
    # so the failure this test pins would go unexercised against a bare _write().
    p = _write_and_commit(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")

    import coordinator_core.ops.plan_status_transition as pst
    from coordinator_core.ops.ceremony.git_native import GitResult

    def _fail(*_a, **_kw):
        return GitResult(returncode=1, stdout="", stderr="simulated commit failure")

    monkeypatch.setattr(pst.git_native, "commit_authored_content", _fail)

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 1
    captured = capsys.readouterr()
    assert "implemented" not in captured.out
    assert "committing it failed" in captured.err
    assert "simulated commit failure" in captured.err
    # The flip itself already landed on disk (write happens before commit).
    assert "status: implemented" in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC10 red-proof: a resume whose on-disk content no longer carries this op's
# own terminal state (status: implemented) must fail loud, never commit.
# ---------------------------------------------------------------------------


def test_ac10_resume_with_non_terminal_on_disk_content_fails_loud(tmp_path, capsys):
    # A plan tracked+committed at "draft", then externally re-written to a
    # DIFFERENT frozen status ("abandoned") and staged -- this is dirty in
    # git (the stranded-write detector's trigger), but "abandoned" is not a
    # state this op's own stamp-implemented verb could ever have authored.
    # The resume path must refuse to launder it as a stranded write of its
    # own and must fail loud rather than commit unvalidated on-disk bytes.
    p = _write(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")
    subprocess.run(["git", "add", "p.md"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15, **no_console_creationflags())
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
        **no_console_creationflags(),
    )
    initial_sha = _head_sha(tmp_path)
    assert initial_sha is not None

    p.write_text("---\nstatus: abandoned\n---\n\nBody.\n", encoding="utf-8")
    subprocess.run(["git", "add", "p.md"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15, **no_console_creationflags())
    # Tracked, staged, dirty relative to HEAD -- NOT a bare '??' entry.

    rc = main(["stamp-implemented", "--plan", str(p)])

    assert rc == 1
    err = capsys.readouterr().err
    assert "resume commit aborted" in err
    assert "status: implemented" not in err or "no longer carries" in err
    assert _head_sha(tmp_path) == initial_sha, "no commit must land on a failed resume validation"


def test_ac5_resumed_stdout_token_distinguishes_from_a_genuine_no_op(tmp_path, capsys, monkeypatch):
    # AC5's channel for this op is an exact, pinned stdout token (not a new
    # exit code -- see Anti-scope) distinguishing a resumed stranded-write
    # commit from a genuine terminal no-op.
    p = _write_and_commit(tmp_path, "p.md", "---\nstatus: draft\n---\n\nBody.\n")

    import coordinator_core.ops.plan_status_transition as pst
    from coordinator_core.ops.ceremony.git_native import GitResult

    def _fail_after_staging(path, content, msg_file, cwd, **_kw):
        subprocess.run(
            ["git", "add", "--", path], cwd=str(cwd), capture_output=True, env=_git_env(), timeout=15,
            **no_console_creationflags(),
        )
        return GitResult(returncode=1, stdout="", stderr="simulated failure")

    monkeypatch.setattr(pst.git_native, "commit_authored_content", _fail_after_staging)
    rc1 = main(["stamp-implemented", "--plan", str(p)])
    assert rc1 == 1
    capsys.readouterr()
    monkeypatch.undo()

    rc2 = main(["stamp-implemented", "--plan", str(p)])
    assert rc2 == 0
    resumed_out = capsys.readouterr().out
    assert "committed the stranded write now" in resumed_out

    # A genuine terminal no-op on a fresh, never-flipped-by-this-CLI plan
    # emits a distinct, non-overlapping token.
    q = _write_and_commit(tmp_path, "q.md", "---\nstatus: implemented\n---\n\nBody.\n")
    rc3 = main(["stamp-implemented", "--plan", str(q)])
    assert rc3 == 0
    noop_out = capsys.readouterr().out
    assert "is terminal/deferred — no-op" in noop_out
    assert "committed the stranded write now" not in noop_out
