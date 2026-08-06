"""
coordinator_core.ops.fleet.tests.test_index_residue_reproduction

C0 of docs/plans/2026-08-05-shared-index-residue-rollback-discipline.md — THE
DECLINED BISECT, RUN FOR REAL. This plan's own root-cause narrative was built
from code reads, not executed reproductions, and two of its three prior
mechanism claims were already sent to example-cockpit-repo and RETRACTED as wrong
(see that plan's ## Problem "CORRECTED" blocks). This file replaces reading
with running: three scratch-repo arms, each asked to prove or disprove one
candidate mechanism, with a GREEN (does-not-reproduce) result on any arm
treated as a real, reportable finding rather than a test to keep tuning until
it agrees with the plan.

Arm A (engine, clean-src): session B's `archive_and_commit` runs while
session A already has two of its own paths staged; session A then runs a
BARE `git commit` (no pathspec — the legal operator motion that actually
absorbed cockpit's residue). Tests whether a CLEAN-src archival's main-index
resync leaves session A's bare commit clean.

Arm B (engine, dirty-src): same shape, but `src` carries an uncommitted
worktree edit at archival time and `Move.restage_src` is left at its default
(`False`). Tests the plan's staff-eng-corrected claim that a FULLY
SUCCESSFUL resync (no retry exhaustion) can still stage a foreign
modification into the shared index — this is a resync-success-path defect,
NOT a re-test of the already-fixed 2026-08-01/02 retry-exhaustion mechanism
(_INDEX_RETRY_MAX_ATTEMPTS in _common.py), which is a different bug with a
different residue shape (stale `RD` rename vs. this arm's `M` modification).

Arm C (no-engine control): session B hand-stages a `git mv` directly into
the shared index — NO `archive_and_commit` call at all — then commits it;
session A bare-commits in the window between B's stage and B's commit. This
arm is the DISCRIMINATOR: it isolates "any concurrent hand-staging absorbs
into a bystander's bare commit" from "one of the six engine sites did it",
which the engine arms alone cannot tell apart.

Every arm installs the REAL `coordinator/bin/coordinator-prepare-commit-msg`
hook (not `wsc_tail`) as the scratch repo's `prepare-commit-msg` and asserts
the `Session-Id:` trailer lands on session A's HAND-RUN commit — cockpit
hand-ran `git commit -F -`, and a test routed through `wsc_tail` instead does
not reproduce the incident (see the source plan's C0 task body).

Result recorded in this dispatch's report (see run-report sidecar): Arm A
did NOT reproduce, Arm B DID reproduce, Arm C DID reproduce — a combination
the source plan's disposition table does not name outright (it separately
names "C reproduces, A doesn't" and "B reproduces" as two of four buckets;
here BOTH hold at once), so per that table's fourth bucket this file reports
the actual finding rather than forcing it into one bucket.

Spec backlinks:
  - Source plan: docs/plans/2026-08-05-shared-index-residue-rollback-discipline.md, task C0
  - Mechanism under test: coordinator_core/ops/fleet/_common.py archive_and_commit
    (private-index git mv + post-commit main-index resync via _update_index_with_retry)
  - Stamper under test: coordinator/bin/coordinator-prepare-commit-msg (claude-klabauter-owned,
    NOT a example-doctrine-repo file — see the source plan's ## Problem correction on this point)
  - Sibling reproduction reused for fixture/mock style:
    test_archive_and_commit_index_resync_residue.py (fleet_repo fixture, retry-budget
    mocking house style) — that file's retry-EXHAUSTION mechanism is deliberately NOT
    re-tested here; this file's Arm B is the DIFFERENT, fully-successful-resync mechanism.

Negative-spec:
  - Never exercises any of this against the live working tree — every arm runs inside
    a fresh `tmp_path` scratch repo (see the source plan's own note that sibling
    sessions execute this tree unversioned).
  - Arm B's dirty-src case deliberately does NOT simulate `.git/index.lock`
    contention or monkeypatch retry constants — it must reproduce (or not) via a
    REAL, fully successful resync, not a mocked failure.
  - `Move.restage_src` is left at its documented default (`False`) throughout;
    setting it `True` would close exactly the gap Arm B is testing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
from pathlib import Path

from coordinator_core.ops.fleet._common import Move, archive_and_commit

_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[4]
_HOOK_SRC = _CLAUDE_KLABAUTER_ROOT / "coordinator" / "bin" / "coordinator-prepare-commit-msg"

_SESSION_A_ID = "11111111-2222-3333-4444-555555555555"


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed.

    Mirrors test_archive_and_commit_index_resync_residue.py's identical helper.
    """
    return asyncio.run(coro)


def _git(root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, env=env,
    )


def _init_scratch_repo(tmp_path: Path) -> Path:
    """A bare scratch repo — no fleet artifact skeleton, deliberately minimal:
    this file is testing raw index mechanics, not any fleet archival family's
    directory conventions."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "index-residue-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Index Residue Test")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _install_real_prepare_commit_msg_hook(root: Path) -> None:
    """Install THE REAL `coordinator-prepare-commit-msg` (claude-klabauter-owned,
    coordinator/bin/) as this scratch repo's prepare-commit-msg hook — a
    scratch repo has no coordinator hooks installed by default, and a
    `wsc_tail`-routed test does not reproduce cockpit's incident (cockpit
    hand-ran `git commit -F -`)."""
    dest = root / ".git" / "hooks" / "prepare-commit-msg"
    shutil.copyfile(_HOOK_SRC, dest)
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _hand_run_commit_as_session_a(root: Path, subject: str) -> None:
    """Session A's bare `git commit` — no pathspec, the legal operator motion
    that absorbed cockpit's residue — run with CLAUDE_SESSION_ID set so the
    real installed hook stamps a Session-Id: trailer, exactly as it would for
    a genuine hand-run coordinator session commit."""
    env = dict(os.environ)
    env["CLAUDE_SESSION_ID"] = _SESSION_A_ID
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    _git(root, "-c", "commit.gpgsign=false", "commit", "-m", subject, env=env)


def _commit_name_status(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "show", "--name-status", "--format=", "-M", ref).stdout


def _commit_trailer_body(root: Path, ref: str = "HEAD") -> str:
    return _git(root, "log", "-1", "--format=%B", ref).stdout


# ---------------------------------------------------------------------------
# Arm A — engine, clean-src
# ---------------------------------------------------------------------------


def test_arm_a_engine_clean_src_does_not_reproduce(tmp_path, monkeypatch):
    """Session B runs a CLEAN-src archival through archive_and_commit while
    session A already has two of its own paths staged. Session A then bare-
    commits (no pathspec).

    FINDING: does NOT reproduce. The main-index resync's `--remove src` /
    `--add dst` pair, run against a clean src, leaves the shared index
    holding exactly session A's own staged paths post-resync — session A's
    bare commit picks up nothing foreign. This is a real, reportable outcome,
    not a defect in the test: it directly supports the source plan's
    staff-eng-corrected reading that a clean-src archival's resync success
    path is residue-free (only the DIRTY-src case, Arm B, is not).
    """
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    root = _init_scratch_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(root)

    src = root / "docs" / "plans" / "src.md"
    src.parent.mkdir(parents=True)
    src.write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")

    # Session A's own two paths, staged BEFORE session B's archival runs.
    a1 = root / "a1.txt"
    a2 = root / "a2.txt"
    a1.write_text("a1\n", encoding="utf-8")
    a2.write_text("a2\n", encoding="utf-8")
    _git(root, "add", "a1.txt", "a2.txt")

    dst = root / "archive" / "specs" / "src.md"
    move = Move(src=src, dst=dst, candidate_id="docs/plans/src.md")
    acted, failed = _run(archive_and_commit(root, [move], "session B archival"))
    assert failed == []
    assert len(acted) == 1
    assert acted[0]["archived"] is True
    assert "index_resync_failed" not in acted[0]

    status_before_a_commits = _git(root, "status", "--porcelain").stdout
    assert status_before_a_commits.strip().splitlines() == ["A  a1.txt", "A  a2.txt"]

    _hand_run_commit_as_session_a(root, "session A commit")

    name_status = _commit_name_status(root)
    touched = {line.split("\t", 1)[1] for line in name_status.strip().splitlines() if line.strip()}
    assert touched == {"a1.txt", "a2.txt"}, (
        f"Arm A: expected session A's bare commit to contain exactly its own "
        f"two paths; got {name_status!r}"
    )

    trailers = _commit_trailer_body(root)
    assert f"Session-Id: {_SESSION_A_ID}" in trailers


# ---------------------------------------------------------------------------
# Arm B — engine, dirty-src
# ---------------------------------------------------------------------------


def test_arm_b_engine_dirty_src_stays_unstaged(tmp_path, monkeypatch):
    """Same shape as Arm A, but `src` carries an uncommitted worktree edit at
    archival time and `Move.restage_src` is left at its default (`False`).

    FORMERLY REPRODUCED (see git history of this test, prior name
    `test_arm_b_engine_dirty_src_reproduces`): the plain `git update-index
    --add -- dst` form read dst's WORKTREE blob; `git mv` physically
    relocated the dirty on-disk file to dst without rehashing it into the
    private-index commit (which carries forward HEAD's last-committed
    content per the private index's `read-tree HEAD` seed). The result was a
    FULLY SUCCESSFUL resync (both update-index calls succeeding on the first
    attempt — no retry exhaustion) that still staged dst's dirty content into
    the shared main index as a modification relative to what the archival
    commit actually recorded, which session A's subsequent bare commit then
    absorbed alongside its own two paths.

    FIXED by the `--cacheinfo` resync (C1 of
    docs/plans/2026-08-05-resync-stages-the-committed-blob.md — the EM will
    supply this chunk's landing SHA; not fabricated here): the resync now
    reads back HEAD's RECORDED (mode, sha) for dst via `git ls-tree HEAD --
    dst` and stages exactly that via `update-index --add --cacheinfo`,
    instead of reading dst's on-disk worktree content. A peer's uncommitted
    dirty-src edit therefore now lands as an UNSTAGED worktree modification
    (`git status`: ` M dst`, leading space) rather than a staged one (`M
    dst`), so it is NOT picked up by a subsequent bare `git commit` — the
    same "staged = claimed, unstaged = contestable" invariant the resync's
    docstring in _common.py cites.

    FINDING now: does NOT reproduce. dst's dirty content stays an unstaged
    worktree modification; `git diff --cached -- dst` is empty; session A's
    bare commit (which still has its own a1.txt/a2.txt staged, so it
    succeeds and HEAD legitimately advances) carries ONLY those two paths —
    dst is absent from its name-status, i.e. session B's dirty edit is not
    absorbed. Absorption is checked on HEAD's actual advancement plus the
    commit's recorded name-status, not on a naive string-membership test —
    this file previously shipped a false green on exactly that shortcut for
    the sibling Arm C assertion (a `git show --name-status` rename line
    renders as `R100<TAB>old<TAB>new`, so `split('\t', 1)[1]` silently
    folds the new path onto the old one).

    This is NOT a re-test of the 2026-08-01/02 retry-exhaustion mechanism
    (a different, already-fixed bug with a different residue shape — see
    test_archive_and_commit_index_resync_residue.py); no retry constants are
    monkeypatched here and no update-index call is mocked to fail.
    """
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    root = _init_scratch_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(root)

    src = root / "docs" / "plans" / "src.md"
    src.parent.mkdir(parents=True)
    src.write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")

    # Dirty src: uncommitted worktree edit, NOT staged, present at archival time.
    src.write_text("hello DIRTY EDIT\n", encoding="utf-8")

    a1 = root / "a1.txt"
    a2 = root / "a2.txt"
    a1.write_text("a1\n", encoding="utf-8")
    a2.write_text("a2\n", encoding="utf-8")
    _git(root, "add", "a1.txt", "a2.txt")

    dst = root / "archive" / "specs" / "src.md"
    # restage_src left at its documented default False — the gap this arm tests.
    move = Move(src=src, dst=dst, candidate_id="docs/plans/src.md")
    acted, failed = _run(archive_and_commit(root, [move], "session B archival"))
    assert failed == []
    assert len(acted) == 1
    assert acted[0]["archived"] is True
    # The resync succeeded fully on the first attempt — this is the
    # success-path divergence, not retry exhaustion.
    assert "index_resync_failed" not in acted[0]

    # HEAD's committed content at dst is the STALE (last-committed) blob —
    # the private index's git mv never rehashed src's dirty on-disk content.
    head_dst_content = _git(root, "show", "HEAD:archive/specs/src.md").stdout
    assert head_dst_content == "hello\n"

    # The shared main index now leaves dst's dirty edit UNSTAGED — the
    # `--cacheinfo` resync stages HEAD's recorded (mode, sha) for dst, not
    # dst's on-disk worktree content, so the dirty edit shows as a
    # worktree-only modification (leading space, not index-column `M`).
    status_before_a_commits = _git(root, "status", "--porcelain").stdout.strip().splitlines()
    assert " M archive/specs/src.md" in status_before_a_commits, (
        f"Arm B: expected dst's dirty edit to be UNSTAGED (leading space) "
        f"after the cacheinfo resync, not staged; got {status_before_a_commits!r}"
    )
    assert sorted(status_before_a_commits) == sorted(["A  a1.txt", "A  a2.txt", " M archive/specs/src.md"])

    diff_cached = _git(root, "diff", "--cached", "--", "archive/specs/src.md").stdout
    assert diff_cached == "", (
        f"Arm B: expected an empty --cached diff at dst (nothing staged "
        f"there post-resync); got {diff_cached!r}"
    )

    head_before = _git(root, "rev-parse", "HEAD").stdout.strip()
    _hand_run_commit_as_session_a(root, "session A commit")
    head_after = _git(root, "rev-parse", "HEAD").stdout.strip()

    # HEAD legitimately advances here — session A's OWN a1.txt/a2.txt are
    # still staged, so the bare commit is not a no-op. The assertion that
    # matters is what it carries, not merely that it ran.
    assert head_after != head_before, (
        "Arm B: session A's bare commit should have advanced HEAD via its "
        "own staged a1.txt/a2.txt"
    )

    name_status = _commit_name_status(root)
    touched = {line.split("\t", 1)[1] for line in name_status.strip().splitlines() if line.strip()}
    assert touched == {"a1.txt", "a2.txt"}, (
        f"Arm B: expected session A's bare commit to carry ONLY its own two "
        f"paths — dst's dirty edit must NOT be absorbed; got {name_status!r}"
    )

    # dst's dirty worktree edit is still present on disk, still unstaged,
    # untouched by session A's commit. Uses rstrip("\n") rather than this
    # file's usual .strip() — a bare .strip() on a SINGLE-line, leading-space
    # porcelain status (exactly this case) eats the leading space that
    # distinguishes unstaged " M" from staged "M ".
    status_after_a_commits = _git(root, "status", "--porcelain").stdout.rstrip("\n").splitlines()
    assert status_after_a_commits == [" M archive/specs/src.md"], (
        f"Arm B: dst's dirty edit should remain the sole unstaged residue "
        f"after session A's commit; got {status_after_a_commits!r}"
    )

    trailers = _commit_trailer_body(root)
    assert f"Session-Id: {_SESSION_A_ID}" in trailers


# ---------------------------------------------------------------------------
# Arm C — no-engine-site control
# ---------------------------------------------------------------------------


def test_arm_c_no_engine_control_reproduces(tmp_path, monkeypatch):
    """No `archive_and_commit` call at all: session B hand-stages a `git mv`
    directly into the shared index, then commits it later. Session A
    bare-commits in the window between B's stage and B's commit.

    FINDING: DOES reproduce, and reproduces the EXACT R100-absorbed shape
    cockpit actually hit (an `R100` rename absorbed into a bystander's bare
    commit, HEAD not yet holding the archival at absorption time). Session A
    also has its own two paths staged before B's hand-staged rename lands,
    mirroring Arms A/B, so this arm proves the actual incident shape — a
    bystander's own legitimate commit absorbing foreign content alongside
    its own work — rather than merely that a bare commit commits whatever is
    staged. This is the discriminating result: it proves the absorption
    mechanism is "any concurrent hand-staging into the shared index,
    followed by a bare commit from a bystander session" — a plain
    operator-motion hazard that exists independent of, and is not produced
    by, any of the six engine sites the source plan's C1-C7 currently scope.
    """
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    root = _init_scratch_repo(tmp_path)
    _install_real_prepare_commit_msg_hook(root)

    src = root / "docs" / "plans" / "src.md"
    src.parent.mkdir(parents=True)
    src.write_text("hello\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")

    # Session A's own two paths, staged BEFORE session B's hand-staged rename
    # — mirrors Arms A/B so Arm C also tests "a bystander's own legitimate
    # commit absorbed foreign content alongside its own work", not merely
    # that a bare commit commits whatever is staged.
    a1 = root / "a1.txt"
    a2 = root / "a2.txt"
    a1.write_text("a1\n", encoding="utf-8")
    a2.write_text("a2\n", encoding="utf-8")
    _git(root, "add", "a1.txt", "a2.txt")

    # Session B hand-stages a git mv directly — NO archive_and_commit call,
    # NO GIT_INDEX_FILE isolation — straight into the shared main index.
    dst = root / "archive" / "specs" / "src.md"
    dst.parent.mkdir(parents=True)
    _git(root, "mv", str(src), str(dst))

    status_before_a_commits = _git(root, "status", "--porcelain").stdout.strip().splitlines()
    assert sorted(status_before_a_commits) == sorted(
        ["A  a1.txt", "A  a2.txt", "R  docs/plans/src.md -> archive/specs/src.md"]
    )

    # Session A bare-commits in the window between B's stage and B's own
    # (later) commit — HEAD does not yet contain the archival.
    _hand_run_commit_as_session_a(root, "session A commit")

    name_status = _commit_name_status(root)
    assert "R100\tdocs/plans/src.md\tarchive/specs/src.md" in name_status.strip().splitlines(), (
        f"Arm C: expected session A's bare commit to carry the R100-absorbed "
        f"rename with no engine site involved; got {name_status!r}"
    )
    touched = {line.split("\t", 1)[1] for line in name_status.strip().splitlines() if line.strip()}
    assert touched == {"a1.txt", "a2.txt", "docs/plans/src.md\tarchive/specs/src.md"}, (
        f"Arm C: expected session A's bare commit to contain its own two "
        f"paths ALONGSIDE the absorbed R100 rename; got {name_status!r}"
    )

    trailers = _commit_trailer_body(root)
    assert f"Session-Id: {_SESSION_A_ID}" in trailers
