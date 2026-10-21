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

import hashlib
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


BASIS_FILE = "grounding.basis.json"
CARRY_FILE = "grounding.carry.json"
_DIGEST_GLOB = "digest-*.md"
_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_DIGEST_TOKEN_RE = re.compile(r"[\w.\-]+")
_NO_CONTRADICTIONS = "No contradictions found."


def line_hashes(text: str) -> list[str]:
    """sha256 hexdigest of each ``str.splitlines()`` line after ``rstrip``; CRLF equals LF."""
    return [hashlib.sha256(ln.rstrip().encode("utf-8")).hexdigest() for ln in text.splitlines()]


def changed_digests(basis: dict[str, list[str]], current: dict[str, str]) -> dict[str, list[int]]:
    """Digest name to its 1-based changed line numbers, for each ``current`` digest differing from ``basis``.

    A digest absent from ``basis`` lists every line. A digest whose hash list differs only by
    truncation or reordering still appears, possibly with an empty line list. Digests only in
    ``basis`` are not reported.
    """
    out: dict[str, list[int]] = {}
    for name, text in current.items():
        now = line_hashes(text)
        prior = basis.get(name)
        if prior == now:
            continue
        prior = prior or []
        out[name] = [i + 1 for i, h in enumerate(now) if i >= len(prior) or prior[i] != h]
    return out


def _trim_trailing_blank(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def report_sections(text: str) -> dict:
    """Split a report into ``{"summary": [...], "digests": {name: [lines]}, "rulings": [...]}``.

    ``summary`` holds the lines before the first ``##`` heading. ``digests`` holds each
    ``## digest-*.md`` body verbatim (heading excluded, trailing blanks trimmed). ``rulings``
    holds the ``real``/``invented`` verdict lines under ``## Contradictions``.
    """
    summary: list[str] = []
    digests: dict[str, list[str]] = {}
    rulings: list[str] = []
    mode, current = "summary", ""
    for raw in text.splitlines():
        h = _HEADING_RE.match(raw.lstrip())
        if h:
            title = h.group(1)
            if fnmatchcase(title, _DIGEST_GLOB):
                mode, current = "digest", title
                digests.setdefault(title, [])
            elif title.lower() == "contradictions":
                mode = "rulings"
            else:
                mode = "other"
            continue
        if mode == "summary":
            summary.append(raw)
        elif mode == "digest":
            digests[current].append(raw)
        elif mode == "rulings" and _verdict(raw.lstrip()) in RULINGS:
            rulings.append(raw)
    return {
        "summary": _trim_trailing_blank(summary),
        "digests": {k: _trim_trailing_blank(v) for k, v in digests.items()},
        "rulings": rulings,
    }


def _ruling_digests(ruling: str) -> set[str]:
    return {t for t in _DIGEST_TOKEN_RE.findall(ruling) if fnmatchcase(t, _DIGEST_GLOB)}


def _ruling_survives(ruling: str, changed: set[str]) -> bool:
    """A ruling naming digests survives when none changed; an unnamed ``invented`` one always does."""
    named = _ruling_digests(ruling)
    if not named:
        return _verdict(ruling.lstrip()) == "invented"
    return not (named & changed)


def carry_forward(
    effective_text: str, changed: set[str], digest_names: list[str], generation: int = 0
) -> dict:
    """The carry document: ``{"generation", "carried", "rulings", "flagged"}``.

    ``carried`` keeps the whole prior section of each name in ``digest_names`` that is not in
    ``changed`` and has a section in ``effective_text``. ``rulings`` keeps a ruling that names at
    least one digest, every one unchanged. ``flagged`` lists the blocking claim lines per carried
    digest that has any.
    """
    parts = report_sections(effective_text)
    carried = {
        n: parts["digests"][n] for n in digest_names if n not in changed and n in parts["digests"]
    }
    rulings = [r for r in parts["rulings"] if _ruling_survives(r, changed)]
    flagged = {
        n: blocked
        for n, lines in carried.items()
        if (blocked := [ln for ln in lines if _verdict(ln.lstrip()) in BLOCKING])
    }
    return {"generation": generation, "carried": carried, "rulings": rulings, "flagged": flagged}


def effective_report(fresh_text: str, carry: dict | None, changed_since_basis: set[str]) -> str:
    """The report the close enforces: carried sections for carried digests, fresh for the rest.

    ``carry`` ``None`` returns ``fresh_text`` unchanged. A carried digest in
    ``changed_since_basis`` gets no section at all, so it reads as ungraded; any fresh section
    for a carried digest is discarded. A fresh ruling naming only carried digests is discarded.
    """
    if carry is None:
        return fresh_text
    carried = carry.get("carried", {})
    fresh = report_sections(fresh_text)
    sections = {n: ln for n, ln in carried.items() if n not in changed_since_basis}
    regraded = {n: ln for n, ln in fresh["digests"].items() if n not in carried}
    sections.update(regraded)
    rulings = [r for r in carry.get("rulings", []) if _ruling_survives(r, changed_since_basis)]
    for r in fresh["rulings"]:
        named = _ruling_digests(r)
        if not named or not named <= carried.keys():
            rulings.append(r)
    out = [f"Grounding report: {len(sections) - len(regraded)} digests carried, {len(regraded)} graded fresh.", ""]
    for name in sorted(sections):
        out += [f"## {name}", *sections[name], ""]
    out += ["## Contradictions", *(rulings or [_NO_CONTRADICTIONS]), ""]
    return "\n".join(out)


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
