"""
coordinator_core.contract.cockpit_schema.tests

pytest port of coordinator-claude `coordinator/cockpit-contract/test/*.ts` (vitest, 10 files) +
`test/verify-superseded-retirement.sh` (Port of: coordinator-claude 7cca4d4c, 2026-07-16) —
see conftest.py for the shared coordinator-claude-clone resolution + fixture-loading helpers
every module in this package uses.

Spec backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md § T4e
Recipe: coordinator-claude scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-T4e-cockpit-contract.md § T4e-d
"""
