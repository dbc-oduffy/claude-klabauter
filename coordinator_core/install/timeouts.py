"""The `install` timeout family — the single named home for DR-349's
install-chain provisioning carve-out.

DR-349 § Carve-outs grants exactly one exemption to this repo's timeout
budget for the install chain: *"Install-chain provisioning — venv creation,
`git clone` of a sibling, package installation. Runs once at install, never
on the session or commit hot path."* That record also states the membership
rule: **a carve-out is named in the record or it does not exist; satisfying a
rationale is not membership.** This module is where the naming happens, so a
reader can audit the whole carve-out as one policy table instead of
reconstructing it from literals scattered across a dozen installers.

Three properties every member holds, and which a candidate must hold to be
admitted here:

1. **The bounded work is not claude-klabauter's compute.** It is a third-party package
   manager, a platform toolchain, or a network leg. No rewrite of ours makes
   `uv sync` faster, so the number is not marking a defect of ours.
2. **Frequency is once per machine**, not once per op. The box-damage product
   (frequency x cost) is near zero even at `DEPENDENCY_SYNC_SECS`.
3. **No hot path reaches it.** No session start, no commit ceremony. Grep
   before admitting a site: a caller on either path disqualifies it outright.
   Op-dispatch reachability alone does not: `REPO_CLONE_SECS` bounds
   `clone_sibling_repo._clone_idempotent`, reachable via
   `ops/repo_bootstrap.py`'s `repo.clone_and_register`
   (`@register_op("install.clone_idempotent")`) — an explicit, reasoned
   exception to the letter of this property, admitted because it still holds
   properties 1 and 2 and dispatch does not make it a hot path: idempotent,
   once per machine, never called from session start or commit ceremony.

Negative spec — what this module is NOT:

- **Not a place to park a slow op of ours.** A bound on claude-klabauter's own code is
  governed by DR-344's kill bar (>1s deleted and rebuilt; 500ms-1s brought
  under 500ms or killed), and importing a name from here does not convert it
  into provisioning. `install/first_run.py :: provision_stamped_engine`
  bounds a `coordinator/bin/publish.py` round — our compute, measured — and is
  deliberately absent from this table for exactly that reason; see that
  function's own budget constant.
- **Not a general timeout vocabulary.** Nothing outside the install chain
  (`coordinator_core/install/`, `scripts/`) may import these names.
  `tests/test_install_timeout_family.py` enforces the quarantine.
- **Not a dial.** No environment variable resolves against these values
  (DR-349 § 3). Changing one is a reviewable line in a diff, which is the
  point.

Values are carried over unchanged from the sites they replace: this module
made the numbers auditable, it did not retune them. Raising one is a policy
change to the whole family, not a local fix — and for the package-manager
members the honest first question is whether the *machine* is the problem,
since none of them bound code we own.

Related: `docs/decisions/DR-349-one-budget-governs-every-constructed-op.md`,
`docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md`,
`docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md` § G11.
"""

from __future__ import annotations

from typing import Dict

#: that follows is bounded by `TOOLCHAIN_BOOTSTRAP_SECS`.
NETWORK_FETCH_SECS = 60

VENV_CREATE_SECS = 120

HEALTH_PROBE_SECS = 120

PLATFORM_UNINSTALL_SECS = 120

REPO_CLONE_SECS = 300

TOOLCHAIN_BOOTSTRAP_SECS = 300

PACKAGE_INSTALL_SECS = 600

PHASE_SUBPROCESS_SECS = 600

PLATFORM_PACKAGE_INSTALL_SECS = 900

FULL_INSTALL_RUN_SECS = 900

DEPENDENCY_LOCK_SECS = 1800

DEPENDENCY_SYNC_SECS = 3600

FAMILY_CEILING_SECS = 3600

MEMBERS: Dict[str, int] = {
    "NETWORK_FETCH_SECS": NETWORK_FETCH_SECS,
    "VENV_CREATE_SECS": VENV_CREATE_SECS,
    "HEALTH_PROBE_SECS": HEALTH_PROBE_SECS,
    "PLATFORM_UNINSTALL_SECS": PLATFORM_UNINSTALL_SECS,
    "REPO_CLONE_SECS": REPO_CLONE_SECS,
    "TOOLCHAIN_BOOTSTRAP_SECS": TOOLCHAIN_BOOTSTRAP_SECS,
    "PACKAGE_INSTALL_SECS": PACKAGE_INSTALL_SECS,
    "PHASE_SUBPROCESS_SECS": PHASE_SUBPROCESS_SECS,
    "PLATFORM_PACKAGE_INSTALL_SECS": PLATFORM_PACKAGE_INSTALL_SECS,
    "FULL_INSTALL_RUN_SECS": FULL_INSTALL_RUN_SECS,
    "DEPENDENCY_LOCK_SECS": DEPENDENCY_LOCK_SECS,
    "DEPENDENCY_SYNC_SECS": DEPENDENCY_SYNC_SECS,
}

__all__ = [*MEMBERS, "FAMILY_CEILING_SECS", "MEMBERS"]
