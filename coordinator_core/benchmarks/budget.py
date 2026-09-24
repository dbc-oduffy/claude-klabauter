"""
coordinator_core.benchmarks.budget — two-level per-op latency budget resolver.

Loads `budget-manifest.json` (a `{schema_version, min_gating_sample_count, defaults, overrides}`
document) and resolves the effective `{target_ms, tolerance}` budget for a given op: a per-op
`overrides` entry wins if present, else the manifest falls back to the op's `OpClass`-tier
default (`defaults.COMPUTE_ONLY` / `defaults.MUTATING`).

**`_provisional` — what it means and who reads it.** C9 (the Phase-0 measurement pass, qsub-01
plan) has RUN: it measured `defaults.COMPUTE_ONLY` and every entry in `overrides` from real per-op
latency distributions and stripped their `"_provisional": true` markers. `defaults.MUTATING` still
carries the marker, and that is deliberate, not leftover — MUTATING was define-only in wave-1
(qsub-01 AC7) and remains unmeasured. The manifest's own `_phase0_baseline.note` records this.

2026-09-24 (docs/plans/2026-09-12-memo-send-enrolled-in-the-composition-gate.md, C1): a second
class of override exists since C9, distinct from a Phase-0-measured one. A spawn-count-only
override's `target_ms`/`tolerance` copy the tier default verbatim (provisional, for a MUTATING
op) only to satisfy `_validated_budget`'s required shape, and its `_rationale` says so by citing
`_validated_budget` in words. Such an override's ABSENCE of `_provisional` is NOT a measurement
claim -- its timing is exactly as settled as the tier default it copies, no more. Nine overrides
already joined this class before `memo.send` did (census: `state/mise-inventory/` per-plan
census row 9 for this plan); this paragraph names the class by its `_rationale` marker rather
than listing keys, since the list moves.

`resolve_budget()` does NOT branch on `_provisional`, by design: it is measurement-state metadata
for readers of the manifest, not an input to budget resolution. Its live readers are
`tests/test_budget.py` (`test_provisional_flags_match_phase0_measurement_state` asserts MUTATING
carries it and COMPUTE_ONLY does not; `test_measured_overrides_carry_no_provisional_marker` asserts
no override carries it) and `ipc.py`'s `_MAX_DECLARED_TOUCH_PATHS` headroom analysis, which cites it
as the reason to treat the 300ms MUTATING target as not yet settled (DR-276).

**Negative spec:** do not delete `_provisional` as dead metadata. It was assessed as unread under
op-proportionality cluster C-17 and the assessment was refuted — see
`state/roadmap/op-proportionality/OVERVIEW.md` § *A flag nothing reads*. It is the only
machine-checked record of which tier is still unmeasured. Do not treat a `_provisional` value as
SLA-authoritative.

**`defaults.HOOK_PORT` — the per-tool-call hook tier.** It exists in the manifest today and is
measured in process time, on the warm path only (DR-315's resident engine). It has no `OpClass`
member — `OpClass` classifies write semantics (authz), not a latency tier — so it is reached only
by naming it explicitly: `resolve_budget(op, "HOOK_PORT")`. It carries `_provisional: true` until
a real hook op has been measured warm end-to-end (ehms-05/06/07); see the `_provisional` section
above for what that marker does and does not mean. **`manifest["overrides"]` entries are
tier-blind** — `resolve_budget` checks `overrides[op]` before consulting any tier, keyed by op
name only, so a `hooks.*` override would win over `HOOK_PORT` silently for any op it covers; no
such override exists today (see `test_budget.py`'s regression guard).

Spec backlink: pln-qsub-01-per-op-end-to-end-late-53ff10 § C3
Spec backlink: docs/plans/2026-09-23-hook-port-budget-tier-and-ehms-04-reconciliation.md § C1
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

    A malformed manifest override
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
