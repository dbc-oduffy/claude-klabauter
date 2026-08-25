"""
Shared fixtures for the cockpit_schema pytest suite.

Every DoE-clone-backed test in this package (fixtures/*.json, schema/*.json,
conformance/**, provenance-parity's sibling artifact-shape-contract schema,
verify-superseded-retirement's CLAUDE.md/wiki greps) resolves the DoE local
clone the SAME way `coordinator_core.ops.emit.doe_drift` already does for the
strang-02 drift-check suite — `resolve_doe_clone()` — and skips gracefully
(not fail-loud) when the clone is absent, matching that module's own
`_DOE_AVAILABLE` pattern (test_doe_drift.py). This package does not co-vendor
DoE's fixtures/schema/test-data — it is a READ, at test-time, of the DoE
clone's HEAD (same anti-drift posture as `read_doe_fixture`).

The clone being present does NOT guarantee any given subdir under it still
exists — DoE commit 7cca4d4c ("delete redundant cockpit-contract TS mirror +
retire vestigial toolchain") removed `cockpit-contract/fixtures/` and
`cockpit-contract/src/` (the TS mirror) wholesale while keeping the clone
itself and `cockpit-contract/schema/` (the byte-frozen contract). Skip
guards therefore key on the PRESENCE OF THE SPECIFIC REQUIRED ARTIFACT
(`FIXTURES_AVAILABLE`, `SCHEMA_AVAILABLE`, `TS_MIRROR_AVAILABLE`), not merely
`DOE_AVAILABLE` (clone-root presence) — a test that needs `fixtures/` must
skip when `fixtures/` is gone even though the clone itself resolved fine.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292 § T4e
Recipe: DoE-claude scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-T4e-cockpit-contract.md § T4e-d
Parity oracle: DoE-claude coordinator/cockpit-contract/test/*.ts (vitest, retired 7cca4d4c — historical parity
reference only, no longer present at that path)
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from coordinator_core.ops.emit.doe_drift import DoeResolveError, resolve_doe_clone

# ---------------------------------------------------------------------------
# DoE clone resolution — module-level (not fixture-scoped) so parametrized
# collection (round-trip's per-entity fixture loop) can branch on it BEFORE
# pytest collection needs a live fixture dependency.
# ---------------------------------------------------------------------------


def _try_resolve_doe_clone() -> Path | None:
    try:
        return resolve_doe_clone()
    except DoeResolveError:
        return None


DOE_CLONE: Path | None = _try_resolve_doe_clone()
DOE_AVAILABLE: bool = DOE_CLONE is not None

skip_no_doe = pytest.mark.skipif(
    not DOE_AVAILABLE, reason="DoE clone not available on this machine (repos.doe_claude unset)"
)

if DOE_AVAILABLE:
    COCKPIT_CONTRACT_DIR = DOE_CLONE / "coordinator" / "cockpit-contract"
    FIXTURES_DIR = COCKPIT_CONTRACT_DIR / "fixtures"
    SCHEMA_DIR = COCKPIT_CONTRACT_DIR / "schema"
    TS_MIRROR_DIR = COCKPIT_CONTRACT_DIR / "src"
else:
    COCKPIT_CONTRACT_DIR = FIXTURES_DIR = SCHEMA_DIR = TS_MIRROR_DIR = None  # type: ignore[assignment]

# Required-ARTIFACT presence, not merely clone presence — see module
# docstring. The clone resolving successfully says nothing about whether a
# given subdir under `cockpit-contract/` still exists on this HEAD.
FIXTURES_AVAILABLE: bool = DOE_AVAILABLE and FIXTURES_DIR.is_dir()
SCHEMA_AVAILABLE: bool = DOE_AVAILABLE and SCHEMA_DIR.is_dir()
TS_MIRROR_AVAILABLE: bool = DOE_AVAILABLE and TS_MIRROR_DIR.is_dir()

# RETIRED, NOT MISCONFIGURED — and retired PARTIALLY, which is the part that is
# easy to get wrong. DoE 7cca4d4c5 ("delete redundant cockpit-contract TS mirror
# + retire vestigial toolchain", 2026-07-16) removed exactly two subdirectories,
# `fixtures/` and `src/`. The parent `cockpit-contract/` survives, and so does
# `schema/` — so SCHEMA_AVAILABLE is True and `skip_no_schema` below does NOT
# fire; its tests are live coverage and are not part of this retirement.
#
# fixtures/ and src/ are absent on EVERY machine and no clone repair brings them
# back. The two reason strings say so outright: the earlier "DoE clone absent, or
# fixtures/ retired" phrasing could not tell an operator whether to go fix their
# checkout, so ~74 permanent skips read as transient environmental noise on every
# run.
#
# These marks are kept rather than deleted, and the tests with them: they are the
# documented historical parity oracle for the cross-language leg (Python entities
# vs the TS/Zod mirror). What they guarded that still MATTERS — the emitted bytes
# CLAUDE.md names as load-bearing for DoE's release capability — is covered by
# tests that actually run: test_committed_emit_drift, test_emit_line_endings,
# test_contract_version_single_source. `test_retired_cockpit_contract_stays_retired`
# in this package is the tripwire if the directory ever returns.
_RETIRED_AT = "DoE 7cca4d4c5 (2026-07-16)"

skip_no_fixtures = pytest.mark.skipif(
    not FIXTURES_AVAILABLE,
    reason=(
        f"DoE cockpit-contract fixtures/ RETIRED at {_RETIRED_AT} — permanently "
        "unavailable on every machine, not a local clone problem; nothing to repair"
    ),
)
skip_no_schema = pytest.mark.skipif(
    not SCHEMA_AVAILABLE,
    reason="DoE cockpit-contract schema/ not available on this machine (DoE clone absent or schema/ missing)",
)
skip_no_ts_mirror = pytest.mark.skipif(
    not TS_MIRROR_AVAILABLE,
    reason=(
        f"DoE cockpit-contract TS mirror (src/) RETIRED at {_RETIRED_AT} — permanently "
        "unavailable on every machine, not a local clone problem; nothing to repair"
    ),
)


def load_fixture(name: str) -> dict[str, Any]:
    """Load `fixtures/<name>.json` from the DoE clone. Caller must be
    FIXTURES_AVAILABLE-gated (skip_no_fixtures) — raises FileNotFoundError
    otherwise."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


def load_schema(name: str) -> dict[str, Any]:
    """Load `schema/<name>.schema.json` from the DoE clone (committed,
    Zod-emitted). Caller must be SCHEMA_AVAILABLE-gated (skip_no_schema)."""
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text())


@pytest.fixture(scope="session")
def doe_clone() -> Path:
    if not DOE_AVAILABLE:
        pytest.skip("DoE clone not available on this machine")
    return DOE_CLONE  # type: ignore[return-value]


@pytest.fixture(scope="session")
def fixtures_dir(doe_clone: Path) -> Path:
    if not FIXTURES_AVAILABLE:
        pytest.skip(
            "DoE cockpit-contract fixtures/ not available (retired — 2026-07-16 C3 7cca4d4c)"
        )
    return FIXTURES_DIR  # type: ignore[return-value]


@pytest.fixture(scope="session")
def schema_dir(doe_clone: Path) -> Path:
    if not SCHEMA_AVAILABLE:
        pytest.skip("DoE cockpit-contract schema/ not available")
    return SCHEMA_DIR  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Zod .parse()/.safeParse() twins — pydantic BaseModel subclasses expose
# model_validate() directly; the one non-BaseModel entity (ScopedEmission,
# a Field(discriminator=...) Annotated Union) needs a TypeAdapter. These two
# helpers dispatch on that shape so every test can write `zod_parse(entity,
# data)` / `zod_safe_parse_ok(entity, data)` uniformly, mirroring the DoE
# suite's `Entity.parse(...)` / `Entity.safeParse(...).success` call shape.
# ---------------------------------------------------------------------------


def zod_parse(entity: Any, data: Any) -> Any:
    """Twin of Zod's `Entity.parse(data)` — raises on invalid input,
    returns the validated/coerced instance on success."""
    if isinstance(entity, type) and issubclass(entity, BaseModel):
        return entity.model_validate(data)
    return TypeAdapter(entity).validate_python(data)


def zod_safe_parse_ok(entity: Any, data: Any) -> bool:
    """Twin of Zod's `Entity.safeParse(data).success`."""
    try:
        zod_parse(entity, data)
        return True
    except ValidationError:
        return False


def zod_dump(entity: Any, instance: Any) -> Any:
    """JSON-round-trippable dict twin of Zod's implicit
    `JSON.parse(JSON.stringify(parsed))` — pydantic's `model_dump(mode="json")`
    is the equivalent serialize-for-wire projection."""
    if isinstance(instance, BaseModel):
        return instance.model_dump(mode="json")
    return TypeAdapter(entity).dump_python(instance, mode="json")
