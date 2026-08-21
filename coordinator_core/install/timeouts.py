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
3. **No hot path reaches it.** No session start, no commit ceremony, no op
   dispatch. Grep before admitting a site: a caller on any of those paths
   disqualifies it outright.

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

#: A third-party installer script fetched over the network (`curl -fsSL ...`),
#: piped into a shell the vendor supplies. Network leg only — the execution
#: that follows is bounded by `TOOLCHAIN_BOOTSTRAP_SECS`.
NETWORK_FETCH_SECS = 60

#: `python -m venv` against a resolved base interpreter. Local disk plus
#: ensurepip; the ceiling is a wedged-child guard, not an expectation.
VENV_CREATE_SECS = 120

#: A post-provision import probe that proves the environment it just built is
#: usable. Bounded by the heaviest declared import (`torch`, seconds cold),
#: never by a package manager.
HEALTH_PROBE_SECS = 120

#: `brew uninstall` of a guarded platform Python, reached only after explicit
#: interactive consent. Removal, not installation, so it does no network work.
PLATFORM_UNINSTALL_SECS = 120

#: `git clone` of a sibling repository. Network plus checkout of a tree whose
#: size we do not control.
REPO_CLONE_SECS = 300

#: A version-manager / toolchain bootstrap (`brew install fnm`, the vendor's
#: `bash -s` installer). Downloads and unpacks a toolchain we do not ship.
TOOLCHAIN_BOOTSTRAP_SECS = 300

#: One `pip install` invocation against a target interpreter. Network plus
#: wheel builds for whichever declared deps have no wheel for this platform.
PACKAGE_INSTALL_SECS = 600

#: One generic install-phase subprocess in the cold maximalist orchestrator,
#: whose heaviest member is a `pip install` — so it shares that ceiling
#: rather than inventing a second one.
PHASE_SUBPROCESS_SECS = 600

#: `brew install` of a large formula (node). Homebrew compiles from source
#: when no bottle matches the platform, which is the case this number exists
#: for; a bottled install is a small fraction of it.
PLATFORM_PACKAGE_INSTALL_SECS = 900

#: A full `scripts/setup.py` run, driven end-to-end by the Windows install
#: acceptance harness. Bounds the whole chain, so it is at least the largest
#: single leg it can reach.
FULL_INSTALL_RUN_SECS = 900

#: `uv lock` — resolving ~250 packages across three platforms against a
#: possibly-cold uv cache.
DEPENDENCY_LOCK_SECS = 1800

#: `uv sync --frozen` — installing that resolved set, including a multi-GB
#: cu130 torch build. The largest member of the family, and the one whose
#: cost is most obviously not ours.
DEPENDENCY_SYNC_SECS = 3600

#: No member may exceed this. Not a budget in DR-349's sense (these sites are
#: carved out of that budget); a ceiling on how far the carve-out itself may
#: stretch, so a future admission has a number to argue against rather than
#: an open field.
FAMILY_CEILING_SECS = 3600

#: The audit table. Every member above appears here with the provisioning
#: work it bounds; `tests/test_install_timeout_family.py` asserts the two
#: stay in step, so a constant cannot be added without describing itself.
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
