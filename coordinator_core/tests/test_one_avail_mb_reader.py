"""Ratchet: host_sampler.py is the only non-test source that reads available memory."""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_ROOTS = (_REPO / "coordinator_core", _REPO / "coordinator" / "bin")
_ALLOWED = "coordinator_core/telemetry/host_sampler.py"
_PATTERN = re.compile(
    r"ullAvailPhys|MemAvailable|vm\.available|virtual_memory\(\)\.available|memorystatus_level|vm_stat|SC_PHYS_PAGES"
)


def _is_test_path(path: Path) -> bool:
    return "tests" in path.parts or path.name.startswith("test_") or path.name == "conftest.py"


def _docstring_lines(tree: ast.AST) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def _reads_memory(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not _PATTERN.search(text):
        return False
    try:
        skip = _docstring_lines(ast.parse(text))
    except SyntaxError:
        skip = set()
    for no, line in enumerate(text.splitlines(), 1):
        if no in skip or line.lstrip().startswith("#"):
            continue
        if _PATTERN.search(line):
            return True
    return False


def _offenders() -> list[str]:
    found = []
    for root in _ROOTS:
        for path in root.rglob("*.py"):
            if _is_test_path(path.relative_to(_REPO)):
                continue
            rel = path.relative_to(_REPO).as_posix()
            if rel != _ALLOWED and _reads_memory(path):
                found.append(rel)
    return sorted(found)


def test_only_host_sampler_reads_available_memory():
    offenders = _offenders()
    assert not offenders, (
        "available memory is read outside host_sampler; call "
        "coordinator_core.telemetry.host_sampler.avail_mb() instead in: " + ", ".join(offenders)
    )


def test_host_sampler_still_reads_it():
    assert _reads_memory(_REPO / _ALLOWED)
