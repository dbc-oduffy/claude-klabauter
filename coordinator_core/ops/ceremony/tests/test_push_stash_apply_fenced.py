"""No engine op stashes a shared worktree: the push-ladder merge recovery refuses
a dirty-tree conflict without touching a file, and a ratchet bans stash calls."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import push as push_mod
from coordinator_core.ops.ceremony.tests.test_push_reject_landed_by_peer import (
    _git,
    _init_repo_with_upstream,
)

_CORE = Path(__file__).resolve().parents[3]

#: git stash subcommands that read; every other `stash` use rewrites a tree.
_READ_ONLY_STASH_VERBS = {"list", "show"}

#: A command string spelling a mutating stash (`shell=True` shapes).
_SHELL_STASH = re.compile(r"\s*git\s+(?:-\S+\s+)*stash\s+(?:push|save|pop|apply|create)\b")

#: Named carve-outs: (path relative to coordinator_core, reason).
_STASH_CARVE_OUTS: dict[str, str] = {}


def _commit_file(repo, rel: str, text: str, msg: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(["add", "--", rel], repo)
    _git(["commit", "-q", "-m", msg], repo)


def _diverged_repo(tmp_path, upstream_edits: str):
    repo = _init_repo_with_upstream(tmp_path)
    _commit_file(repo, "docs/note.md", "v1", "tracked")
    _git(["push", "-q", "origin", "work/x"], repo)
    other = tmp_path / "other"
    _git(["clone", "-q", str(tmp_path / "origin.git"), str(other)], tmp_path)
    _git(["config", "user.email", "t@t.example"], other)
    _git(["config", "user.name", "t"], other)
    _git(["checkout", "-q", "work/x"], other)
    _commit_file(other, upstream_edits, "upstream", "upstream edit")
    _git(["push", "-q", "origin", "work/x"], other)
    _commit_file(repo, "ours.txt", "unpublished", "ours")
    _git(["fetch", "-q", "origin"], repo)
    return repo


def _snapshot(repo: Path, rels):
    return {r: ((repo / r).read_bytes(), (repo / r).stat().st_mtime_ns) for r in rels}


def _assert_no_stash(repo: Path) -> None:
    assert not (repo / ".git" / "refs" / "stash").exists()
    assert _git(["stash", "list"], repo).strip() == ""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_dirty_tree_refusal_leaves_every_file_and_mtime_untouched(tmp_path):
    repo = _diverged_repo(tmp_path, "docs/note.md")
    dirty = ["docs/note.md", "peer_wip_a.txt", "peer_wip_b.txt"]
    for rel in dirty:
        (repo / rel).write_text("peer wip " + rel, encoding="utf-8")
        os.utime(repo / rel, ns=(10**18, 10**18))
    head_before = _git(["rev-parse", "HEAD"], repo).strip()
    before = _snapshot(repo, dirty)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code != 0
    assert reason.startswith("merge recovery declined, nothing touched: ")
    assert _git(["rev-parse", "HEAD"], repo).strip() == head_before
    assert _snapshot(repo, dirty) == before
    _assert_no_stash(repo)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_clean_merge_over_unrelated_dirty_files_keeps_their_mtimes(tmp_path):
    repo = _diverged_repo(tmp_path, "docs/other.md")
    dirty = ["peer_wip_a.txt", "peer_wip_b.txt"]
    for rel in dirty:
        (repo / rel).write_text("peer wip " + rel, encoding="utf-8")
        os.utime(repo / rel, ns=(10**18, 10**18))
    before = _snapshot(repo, dirty)

    code, reason = push_mod._rebase_onto_fetched_ref(repo, "origin/work/x", "work/x")

    assert code == 0, reason
    assert _snapshot(repo, dirty) == before
    _assert_no_stash(repo)


def _stash_invocations(source: str):
    """Line numbers where a list/tuple literal or a command string spells a mutating `git stash` or `--autostash`."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and node.value == "--autostash":
            hits.append(node.lineno)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SHELL_STASH.match(node.value):
            hits.append(node.lineno)
        if isinstance(node, (ast.List, ast.Tuple)):
            elts = node.elts
            for i, el in enumerate(elts):
                if not (isinstance(el, ast.Constant) and el.value == "stash"):
                    continue
                nxt = elts[i + 1] if i + 1 < len(elts) else None
                verb = nxt.value if isinstance(nxt, ast.Constant) else None
                if verb in _READ_ONLY_STASH_VERBS:
                    continue
                if isinstance(nxt, ast.Constant) and not isinstance(verb, str):
                    continue
                hits.append(el.lineno)
    return hits


def _engine_sources():
    for path in _CORE.rglob("*.py"):
        rel = path.relative_to(_CORE).as_posix()
        name = path.name
        if "/tests/" in "/" + rel or name.startswith("test_") or name == "conftest.py":
            continue
        if rel.startswith(("bash_guards/", "benchmarks/")):
            continue
        yield rel, path


def test_ratchet_detector_sees_the_shapes_it_bans():
    assert _stash_invocations('run(["stash", "create"])')
    assert _stash_invocations('run(["git", "stash"])')
    assert _stash_invocations('run(["pull", "--rebase", "--autostash"])')
    assert not _stash_invocations('run(["stash", "list"])')
    assert _stash_invocations('run("git stash push -u", shell=True)')
    assert not _stash_invocations('x = "git stash"')
    assert not _stash_invocations('run("git stash list", shell=True)')


def test_no_engine_module_stashes_a_worktree_outside_a_named_carve_out():
    offenders = {}
    for rel, path in _engine_sources():
        if rel in _STASH_CARVE_OUTS:
            continue
        try:
            lines = _stash_invocations(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if lines:
            offenders[rel] = lines
    assert not offenders, (
        f"stash/autostash invocation in coordinator_core: {offenders}; "
        "refuse and report instead, or add a named carve-out for an isolated temp index"
    )


def test_every_carve_out_still_has_a_stash_call():
    stale = [
        rel for rel in _STASH_CARVE_OUTS
        if not _stash_invocations((_CORE / rel).read_text(encoding="utf-8"))
    ]
    assert not stale, f"carve-out with no stash call left, delete it: {stale}"
