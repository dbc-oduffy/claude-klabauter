"""
Guard: every shipped `.py` module under `coordinator_core/` and
`coordinator/bin/` must be free of `SyntaxError`.

Purpose: a marker-insertion pass (or any other mechanical rewrite) that
splices a statement between a module docstring and a `from __future__
import ...` line produces a hard `SyntaxError` -- future statements must
precede all other statements. That class of break went unnoticed across
19 files (§ state/bug-backlog, generator-provenance marker misplacement,
2026-08-14) because the obvious sanity check, `ast.parse`, does NOT enforce
future-statement placement or ordering: `ast.parse` happily returns a clean
tree for a file where `from __future__ import annotations` sits after other
statements, since that rule is enforced by the compiler's own AST-to-bytecode
pass (`symtable`/`compile`), not by the parser grammar. This test therefore
uses `compile(src, path, "exec")`, the same call the interpreter itself makes
on import, so a module that is importable in practice is exactly the
population this test admits.

Scoped to `coordinator_core/` and `coordinator/bin/` (not the whole repo)
to keep this fast-tier-eligible -- these two trees hold the engine's own
importable surface; `docs/`, `state/`, `archive/`, and vendored/sibling
trees are out of scope for an import-time guard.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SWEPT_DIRS = ("coordinator_core", "coordinator/bin")


def _iter_py_files():
    for rel_dir in _SWEPT_DIRS:
        root = _REPO_ROOT / rel_dir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            if path.is_file():
                yield path


def test_every_swept_module_compiles():
    failures: list[str] = []
    for path in _iter_py_files():
        try:
            source = path.read_bytes()
        except OSError:
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(_REPO_ROOT)}: {exc}")

    assert not failures, "SyntaxError in swept module(s):\n" + "\n".join(failures)
