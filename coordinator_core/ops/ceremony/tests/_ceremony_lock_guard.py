"""
coordinator_core.ops.ceremony.tests._ceremony_lock_guard

Shared repo-wide reintroduction guard for the deleted `ceremony_lock.py`
mutex (docs/plans/2026-08-07-excise-the-ceremony-lock.md § C7/AC9). Both
AC9-pinned tests (`test_post_commit_tail.py::test_module_does_not_import_
ceremony_lock`, `test_wsc_tail_parity.py::test_ac4_no_ceremony_lock_
nesting_remains_on_any_live_path`) call `assert_no_ceremony_lock_
reintroduction` rather than each re-implementing the walk -- prior to this
extraction the ~15-line body was duplicated byte-for-byte across both files
with no divergence detection (S3 close review, finding 2).

What this enforces (and no more): under `coordinator_core/` or
`coordinator/bin/`, no module may (a) exist as a `*ceremony_lock*.py` file
-- catches a bare `git checkout` restore before anything imports it yet --
(b) import a module whose dotted path has a `ceremony_lock` component,
static, dynamic (`importlib.import_module(...)`/`__import__(...)`), or
spread across a parenthesized multi-line `from ... import (...)` -- AST-
walked, not line-prefix string matching, so all three shapes are the same
check -- or (c) define, call, or `with`-enter anything named exactly
`ceremony_lock` (the acquire-call and context-manager shapes the deleted
mutex used; S3 close review, finding 4 -- this was covered by the pre-C7
oracle's `"ceremony_lock(" not in source` assertion and had been dropped by
the import-only re-point).

What this does NOT enforce, deliberately: a mutex reintroduced under an
unrelated name (`commit_mutex.py`, `class WscCommitLock`, an inline
double-checked-file-lock with no `ceremony_lock` identifier anywhere) is
invisible to this guard. The Anti-scope this guard backs reads "in any
file, under any name" -- this guard enforces exactly one name. Narrowing
detection to the literal `ceremony_lock` identifier is a deliberate,
documented choice (S3 close review, finding 1): building a shape/behavioral
check for "anything that acts like a mutex" is a materially larger,
separately-sized piece of work, not a mechanical fix. Callers citing this
guard should say "the only name this catches is `ceremony_lock`; a renamed
reintroduction needs plan review to catch," not claim blanket coverage of
the Anti-scope.

Why AST rather than a substring scan: this guard's own two callers, and
several sibling modules under `coordinator_core/ops/ceremony/`, carry
extensive PROSE describing the deleted mechanism ("`ceremony_lock` was
removed 2026-08-07", "no outer `ceremony_lock` wrap remains", ...) in
docstrings and comments -- a plain substring match over raw text would
flag every one of those as an offender. AST identifier/import matching
only fires on live code shapes (an actual `Import`/`ImportFrom`/`Call`/
`Name`/`Attribute`/`def`), never on a string constant or comment, so this
module's own extensive self-documentation about the guard it implements
does not trip itself.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TARGET = "ceremony_lock"

# This file's own name matches the `*ceremony_lock*.py` filesystem check
# below -- it is the guard implementation, not a reintroduced lock module.
# Excluded by exact relative-path suffix, not swept up as an offender.
_SELF_RELPATH_SUFFIX = "tests/_ceremony_lock_guard.py"


def _is_target_dotted(dotted: str) -> bool:
    return _TARGET in dotted.split(".")


def _identifier_hits(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(_is_target_dotted(alias.name) for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if _is_target_dotted(module) or any(alias.name == _TARGET for alias in node.names):
                return True
        elif isinstance(node, ast.Call):
            func = node.func
            func_name = getattr(func, "attr", None) or getattr(func, "id", None)
            if func_name in ("import_module", "__import__"):
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if _is_target_dotted(arg.value):
                            return True
            if func_name == _TARGET:
                return True
        elif isinstance(node, (ast.Name, ast.Attribute)):
            name = getattr(node, "id", None) or getattr(node, "attr", None)
            if name == _TARGET:
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == _TARGET:
                return True
    return False


def assert_no_ceremony_lock_reintroduction(
    repo_root: Path,
    *,
    rel_dirs: tuple[str, ...] = ("coordinator_core", "coordinator/bin"),
) -> None:
    """Raise AssertionError if any `*ceremony_lock*.py` file, static/dynamic
    import, or `ceremony_lock`-named def/call/with-statement exists under
    `rel_dirs`. See module docstring for exactly what is (and is not)
    covered -- narrower than the Anti-scope's "any file, any name" text."""
    offenders: list[str] = []
    scanned = 0
    for rel_dir in rel_dirs:
        base = repo_root / rel_dir
        assert base.is_dir(), (
            f"guard root missing or moved: {rel_dir} -- a silently-vacuous "
            "guard is the exact failure mode this guard exists to prevent"
        )
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts or ".pytest_cache" in path.parts:
                continue
            rel = path.relative_to(repo_root).as_posix()
            if rel.endswith(_SELF_RELPATH_SUFFIX):
                continue
            if _TARGET in path.stem:
                offenders.append(f"{rel}: filename matches *{_TARGET}*.py")
                continue
            scanned += 1
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                # Not this guard's job to police unparsable fixtures; the
                # filename check above already covers the cheapest
                # reintroduction shape independent of parseability.
                continue
            if _identifier_hits(tree):
                offenders.append(f"{rel}: references `{_TARGET}` (import, call, or definition)")
    assert scanned > 0, "guard scanned zero files -- vacuous pass (empty roots?)"
    assert offenders == [], f"unexpected ceremony_lock reintroduction: {offenders}"
