"""AC-7 pin (archive/specs/2026-08/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md
chunk C5): `apply_base.scoped_commit` is already O(1) — exactly 2
`.git/index.lock`-taking git invocations (one `add`, one `commit`) per
call, regardless of how many logical items a caller's own loop is
committing, because `scoped_commit` takes a single `artifact_rel_path`
and is called once per artifact. This file counts the ACTUAL git
invocations through the injected `run_git` seam (never by inspection) so
a later edit that reintroduces a per-item `add`/`commit` loop INTO
`scoped_commit` itself is caught, not merely asserted away in prose.

Spec backlink: archive/specs/2026-08/2026-08-13-commit-seams-inherit-lock-reap-and-retry.md,
chunk C5, AC-7's second half ("The two O(1)-already sites ... are pinned
so a later edit cannot silently reintroduce the per-item shape.").
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core.contract import apply_base


class _CountingRunGit:
    """Records every invocation's verb (`args[0]`) — the acquisition-count
    seam this pin reads, mirroring `test_apply_base.py::_RecordingRunGit`'s
    own fake-git shape (kept as a SEPARATE fixture in this new file rather
    than importing that module's private class, per this chunk's own file
    scope)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.calls.append(list(args))
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")  # "changed"
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="deadbeef" * 5 + "\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args}")

    @property
    def lock_taking_calls(self) -> int:
        return sum(1 for c in self.calls if c[0] in ("add", "commit"))


class TestScopedCommitAcquisitionCountIsPinned:
    def test_single_call_takes_exactly_two_lock_acquisitions(self, tmp_path):
        run_git = _CountingRunGit()
        sha = apply_base.scoped_commit(tmp_path, "state/h1.md", "apply: d1", run_git)

        assert sha == "deadbeef" * 5
        assert run_git.lock_taking_calls == 2
        # Review: code-reviewer (P3) — the loop-shaped variant of this test
        # allocated a fresh `_CountingRunGit()` per iteration, so it never
        # accumulated a count across calls; it was byte-identical coverage
        # to this test run three times and its docstring overclaimed a
        # cross-call pin it did not assert. Deleted rather than made real:
        # `scoped_commit` holds no state of its own between calls (each
        # call takes a fresh `run_git`, `cwd`, and `artifact_rel_path`), so
        # a shared-counter version would not catch any production-reachable
        # pathology beyond what this single-call test already pins.
