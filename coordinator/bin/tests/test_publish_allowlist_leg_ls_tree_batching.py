"""coordinator/bin/tests/test_publish_allowlist_leg_ls_tree_batching.py —
regression test for C34 (docs/plans/2026-08-07-n-plus-one-git-spawn-class-
and-amplification-gate.md): `_publish_relevant_allowlist_leg` used to call
`_git_ls_tree_entry_files` once PER ALLOWLIST ENTRY — an N+1 `git ls-tree`
spawn per row for any target with more than one allowlist entry. It now
batches all entries mapped to the SAME contributing root into a single
`git ls-tree -r --name-only <ref> -- <entry1> <entry2> ...` spawn via
`_git_ls_tree_entries_files`.

This test drives the REAL function against a real git fixture repo (no
mocked git output) and asserts two things a mock could fake past:

1. Correctness — the batched leg returns the SAME per-root relative-path
   union a naive per-entry loop would have produced, including the DR-227
   UNION-with-live-filesystem contract and the § Anti-scope 25 rule that
   an entry matching nothing in git history is kept UNRESOLVED (its
   literal string), never silently dropped.
2. Spawn count — one root with N allowlist entries costs exactly ONE
   `git ls-tree` spawn, not N; two contributing roots cost exactly TWO.

Run: python -m pytest
coordinator/bin/tests/test_publish_allowlist_leg_ls_tree_batching.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_allowlist_ls_tree_batching_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_repo_with_files(root: Path, files: "dict[str, str]") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "publish-ls-tree-batching-test@claude-klabauter.test")
    _git(root, "config", "user.name", "Publish LS Tree Batching Test")
    _git(root, "config", "commit.gpgsign", "false")
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "chore: fixture files")


def _wrap_subprocess_run_counter(monkeypatch):
    """Wraps `subprocess.run` as it's bound inside the `publish` module so
    every real `git ls-tree` invocation this test's code under test issues
    is counted, without faking git's own behavior (no mocked stdout)."""
    calls: "list[list[str]]" = []
    real_run = subprocess.run

    def counting_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and len(cmd) > 3 and cmd[3] == "ls-tree":
            calls.append(cmd)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(publish.subprocess, "run", counting_run)
    return calls


def test_single_root_multi_entry_allowlist_batches_into_one_ls_tree_spawn(
    monkeypatch, tmp_path
):
    root = tmp_path / "src"
    _init_repo_with_files(
        root,
        {
            "docs/a.md": "a",
            "docs/b.md": "b",
            "README.md": "readme",
        },
    )

    calls = _wrap_subprocess_run_counter(monkeypatch)

    target = publish.ResolvedTarget(
        name="row-a",
        mode="mirror",
        source_dir=root,
        dest_dir=tmp_path / "dst",
        allowlist="docs,README.md",
    )

    result = publish._publish_relevant_allowlist_leg(target)

    ls_tree_calls = [c for c in calls if root.name in " ".join(c) or str(root) in " ".join(c)]
    assert len(ls_tree_calls) == 1, f"expected exactly one batched ls-tree spawn, got {ls_tree_calls}"

    assert root in result
    rels = {r.removeprefix(":(literal)") for r in result[root]}
    assert rels == {"docs/a.md", "docs/b.md", "README.md"}


def test_two_contributing_roots_cost_two_ls_tree_spawns(monkeypatch, tmp_path):
    root_a = tmp_path / "src-a"
    root_b = tmp_path / "src-b"
    _init_repo_with_files(root_a, {"one.md": "1", "two.md": "2"})
    _init_repo_with_files(root_b, {"three.md": "3"})

    calls = _wrap_subprocess_run_counter(monkeypatch)

    target = publish.ResolvedTarget(
        name="row-b",
        mode="mirror",
        source_dir=root_a,
        dest_dir=tmp_path / "dst",
        allowlist="one.md,two.md,three.md",
        source_map=f"{root_a}=one.md,two.md;{root_b}=three.md",
    )

    result = publish._publish_relevant_allowlist_leg(target)

    assert len(calls) == 2, f"expected one ls-tree spawn per contributing root, got {len(calls)}: {calls}"

    rels_a = {r.removeprefix(":(literal)") for r in result[root_a]}
    rels_b = {r.removeprefix(":(literal)") for r in result[root_b]}
    assert rels_a == {"one.md", "two.md"}
    assert rels_b == {"three.md"}


def test_entry_absent_from_git_history_stays_unresolved_not_dropped(monkeypatch, tmp_path):
    """§ Anti-scope 25 — an allowlist entry that matches nothing in
    `git ls-tree` (never committed, e.g. worktree-only) must not vanish
    from the leg; the live-filesystem half's unresolved-literal fallback
    keeps it present, and the batched git-history half's silent omission
    of that one entry from its own output must not narrow that away."""
    root = tmp_path / "src"
    _init_repo_with_files(root, {"tracked.md": "t"})

    target = publish.ResolvedTarget(
        name="row-c",
        mode="mirror",
        source_dir=root,
        dest_dir=tmp_path / "dst",
        allowlist="tracked.md,never-existed.md",
    )

    result = publish._publish_relevant_allowlist_leg(target)

    rels = {r.removeprefix(":(literal)") for r in result[root]}
    assert "tracked.md" in rels
    assert "never-existed.md" in rels, (
        "entry absent from both disk and git history must be kept as the "
        "unresolved literal entry, never silently dropped"
    )


def test_batched_ls_tree_entries_files_matches_per_entry_union(tmp_path):
    """Direct unit check on `_git_ls_tree_entries_files` itself: batching
    multiple entries into one spawn returns the exact union a per-entry
    loop over `_git_ls_tree_entry_files` would have produced."""
    root = tmp_path / "src"
    _init_repo_with_files(
        root,
        {
            "pkg/a.py": "a",
            "pkg/sub/b.py": "b",
            "top.py": "top",
        },
    )

    per_entry_union: "set[str]" = set()
    for entry in ("pkg", "top.py", "missing-entry.py"):
        per_entry_union.update(publish._git_ls_tree_entry_files(root, entry))

    batched = publish._git_ls_tree_entries_files(root, ["pkg", "top.py", "missing-entry.py"])

    assert batched == per_entry_union
    assert batched == {"pkg/a.py", "pkg/sub/b.py", "top.py"}


def test_git_ls_tree_entries_files_empty_entries_returns_empty_set(tmp_path):
    root = tmp_path / "src"
    _init_repo_with_files(root, {"a.md": "a"})
    assert publish._git_ls_tree_entries_files(root, []) == set()
