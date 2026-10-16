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
from fnmatch import fnmatchcase

GROUNDING_REPORT = "grounding.md"
CLAIM_VERDICTS = ("grounded", "ungrounded", "contradicted")
RULINGS = ("real", "invented")
BLOCKING = ("ungrounded", "contradicted", "invented")

_VERDICT_RE = re.compile(r"^-\s+\*\*(" + "|".join(CLAIM_VERDICTS + RULINGS) + r")\*\*", re.IGNORECASE)
_SECTION_RE = re.compile(r"^##\s+(\S+)\s*$")
_LINE_MAX = 160


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
