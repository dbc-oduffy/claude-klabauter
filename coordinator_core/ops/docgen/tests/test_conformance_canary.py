"""coordinator_core.ops.docgen.tests.test_conformance_canary — loud gate for every
skip-if-DoE-absent conformance LANE registered in ``LANES`` (C6, AC5; extended
2026-07-25 to also cover the ``contract_blocks``/``header_style`` lane).

Purpose: several test modules across ``coordinator_core`` are entirely
module-level ``skipif(DoE clone unavailable)`` — on a genuinely
consumer/CI-standalone install with no sibling DoE clone, the guarantees those
modules exist to prove degrade to a SILENT SKIP, not a failure. This repo is
explicitly published as its own standalone install-chain node, so
DoE-clone-absent is the *normal* consumer shape, not an edge case — a future
template/contract divergence in that environment would report green
(code-review finding, 2026-07-21, Finding 1 / D1 of the strang-12
doc-generation slice review).

This module is the loud counterpart: in any environment flagged "should have
the DoE clone" (CI, or a dev machine that opts in), a missing/unresolvable
clone here is a FAILURE, not a skip, FOR EVERY REGISTERED LANE. A
genuinely-clone-absent consumer install is the documented, deliberately-optional
lane and is still allowed to skip THIS canary too — see
``_environment_requires_doe_clone``. The gate is "loud when it should have run
and didn't," not "always require the clone everywhere."

Lane registration is DATA, not a hand-copied test function per lane (2026-07-25
generalization) — adding a third guarded lane is a one-line append to ``LANES``,
never a second copy of the escalation logic in ``_environment_requires_doe_clone``
or the parametrized test body below. Do NOT copy this module into a second
canary file for a new lane; append to ``LANES`` here instead — the escalation
logic must exist exactly once in the repo.

Registered lanes:
  - ``docgen-byte-identity``: ``test_c6_conformance.py`` and
    ``test_type_enum.py``'s ``TestAC4LiveConformance`` (~29 tests) — DoE-HEAD
    template byte-identity.
  - ``contract-blocks-header-style``:
    ``subagent_sandbox/tests/test_provision_report_contract_blocks_byte_identity.py``
    — proves the ``contract_blocks`` assembler's ``header_style``-aware
    extraction reproduces DoE's registered snippet content byte-for-byte.

Spec backlink: pln-strang-12-document-generation--75a7eb § C6 (AC5)
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

from coordinator_core.ops.emit.doe_drift import DoeResolveError, resolve_doe_clone
from coordinator_core.testing.doe_root import doe_root_and_present

_TRUTHY = {"1", "true", "yes"}


def _environment_requires_doe_clone() -> bool:
    """True in any environment that has opted into the strict conformance gate.

    Recognizes the conventional ``CI`` flag most CI systems set (``CI=true``/
    ``CI=1``) plus a dedicated ``CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE`` override for a
    dev machine that wants the loud check locally too. A machine/CI runner
    that sets NEITHER is the documented "sibling-clone-optional" lane and is
    free to skip — this is the one sanctioned, DOCUMENTED optional-skip case,
    not an ambient default that quietly swallows every install.
    """
    if os.environ.get("CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE", "").strip().lower() in _TRUTHY:
        return True
    return os.environ.get("CI", "").strip().lower() in _TRUTHY


def _resolve_docgen_doe_clone() -> None:
    """Raise iff the DoE-HEAD byte-identity conformance lane's clone is unresolvable."""
    resolve_doe_clone()


def _resolve_contract_blocks_doe_root() -> None:
    """Raise iff the contract_blocks/header_style byte-identity lane's DoE root
    is unresolvable — the same ``coordinator_core.testing.doe_root`` resolver
    ``test_provision_report_contract_blocks_byte_identity.py`` gates its own
    module-level ``skipif`` on.
    """
    root, present = doe_root_and_present()
    if not present:
        raise DoeResolveError(
            f"sibling DoE-claude checkout not resolvable (resolved root={root!r})"
        )


#: Each entry: ``(lane_id, human description used in the failure message, resolver)``.
#: ``resolver`` raises (any exception) iff the lane's DoE dependency is
#: unresolvable, and returns normally iff it resolved. Append a new tuple here
#: to register a new guarded lane — never author a second canary module.
LANES: list[tuple[str, str, Callable[[], None]]] = [
    (
        "docgen-byte-identity",
        "byte-identity conformance lane (test_c6_conformance.py, "
        "test_type_enum.py::TestAC4LiveConformance — ~29 tests)",
        _resolve_docgen_doe_clone,
    ),
    (
        "contract-blocks-header-style",
        "contract_blocks/header_style byte-identity lane "
        "(subagent_sandbox/tests/test_provision_report_contract_blocks_byte_identity.py)",
        _resolve_contract_blocks_doe_root,
    ),
]


@pytest.mark.parametrize(
    "lane_id,description,resolver", LANES, ids=[lane[0] for lane in LANES]
)
def test_doe_clone_conformance_lane_ran_or_environment_is_documented_optional(
    lane_id: str, description: str, resolver: Callable[[], None]
) -> None:
    """FAIL (not skip) if this environment should have run ``description``'s
    lane but the DoE clone is unresolvable — the exact silent-skip class a
    code-review finding flagged. A genuinely clone-absent, non-CI dev machine
    is the documented optional-skip lane and is allowed to skip this canary
    too, so the conformance guarantee's absence is at least loud where it
    matters (CI, or an opted-in dev machine) without forcing every consumer
    install to carry a sibling DoE clone it has no other reason to have.
    """
    if not _environment_requires_doe_clone():
        pytest.skip(
            "DOCUMENTED optional-skip lane: neither CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE "
            "nor CI is set truthy — this machine is not flagged as "
            "should-have-DoE-clone (see module docstring)"
        )
    try:
        resolver()
    except Exception as exc:
        pytest.fail(
            f"{description} is SILENTLY SKIPPING in an environment flagged "
            f"should-have-DoE-clone (CI or CLAUDE_KLABAUTER_REQUIRE_DOE_CONFORMANCE "
            f"truthy): {exc}"
        )
