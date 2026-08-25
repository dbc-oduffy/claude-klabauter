"""Contract bump vs. vendored-pin re-vendor — same-commit invariant guard.

Purpose: `validate.assert_version_consistency()` (the DSR-2026-06-23-4 silent-break guard)
already enforces makima's own `CONTRACT_VERSION` against the vendored schema bundle's
`.version` — but it only FIRES when an emit path actually runs, so a desync surfaces as an
incidental `ContractPinError` buried inside ~55 unrelated emit tests, with no single obvious
"the bundle is stale" signal. See the 2026-07-22 project-opticon-em cross-repo memo
(cross-repo/inbox/2026-07-22-project-opticon-em-cockpit-contract-version-desync-wedges-emit-cadence.md):
a CONTRACT_VERSION bump (2.20.0 -> 2.21.0, `87122daa`) landed without a same-commit re-vendor,
which wedged `emit.cadence` fleet-wide for ~2 days while `/workday-complete`'s best-effort
Step 10.6 silently swallowed the failure in every consumer repo.

This module is the single, obviously-named place that check lives — a bump without a
same-commit re-vendor turns red HERE, not two commits and 55 test failures later.

Skips only when the vendored pin is entirely absent (fresh clone, `requires_vendor_pin` —
see conftest.py); a present-but-mismatched pin is exactly the failure this test exists to
catch and must never skip.

Spec backlink: cross-repo/inbox/2026-07-22-project-opticon-em-cockpit-contract-version-desync-wedges-emit-cadence.md
"""

from __future__ import annotations

import pytest

from coordinator_core.contract.cockpit_schema import CONTRACT_VERSION
from coordinator_core.ops.emit import validate


@pytest.mark.usefixtures("requires_vendor_pin")
def test_contract_version_matches_vendored_pin():
    """CONTRACT_VERSION and the vendored bundle's .version must agree, always.

    Delegates to `validate.assert_version_consistency()` so this test and the production
    guard it exists to spotlight can never drift apart in what they check — a failure here
    raises the SAME `ContractPinError`, whose message already states the remediation
    (re-vendor from the DoE cockpit-contract-release tag via
    `python bin/makima-revendor-cockpit-contract.py`, or bump CONTRACT_VERSION to match).
    """
    schema_version = validate.assert_version_consistency()
    assert schema_version == CONTRACT_VERSION, (
        f"cockpit-contract version desync — makima's own CONTRACT_VERSION={CONTRACT_VERSION} "
        f"but the vendored schema bundle .version={schema_version}. A CONTRACT_VERSION bump "
        "must land in the SAME commit as the vendored bundle re-vendor (or vice versa) — "
        "remediation: re-vendor from the DoE cockpit-contract-release tag "
        "(python bin/makima-revendor-cockpit-contract.py)."
    )
