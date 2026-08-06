"""
test_committed_emit_drift — guard that example-doctrine-repo's committed `schema/*.json` matches
a fresh emit from THIS package's pydantic source.

Pytest port of example-doctrine-repo `coordinator/cockpit-contract/test/committed-emit-drift.test.ts`.
THE T4e parity oracle (recipe § 4) — the pre-delete gate for the Zod source.

Failure class guarded: a merge or hand-edit leaves example-doctrine-repo's committed
`schema/*.json` out of sync with this package's pydantic source, with no test
failing. On divergence, the assertion message names the out-of-sync files.

Coverage is driven entirely by `ENTITY_SCHEMAS` (`coordinator_core.contract.cockpit_schema`):
every key in that dict gets a fresh-vs-committed schema-file diff below, with no per-entity
code in this module.

`CommitClosure` (docs/plans/2026-07-17-commit-closure-emission-fact.md § C2/AC2) is
DELIBERATELY EXCLUDED from `ENTITY_SCHEMAS` — this is an architectural decision, not a
pending follow-up. `CommitClosure` is claude-klabauter-net-new with no example-doctrine-repo `index.ts` counterpart;
registering it would (a) break the index.ts-verbatim-port invariant `ENTITY_SCHEMAS` exists
to preserve, and (b) force a cross-repo committed example-doctrine-repo-side `commit-closure.schema.json` that
this drift gate would then demand — exactly the store-less, no-cross-repo-schema-landing
posture this emission leg is designed to avoid. `CommitClosure` is validated shape-based,
per-repo, instead (`coordinator_core/contract/cockpit_schema/validate.py:440-467`). This gate
therefore does not exercise `CommitClosure`, by design, until/unless it is promoted to a
Example-doctrine-repo-contract entity. Tracked: `state/improvement-queue/2026-07-17-commit-closure-entity-intentionally-excl-328d5281c0d2.yaml`.
No per-entity change belongs in this file; if `CommitClosure` is ever promoted, registering
it in `ENTITY_SCHEMAS` is sufficient for this generic loop to pick it up.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
"""
from __future__ import annotations

import json

from coordinator_core.contract.cockpit_schema import CONTRACT_VERSION, ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas
from coordinator_core.contract.cockpit_schema.tests.conftest import skip_no_schema


@skip_no_schema
def test_every_committed_schema_file_matches_fresh_emit(tmp_path, schema_dir):
    fresh_dir = tmp_path / "fresh-emit"
    emit_schemas(ENTITY_SCHEMAS, out_dir=fresh_dir)

    committed_files = sorted(p.name for p in schema_dir.glob("*.schema.json"))
    fresh_files = sorted(p.name for p in fresh_dir.glob("*.schema.json"))

    # File-set symmetry: catch added/removed entities that weren't re-emitted.
    assert fresh_files == committed_files, (
        "fresh emit produced a different set of schema files than are committed in example-doctrine-repo — "
        "run emit_schema.py and commit the result in coordinator/cockpit-contract/schema/."
    )

    drifted = []
    for filename in committed_files:
        committed_content = (schema_dir / filename).read_text(encoding="utf-8")
        fresh_content = (fresh_dir / filename).read_text(encoding="utf-8")
        if committed_content != fresh_content:
            drifted.append(filename)

    assert drifted == [], (
        "committed example-doctrine-repo cockpit-contract schema/*.json is out of sync with the claude-klabauter "
        f"pydantic source.\nOut-of-sync files: {', '.join(drifted)}\n"
        "Remediation: re-run emit_schema.py and commit the result in example-doctrine-repo's "
        "coordinator/cockpit-contract/schema/."
    )


@skip_no_schema
def test_committed_bundle_version_matches_contract_version(schema_dir):
    bundle = json.loads((schema_dir / "cockpit-contract.schema.json").read_text(encoding="utf-8"))
    assert bundle["version"] == CONTRACT_VERSION
