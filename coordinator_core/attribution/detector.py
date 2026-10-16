from __future__ import annotations

import ast
import io
import re
import tokenize
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from .patterns import PATTERNS

#: Whole-line forms this repo's own plan/handoff prose uses to attribute a
#: fix to a reviewer -- an HTML comment (`<!-- Review: ... -->`) or a
#: YAML/shell-comment line (`# Review: ...`). Matched per-line, anchored
#: start-to-end (leading/trailing whitespace aside), so a line carrying
#: other content alongside a `# Review:` fragment is left alone -- only a
#: line that IS the annotation is dropped.
_LINE_IS_REVIEW_ANNOTATION = re.compile(
    r"^\s*(?:<!--\s*Review:.*-->|#\s*Review:.*)\s*$", re.IGNORECASE
)

#: Repo-root-anchored prefixes only -- a path is exempt when it STARTS WITH
#: one of these, never when one occurs as a substring anywhere in the path.
#: The old substring-anywhere check wrongly exempted 54 tracked non-md
#: source files (e.g. `coordinator/bin/provision-sidecar.py`, which contains
#: "sidecar" but is not one of these roots).
EXEMPT_PATH_PREFIXES = (
    "state/",
    "docs/plans/",
    "docs/wiki/",
    "docs/decisions/",
    "tasks/",
    "archive/",
    ".coordinator-local/",
    "coordinator_core/attribution/",
)


@dataclass(frozen=True)
class Match:
    pattern: str
    span: tuple[int, int]
    text: str
    #: 1-indexed line number within the scanned text. 0 (the default) means
    #: "not computed" -- `scan_text` never sets it, since it answers a
    #: whole-blob "does this text carry attribution" question with no line
    #: axis; only `scan_added_lines` (per-added-line, commit-gate use) fills
    #: it in.
    line_no: int = 0


def is_exempt_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized.endswith(".md"):
        return True
    for prefix in EXEMPT_PATH_PREFIXES:
        if normalized.startswith(prefix):
            return True
    return False


def is_test_path(path: str) -> bool:
    """True for a test module path: `test_*.py`, `*_test.py`, `conftest.py`,
    or any path with a `tests/` directory component."""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    if base == "conftest.py" or (
        base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))
    ):
        return True
    return "tests" in parts[:-1]


def _fixture_string_spans(text: str) -> list[tuple[int, int]]:
    """Char-offset `(start, end)` spans of string-literal tokens that are not
    docstrings. Unparseable text returns `[]` (no exemption)."""
    try:
        tree = ast.parse(text)
        raw_lines = io.StringIO(text).readlines()
        starts = [0]
        for ln in raw_lines:
            starts.append(starts[-1] + len(ln))

        def off(row: int, col: int) -> int:
            return starts[row - 1] + col

        def byte_off(row: int, col_bytes: int) -> int:
            line = raw_lines[row - 1] if row - 1 < len(raw_lines) else ""
            return off(row, len(line.encode("utf-8")[:col_bytes].decode("utf-8", "ignore")))

        doc_spans: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                v = first.value
                doc_spans.append(
                    (
                        byte_off(v.lineno, v.col_offset),
                        byte_off(v.end_lineno, v.end_col_offset),
                    )
                )

        fstart = getattr(tokenize, "FSTRING_START", None)
        fend = getattr(tokenize, "FSTRING_END", None)
        spans: list[tuple[int, int]] = []
        depth = 0
        fs_begin = 0
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if fstart is not None and tok.type == fstart:
                if depth == 0:
                    fs_begin = off(*tok.start)
                depth += 1
            elif fend is not None and tok.type == fend:
                depth -= 1
                if depth == 0:
                    spans.append((fs_begin, off(*tok.end)))
            elif tok.type == tokenize.STRING and depth == 0:
                spans.append((off(*tok.start), off(*tok.end)))
        return [
            sp
            for sp in spans
            if not any(d0 <= sp[0] and sp[1] <= d1 for d0, d1 in doc_spans)
        ]
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return []


def _in_spans(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= start and end <= e for s, e in spans)


def scan_text(text: str, path: str) -> list[Match]:
    if is_exempt_path(path):
        return []
    matches: list[Match] = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append(Match(pattern=name, span=m.span(), text=m.group(0)))
    if matches and is_test_path(path) and path.endswith(".py"):
        spans = _fixture_string_spans(text)
        if spans:
            matches = [m for m in matches if not _in_spans(*m.span, spans)]
    return matches


def scan_added_lines(
    new_text: str, old_text: Optional[str], path: str
) -> list[Match]:
    """Matches only for lines whose count in `new_text` rose relative to
    `old_text` -- a `collections.Counter` multiset difference over
    `splitlines()` (CRLF-agnostic: `str.splitlines()` treats `\\r\\n`, `\\r`
    and `\\n` alike as one break, so a CRLF file is scanned the same as an
    LF one), linear in both texts, no `difflib`.

    "Added" here means "present more times in `new_text` than in
    `old_text`" -- an UNCHANGED line, however many times it repeats,
    contributes 0 to the difference and is never scanned; only the excess
    occurrences are. `old_text=None` (a path with nothing to compare
    against -- absent at HEAD and unresolved by any rename source) means
    EVERY line in `new_text` counts as added.

    Which of several identical-text occurrences is treated as "the" added
    one is arbitrary (file order, first-seen) -- immaterial, since the
    matched substring and pattern name are identical for every occurrence
    of the same line; only the reported `line_no` differs.

    Callers pass `is_exempt_path(path)`-filtered paths only (the gate does
    this filtering itself, cheaply, before any IO) -- this function
    re-checks exemption anyway, matching `scan_text`'s own self-guard, so a
    caller that forgets the filter never leaks a legacy exemption.
    """
    if is_exempt_path(path):
        return []
    new_lines = new_text.splitlines()
    if old_text is None:
        added_counts: Counter = Counter(new_lines)
    else:
        added_counts = Counter(new_lines) - Counter(old_text.splitlines())
    if not added_counts:
        return []
    remaining = dict(added_counts)
    matches: list[Match] = []
    for idx, line in enumerate(new_lines, start=1):
        left = remaining.get(line)
        if not left:
            continue
        remaining[line] = left - 1
        for name, pattern in PATTERNS.items():
            for m in pattern.finditer(line):
                matches.append(
                    Match(pattern=name, span=m.span(), text=m.group(0), line_no=idx)
                )
    if matches and is_test_path(path) and path.endswith(".py"):
        matches = _drop_fixture_matches(new_text, new_lines, matches)
    return matches


def _drop_fixture_matches(
    new_text: str, new_lines: list[str], matches: list[Match]
) -> list[Match]:
    """Drop a match only when every occurrence of its line's text lies inside
    a fixture string, so the arbitrary first-seen occurrence never decides."""
    spans = _fixture_string_spans(new_text)
    if not spans:
        return matches
    starts = [0]
    for ln in new_text.splitlines(keepends=True):
        starts.append(starts[-1] + len(ln))
    by_text: dict[str, list[int]] = {}
    for i, ln in enumerate(new_lines):
        by_text.setdefault(ln, []).append(i)
    kept: list[Match] = []
    for m in matches:
        line = new_lines[m.line_no - 1]
        if all(
            _in_spans(starts[i] + m.span[0], starts[i] + m.span[1], spans)
            for i in by_text[line]
        ):
            continue
        kept.append(m)
    return kept


def strip_review_annotations(text: str) -> str:
    """Drop whole lines that are a `<!-- Review: ... -->` HTML comment or a
    `# Review: ...` YAML/shell comment, leaving every other line
    byte-identical, including its original line ending. Used on
    engine-inlined plan text (`derive_plan_context`) so a goal/problem
    excerpt the engine builds for an executor prompt carries no reviewer
    attribution -- the engine strips its own inlined text; stripping what
    executors read from the plan file itself is DoE-side (C7 memo).
    """
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    for line in lines:
        content = line.rstrip("\r\n")
        if _LINE_IS_REVIEW_ANNOTATION.match(content):
            continue
        kept.append(line)
    return "".join(kept)
