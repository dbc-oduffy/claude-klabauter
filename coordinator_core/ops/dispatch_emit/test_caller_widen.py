"""Widen each wave row's writes to the test files that import a module it writes.

Pure: the one `git grep` that finds the referencers is run by the caller
(`emit.compose_script`) and handed in as text. A referencer that a different
row of the same wave writes is never added to this row (two writers of one
file in one wave); it appears under "outside your writes" in the row's
tests-exercising section instead, so the executor's PARTIAL cites it.
A `*.golden.*` fixture a covering test names is widened the same way, so the
byte-compared golden is regenerated with its generator.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional, Sequence

from coordinator_core.ops.dispatch_emit.wave_map import UNDECLARED

#: Pathspecs `git grep` is limited to: test files only.
TEST_PATHSPECS = ("*test_*.py", "*_test.py", "*/tests/*.py")


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


#: Grep alternative for a line naming a golden fixture file (`<stem>.golden.<ext>`).
GOLDEN_GREP = r"[A-Za-z0-9_-]+\.golden\.[A-Za-z0-9]+"
_GOLDEN_RX = re.compile(GOLDEN_GREP)


def goldens_of(test: str, pairs: Sequence[tuple]) -> list:
    """Repo-relative golden fixtures `test` names: beside the test, or under its `fixtures/`
    when the naming line mentions it."""
    base = test.replace("\\", "/").rpartition("/")[0]
    found = set()
    for t, text in pairs:
        if t != test:
            continue
        for m in _GOLDEN_RX.finditer(text):
            sub = "fixtures/" if "fixtures" in text else ""
            found.add("/".join(p for p in (base, f"{sub}{m.group(0)}") if p))
    return sorted(found)


def referencers(paths: Sequence[str], pairs: Sequence[tuple]) -> dict:
    """module path -> sorted test files whose matched line imports it."""
    out = {}
    regexes = {p: module_regex(p) for p in paths}
    for p, rx in regexes.items():
        out[p] = sorted({t for t, text in pairs if rx.search(text)})
    return out


def widen_row(row, wave_rows: Sequence, by_module: dict, pairs: Sequence[tuple] = ()) -> Widened:
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
        for test in by_module.get(module, ()):
            for golden in goldens_of(test, pairs):
                if golden in own or golden in added or golden in excluded:
                    continue
                (excluded if golden in peers else added).append(golden)
    return Widened(tuple(added), tuple(excluded))


def _exercising_sources(row) -> list:
    return [m for m in module_paths(row) if m.rsplit("/", 1)[-1] != "conftest.py"]


def widen_waves(waves: list, run_grep) -> list:
    """`waves` with each row's writes widened to importing tests and its body given the
    tests-exercising section. `run_grep(pattern, pathspecs)` returns `git grep` stdout, or
    None if it could not run; it is called once. Only import lines widen `writes`; a test
    that merely names a source file is listed in the section, never written."""
    # Deferred: dispatch_row_lint imports this module at load.
    from coordinator_core.ops.dispatch_emit import dispatch_row_lint

    modules = sorted({m for wave in waves for row in wave for m in module_paths(row)})
    sources = sorted({m for wave in waves for row in wave for m in _exercising_sources(row)})
    parts = [p for p in (grep_pattern(modules), dispatch_row_lint.exercising_pattern(sources)) if p]
    if not parts:
        return waves
    if modules:
        parts.append(GOLDEN_GREP)
    pattern = "|".join(parts)
    output = run_grep(pattern, TEST_PATHSPECS)
    if not output:
        return waves
    pairs = parse_grep(output)
    by_module = referencers(modules, pairs)
    result = []
    for wave in waves:
        new_wave = []
        for row in wave:
            w = widen_row(row, wave, by_module, pairs)
            writes = [*row.writes, *w.writes] if w.writes else row.writes
            exercising = sorted(
                {t for s in _exercising_sources(row) for t in dispatch_row_lint.tests_exercising(s, pairs)}
            )
            section = dispatch_row_lint.tests_exercising_section(writes, exercising, pattern)
            if not w.writes and section is None:
                new_wave.append(row)
                continue
            body = row.body
            if section is not None:
                body = f"{body}\n\n{section}" if body else section
            new_wave.append(row._replace(writes=writes, body=body))
        result.append(new_wave)
    return result
