"""Pure dispatch-row lints: the disjoint-write split offer and the tests-exercising section.

No I/O and no spawn: callers hand in rows, paths and `git grep` pairs.

Traps:
- Whole-token prefix: `session_state` is a prefix of `session_state_writer`, but `plan` is not
  a prefix of `planner`. The prefix must end on an `_` boundary.
- A test exercises a source only through an import-shaped line or the source's file name at a
  non-word boundary. A bare word never matches: a stem like `cli` would match everything.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional, Protocol, Sequence

from coordinator_core.ops.dispatch_emit.pathspec import _declared_paths
from coordinator_core.ops.dispatch_emit.test_caller_widen import (
    TEST_PATHSPECS,
    grep_pattern,
    module_regex,
)

#: Entries the tests-exercising section lists before it falls back to the literal query.
SECTION_CAP = 25

SECTION_HEAD = "## Tests that exercise the code you change"

_ERE_SPECIAL = frozenset(".^$*+?()[]{}|\\")
_BOUNDARY = "(^|[^[:alnum:]_-])"


class LintRow(Protocol):
    """The attributes the lints read; satisfied by `EmitterRow` and `WaveRow`."""

    id: str
    title: str
    body: str
    surface: str
    writes: object


def _affinity_key(path: str) -> str:
    name = PurePosixPath(path.replace("\\", "/")).name.lstrip(".")
    key = name.split(".", 1)[0].lower().replace("-", "_")
    if key.startswith("test_"):
        key = key[len("test_"):]
    if key.endswith("_test"):
        key = key[: -len("_test")]
    return key


def _token_prefix(short: str, long: str) -> bool:
    return bool(short) and (short == long or long.startswith(short + "_"))


def _coherent(a: str, b: str, ka: str, kb: str) -> bool:
    if _token_prefix(ka, kb) or _token_prefix(kb, ka):
        return True
    pa, pb = PurePosixPath(a.replace("\\", "/")), PurePosixPath(b.replace("\\", "/"))
    return pa in pb.parents or pb in pa.parents


def write_groups(paths: list[str]) -> list[list[str]]:
    """Connected components of the coherence relation over `paths`, in first-seen order."""
    items = list(dict.fromkeys(paths))
    keys = [_affinity_key(p) for p in items]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _coherent(items[i], items[j], keys[i], keys[j]):
                parent[find(j)] = find(i)
    groups: dict[int, list[str]] = {}
    for i, p in enumerate(items):
        groups.setdefault(find(i), []).append(p)
    return list(groups.values())


_AND = re.compile(r"\band\b", re.IGNORECASE)


def _top_level_items(title: str) -> list[str]:
    items, buf, depth, in_tick = [], [], 0, False
    for ch in title:
        if ch == "`":
            in_tick = not in_tick
        elif not in_tick and ch == "(":
            depth += 1
        elif not in_tick and ch == ")" and depth:
            depth -= 1
        if ch == "," and depth == 0 and not in_tick:
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf).strip())
    return items


def title_conjoins(title: str) -> bool:
    """True when the title joins deliverables: ` + `, ` & `, the whole word `and`, or three
    or more top-level comma items (commas inside backticks or parentheses do not count)."""
    if " + " in title or " & " in title or _AND.search(title):
        return True
    return sum(1 for item in _top_level_items(title) if item) >= 3


def disjoint_write_offer(row: LintRow) -> Optional[str]:
    """The split offer for a conjoined-title row whose writes fall in two or more groups, or
    None. A body line starting `Coherence:` quiets it."""
    try:
        paths = _declared_paths(row)  # type: ignore[arg-type]
    except ValueError:
        return None
    groups = write_groups(paths)
    if len(groups) < 2 or not title_conjoins(row.title):
        return None
    if any(line.strip().startswith("Coherence:") for line in (row.body or "").splitlines()):
        return None
    n = len(groups)
    return (
        f"row {row.id} writes {n} disjoint targets and its title conjoins deliverables: "
        f'split it into {n} rows, or add a "Coherence: <reason>" line to its body.'
    )


def _ere_escape(text: str) -> str:
    return "".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in text)


def _py_sources(sources: Sequence[str]) -> list[str]:
    return sorted({s.replace("\\", "/") for s in sources if s.endswith(".py")})


def _filename_regex(name: str) -> "re.Pattern[str]":
    return re.compile(rf"(?<![\w-]){re.escape(name)}")


def exercising_pattern(sources: Sequence[str]) -> Optional[str]:
    """One ERE for `git grep -E`: every import line of a source plus every line naming a
    source file after a non-word, non-dash character. No trailing boundary: `foo.py` also
    matches `foo.pyx`. None when no source is a `.py` file."""
    paths = _py_sources(sources)
    if not paths:
        return None
    names = sorted({PurePosixPath(p).name for p in paths})
    filenames = _BOUNDARY + "(" + "|".join(_ere_escape(n) for n in names) + ")"
    return f"{grep_pattern(paths)}|{filenames}"


def tests_exercising(source_path: str, grep_pairs: Sequence[tuple[str, str]]) -> list[str]:
    """Sorted test paths among `grep_pairs` (`(test_path, line)`) whose line imports
    `source_path` or names its file."""
    path = source_path.replace("\\", "/")
    if not path.endswith(".py"):
        return []
    imports = module_regex(path)
    named = _filename_regex(PurePosixPath(path).name)
    return sorted({t for t, line in grep_pairs if imports.search(line) or named.search(line)})


def _query(pattern: str) -> str:
    quoted = pattern.replace("'", "'\\''")
    specs = " ".join(f"'{s}'" for s in TEST_PATHSPECS)
    return f"git grep -lE '{quoted}' -- {specs}"


def tests_exercising_section(
    row_writes: Sequence[str], exercising: Sequence[str], pattern: str
) -> Optional[str]:
    """The brief section listing the tests that exercise the row's sources, or None when
    there are none. At most `SECTION_CAP` entries are listed; past that it carries the total
    and the literal query."""
    tests = sorted(set(exercising))
    if not tests:
        return None
    own = set(row_writes)
    inside = [t for t in tests if t in own]
    outside = [t for t in tests if t not in own]
    shown = (inside + outside)[:SECTION_CAP]
    lines = [
        SECTION_HEAD,
        "",
        "Run all of these, not only your ACs. A listed test outside your writes that "
        "changes verdict is a finding: return PARTIAL naming it, do not edit it.",
    ]
    for label, group in (
        ("In your writes: run them", inside),
        ("Outside your writes: run, do not edit", outside),
    ):
        group = [t for t in group if t in shown]
        if group:
            lines += ["", f"{label}:", *(f"- `{t}`" for t in group)]
    if len(tests) > SECTION_CAP:
        lines += [
            "",
            f"{len(tests)} tests in total; the first {SECTION_CAP} are listed. All of them: "
            f"`{_query(pattern)}`",
        ]
    lines += [
        "",
        "A fallback or clamp resolver added inside a function these tests patch around takes "
        "its root as a parameter, so their patch still isolates it.",
    ]
    return "\n".join(lines)
