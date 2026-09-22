"""test_common_console_suppression.py — every `asyncio.create_subprocess_exec`
git spawn in `coordinator_core.ops.fleet._common` must splat
`no_console_creationflags()`, so none of them pops a visible conhost window
on Windows.

Defect this closes: state/bug-backlog/2026-08-30-five-git-spawn-sites-pop-a-
conhost-windo-33ec37ad7c8a.yaml — four `_common.py` sites
(`_empty_private_index_breach`'s `git write-tree`, `rm_and_commit`'s
`git read-tree HEAD` / `git rm --` / `git checkout HEAD --`) spawned bare,
with no `creationflags=` at all. The fifth named site,
`archive_stamp.py::_run_git`, was already routed through the module-level
`_NO_CONSOLE = no_console_creationflags()` constant (DR-054) — out of this
test's scope, which is `_common.py` only.

Negative-spec: this is a narrow, file-scoped AST count, not a re-run of the
repo-wide `coordinator_core/tests/test_no_bare_hot_path_spawn.py` standing
gate — that gate has an unrelated pre-existing failure (`scripts` root) as
of this fix's authoring and is not this test's job to make green.

Spec backlink: state/bug-backlog/2026-08-30-five-git-spawn-sites-pop-a-conhost-windo-33ec37ad7c8a.yaml
"""
from __future__ import annotations

import ast
from pathlib import Path

_COMMON_PATH = (
    Path(__file__).resolve().parents[1] / "_common.py"
)


def _create_subprocess_exec_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "create_subprocess_exec"
            and isinstance(func.value, ast.Name)
            and func.value.id == "asyncio"
        ):
            calls.append(node)
    return calls


def _has_no_console_splat(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg is None and isinstance(kw.value, ast.Call):
            inner = kw.value.func
            name = inner.id if isinstance(inner, ast.Name) else getattr(inner, "attr", None)
            if name == "no_console_creationflags":
                return True
    return False


def test_every_create_subprocess_exec_call_in_common_splats_no_console_creationflags():
    source = _COMMON_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_COMMON_PATH))
    calls = _create_subprocess_exec_calls(tree)
    assert calls, "expected at least one asyncio.create_subprocess_exec call in _common.py"
    unwired = [c.lineno for c in calls if not _has_no_console_splat(c)]
    assert unwired == [], (
        "asyncio.create_subprocess_exec call(s) missing "
        "**no_console_creationflags() at line(s): " + ", ".join(map(str, unwired))
    )
