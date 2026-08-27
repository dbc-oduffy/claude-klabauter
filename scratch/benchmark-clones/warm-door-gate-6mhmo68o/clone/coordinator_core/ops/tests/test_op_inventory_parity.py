"""
coordinator_core.ops.tests.test_op_inventory_parity

Wires scripts/gen_ported_ops_fragment.py's existing `check_freshness()` into a
gate for the two committed op-inventory artifacts:

  - .github/op-inventory.json     (the sixth copy of the op shape, alongside
    OP_MODULE_MAP / _EAGER_OP_MODULES / OP_CLASSIFICATION / _OP_KEY_SCOPE /
    the live _REGISTRY — read by ops/gate_dimension_latency.py as its
    op -> module-path source).
  - .github/ported-ops-paths.txt  (the eighth surface — a module-PATH
    projection, not an op-KEY one, so the AC13 sweep's key-intersection
    detector cannot see it; found instead by a module-path-intersection
    pass against set(OP_MODULE_MAP.values())).

This is deliberately a THIN wire, not a second parity story: both artifacts
already have a generator (`scripts/gen_ported_ops_fragment.py`), a writer
(`write_artifacts()`), and a drift oracle (`check_freshness()`), and that
oracle is already exercised once in `test_gen_ported_ops_fragment.py`. This
module exists so drift on either artifact fails the suite under a name that
reads as a gate ("parity") rather than as one assertion buried inside the
generator's own broader test file — a hand-written map-vs-inventory diff
here would be a THIRD consistency story over the same file and is
deliberately not what this does.

Spec backlink: state/dispatch-briefs/2026-08-22-the-import-path-costs-nothing/C14.md
Widens:        docs/plans/2026-08-22-the-import-path-costs-nothing.md
Exercises:     scripts/gen_ported_ops_fragment.py :: check_freshness()
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import scripts.gen_ported_ops_fragment as gpof  # noqa: E402


def test_op_inventory_and_ported_ops_paths_are_fresh():
    """Gate: .github/op-inventory.json and .github/ported-ops-paths.txt must
    match a live `discover_records()` regeneration. Before this chunk's fix,
    this failed for real reasons (both artifacts were stale against
    `@register_op` call sites added since they were last regenerated) — this
    test is what turns that fact into a commit-time failure instead of a
    silent drift nobody notices until `ops/gate_dimension_latency.py` reads a
    stale op -> module-path mapping from disk."""
    problems = gpof.check_freshness()
    assert problems == [], (
        "op-inventory artifacts are stale — run "
        "`python scripts/gen_ported_ops_fragment.py --write` and commit the "
        f"result. Drift: {problems}"
    )
