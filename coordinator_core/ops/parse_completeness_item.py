"""
coordinator_core.ops.parse_completeness_item — parse one completeness-checklist
item into {class, assertion, probe} and emit structured output.

Purpose: single source of parse+classify for the pickup skill (C4) and the
completeness-checklist test suite (C9) — round-trip-contract-tests doctrine.

Grammar (pinned):
    <class>: <assertion text> [probe: <shell-command>]
    - <class> in {live, restart-gated}. Anything else -> malformed -> exit non-zero.
    - [probe: ...] is optional. Everything between "probe: " and the FINAL
      unescaped "]" is the command verbatim. A literal trailing "]" inside the
      command is escaped as "\\]".
    - A missing "<class>: " prefix, an empty assertion, or an unterminated
      "[probe:" -> malformed.

Exit codes:
    0 -- success
    1 -- malformed input (message on stderr)

Negative-spec (do NOT "fix" mid-port): pure parser, no side effects. Does NOT
execute the probe command; does NOT read from files.

Oracle quirk, faithfully reproduced (do NOT "fix"): when input arrives via
stdin (no CLI arg), the oracle's `while IFS= read -r line; do ... done` loop
silently DROPS a final line that lacks a trailing newline -- `read` returns
non-zero on that final partial read and bash exits the while-loop BEFORE
running the body for it. A stdin payload with no trailing "\\n" at all (or
whose only content is the unterminated tail) yields an empty item, which then
surfaces as the ordinary "empty input" malformed error -- not a distinct
"missing trailing newline" diagnostic. This module replicates that by only
considering "\\n"-terminated segments of stdin as candidate lines.

Port of: parse-completeness-item.sh (example-doctrine-repo b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-06-24-install-baton-completeness-claude-code-validation.md § C3 (example-doctrine-repo)
"""

from __future__ import annotations

import sys
from typing import List, Optional, Tuple

_PROG = "parse-completeness-item"  # literal program-name prefix -- matches oracle stderr

_CLASSES = ("live", "restart-gated")


class _Malformed(Exception):
    """Internal control-flow exception carrying the oracle-exact stderr message."""


def _read_item_from_stdin() -> str:
    """Mirror the oracle's `while IFS= read -r line; do ... done` stdin loop.

    Blank lines are always skipped (pre-content OR post-content -- the oracle
    treats every `-z "$line"` line as a skippable artifact, not just leading
    ones). The first non-blank line becomes `item`; any FURTHER non-blank line
    is a multi-line error. A final unterminated (no trailing "\\n") segment is
    silently dropped, matching the `read` builtin's exit-on-EOF-without-newline
    behavior -- see module docstring "Oracle quirk" note.
    """
    data = sys.stdin.read()
    # Only "\n"-terminated segments count as lines `read` would have seen;
    # a trailing partial (unterminated) tail is dropped, faithfully
    # reproducing the oracle's read-loop truncation quirk. `str.split("\n")`
    # always yields one MORE element than there are terminated lines (either
    # a trailing "" when data ends with "\n", or the unterminated tail
    # itself) -- dropping the last element handles both cases identically.
    lines = data.split("\n")[:-1]

    item = ""
    for line in lines:
        if line == "":
            continue
        if item != "":
            raise _Malformed("multi-line input not supported")
        item = line
    return item


def _split_class(item: str) -> Tuple[str, str]:
    """Extract the `<class>: ` prefix. Raises _Malformed on any grammar violation."""
    for cls in _CLASSES:
        prefix = f"{cls}: "
        if item.startswith(prefix):
            return cls, item[len(prefix):]

    if item in (f"{c}:" for c in _CLASSES):
        raise _Malformed(f'empty assertion (nothing after class prefix): {item}')

    if ":" in item:
        bad_class = item.split(":", 1)[0]
        raise _Malformed(
            f'unknown class "{bad_class}" (must be "live" or "restart-gated"): {item}'
        )

    raise _Malformed(f'missing "<class>: " prefix: {item}')


def _strip_trailing_ws(text: str) -> str:
    """Strip trailing space/tab only (not other whitespace) -- matches the
    oracle's `case *" "|*"\\t")` peel loop, not Python's broader str.rstrip()."""
    end = len(text)
    while end > 0 and text[end - 1] in (" ", "\t"):
        end -= 1
    return text[:end]


def _extract_probe(item: str, item_rest: str) -> Tuple[str, str]:
    """Split item_rest into (assertion, probe). Raises _Malformed on malformed probe syntax."""
    needle = " [probe: "
    idx = item_rest.find(needle)
    if idx == -1:
        return _strip_trailing_ws(item_rest), ""

    assertion = item_rest[:idx]
    remainder = item_rest[idx + len(needle):]

    if assertion == "":
        raise _Malformed(
            f"empty assertion (probe present but assertion is empty): {item}"
        )

    if not remainder.endswith("]"):
        raise _Malformed(f'unterminated [probe: (no closing "]"): {item}')

    # Peel from the right to find the final UNESCAPED "]" -- a "\]" pair is an
    # escaped literal bracket inside the command, not the closing delimiter.
    probe_scan = remainder
    probe_raw_content: Optional[str] = None
    while True:
        without_close = probe_scan[:-1]  # strip the trailing "]" (invariant: probe_scan ends with "]")
        if without_close.endswith("\\"):
            probe_scan = without_close[:-1]
            if probe_scan.endswith("]"):
                continue
            raise _Malformed(
                f'unterminated [probe: (all "]" are escaped): {item}'
            )
        probe_raw_content = without_close
        break

    if not probe_raw_content:
        raise _Malformed(f"empty probe command in [probe: ]: {item}")

    probe = probe_raw_content.replace("\\]", "]")
    return assertion, probe


def parse_completeness_item(item: str) -> Tuple[str, str, str]:
    """Parse one completeness-checklist item string.

    Returns (item_class, assertion, probe) on success.
    Raises _Malformed (caller renders to the oracle-exact stderr line) on any
    grammar violation.
    """
    if item == "":
        raise _Malformed("empty input")

    item_class, item_rest = _split_class(item)

    if item_rest == "":
        raise _Malformed(f"empty assertion after class prefix: {item}")

    assertion, probe = _extract_probe(item, item_rest)

    if assertion == "":
        raise _Malformed(f"empty assertion: {item}")

    return item_class, assertion, probe


def main(argv: List[str]) -> int:
    """CLI entry: read item ($1 or stdin), parse, print structured output, return rc."""
    if argv:
        item = argv[0]
    else:
        try:
            item = _read_item_from_stdin()
        except _Malformed as exc:
            print(f"{_PROG}: malformed item: {exc}", file=sys.stderr)
            return 1

    try:
        item_class, assertion, probe = parse_completeness_item(item)
    except _Malformed as exc:
        print(f"{_PROG}: malformed item: {exc}", file=sys.stderr)
        return 1

    print(f"class={item_class}")
    print(f"assertion={assertion}")
    print(f"probe={probe}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
