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
            return SimpleNamespace(returncode=1, stdout="", stderr="")
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
