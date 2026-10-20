"""The fire receipt writer: `<script>.emitted.json` beside a freshly emitted workflow script.

The one place a fire's receipt is written, so every emitter that fires a bound script produces
the shape `hooks.allow_emitted_workflow_fire` verifies.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path

#: Env name carrying a fired child's one-time ownership token. The `COORDINATOR_ALLOW_` prefix is
#: in `warm.env_forwarding.CALLER_PREFIXES`, so it reaches the guard's `params["env"]` with no
#: wiring change; a name outside the prefixes is dropped before the guard sees it.
FIRE_TOKEN_ENV = "COORDINATOR_ALLOW_FIRE_TOKEN"


def fire_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def stamp_fire_ownership(script_path: Path, fire_id: str, token: str) -> bool:
    """Record on `<script>.emitted.json` that `fire_id` owns the script, bound to `token`'s digest.

    The receipt holds only the digest, so reading the receipt does not yield a usable token.
    No receipt (a hand-authored script), or one that cannot be rewritten, returns False and
    leaves the file as it was.
    """
    receipt_path = Path(script_path).with_name(Path(script_path).name + ".emitted.json")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict):
            return False
        receipt["fire"] = {"fire_id": fire_id, "token_sha256": fire_token_digest(token)}
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
    except (OSError, ValueError):
        return False
    return True


def fire_token_matches(receipt: Mapping, env: object) -> bool:
    """True when `env` carries the token whose digest the receipt's `fire` stamp holds."""
    fire = receipt.get("fire")
    if not isinstance(fire, Mapping) or not isinstance(env, Mapping):
        return False
    recorded = fire.get("token_sha256")
    token = env.get(FIRE_TOKEN_ENV)
    if not (isinstance(recorded, str) and recorded and isinstance(token, str) and token):
        return False
    return fire_token_digest(token) == recorded


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
