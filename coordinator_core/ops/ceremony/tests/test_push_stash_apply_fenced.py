"""No engine op stashes a shared worktree: the push-ladder merge recovery refuses
a dirty-tree conflict without touching a file, and a ratchet bans stash calls."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest


_CORE = Path(__file__).resolve().parents[3]

#: git stash subcommands that read; every other `stash` use rewrites a tree.
_READ_ONLY_STASH_VERBS = {"list", "show"}

#: A command string spelling a mutating stash (`shell=True` shapes).
_SHELL_STASH = re.compile(r"\s*git\s+(?:-\S+\s+)*stash\s+(?:push|save|pop|apply|create)\b")

#: Named carve-outs: (path relative to coordinator_core, reason).
_STASH_CARVE_OUTS: dict[str, str] = {}


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
