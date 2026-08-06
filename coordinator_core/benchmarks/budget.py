"""
coordinator_core.benchmarks.budget — two-level per-op latency budget resolver.

Loads `budget-manifest.json` (a `{schema_version, min_gating_sample_count, defaults, overrides}`
document) and resolves the effective `{target_ms, tolerance}` budget for a given op: a per-op
`overrides` entry wins if present, else the manifest falls back to the op's `OpClass`-tier
default (`defaults.COMPUTE_ONLY` / `defaults.MUTATING`).

At Phase-0 (this chunk), every default and override in the manifest is a PLACEHOLDER value
marked `"_provisional": true` — C9 (the Phase-0 measurement pass, qsub-01 plan) overwrites them
with honest budgets measured from real per-op latency distributions. Do not treat provisional
values as SLA-authoritative.

**Third tier, forward-binding only (do NOT populate):** a future per-tool-call (hook-port) budget
tier is anticipated — see qsub-01 plan § Out of scope, "A future per-tool-call (hook-port) budget
tier — foundational #4, forward-binding note only." JSON has no native comment syntax, so this
future `defaults.HOOK_PORT` (or equivalent) slot is documented here rather than seeded empty in
the manifest; when that tier lands, add a `defaults.HOOK_PORT` key to `budget-manifest.json` and
extend this resolver's op_class handling accordingly. It is NOT present in the manifest today.

Spec backlink: docs/plans/2026-07-10-qsub-01-latency-benchmark-harness.md § C3
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from coordinator_core.authz.classification import OpClass

_MANIFEST_PATH = Path(__file__).parent / "budget-manifest.json"


def _op_class_name(op_class: Union[OpClass, str]) -> str:
    """Normalize an OpClass enum member or its str name to the manifest's tier key."""
    if isinstance(op_class, OpClass):
        return op_class.name
    return str(op_class)


def load_manifest(manifest_path: Path = _MANIFEST_PATH) -> dict:
    """Load and parse the budget manifest JSON document from disk."""
    with open(manifest_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_budget(
    op: str,
    op_class: Union[OpClass, str],
    manifest: Union[dict, None] = None,
) -> dict:
    """Resolve the effective {target_ms, tolerance} budget for `op`.

    Per-op entries in `manifest["overrides"]` win over the `OpClass`-tier default in
    `manifest["defaults"]`. `op_class` may be an `OpClass` enum member or its `.name` string
    (e.g. "COMPUTE_ONLY"). Raises `KeyError` if no default exists for the resolved tier name —
    fail loud rather than silently falling back to an unrelated tier.
    """
    if manifest is None:
        manifest = load_manifest()

    overrides = manifest.get("overrides", {})
    if op in overrides:
        return _validated_budget(overrides[op], op)

    tier = _op_class_name(op_class)
    defaults = manifest.get("defaults", {})
    if tier not in defaults:
        raise KeyError(f"no budget default for op_class tier {tier!r}")
    return defaults[tier]


def _validated_budget(budget: dict, op: str) -> dict:
    """Validate a resolved override budget dict's shape before returning it.

    Review: code-reviewer (Slice A F3, P2) — a malformed manifest override
    (missing target_ms/tolerance, or a tolerance missing kind/value) previously
    surfaced as an opaque KeyError/AttributeError deep in gate.py's
    _tolerance_field, far from the actual defect site (the manifest). Fails
    loud here instead, naming the op and the missing key, matching the
    fail-loud discipline already applied to the missing-default-tier path.
    """
    if "target_ms" not in budget:
        raise ValueError(f"budget override for op {op!r} is missing 'target_ms': {budget!r}")
    tolerance = budget.get("tolerance")
    if not isinstance(tolerance, dict):
        raise ValueError(f"budget override for op {op!r} is missing 'tolerance': {budget!r}")
    if "kind" not in tolerance:
        raise ValueError(f"budget override for op {op!r}'s tolerance is missing 'kind': {tolerance!r}")
    if "value" not in tolerance:
        raise ValueError(f"budget override for op {op!r}'s tolerance is missing 'value': {tolerance!r}")
    return budget
