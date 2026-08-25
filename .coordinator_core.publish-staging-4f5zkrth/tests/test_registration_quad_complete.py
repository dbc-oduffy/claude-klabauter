"""
coordinator_core.tests.test_registration_quad_complete — guards the
`ipc._REGISTRY` vs `authz.classification.OP_CLASSIFICATION` population gap.

Purpose: C17 (docs/plans/2026-08-20-a-refusal-cannot-exit-zero.md) closed a
persistent 14-op gap between the two populations. This test asserts the set
difference is empty so the gap cannot silently reopen as new ops are
registered without a classification entry.

`_REGISTRY` is 0 on a bare import and fills by import-time self-registration
— `import coordinator_core.ops` MUST run before reading it. `OP_CLASSIFICATION`
is a static dict and needs no such import. The two are separate populations;
neither is a proxy for the other.
"""

from __future__ import annotations

import coordinator_core.ops  # noqa: F401 — import-time self-registration
from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION


def test_every_registered_op_is_classified():
    gap = sorted(set(ipc._REGISTRY) - set(OP_CLASSIFICATION))
    assert gap == [], f"registered ops missing a classification entry: {gap}"
