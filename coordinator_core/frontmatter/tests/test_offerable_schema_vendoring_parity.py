"""Parity guard: `offerable: true` in the registry manifest implies a
vendored schema exists.

Companion to coordinator-claude's `test_every_ops_schema_has_vendored_counterpart`
(coordinator/tests/test_vendored_schema_version_parity.py) — that test
sweeps coordinator-claude's schema source directory; this one sweeps the manifest's
`offerable` flag from claude-klabauter's side, which is the surface
`validate_frontmatter_schema_deny.py` actually keys scaffold-offer/
shape-validation on since `b4c6df071` repointed `schemas_dir` at the
vendored corpus (coordinator_core/frontmatter/schemas/) instead of a live
Coordinator-claude checkout.

Was `pending_fix` from 2026-08-06 while twelve `offerable: true` doc types
had no vendored schema (completion-entry, decision, docs-check-sidecar,
goal, health-status, plan-coverage-check, prior-art-check, problem-set,
research-synthesis, review-sidecar, sizing-object,
strategic-self-description) — see
cross-repo/inbox/2026-08-06-coordinator-claude-em-twelve-doc-types-lost-write-enforcement-today.md
and state/audits/2026-08-06-offerable-types-without-vendored-schemas.md.
All twelve were vendored the same day from coordinator-claude's committed bytes at
`e24d88781` (via `git show <sha>:coordinator/schemas/<name>.schema.json`,
never a working-tree read — a working-tree copy would bake back in the very
live-tree coupling `b4c6df071` removed), so the mark is gone and this is a
live guard again. Do not re-add `pending_fix` to park a future regression:
a red here means a doc type is silently unenforceable at write time, which
is the condition this file exists to make loud.

Standing caveat, and the reason this guard is necessary but not sufficient:
it asserts that an offerable type HAS a vendored schema. It cannot assert
that a type WITHOUT one is legitimately excluded — that is coordinator-claude's
`DOE_ONLY_SCHEMAS`, whose own criterion ("validated solely by a live
write-guard path") was falsified in two commits four days apart, in two
repos, while its guarding test stayed green because it asserts roster
membership rather than the criterion behind the roster. Both directions
need testing and only one of them lives here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_CLAUDE_KLABAUTER_SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas"


def _resolve_manifest() -> Optional[dict]:
    try:
        doe_root = coordinator_doe_root()
    except Exception:  # noqa: BLE001 — resolution failure -> skip, not fail
        return None
    if not doe_root:
        return None
    manifest_path = Path(doe_root) / "coordinator" / "schemas" / "coordinator-registry.manifest.json"
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — resolution failure -> skip, not fail
        return None


def _offerable_schema_names() -> list[str]:
    manifest = _resolve_manifest()
    if manifest is None:
        return []
    names = {
        d["schemaName"]
        for d in manifest.get("docTypes", [])
        if d.get("offerable") is True and d.get("schemaName")
    }
    return sorted(names)


@pytest.mark.parametrize("schema_name", _offerable_schema_names() or [None])
def test_every_offerable_doc_type_has_vendored_schema(schema_name: Optional[str]) -> None:
    """`offerable: true` in coordinator-registry.manifest.json must imply a
    vendored schema at coordinator_core/frontmatter/schemas/<schemaName>.*
    — else `_match_schema` silently returns None for every write of that
    type and both the scaffold offer and shape validation go dark with no
    signal (the b4c6df071 regression this test exists to keep visible).
    """
    if schema_name is None:
        pytest.skip(
            "manifest unresolvable (no live coordinator-claude checkout on this machine) -- "
            "this parity guard needs one to enumerate offerable doc types"
        )
    candidates = [
        _CLAUDE_KLABAUTER_SCHEMAS_DIR / f"{schema_name}.schema.json",
        _CLAUDE_KLABAUTER_SCHEMAS_DIR / f"{schema_name}.yaml",
    ]
    assert any(c.is_file() for c in candidates), (
        f"{schema_name}: offerable: true in coordinator-registry.manifest.json but no "
        f"vendored schema at {candidates[0]} or {candidates[1]}. Scaffold offer and "
        "shape validation are silently inactive for this doc type on every write. Fix: "
        f"vendor the schema into {_CLAUDE_KLABAUTER_SCHEMAS_DIR}, or mark the manifest entry "
        "offerable: false with a stated excludeReason if it genuinely should not be "
        "offered/validated."
    )
