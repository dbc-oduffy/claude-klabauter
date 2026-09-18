"""Shared, single-owner export of the D2 ratio-tier boundaries.

Purpose: `tier_boundaries_for(surface)` is the ONE seam through which
DoE-claude's Layer 1 (the ratio guard, C8/C9a/C9b) reads Layer-2 ceiling
data. It reads C3a's baseline `aspirational_ceiling` per surface (measured
against DoE-claude's own corpus and committed there, at
`<doe_root>/coordinator/tests/baselines/doctrine-surface-weight.json`) and
returns D2's three bands (under ceiling -> 2:1, 1-3x -> 5:1, over 3x -> 10:1)
as boundary values, each paired with its `credit_scope` (PM ruling,
2026-08-13): `"surface"` at the 2:1 tier, `"file"` at the 5:1 and 10:1
tiers. A tier is two properties -- ratio and credit scope -- not one;
callers must read both off this single lookup rather than re-deriving
credit scope from the ratio.

Deliberately NOT a pytest module. A hook importing from `coordinator/tests/`
would be a hooks-plane -> test-plane dependency with no precedent in that
tree, and would make a production guard's behaviour hostage to the test
tree's import health on the hook hot path (D6, C3b review amendment). This
module is the shared non-test owner both planes read: C3b's own test
(`test_tier_boundaries_match_d2_table`) and the ratio guard (C8) as
consumers -- C8 must never re-derive or re-read the baseline JSON itself.

Negative-spec: this module does NOT gate anything -- it only maps a
surface's already-recorded `aspirational_ceiling` onto D2's band edges. It
has no comparator that decides pass/fail for any file; that job belongs
entirely to Layer 1 (the ratio guard), which is the sole consumer of this
export's output for pricing decisions.

Path resolution: "doctrine asset" class (§ Path resolution,
docs/plans/2026-09-18-doe-holds-no-scripts.md), resolved through the plugin
root exactly as that section names: `coordinator_core.warm.caller_context ::
resolve_caller_context`, falling back to
`coordinator_core.subagent_sandbox.provision_report :: resolve_plugin_root`'s
own three-rung ambient probe -- the same pair this module's coordinator/bin/
siblings from the same chunk (`generate-doctrine-surfaces.py`,
`generate-doctrine-surface-split.py`) use. The DoE original derived
`REPO_ROOT` from `Path(__file__).resolve().parents[2]` -- the
DoE-claude@b644d5a9 lesson this wave exists to fix: that resolved correctly
only so long as the file stayed inside DoE-claude's own `coordinator/lib/`.
This module has no claude-klabauter-side consumer today (every importer is a
DoE-resident hook, per this chunk's arrival record); it lands here because
DoE's `coordinator/lib` is not published -- the mirror's `lib` is sourced
from claude-klabauter -- so every published citation of it was already dangling for a
consumer before this move (plan intro).

Spec: docs/plans/2026-08-13-doctrinal-surface-weight-ratchet.md, chunk C3b
(§ D2, D6, AC A20).
Arrived from DoE-claude coordinator/lib/doctrine_surface_tiers.py
(docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W3-C6).
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.warm.caller_context import resolve_caller_context


def _baseline_path() -> Path:
    """`<doe_root>/coordinator/tests/baselines/doctrine-surface-weight.json`
    -- a DoE-claude test-tree asset this module reads but does not own or
    write. See module docstring § Path resolution."""
    plugin_root = resolve_caller_context().plugin_root
    if plugin_root is None:
        raise RuntimeError(
            "doctrine_surface_tiers: cannot resolve the DoE-claude plugin root -- "
            "resolve_caller_context().plugin_root returned no result. Set "
            "CLAUDE_PLUGIN_ROOT, or register the coordinator-claude plugin "
            "install / .doe-root pointer (see resolve_plugin_root())."
        )
    return Path(plugin_root) / "tests" / "baselines" / "doctrine-surface-weight.json"


def _load_baseline() -> dict:
    return json.loads(_baseline_path().read_text(encoding="utf-8"))


def admission_cap_for(surface: str) -> int:
    """The new-file admission threshold (bytes) for `surface` (D3): a file
    with no history has nothing to cut against, so it is refused only for
    arriving larger than this value. The wiki carries its own separately
    p90-derived `admission_cap` in the baseline (53,858 B, distinct from
    its `aspirational_ceiling`, D2's wiki carve-out); every other surface
    admits on its own `aspirational_ceiling` directly. Single owner of
    this read -- callers (the ratio guard, C8/C9a) must not read the
    baseline JSON themselves or hardcode either value."""
    baseline = _load_baseline()
    surfaces = baseline["surfaces"]
    if surface not in surfaces:
        raise KeyError(
            f"{surface!r} is not a surface this baseline measures -- "
            f"known surfaces: {sorted(surfaces)}"
        )
    cap = surfaces[surface].get("admission_cap")
    if cap is not None:
        return cap
    return tier_boundaries_for(surface)["ceiling"]


def tier_boundaries_for(surface: str) -> dict:
    """Return D2's three ratio tiers for `surface`, each paired with its
    credit scope.

    Reads `aspirational_ceiling` for `surface` from C3a's baseline
    (REPORTED ONLY there -- never gated -- and read here for exactly this
    tier-boundary export, D2/D6/A20's sole permitted consumer of the value
    besides the baseline writer itself).

    Returns:
        {
          "ceiling": <int>,           # C's aspirational_ceiling for this surface
          "tiers": [
            {"max": <C>,        "ratio": 2, "credit_scope": "surface"},
            {"max": <3*C>,      "ratio": 5, "credit_scope": "file"},
            {"max": None,       "ratio": 10, "credit_scope": "file"},
          ],
        }

    Band selection (caller's job, not this function's): a file's pre-edit
    size in [0, C) is the first ("surface") tier, [C, 3C) the second, [3C,
    inf) the third -- half-open intervals closing both edges, selected on
    the file's size BEFORE the edit on both Layer-1 legs (D2).
    """
    baseline = _load_baseline()
    surfaces = baseline["surfaces"]
    if surface not in surfaces:
        raise KeyError(
            f"{surface!r} is not a surface this baseline measures -- "
            f"known surfaces: {sorted(surfaces)}"
        )
    ceiling = surfaces[surface]["aspirational_ceiling"]
    return {
        "ceiling": ceiling,
        "tiers": [
            {"max": ceiling, "ratio": 2, "credit_scope": "surface"},
            {"max": ceiling * 3, "ratio": 5, "credit_scope": "file"},
            {"max": None, "ratio": 10, "credit_scope": "file"},
        ],
    }
