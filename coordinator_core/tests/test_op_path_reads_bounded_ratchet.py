"""Ratchet: raw unbounded file reads on op paths never grow; new reads go through bounded_read."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import coordinator_core

pytestmark = pytest.mark.complete

ROOT = Path(coordinator_core.__file__).resolve().parent
SCANNED_DIRS = ("ops", "session")
SANCTIONED_READER = Path("ops/dispatch_emit/halt_resume_record.py")

# Ceiling, not a target: converting a site to halt_resume_record.bounded_read only lowers the
# count, so lower this pin in the same commit. Raising it is the failure this test exists for.
PINNED_RAW_READS = 1818

_READ_ATTRS = {"read_bytes", "read_text"}
_NON_FILE_OPEN_OWNERS = {"os", "webbrowser"}
_WRITE_MODE_CHARS = set("wax+")


def _open_mode(call: ast.Call):
    """Return the literal mode string, "" when absent, or None when not a literal."""
    node = None
    if len(call.args) >= 2:
        node = call.args[1]
    for kw in call.keywords:
        if kw.arg == "mode":
            node = kw.value
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def raw_read_lines(source: str) -> list[int]:
    """Line numbers of read_bytes/read_text calls and open()-for-read calls in `source`."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _READ_ATTRS:
            hits.append(node.lineno)
            continue
        is_open = isinstance(func, ast.Name) and func.id == "open"
        if isinstance(func, ast.Attribute) and func.attr == "open":
            owner = func.value
            is_open = not (isinstance(owner, ast.Name) and owner.id in _NON_FILE_OPEN_OWNERS)
        if not is_open:
            continue
        mode = _open_mode(node)
        if mode is None or not (_WRITE_MODE_CHARS & set(mode)):
            hits.append(node.lineno)
    return hits


def _scanned_files():
    for sub in SCANNED_DIRS:
        for path in sorted((ROOT / sub).rglob("*.py")):
            rel = path.relative_to(ROOT)
            if "tests" in rel.parts or rel == SANCTIONED_READER:
                continue
            yield path


def _count_by_file() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _scanned_files():
        try:
            n = len(raw_read_lines(path.read_text(encoding="utf-8")))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if n:
            counts[path.relative_to(ROOT).as_posix()] = n
    return counts


def test_raw_unbounded_reads_on_op_paths_do_not_grow():
    counts = _count_by_file()
    total = sum(counts.values())
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:8]
    assert total <= PINNED_RAW_READS, (
        f"raw read_bytes/read_text/open-for-read calls on op paths rose to {total} "
        f"(pin {PINNED_RAW_READS}). Read through "
        "coordinator_core.ops.dispatch_emit.halt_resume_record.bounded_read (size cap plus "
        f"deadline). Heaviest files: {top}"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from pathlib import Path\nPath('x').read_text()\n",
        "p.read_bytes()\n",
        "open('x')\n",
        "open('x', 'rb')\n",
        "open('x', mode=m)\n",
        "p.open('r')\n",
    ],
)
def test_scan_counts_a_raw_read(source):
    assert raw_read_lines(source)


@pytest.mark.parametrize(
    "source",
    [
        "open('x', 'w')\n",
        "open('x', 'ab')\n",
        "p.open(mode='r+')\n",
        "os.open(p, os.O_RDONLY)\n",
        "p.write_text('x')\n",
        "bounded_read(p, 500)\n",
    ],
)
def test_scan_ignores_writes_and_the_sanctioned_reader(source):
    assert not raw_read_lines(source)
