from __future__ import annotations

import ast

from coordinator_core.tests.test_async_handler_discipline import (
    _find_blocking_calls_in_async_fn,
)


def _violations_for_source(src: str) -> list[tuple[int, str]]:
    tree = ast.parse(src)
    fn_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    assert len(fn_nodes) == 1, "planted source must define exactly one async def"
    return _find_blocking_calls_in_async_fn(fn_nodes[0])


def test_planted_os_rename_is_detected() -> None:
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
