"""The guard that `handoff-housekeeping` warm-serves, and keeps warm-serving.

Purpose: every timing figure in the governing plan
(`docs/plans/2026-08-27-one-corpus-read-or-the-housekeeping-job-dies-a-fourth-time.md`)
is a WARM figure. Reached cold this job pays ~109ms of interpreter-plus-engine
import, or ~163ms through the `.cmd` forwarder, against a 200ms process-time
bar for a job measured at 65-95ms. So warm-serving is not a property this
door happens to have; it is the difference between the job being under the bar
and over it.

WHY A DEDICATED TEST AND NOT JUST THE C8 RATCHET. The ratchet
(`test_every_allowlisted_name_warm_serves.py`) sweeps the whole allowlist and
tolerates any name recorded in its `_BASELINE`. That is the right shape for a
368-name corpus being walked back to green, and the wrong shape for this one
name: a future regression here can be silenced by adding a baseline row, which
is a legal move in that suite and a defect in this one. This module fails
closed on findings with no baseline to absorb them.

The three conditions are `classify_entrypoint`'s, not a second opinion — this
module imports C1's committed AST-only instrument rather than re-deriving a
fourth answer to "does this name warm-serve?" (three prior hand-derivations
produced three wrong counts; see that module's docstring).

Negative-spec: this module does NOT import, exec, or invoke
`coordinator/bin/handoff-housekeeping.py`. Importing the module body to test
it is the exact hazard condition (3) exists to detect.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.warm.serve_classifier import classify_entrypoint

NAME = "handoff-housekeeping"

_ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[2] / "ops" / "warm_entrypoint_allowlist.json"
)


def test_the_script_resolves_at_the_path_the_door_looks_for() -> None:
    """Condition 1. A name absent from `coordinator/bin/<name>.py` is a
    distinct failure from "resolves but is unservable" — the door fails it
    closed at request time, silently, with the job simply never running warm."""
    verdict = classify_entrypoint(NAME)
    assert verdict.script_exists, (
        f"{NAME}: no script at {verdict.script_relpath} — the warm door resolves "
        f"coordinator/bin/<name>.py and nothing else"
    )


def test_main_is_callable_with_one_argument() -> None:
    """Condition 2, and the one a naive check misses.

    `def main():` satisfies "a main exists" and then raises TypeError the first
    time the door calls `main(argv)`. ~160 claude-klabauter names were once counted as
    warm-serving on exactly that basis. Arity is asserted separately from
    existence so a regression to zero-arity reads as an arity failure here
    rather than as a passing has-a-main check."""
    verdict = classify_entrypoint(NAME)
    assert verdict.has_main, f"{NAME}: no module-level `main` def"
    assert verdict.main_arity_ok, (
        f"{NAME}: `main` is not callable as main(argv) — the warm door passes "
        f"one argument and this signature will raise TypeError on the first call"
    )


def test_the_module_body_is_inert() -> None:
    """Condition 3 — no work at import time, INCLUDING no module-scope
    non-stdlib import.

    The failure this catches is not hypothetical: a module-scope
    `from lib.cc_invoke import ...` is verbatim what killed
    `coordinator-auto-push.py` on the forwarder route (2a66fc8e9). Every
    `coordinator_core` import in this door is deferred into `main()` for this
    reason; hoisting one for tidiness fails this test, which is the point —
    without it the door keeps working and silently costs 163ms forever."""
    verdict = classify_entrypoint(NAME)
    assert not verdict.findings, (
        f"{NAME}: module body is not inert — "
        + "; ".join(str(f) for f in verdict.findings)
    )


def test_the_name_is_on_the_committed_warm_allowlist() -> None:
    """Serving is gated by the allowlist as well as by the file's shape: a name
    absent from `warm_entrypoint_allowlist.json` refuses (fail closed) rather
    than warm-loading an unvetted CLI into the shared server ~50 sessions use.
    A door that satisfies all three structural conditions and is not on the
    list still never serves warm — so both halves are asserted, not one."""
    entrypoints = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))["entrypoints"]
    assert NAME in entrypoints, (
        f"{NAME}: absent from warm_entrypoint_allowlist.json — invoke.from_argv "
        f"fails the name closed and the job runs cold at ~163ms"
    )
