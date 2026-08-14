"""
test_contract_version_single_source — guard that CONTRACT_VERSION has exactly
one literal source.

`emit_schema.py` owns the literal (dependency-free leaf module, must stay
independently importable as the `coordinator-cockpit-emit-schema`
console-script entrypoint regardless of `__init__.py`'s registry-wiring
state); `__init__.py` re-exports it via a PEP 562 module-level
`__getattr__` (lazy), NOT a top-of-file `from .emit_schema import
CONTRACT_VERSION` — see that `__getattr__`'s own comment for why an eager
import there collides with DoE's `python -m
coordinator_core.contract.cockpit_schema.emit_schema` invocation shape
(`regen-cockpit-schema.py:208`) and produces a spurious `RuntimeWarning`
on their release-critical regeneration console. Unlike
`test_committed_emit_drift.py::test_committed_bundle_version_matches_contract_version`
(which is `@skip_no_schema`-gated on DoE's committed schema dir being
present), this test asserts identity/object-equality between the two
importable names directly and needs no external schema dir — it ALWAYS
runs, so a reintroduced second literal cannot silently slip past a
schema-dir-absent CI run the way the skip-gated drift test can.

Spec backlink: cross-repo/inbox flag from claude-central-em, "two copies of
a version string with no guard" (2026-07-21).
Negative-spec: this test does not touch
`coordinator_core/ops/emit_artifact_shape_contract.py`'s "1.17.0" (a
DIFFERENT, unrelated contract version — not a desync target) or the
TypeScript `src/index.ts` CONTRACT_VERSION checked by
`coordinator_core/contract/cockpit_schema/tests/test_verify_superseded_retirement.py`
(that TS tree is being retired by a separate workstream).
"""
from __future__ import annotations

import coordinator_core.contract.cockpit_schema as pkg
from coordinator_core.contract.cockpit_schema.emit_schema import (
    CONTRACT_VERSION as emit_schema_version,
)


def test_contract_version_is_a_single_object():
    # `pkg.CONTRACT_VERSION` (attribute access, not a top-of-file `from
    # pkg import CONTRACT_VERSION`) is what exercises the PEP 562
    # `__getattr__` lazy re-export path — proving __init__.py resolves to
    # emit_schema.py's SAME binding on demand rather than redeclaring an
    # equal-but-independent literal that could silently drift later.
    assert pkg.CONTRACT_VERSION is emit_schema_version


def test_contract_version_matches_semver_shape():
    parts = pkg.CONTRACT_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_unknown_attribute_still_raises_attribute_error():
    # __getattr__ must not become a silent catch-all for typos/renames —
    # confirms the negative branch of the PEP 562 hook still fails loud.
    import pytest

    with pytest.raises(AttributeError):
        pkg.THIS_ATTRIBUTE_DOES_NOT_EXIST
