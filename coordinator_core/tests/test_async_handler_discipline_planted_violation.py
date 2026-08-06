"""Planted-violation self-test for the widened AC-3 Gap-3 gate.

Purpose: prove test_async_handler_discipline.py's widened detection
(os.rename/os.walk/shutil.* directly inside an async def body) actually
fires, rather than only asserting the current (violation-free) codebase is
green. A gate whose only test is "the codebase currently passes" can silently
regress to detecting nothing and still stay green forever — this test
supplies synthetic source containing each newly-detected shape and asserts
the gate's own AST walk flags it.

Spec backlink: this module exists because the code-review finding for AC-3
Gap-3 (2026-07-22, state/review-trail/findings/
2026-07-22-codereview-sliceW3-familyA-ops-buildout.md Finding 1) named the
gate's narrow subprocess-only implementation as a live blind spot; this test
is the proof the widened implementation closes it.

Negative-spec:
  - Does NOT re-scan the real coordinator_core/ops or hooks trees — this is a
    synthetic-source unit test of the gate's AST-walking helpers, not another
    pass of the parametrized file-sweep test in the sibling module.
  - Does NOT assert on subprocess detection (already covered by the
    pre-existing sibling test) — scope is strictly the three newly-widened
    call shapes (os.rename, os.walk, shutil.*).
"""
from __future__ import annotations

import ast

from coordinator_core.tests.test_async_handler_discipline import (
    _find_blocking_calls_in_async_fn,
)


def _violations_for_source(src: str) -> list[tuple[int, str]]:
    """Parse *src*, find the single AsyncFunctionDef, return its violations."""
    tree = ast.parse(src)
    fn_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert len(fn_nodes) == 1, "planted source must define exactly one async def"
    return _find_blocking_calls_in_async_fn(fn_nodes[0])


def test_planted_os_rename_is_detected() -> None:
    """A direct os.rename(...) inside an async def body must be flagged."""
    src = (
        "import os\n"
        "async def _handler(params, repo_root=None):\n"
        "    os.rename(src, dest)\n"
        "    return {}\n"
    )
    violations = _violations_for_source(src)
    assert violations, "widened gate failed to detect a planted os.rename() violation"
    assert any("os.rename" in call for _, call in violations)


def test_planted_os_walk_is_detected() -> None:
    """A direct os.walk(...) inside an async def body must be flagged."""
    src = (
        "import os\n"
        "async def _handler(params, repo_root=None):\n"
        "    for _ in os.walk(root):\n"
        "        pass\n"
        "    return {}\n"
    )
    violations = _violations_for_source(src)
    assert violations, "widened gate failed to detect a planted os.walk() violation"
    assert any("os.walk" in call for _, call in violations)


def test_planted_shutil_copytree_is_detected() -> None:
    """A direct shutil.copytree(...) inside an async def body must be flagged."""
    src = (
        "import shutil\n"
        "async def _handler(params, repo_root=None):\n"
        "    shutil.copytree(src, dst)\n"
        "    return {}\n"
    )
    violations = _violations_for_source(src)
    assert violations, "widened gate failed to detect a planted shutil.copytree() violation"
    assert any("shutil.copytree" in call for _, call in violations)


def test_planted_shutil_rmtree_is_detected() -> None:
    """A direct shutil.rmtree(...) inside an async def body must be flagged."""
    src = (
        "import shutil\n"
        "async def _handler(params, repo_root=None):\n"
        "    shutil.rmtree(target)\n"
        "    return {}\n"
    )
    violations = _violations_for_source(src)
    assert violations, "widened gate failed to detect a planted shutil.rmtree() violation"
    assert any("shutil.rmtree" in call for _, call in violations)


def test_planted_os_rename_inside_to_thread_is_not_flagged() -> None:
    """os.rename wrapped in asyncio.to_thread(...) is the CORRECT pattern — no violation."""
    src = (
        "import asyncio\n"
        "import os\n"
        "async def _handler(params, repo_root=None):\n"
        "    return await asyncio.to_thread(os.rename, src, dest)\n"
    )
    violations = _violations_for_source(src)
    assert not violations, (
        "os.rename inside asyncio.to_thread(...) must NOT be flagged: "
        f"got {violations!r}"
    )
