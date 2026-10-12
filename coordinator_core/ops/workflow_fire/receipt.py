"""The fire receipt writer: `<script>.emitted.json` beside a freshly emitted workflow script.

The one place a fire's receipt is written, so every emitter that fires a bound script produces
the shape `hooks.allow_emitted_workflow_fire` verifies.
"""
from __future__ import annotations

import sys
from pathlib import Path


def write_fire_receipt(
    script_path: Path, plan_path: "str | None" = None, plans: "list[str] | None" = None
) -> None:
    """Write `<script_path>.emitted.json` through the engine's own `emission_receipt._write_emission_receipt`.

    Best-effort: a receipt that fails to write is narrated on stderr and never fails the emit;
    the script is the deliverable and the receipt is evidence about it. `plans` is recorded
    only when non-empty.
    """
    try:
        from coordinator_core.ops.dispatch_emit.emission_receipt import _write_emission_receipt

        _write_emission_receipt(script_path, plan_path, {}, extras={"plans": plans} if plans else None)
    except Exception as exc:  # noqa: BLE001 -- best-effort; must never fail the emit
        print(f"  WARNING: could not write emission receipt for {Path(script_path).name}: {exc}", file=sys.stderr)
