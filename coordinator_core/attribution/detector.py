from __future__ import annotations

import re
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


def scan_text(text: str, path: str) -> list[Match]:
    if is_exempt_path(path):
        return []
    matches: list[Match] = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            matches.append(Match(pattern=name, span=m.span(), text=m.group(0)))
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
    return matches


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
