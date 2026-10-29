"""Parser for the scouts grounding report: names what blocks a ``research.close``.

Producer: DoE ``scouts-grounding-prompt-template.md`` step 5 writes ``grounding.md``. The engine
enforces that agent's verdicts and never forms them (pure text, no IO, no spawn).

Grammar, matched after ``lstrip`` on each ``str.splitlines()`` line (CRLF and indented bullets
parse like LF):

- Digest section: ``## <name>`` where ``<name>`` matches ``digest-*.md``.
- Claim line: ``- **grounded**``, ``- **ungrounded**`` or ``- **contradicted**``, then the claim
  and its evidence.
- Open section: ``## Open questions``. A claim line under it is listed as ``open``: never graded,
  never blocking, not a graded claim. Any other ``##`` heading ends it.
- Contradiction ruling: ``- **real**``, ``- **invented**`` or ``- **dismissed**``. ``real`` means
  the sources themselves disagree; ``invented`` means the digest's own text claims a source
  disagreement the sources do not support; ``dismissed`` is every other tension that does not
  hold, including one the grader raised itself. ``real`` and ``dismissed`` never block;
  ``invented`` does. Trap: graders read "invented" as "this tension is invented", which is
  ``dismissed`` -- ``RULING_NOTE`` says so explicitly.

A carried ruling is keyed to the content sha of every digest it judged and counts only while each
of those digests is present with the same sha, and is also keyed to the grader version.
"""

import hashlib
import re
from collections.abc import Iterable
from fnmatch import fnmatchcase

GROUNDING_REPORT = "grounding.md"
CLAIM_VERDICTS = ("grounded", "ungrounded", "contradicted")
RULINGS = ("real", "invented", "dismissed")
BLOCKING = ("ungrounded", "contradicted", "invented")
OPEN = "open"
OPEN_SECTION = "Open questions"

RULING_NOTE = (
    "Name the digest file(s) each ruling line judges.\n"
    "Rulings: real = the sources themselves disagree; invented = the digest's own text claims two "
    "sources disagree, and they do not; dismissed = every other tension that does not hold, "
    "including one you raised yourself or a digest that is consistent with itself.\n"
    "If your ruling says the digest does not contradict itself or no contradiction exists, it is "
    "dismissed, never invented.\n"
)
REOPEN_NOTE = (
    "Re-open every cited source before grading a claim ungrounded or contradicted. A downgrade "
    "must rest on what the re-opened source says; \"not re-opened\" is not a reason and is refused.\n"
)
# The first-generation grader is DoE's template, which the engine cannot version: a report with
# no recorded version is always stale. Both brief notes feed the version so a changed grading
# instruction stales the grades made under the old one.
GRADER_VERSION = hashlib.sha256((REOPEN_NOTE + RULING_NOTE).encode("utf-8")).hexdigest()[:12]
_NOT_REOPENED_RE = re.compile(r"not[\s-]+re-?opened", re.IGNORECASE)

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
    holds the ruling verdict lines under ``## Contradictions``. ``open`` holds the lines under
    ``## Open questions`` (trailing blanks trimmed).
    """
    summary: list[str] = []
    digests: dict[str, list[str]] = {}
    rulings: list[str] = []
    open_lines: list[str] = []
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
            elif title.lower() == OPEN_SECTION.lower():
                mode = "open"
            else:
                mode = "other"
            continue
        if mode == "summary":
            summary.append(raw)
        elif mode == "digest":
            digests[current].append(raw)
        elif mode == "rulings" and _verdict(raw.lstrip()) in RULINGS:
            rulings.append(raw)
        elif mode == "open":
            open_lines.append(raw)
    return {
        "summary": _trim_trailing_blank(summary),
        "digests": {k: _trim_trailing_blank(v) for k, v in digests.items()},
        "rulings": rulings,
        "open": _trim_trailing_blank(open_lines),
    }


def _ruling_digests(ruling: str) -> set[str]:
    return {t for t in _DIGEST_TOKEN_RE.findall(ruling) if fnmatchcase(t, _DIGEST_GLOB)}


def basis_sha(hashes: list[str]) -> str:
    """Content sha of a digest from its ``grounding.basis.json`` line hashes."""
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


def digest_sha(text: str) -> str:
    """Content sha of digest text; CRLF and trailing spaces do not change it."""
    return basis_sha(line_hashes(text))


def key_ruling(line: str, judged: dict[str, str], *, grader: str | None) -> dict:
    """Key a ruling line to the sha of each digest it judged and to the ``grader`` version that wrote it.

    ``judged`` maps digest name to sha. A line naming none of ``judged``'s digests judged the
    whole set, so it is keyed to every entry. ``grader`` ``None`` records an unversioned grader.
    """
    names = _ruling_digests(line) & judged.keys() or set(judged)
    return {"line": line, "digests": {n: judged[n] for n in sorted(names)}, "grader": grader}


def fresh_grader_current(carry: dict | None) -> bool:
    """True iff ``carry`` records ``GRADER_VERSION``; a missing or pre-versioning carry is false."""
    return isinstance(carry, dict) and carry.get("grader") == GRADER_VERSION


def ruling_survives(ruling: object, current: dict[str, str]) -> bool:
    """True iff ``ruling`` is a keyed ruling of ``GRADER_VERSION`` and every digest it judged is in ``current`` with an equal sha.

    A bare string (pre-keying carry), a malformed entry, or a ruling of another grader version
    never survives.
    """
    if not isinstance(ruling, dict) or not isinstance(ruling.get("line"), str):
        return False
    if ruling.get("grader") != GRADER_VERSION:
        return False
    digests = ruling.get("digests")
    if not isinstance(digests, dict) or not digests:
        return False
    return all(current.get(n) == sha for n, sha in digests.items())


def dropped_ruling_digests(rulings: list, current: dict[str, str]) -> set[str]:
    """Digests in ``current`` that a non-surviving ruling judged; reground regrades them.

    A keyed ruling contributes its keyed names still present. A bare-string or malformed ruling
    contributes the digest names it mentions, or every name in ``current`` when it mentions none.
    """
    out: set[str] = set()
    for r in rulings:
        if ruling_survives(r, current):
            continue
        digests = r.get("digests") if isinstance(r, dict) else None
        if isinstance(digests, dict) and digests:
            out |= digests.keys() & current.keys()
            continue
        line = r if isinstance(r, str) else r.get("line") if isinstance(r, dict) else None
        named = _ruling_digests(line) if isinstance(line, str) else set()
        out |= (named & current.keys()) if named else set(current)
    return out


def carry_forward(
    effective_text: str,
    changed: set[str],
    digest_names: list[str],
    generation: int = 0,
    *,
    rulings: list[dict],
    grader: str,
) -> dict:
    """The carry document: ``{"generation", "carried", "rulings", "flagged", "grader"}``.

    ``grader`` is the version of the brief this reground writes, i.e. of the grader that writes
    the next report.

    ``carried`` keeps the whole prior section of each name in ``digest_names`` that is not in
    ``changed`` and has a section in ``effective_text``. ``rulings`` stores the given keyed
    rulings as is; the caller filters them with ``ruling_survives``. ``flagged`` lists the
    blocking claim lines per carried digest that has any.
    """
    parts = report_sections(effective_text)
    carried = {
        n: parts["digests"][n] for n in digest_names if n not in changed and n in parts["digests"]
    }
    flagged = {
        n: blocked
        for n, lines in carried.items()
        if (blocked := [ln for ln in lines if _verdict(ln.lstrip()) in BLOCKING])
    }
    doc = {
        "generation": generation, "carried": carried, "rulings": rulings, "flagged": flagged,
        "grader": grader,
    }
    if parts["open"]:
        doc["open"] = parts["open"]
    return doc


def effective_report(
    fresh_text: str, carry: dict | None, changed_since_basis: set[str], current: dict[str, str]
) -> str:
    """The report the close enforces: carried sections for carried digests, fresh for the rest.

    ``carry`` ``None`` returns ``fresh_text`` unchanged. A carried digest in
    ``changed_since_basis`` gets no section at all, so it reads as ungraded; any fresh section
    for a carried digest is discarded. A carried ruling counts only while ``ruling_survives``
    against ``current`` (digest name to sha). A fresh ruling naming only carried digests is
    discarded.
    """
    if carry is None:
        return fresh_text
    carried = carry.get("carried", {})
    fresh = report_sections(fresh_text)
    sections = {n: ln for n, ln in carried.items() if n not in changed_since_basis}
    regraded = {n: ln for n, ln in fresh["digests"].items() if n not in carried}
    sections.update(regraded)
    rulings = [r["line"] for r in carry.get("rulings", []) if ruling_survives(r, current)]
    for r in fresh["rulings"]:
        named = _ruling_digests(r)
        if not named or not named <= carried.keys():
            rulings.append(r)
    out = [f"Grounding report: {len(sections) - len(regraded)} digests carried, {len(regraded)} graded fresh.", ""]
    for name in sorted(sections):
        out += [f"## {name}", *sections[name], ""]
    out += ["## Contradictions", *(rulings or [_NO_CONTRADICTIONS]), ""]
    open_lines = list(carry.get("open", []))
    open_lines += [ln for ln in fresh["open"] if ln not in open_lines]
    if open_lines:
        out += [f"## {OPEN_SECTION}", *open_lines, ""]
    return "\n".join(out)


def _verdicts(text: str) -> Iterable[tuple[str, str]]:
    """Yield ``(verdict, line)`` per verdict line; a claim verdict under ``## Open questions`` is ``open``."""
    in_open = False
    for raw in text.splitlines():
        line = raw.lstrip()
        h = _HEADING_RE.match(line)
        if h:
            in_open = h.group(1).lower() == OPEN_SECTION.lower()
            continue
        v = _verdict(line)
        if v is None:
            continue
        yield (OPEN if in_open and v in CLAIM_VERDICTS else v), line


def grounding_counts(text: str) -> dict[str, int]:
    """Count each verdict (the three claim verdicts, three rulings and ``open``) over the report's verdict lines.

    A claim line under ``## Open questions`` counts ``open`` and none of the claim verdicts.
    """
    counts = dict.fromkeys(CLAIM_VERDICTS + RULINGS + (OPEN,), 0)
    for v, _ in _verdicts(text):
        counts[v] += 1
    return counts


def grounding_problems(text: str, digest_names: list[str]) -> list[str]:
    """Messages naming what keeps the report from clearing the digest; ``[]`` clears it.

    One message per blocking verdict line (verdict plus the line, truncated), one per name in
    ``digest_names`` with no ``## <name>`` section, and one when no graded claim line exists.
    Lines under ``## Open questions`` are open: neither blocking nor a graded claim.
    """
    problems: list[str] = []
    sections: set[str] = set()
    claims = 0
    for raw in text.splitlines():
        m = _SECTION_RE.match(raw.lstrip())
        if m and fnmatchcase(m.group(1), "digest-*.md"):
            sections.add(m.group(1))
    for v, line in _verdicts(text):
        if v == OPEN:
            continue
        if v in CLAIM_VERDICTS:
            claims += 1
        if v in BLOCKING:
            if v in CLAIM_VERDICTS and _NOT_REOPENED_RE.search(line):
                problems.append(f"{v} without re-opening its source (re-open each cited source, then regrade): {line.rstrip()[:_LINE_MAX]}")
            else:
                problems.append(f"{v}: {line.rstrip()[:_LINE_MAX]}")
    for name in digest_names:
        if name not in sections:
            problems.append(f"no grounding section for {name}")
    if claims == 0:
        problems.append(f"{GROUNDING_REPORT} grades no claim")
    return problems
