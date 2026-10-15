"""Widen each wave row's writes to the test files that import a module it writes.

Pure: the one `git grep` that finds the referencers is run by the caller
(`emit.compose_script`) and handed in as text. A referencer that a different
row of the same wave writes is never added to this row (two writers of one
file in one wave); it is named in the row's body instead, so the executor's
PARTIAL cites it and the revise path widens it.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional, Sequence

from coordinator_core.ops.dispatch_emit.wave_map import UNDECLARED

#: Pathspecs `git grep` is limited to: test files only.
TEST_PATHSPECS = ("*test_*.py", "*_test.py", "*/tests/*.py")

_NOTE_HEAD = "## Test referencers outside this row's writes"


class Widened(NamedTuple):
    writes: tuple
    excluded: tuple


def is_test_path(path: str) -> bool:
    p = path.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{p}"


def module_paths(row) -> list:
    """The non-test `.py` files a row declares in `writes`; none if undeclared."""
    if row.writes is UNDECLARED:
        return []
    return [
        p
        for p in row.writes
        if p.endswith(".py") and not is_test_path(p) and not p.endswith("__init__.py")
    ]


def _dotted(path: str) -> str:
    return path.replace("\\", "/")[: -len(".py")].replace("/", ".")


def module_regex(path: str) -> "re.Pattern[str]":
    """Matches an import of the module at `path`: `import a.b.c`, `from a.b.c import x`,
    `from a.b import c`."""
    dotted = _dotted(path)
    parent, _, stem = dotted.rpartition(".")
    full = re.escape(dotted) + r"\b"
    if not parent:
        return re.compile(rf"^\s*(?:import|from)\s+{full}")
    return re.compile(
        rf"^\s*(?:(?:import|from)\s+{full}|from\s+{re.escape(parent)}\s+import\b.*\b{re.escape(stem)}\b)"
    )


def grep_pattern(paths: Sequence[str]) -> Optional[str]:
    """One ERE for `git grep -E` that is a superset of every module's `module_regex`."""
    alts = []
    for path in paths:
        dotted = _dotted(path)
        parent, _, stem = dotted.rpartition(".")
        alts.append(re.escape(dotted))
        if parent:
            alts.append(re.escape(parent) + r" +import .*" + re.escape(stem))
    if not alts:
        return None
    return "^[ \t]*(import|from) +(" + "|".join(alts) + ")"


def parse_grep(output: str) -> list:
    """`(test_path, line_text)` pairs from `git grep` output lines `path:text`."""
    pairs = []
    for line in output.splitlines():
        path, sep, text = line.partition(":")
        if sep and path.endswith(".py"):
            pairs.append((path, text))
    return pairs


def referencers(paths: Sequence[str], pairs: Sequence[tuple]) -> dict:
    """module path -> sorted test files whose matched line imports it."""
    out = {}
    regexes = {p: module_regex(p) for p in paths}
    for p, rx in regexes.items():
        out[p] = sorted({t for t, text in pairs if rx.search(text)})
    return out


def widen_row(row, wave_rows: Sequence, by_module: dict) -> Widened:
    own = set(row.writes) if row.writes is not UNDECLARED else set()
    peers = {
        w
        for other in wave_rows
        if other.id != row.id and other.writes is not UNDECLARED
        for w in other.writes
    }
    added, excluded = [], []
    for module in module_paths(row):
        for test in by_module.get(module, ()):
            if test in own or test in added or test in excluded:
                continue
            (excluded if test in peers else added).append(test)
    return Widened(tuple(added), tuple(excluded))


def excluded_note(excluded: Sequence[str]) -> str:
    listed = "\n".join(f"- `{t}`" for t in excluded)
    return (
        f"{_NOTE_HEAD}\n\nThese tests import a module you write, and another row of this wave "
        "writes them, so they are outside your footprint. If your change breaks one, "
        f"return PARTIAL naming it; do not edit it.\n\n{listed}"
    )


def widen_waves(waves: list, run_grep) -> list:
    """`waves` with each row's writes widened. `run_grep(pattern, pathspecs)` returns
    `git grep` stdout, or None if it could not run; it is called once."""
    modules = sorted({m for wave in waves for row in wave for m in module_paths(row)})
    pattern = grep_pattern(modules)
    if pattern is None:
        return waves
    output = run_grep(pattern, TEST_PATHSPECS)
    if not output:
        return waves
    by_module = referencers(modules, parse_grep(output))
    result = []
    for wave in waves:
        new_wave = []
        for row in wave:
            w = widen_row(row, wave, by_module)
            if not w.writes and not w.excluded:
                new_wave.append(row)
                continue
            body = row.body
            if w.excluded:
                body = f"{body}\n\n{excluded_note(w.excluded)}" if body else excluded_note(w.excluded)
            writes = [*row.writes, *w.writes] if w.writes else row.writes
            new_wave.append(row._replace(writes=writes, body=body))
        result.append(new_wave)
    return result
