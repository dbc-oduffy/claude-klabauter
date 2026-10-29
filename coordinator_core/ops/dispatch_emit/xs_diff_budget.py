"""Pure XS diff-budget verdict over `git diff --numstat` rows; no git, no I/O.

Prices an XS-arm run's whole own diff against the XS lane budget constants so a caller can refuse
to commit it unreviewed.
"""

from __future__ import annotations

from typing import Final, Iterable, Optional

from coordinator_core.session.xs_lane_budget import BUDGET_FILES, BUDGET_LINES

REFUSED_XS_OVER_BUDGET: Final[str] = "xs-over-budget"


def parse_numstat(text: str) -> list[tuple[int, int, str]]:
    """`added\\tdeleted\\tpath` rows -> (added, deleted, path). A binary row (`-\\t-\\tpath`) is
    (0, 0, path); a malformed line is skipped."""
    rows: list[tuple[int, int, str]] = []
    for line in (text or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2]:
            continue
        a, d, path = parts
        try:
            rows.append((int(a), int(d), path))
        except ValueError:
            if a == "-" and d == "-":
                rows.append((0, 0, path))
    return rows


#: Run bookkeeping the commit carries beside the product (run records, scratch scripts, session
#: share sidecars, task ephemera): never priced against the product budget.
BOOKKEEPING_PREFIXES: Final[tuple[str, ...]] = ("state/", "scratch/", ".coordinator-local/", "tasks/")


def product_rows(
    rows: Iterable[tuple[int, int, str]], exempt: Iterable[str] = ()
) -> list[tuple[int, int, str]]:
    """``rows`` without bookkeeping-prefixed paths and without the ``exempt`` paths (the run's
    plan and cited sizing), which no chunk declares but the commit carries."""
    skip = set(exempt)
    return [r for r in rows if r[2] not in skip and not r[2].startswith(BOOKKEEPING_PREFIXES)]


def verdict(rows: Iterable[tuple[int, int, str]]) -> Optional[dict]:
    """None when the diff fits the XS budget, else {files, lines, budget_files, budget_lines}.
    Lines are added + deleted; a binary row counts as a file at zero lines."""
    files = 0
    lines = 0
    for added, deleted, _path in rows:
        files += 1
        lines += added + deleted
    if files <= BUDGET_FILES and lines <= BUDGET_LINES:
        return None
    return {
        "files": files,
        "lines": lines,
        "budget_files": BUDGET_FILES,
        "budget_lines": BUDGET_LINES,
    }


RESIZE_ARM: Final[str] = "S"


def resize_next_action(v: dict, sizing_rel: Optional[str], resized: bool = True) -> str:
    """The in-run resize report: the measured size against the XS budget, then the S-arm gates.
    ``resized`` False states the sizing was not amended, so the record still reads XS."""
    target = sizing_rel or "the sizing"
    head = (
        f"XS run diff is {v['files']} files, {v['lines']} lines; XS budget is "
        f"{v['budget_files']} files, {v['budget_lines']} lines. "
    )
    if resized:
        return head + f"Resized {target} to {RESIZE_ARM} in-run; the {RESIZE_ARM}-arm gates apply."
    return head + f"{target} was not amended; resize it to {RESIZE_ARM} or larger. The {RESIZE_ARM}-arm gates apply."


def resize_marker(v: dict) -> dict:
    """The over-budget outcome the caller applies by resizing to S, not a refusal."""
    return {
        "resize_arm": RESIZE_ARM,
        "measured": {"files": v["files"], "lines": v["lines"]},
        "budget": {"files": v["budget_files"], "lines": v["budget_lines"]},
    }
