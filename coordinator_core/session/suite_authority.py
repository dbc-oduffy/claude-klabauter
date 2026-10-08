"""
coordinator_core.session.suite_authority -- who may run the fast/full suite.

AUTHORITY MATRIX (the one statement; other modules cite it, never restate it):

  Cloud EM    fast: ``cloud-box``    full: ``cloud-box``
  Box EM      fast/full: a live Tier-U grant file (``check_tier_u_grant``)
  Subagent    denied by the bash guard's identity leg before any basis here

``cloud-box`` discharges the AUTHORITY leg only; the suite-mutex wrapper and
mutex-holder legs remain every caller's.

``cloud_box_basis`` answers cloud iff the one forwarded harness marker
``CLAUDE_CODE_REMOTE == "true"`` is present AND the machine rung reads
``cloud``. ``suspect`` never qualifies: the Windows rung derives from
``PROCESSOR_IDENTIFIER``, an env var an inline prefix forges. Other harness
markers are not forwarded, so honouring them would make a CLI and the guard
disagree. ``machine_rung`` is called with NO caller env so a forged mapping
cannot seed its process-wide memo.

SPOOFING STANCE: these controls stop accident and drift, not an adversarial
agent that writes code. Residual, accepted: a Linux host whose rung reads
``cloud`` without being a Claude cloud session, launched with the marker set.

NEGATIVE SPEC: no spawn; never raises; fails closed; does not touch
``check_tier_u_grant`` semantics.
"""

from __future__ import annotations

from typing import Mapping, NamedTuple, Optional

from coordinator_core import env_locality
from coordinator_core.session import grant

__all__ = ["SuiteAuthority", "cloud_box_basis", "suite_authority"]

BASIS_CLOUD = "cloud-box"
BASIS_GRANT = "grant"


class SuiteAuthority(NamedTuple):
    authorized: bool
    basis: Optional[str]
    record: Optional[dict]


def cloud_box_basis(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Basis string iff this is a cloud session on a cloud-rung machine, else None."""
    try:
        if env is None:
            import os
            env = os.environ
        if env.get("CLAUDE_CODE_REMOTE") != "true":
            return None
        rung = env_locality.machine_rung()
        if rung.call != "cloud":
            return None
        return "cloud-box: CLAUDE_CODE_REMOTE=true; machine=cloud"
    except Exception:
        return None


def suite_authority(
    cwd: Optional[str] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    session_id: Optional[str] = None,
) -> SuiteAuthority:
    """Cloud basis first, then the live grant file. Fails closed."""
    if cloud_box_basis(env) is not None:
        return SuiteAuthority(True, BASIS_CLOUD, None)
    try:
        granted, record = grant.check_tier_u_grant(cwd, session_id=session_id)
    except Exception:
        return SuiteAuthority(False, None, None)
    if granted:
        return SuiteAuthority(True, BASIS_GRANT, record)
    return SuiteAuthority(False, None, None)
