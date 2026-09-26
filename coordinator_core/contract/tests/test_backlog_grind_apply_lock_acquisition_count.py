from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.backlog_grind_assemble import apply as bga_apply


class _CountingFakeGit:

    def __init__(self) -> None:
        self.log: list[tuple[str, ...]] = []

    def __call__(self, args: list[str], cwd: Path):
        self.log.append(tuple(args))
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="work/x\n", stderr="")
        if args[0] == "add":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:2] == ["diff", "--cached"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "commit":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "checkout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"fakesha{len(self.log)}\n", stderr="")
        raise AssertionError(f"unexpected git invocation: {args!r}")

    @property
    def add_calls(self) -> int:
        return sum(1 for c in self.log if c and c[0] == "add")

    @property
    def commit_calls(self) -> int:
        return sum(1 for c in self.log if c and c[0] == "commit")


def _n_item_payload(n: int) -> list[str]:
    items = [
        {"paths": [f"state/item-{i}.md"], "message": f"item {i}", "verified": True}
        for i in range(n)
    ]
    payload = {"items": items, "branch": "work/x", "expected_branch": "work/x"}
    return [json.dumps(payload)]


class TestCommitPerItemAcquisitionCountNoLongerScalesWithN:
    def test_three_items_take_one_add_and_three_commits(self, tmp_path, monkeypatch):
        fake_git = _CountingFakeGit()
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]

        handler(_n_item_payload(3), tmp_path)

        assert fake_git.add_calls == 1
        assert fake_git.commit_calls == 3

    def test_seven_items_still_take_exactly_one_add(self, tmp_path, monkeypatch):
        fake_git = _CountingFakeGit()
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]

        handler(_n_item_payload(7), tmp_path)

        assert fake_git.add_calls == 1
        assert fake_git.commit_calls == 7

    def test_non_pass_items_are_excluded_from_the_staged_pathspec(self, tmp_path, monkeypatch):
        """A non-PASS item never commits (D-3(c)'s checkout sub-path) and
        must never be part of the combined `add` pathspec either -- staging
        a path this run is about to `git checkout --` (revert) would be a
        no-op at best, but asserting the pre-pass only ever collects
        VERIFIED items' paths is the actual AC-7 contract this test pins."""
        fake_git = _CountingFakeGit()
        monkeypatch.setattr(bga_apply, "_run_git", fake_git)
        handler = bga_apply._CLI_DISPATCH["commit-per-item"]
        payload = {
            "items": [
                {"paths": ["state/ok.md"], "message": "ok", "verified": True},
                {"paths": ["state/bad.md"], "message": "bad", "verified": False},
            ],
            "branch": "work/x",
            "expected_branch": "work/x",
        }

        handler([json.dumps(payload)], tmp_path)

        add_call = next(c for c in fake_git.log if c and c[0] == "add")
        assert "state/ok.md" in add_call
        assert "state/bad.md" not in add_call
        assert fake_git.commit_calls == 1
