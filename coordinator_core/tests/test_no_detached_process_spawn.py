"""
coordinator_core.tests.test_no_detached_process_spawn — DETACHED_PROCESS is
forbidden in live engine code, because it is a console-window amplifier.

Why this guard exists, in one paragraph. `DETACHED_PROCESS` gives the child no
console at all. Every console-subsystem descendant of that child -- `git.exe`,
each hook interpreter, `claude.exe` itself -- then has no console to inherit and
allocates its OWN `conhost.exe`, WITH a visible window. A single detached spawn
therefore turns its whole process subtree into a window factory. The correct flag
is `CREATE_NO_WINDOW`, which gives the child a console that HAS no window; that
windowless console IS inherited, so one flag on the root spawn silences the
entire subtree.

The trap this guard is really aimed at. `DETACHED_PROCESS | CREATE_NO_WINDOW`
LOOKS like belt-and-braces suppression and is in fact a no-op: Win32 documents
`CREATE_NO_WINDOW` as ignored whenever `DETACHED_PROCESS` or
`CREATE_NEW_CONSOLE` is set. Two live sites in this engine carried exactly that
spelling and so read as console-suppressed while flashing; measured, the
combination was indistinguishable from bare `DETACHED_PROCESS` (6 visible
console windows across 3 spawns, versus 0 for `CREATE_NO_WINDOW` alone). A guard
that only forbade the bare form would have passed both of them, which is why
this one forbids the NAME outright rather than any particular combination.

Detached LIFETIME is not what `DETACHED_PROCESS` buys and is not lost by
dropping it -- measured, not assumed: Windows does not reap children on parent
exit, so a `CREATE_NO_WINDOW` child outlives a hard-killed parent identically.
Ctrl-C isolation is carried by `CREATE_NEW_PROCESS_GROUP`, which is untouched.

negative-spec -- this guard scans CODE, never prose. Docstrings and comments
naming `DETACHED_PROCESS` are how the surrounding negative-specs explain the
rule, and flagging those would force the explanation to be deleted in order to
satisfy the guard. Widen this to new spawn shapes; never narrow it to a single
flag combination, which is the exact hole that let the no-op spelling ship.

Measurement: `state/audits/2026-08-21-detached-process-console-window-storm.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parent.parent

#: Scanned as live engine code. Test trees are excluded because a regression
#: test's whole job is to assert the flag is ABSENT, which requires naming it.
_EXCLUDED_DIR_NAMES = frozenset({"tests", "__pycache__", "_vendor"})

_FORBIDDEN = "DETACHED_PROCESS"


def _live_modules() -> list[Path]:
    out: list[Path] = []
    for path in _ENGINE_ROOT.rglob("*.py"):
        parts = set(path.relative_to(_ENGINE_ROOT).parts)
        if parts & _EXCLUDED_DIR_NAMES:
            continue
        if path.name.startswith("test_"):
            continue
        out.append(path)
    return sorted(out)


def _code_references(path: Path) -> list[int]:
    """Return line numbers where `DETACHED_PROCESS` appears in CODE.

    Walks the AST rather than the text, so the name is only reported when it is
    an actual attribute access (`subprocess.DETACHED_PROCESS`), a bare/underscored
    identifier, or a string literal used as a `getattr` lookup key. Prose --
    docstrings and `#` comments -- never reaches the AST as any of those, so the
    negative-specs that explain this rule do not trip it.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []

    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == _FORBIDDEN:
            hits.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id.lstrip("_") == _FORBIDDEN:
            hits.append(node.lineno)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "getattr":
            for arg in node.args[1:2]:
                if isinstance(arg, ast.Constant) and arg.value == _FORBIDDEN:
                    hits.append(node.lineno)
    return sorted(set(hits))


def test_no_live_module_references_detached_process() -> None:
    offenders: list[str] = []
    for path in _live_modules():
        for lineno in _code_references(path):
            rel = path.relative_to(_ENGINE_ROOT).as_posix()
            offenders.append(f"coordinator_core/{rel}:{lineno}")

    assert not offenders, (
        "DETACHED_PROCESS is forbidden in live engine code -- it leaves the child "
        "console-less, so every descendant allocates its own WINDOWED console.\n"
        "Use CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP instead. ORing "
        "DETACHED_PROCESS with CREATE_NO_WINDOW is NOT a fix: Win32 ignores "
        "CREATE_NO_WINDOW whenever DETACHED_PROCESS is set.\n"
        "Sites:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "module_relpath",
    [
        "ops/workflow_fire/fire.py",
        "hooks/auto_push.py",
        "ops/ceremony/detached_spawn.py",
    ],
)
def test_known_detached_spawn_sites_stay_windowless(module_relpath: str) -> None:
    """The three sites that carried the defect are pinned by name.

    The sweep above would catch a regression here too, but naming them keeps the
    failure legible: these are the spawn paths whose SUBTREES flash, and a
    reviewer seeing this test fail should know the operator-visible symptom
    rather than only that a lint rule tripped.
    """
    path = _ENGINE_ROOT / module_relpath
    assert path.is_file(), f"{module_relpath} moved; re-point this guard, do not delete it"
    assert not _code_references(path), (
        f"coordinator_core/{module_relpath} references DETACHED_PROCESS again -- "
        "this is the console-window storm regressing."
    )
