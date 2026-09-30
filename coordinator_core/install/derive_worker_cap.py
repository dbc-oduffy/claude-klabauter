
from __future__ import annotations

import argparse
import re
import sys
from typing import Optional, Tuple

from coordinator_core.benchmarks.concurrency_probe import (
    compute_parallelism_cap,
    default_physical_cores,
    default_usable_ram_gb,
)
from coordinator_core.conservatism import SafeDirection, declares_safe_direction

#: A worker request this module is willing to bound. Both auto-detecting forms
#: resolve from the host's CPU count; `-n <int>` does not, and is left alone.
#: Quote-aware variants — Review: code-reviewer P2 — a bare `_WORKER_REQUEST`/
#: `_MAXPROCESSES` search has no quoting discipline, so a literal `-n auto` or
#: `--maxprocesses=` occurring inside a quoted `-k`/`-m` marker expression
#: (`pytest -k "not -n auto"`) would be misdetected/rewritten, corrupting the
#: quoted argument rather than bounding a real worker request. Each pattern is
#: an alternation that consumes a whole quoted span first (group left
#: unset) so the target (captured in group 1) can only match outside one;
#: callers must iterate and use the first match whose group 1 is non-None.
#: Review: code-reviewer P2 -- an unbalanced/unterminated quote has no
#: closing mark for `_QUOTE_SPAN` to match, so text after the stray opening
#: quote is scanned as if unquoted and remains eligible for detection/
#: rewriting (see `test_an_unterminated_quote_fails_open_not_closed`). This
#: is fail-open, not fail-closed, and is left as-is: a command with an
#: unbalanced quote is already malformed and will fail at the shell
#: regardless, so degrading to "treat the rest as unquoted" is not the
#: failure that matters here.
_QUOTE_SPAN = r'"[^"]*"|\'[^\']*\''
_WORKER_REQUEST_QA = re.compile(_QUOTE_SPAN + r"|(-n\s+(?:auto|logical)\b)")
_MAXPROCESSES_QA = re.compile(_QUOTE_SPAN + r"|(--maxprocesses=\d+)")


def _first_real_match(pattern: "re.Pattern", s: str):
    for m in pattern.finditer(s):
        if m.group(1) is not None:
            return m
    return None


def derive_cap(
    physical_cores: Optional[int] = None,
    usable_ram_gb: Optional[float] = None,
) -> int:
    cores = physical_cores if physical_cores is not None else default_physical_cores()
    ram = usable_ram_gb if usable_ram_gb is not None else default_usable_ram_gb()
    return compute_parallelism_cap(cores, ram)


def apply_cap_to_command(command: str, cap: int) -> str:
    match = _first_real_match(_WORKER_REQUEST_QA, command)
    if match is None:
        return command

    rewritten, replaced = [], False
    cursor = 0
    for existing in _MAXPROCESSES_QA.finditer(command):
        if existing.group(1) is None:
            continue
        start, end = existing.span(1)
        rewritten.append(command[cursor:start])
        rewritten.append(f"--maxprocesses={cap}")
        cursor = end
        replaced = True
    if replaced:
        rewritten.append(command[cursor:])
        return "".join(rewritten)

    end = match.end(1)
    return f"{command[:end]} --maxprocesses={cap}{command[end:]}"


@declares_safe_direction(
    SafeDirection.FALL_BACK,
    anchor=lambda result: result[1] is None,
    because=(
        "the caller is a test-command resolver, so refusing here would take the fast tier "
        "offline on any host this module cannot read -- a psutil-less container is exactly "
        "the host that most needs to be able to run its tests; RAISE was rejected because "
        "the degraded answer is bounded and visible: the command as committed, which is the "
        "same ceiling every host obeyed before this module existed"
    ),
)
def cap_command_for_this_box(command: str) -> Tuple[str, Optional[int]]:
    try:
        cap = derive_cap()
    except Exception as exc:
        # Stay broad (an unexpected exception type
        # is exactly the case FALL_BACK exists to survive; narrowing to
        # psutil's own types would take the fast tier offline on the host
        # least able to afford it if a *coding* defect ever raised here
        # instead). The gap was diagnosability, not breadth: surface what was
        # swallowed rather than losing it silently.
        print(
            f"[cap_command_for_this_box] worker cap not derivable "
            f"({type(exc).__name__}: {exc}); using the command's committed "
            "ceiling unchanged.",
            file=sys.stderr,
        )
        return command, None
    return apply_cap_to_command(command, cap), cap


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="derive-worker-cap",
        description="Report this box's derived xdist worker cap.",
    )
    parser.parse_args(argv)

    try:
        cap = derive_cap()
    except Exception as exc:
        print(f"derive-worker-cap: cannot derive a cap on this box: {exc}", file=sys.stderr)
        print(
            "derive-worker-cap: commands resolve with their committed ceiling unchanged.",
            file=sys.stderr,
        )
        return 2

    print(f"physical cores: {default_physical_cores()}")
    print(f"usable RAM GB:  {default_usable_ram_gb():.1f}")
    print(f"derived cap:    {cap}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
