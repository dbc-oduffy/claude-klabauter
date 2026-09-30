"""coordinator_core.ceremony_common.cli_rejection — the shared discriminator
separating an argparse rejection from a CLI's own semantic exit code at the
in-process ceremony dispatch seam.

Background: `workday_complete`/`workstream_complete`/`workweek_complete`
`apply.py`'s `_invoke_cli_main` each do `except SystemExit as exc:` and
flatten `exc.code` to a bare int. `wsc-tail.py`'s own exit ladder
(`describe_wsc_tail_outcome`'s `WSC_TAIL_EXIT_LADDER`) assigns exit 2 the
meaning "commit landed, tail item needs attention — not a halt, proceed".
An `argparse.parse_args` rejection of a skewed argv (a klabauter-mirror row
landing order incident, 2026-08-15) raises `SystemExit(2)` too, and that
flattening makes the two indistinguishable: a tool that ran nothing at all
reads identically to one that ran and found a soft issue.

`classify_cli_exit` is the ONE shared implementation all three
`_invoke_cli_main` sites route through — not three patched copies. It
distinguishes `CliExitClass.ARGV_REJECTED` from `CliExitClass.RETURNED`
using three signals together, never the raise alone: raising `SystemExit`
is necessary but not sufficient — the ~16 zero-arg `def main() -> None`
trampolines (`sys.exit(op_main(sys.argv[1:]))`) always raise, and always
carry a semantic code when their own op fails. The captured stderr's shape
is what separates an actual argparse rejection from a raised semantic exit.

Spec backlink:
docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md, chunk C5

Negative-spec:
    - Does NOT change what any CLI's `main()` returns, and does NOT change
      the exit code any ceremony reports upward. This module classifies an
      already-resolved `(raised, code, stderr_text)` triple; it never
      recomputes or overrides `code` itself.
    - Does NOT execute or import a target CLI. The caller has already
      invoked it and captured its stderr; this module is pure
      classification over the result.
    - Does NOT treat `code == 2` alone, or `raised` alone, as sufficient.
      Either alone misclassifies a real case: `code == 2` alone would
      relabel `wsc-tail.py`'s own documented soft-fail as a rejection;
      `raised` alone would relabel every zero-arg trampoline's semantic
      exit-2 as a rejection, which is the defect class this module exists
      to remove, inverted.
"""

from __future__ import annotations

import re
from enum import Enum

#: argparse's own rejection banner shape (`ArgumentParser.error` ->
#: `self.print_usage(sys.stderr)` then `"%(prog)s: error: %(message)s"`,
#: both to stderr, both present together on every `parse_args` failure).
#: Anchored per-line (`re.MULTILINE`) since a directive's own stdout/stderr
#: interleaving upstream of the usage banner is not itself disqualifying —
#: only the banner's own two lines must both be present.
_USAGE_LINE_RE = re.compile(r"^usage:", re.MULTILINE)
_ERROR_MARKER = ": error: "


class CliExitClass(Enum):
    """A dispatched CLI's exit, classified into exactly two named outcomes.

    `ARGV_REJECTED` — the callee never ran; argparse rejected the argv
    before any op-level code executed. `RETURNED` — everything else: a
    clean exit, a CLI's own semantic non-zero exit (returned or raised),
    or a raised `SystemExit` whose stderr is not argparse-shaped."""

    ARGV_REJECTED = "argv_rejected"
    RETURNED = "returned"


def classify_cli_exit(raised: bool, code: int, stderr_text: str) -> CliExitClass:
    """Classifies one dispatched CLI invocation's outcome.

    `ARGV_REJECTED` requires ALL THREE conditions: `raised` is `True` (the
    callee raised `SystemExit` rather than returning), `code == 2`, AND
    `stderr_text` is argparse-shaped (matches `^usage:` on some line AND
    contains the literal `": error: "` marker). Any invocation missing even
    one of the three — including a raised `SystemExit(2)` whose stderr is
    NOT argparse-shaped, the zero-arg-trampoline case — classifies
    `RETURNED`."""
    if not raised or code != 2:
        return CliExitClass.RETURNED
    if not _USAGE_LINE_RE.search(stderr_text):
        return CliExitClass.RETURNED
    if _ERROR_MARKER not in stderr_text:
        return CliExitClass.RETURNED
    return CliExitClass.ARGV_REJECTED


def describe_exit_class(exit_class: CliExitClass) -> str:
    """The operator-facing note a ceremony's failure message appends for
    `exit_class` — non-empty only for `ARGV_REJECTED`, so a caller can
    unconditionally append it (`f"{msg} — {note}"` guarded on truthiness)
    without special-casing the common `RETURNED` case."""
    if exit_class is CliExitClass.ARGV_REJECTED:
        return "argv rejected — argparse refused this invocation before the CLI ran, not a semantic exit"
    return ""
