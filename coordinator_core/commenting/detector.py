from __future__ import annotations

from coordinator_core.attribution import Match, is_exempt_path

from .patterns import PATTERNS

#: Every PATTERNS family is anchored to one of these literal substrings
#: before its full regex ever runs (see patterns.py's `_MARKER`). A plain
#: `in` substring test is answered in C and rules out the large majority of
#: lines in an ordinary source file with none of these chars, at a fraction
#: of the cost of even attempting the family's own regex against that line.
_MARKER_LITERALS = ("#", "//", "/*", "*", "<!--")


def scan_text(text: str, path: str) -> list[Match]:
    """Does this text, at this path, carry a comment shape the commenting
    standard bans (changelog, attribution, task-ref, narration, or grep-bait)?

    Exemption is `attribution.is_exempt_path`, reused rather than copied, so
    the two detectors never drift on which paths are exempt.

    Matched per line, not against the whole blob: every PATTERNS family is
    already bounded to a single line (`[^\\n]`-scoped), so this changes
    nothing about which matches are found or their reported spans/text --
    only where the regex engine is asked to look. Run against a whole
    multi-line file, each family's `finditer` walks a start position at
    every offset in the file; run per line with a cheap literal pre-filter,
    it walks a start position only within lines that could possibly match.
    That is the whole of the difference between this function taking
    single-digit milliseconds and multiple seconds on a large file.

    One matching difference from the prior whole-blob scan: `grep_bait_token`
    uses `\\s*`, which matches `\\n`. Scanned as one blob under
    `re.MULTILINE`, that let a match stretch across trailing blank lines
    after the token; every other family is `[^\\n]`-bounded and never did
    this. Per-line matching closes that stretch — in line with every
    family's own documented invariant (anchored to a marker AND a
    single-line shape) rather than a deliberate loosening of it.
    """
    if is_exempt_path(path):
        return []
    matches: list[Match] = []
    offset = 0
    for line in text.split("\n"):
        if any(marker in line for marker in _MARKER_LITERALS):
            for name, pattern in PATTERNS.items():
                for m in pattern.finditer(line):
                    matches.append(
                        Match(
                            pattern=name,
                            span=(offset + m.start(), offset + m.end()),
                            text=m.group(0),
                        )
                    )
        offset += len(line) + 1
    return matches
