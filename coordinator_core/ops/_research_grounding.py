"""Parser for the scouts grounding report: names what blocks a ``research.close``.

Producer: DoE ``scouts-grounding-prompt-template.md`` step 5 writes ``grounding.md``. The engine
enforces that agent's verdicts and never forms them (pure text, no IO, no spawn).

Grammar, matched after ``lstrip`` on each ``str.splitlines()`` line (CRLF and indented bullets
parse like LF):

- Digest section: ``## <name>`` where ``<name>`` matches ``digest-*.md``.
- Claim line: ``- **grounded**``, ``- **ungrounded**`` or ``- **contradicted**``, then the claim
  and its evidence.
- Contradiction ruling: ``- **real**`` or ``- **invented**``. A ``real`` ruling means the sources
  disagree; it never blocks.
"""

import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

GROUNDING_REPORT = "grounding.md"
CLAIM_VERDICTS = ("grounded", "ungrounded", "contradicted")
RULINGS = ("real", "invented")
BLOCKING = ("ungrounded", "contradicted", "invented")

_VERDICT_RE = re.compile(r"^-\s+\*\*(" + "|".join(CLAIM_VERDICTS + RULINGS) + r")\*\*", re.IGNORECASE)
_SECTION_RE = re.compile(r"^##\s+(\S+)\s*$")
_LINE_MAX = 160

REGROUND_OP = "research.reground"
STALE_REPORT_PREFIX = "grounding.stale-"
REGROUND_BRIEF_PREFIX = "reground-"
_STALE_RE = re.compile(r"^grounding\.stale-(\d+)\.md$")
_BRIEF_RE = re.compile(r"^reground-(\d+)\.md$")


def _require_generation(n: int) -> int:
    if n < 1:
        raise ValueError(f"generation must be >= 1, got {n}")
    return n


def stale_report_name(n: int) -> str:
    """Name a moved-aside grounding report for generation ``n``; refuses ``n < 1``."""
    return f"{STALE_REPORT_PREFIX}{_require_generation(n)}.md"


def reground_brief_name(n: int) -> str:
    """Name the brief a re-ground of generation ``n`` reads; refuses ``n < 1``."""
    return f"{REGROUND_BRIEF_PREFIX}{_require_generation(n)}.md"


def next_stale_index(names: Iterable[str]) -> int:
    """First unused generation: 1 + the max ``n`` over stale-report and reground-brief names, else 1.

    Names matching neither ``grounding.stale-<n>.md`` nor ``reground-<n>.md`` are ignored.
    """
    top = 0
    for name in names:
        m = _STALE_RE.match(name) or _BRIEF_RE.match(name)
        if m:
            top = max(top, int(m.group(1)))
    return top + 1


def grounding_predates_digest(grounding_mtime: float, digest_mtimes: Iterable[float]) -> bool:
    """True iff some digest mtime is strictly newer than the grounding report's."""
    return any(m > grounding_mtime for m in digest_mtimes)


def _verdict(line: str) -> str | None:
    m = _VERDICT_RE.match(line)
    return m.group(1).lower() if m else None


def grounding_counts(text: str) -> dict[str, int]:
    """Count each of the five verdicts over the report's verdict lines."""
    counts = dict.fromkeys(CLAIM_VERDICTS + RULINGS, 0)
    for raw in text.splitlines():
        v = _verdict(raw.lstrip())
        if v:
            counts[v] += 1
    return counts


def grounding_problems(text: str, digest_names: list[str]) -> list[str]:
    """Messages naming what keeps the report from clearing the digest; ``[]`` clears it.

    One message per blocking verdict line (verdict plus the line, truncated), one per name in
    ``digest_names`` with no ``## <name>`` section, and one when no claim line exists.
    """
    problems: list[str] = []
    sections: set[str] = set()
    claims = 0
    for raw in text.splitlines():
        line = raw.lstrip()
        v = _verdict(line)
        if v is None:
            m = _SECTION_RE.match(line)
            if m and fnmatchcase(m.group(1), "digest-*.md"):
                sections.add(m.group(1))
            continue
        if v in CLAIM_VERDICTS:
            claims += 1
        if v in BLOCKING:
            problems.append(f"{v}: {line.rstrip()[:_LINE_MAX]}")
    for name in digest_names:
        if name not in sections:
            problems.append(f"no grounding section for {name}")
    if claims == 0:
        problems.append(f"{GROUNDING_REPORT} grades no claim")
    return problems
