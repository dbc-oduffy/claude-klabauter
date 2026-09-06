"""
coordinator_core.install.derive_worker_cap -- resolve the xdist worker ceiling
from this box's hardware instead of from a number somebody typed.

Purpose: ``CLAUDE.md`` § Build & Test and ``docs/reference/test-tiers.md``
§ "Worker count is a derivation, not a constant" both state that the fast-tier
worker cap is ``min(physical_cores / 2, usable_RAM_GB * 1024 / 150MB)``,
recomputed per box. ``concurrency_probe.compute_parallelism_cap`` implements
that formula correctly, but no worker-spawning path consumed it: the number a
test run actually obeyed was the literal ``--maxprocesses=7`` in
``coordinator.local.md``. This module is the consumer, applied by
``coordinator_core.resolve_validation_cmd`` as it hands the command to a runner.

Why this resolves at read time rather than writing the config, and the psutil
condition that governs both halves of the derivation: see
``docs/reference/test-tiers.md`` § "Worker count is a derivation, not a
constant" -- that section is the authored home for this argument; this
docstring does not re-derive it.

Negative spec:

- **Does not change any brightline number and does not touch DR-344.** This is
  the test-worker ceiling only.
- **Never raises a ceiling.** ``--maxprocesses`` is a one-sided ``min()``; this
  module only ever lowers what a host would otherwise spawn.
- **Does not invent a ceiling where the command asked for none.** A command with
  no ``-n auto``/``-n logical`` is returned byte-identical: a ceiling means
  nothing without an auto-detected worker request to bound, and ``-n <int>`` is
  already an explicit human count rather than a request for the core count.
- **Does not write ``coordinator.local.md``.** See above.
- **Does not change how the underlying primitives declare failure.**
  ``default_physical_cores`` falling back to *logical* cores when psutil is
  absent is inherited as-is; roadmap baton ``cloud-em-01`` owns that.
"""

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
    """First match of `pattern`'s group 1 that lies outside a quoted span."""
    for m in pattern.finditer(s):
        if m.group(1) is not None:
            return m
    return None


def derive_cap(
    physical_cores: Optional[int] = None,
    usable_ram_gb: Optional[float] = None,
) -> int:
    """The doctrine cap for this box, or for the supplied hardware figures.

    Both arguments exist so the derivation can be exercised against hardware
    other than the caller's -- that is what makes "a box whose hardware changed
    gets a different cap" testable without changing a box.

    Propagates `default_usable_ram_gb`'s refusal on a host it cannot read; see
    `cap_command_for_this_box` for what the consumer does with that.
    """
    cores = physical_cores if physical_cores is not None else default_physical_cores()
    ram = usable_ram_gb if usable_ram_gb is not None else default_usable_ram_gb()
    return compute_parallelism_cap(cores, ram)


def apply_cap_to_command(command: str, cap: int) -> str:
    """Return `command` with its `--maxprocesses` ceiling set to `cap`.

    Returns the command unchanged when it makes no auto-detecting worker
    request. Inserts the flag directly after the request when it is absent.
    Idempotent. Quote-aware: a `-n auto`/`-n logical` or `--maxprocesses=`
    shape occurring inside a quoted `-k`/`-m` sub-argument is not a real
    worker request and is left untouched.
    """
    match = _first_real_match(_WORKER_REQUEST_QA, command)
    if match is None:
        return command

    # Review: code-reviewer P2 (integrator follow-up) -- rewrite EVERY unquoted
    # occurrence, not just the first: argparse is last-wins, so rewriting only
    # the first leaves `--maxprocesses=7 --maxprocesses=9` running at 9 and
    # the ceiling silently defeated. Quote-awareness is what makes replace-all
    # safe here -- a quoted sub-argument is excluded from group 1 entirely, so
    # replacing every unquoted occurrence can't touch one.
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
    """`(command with this box's ceiling, the cap)`, or `(command, None)`.

    The `None` arm is the declared fall-back: an unreadable machine yields the
    command exactly as the caller supplied it. A cap that cannot be derived is
    not a reason to run uncapped -- the committed ceiling still applies.
    """
    try:
        cap = derive_cap()
    except Exception as exc:
        # Review: code-reviewer P2 — stay broad (an unexpected exception type
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
