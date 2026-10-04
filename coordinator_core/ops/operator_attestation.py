"""Operator attestation: the only way an `indeterminate` exit criterion mints as met.

Some falsifiers are operator-only by design (a billable smoke run that needs a human at a
real terminal), so no judge can observe them and the run's criterion is `indeterminate`.
Contract: coordinator-content-repo `coordinator/docs/wiki/reviewer-pipeline/falsifier-integrity.md`
§ Operator-only falsifiers.

    prime_exit_criterion:
      falsifier:
        mode: operator                    # absent means automated
      operator_attestation:
        artifact: state/audits/<file>.md  # repo-relative, committed at HEAD
        attested_by: <session id>
        verdict: pass                     # pass | fail
        ran_against: <sha>                # must resolve to a commit
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

import yaml

OPERATOR_MODE = "operator"
_ATTESTATION_KEYS = ("artifact", "attested_by", "verdict", "ran_against")

RunGit = Callable[[list], str]


def attested(fm_text: str, *, run_git: RunGit) -> Tuple[Optional[str], Optional[str]]:
    """`(receipt, None)` when an operator attestation discharges the criterion,
    `(None, problem)` when the plan declares operator mode but the attestation fails,
    `(None, None)` when the plan does not declare operator mode.

    At most two git spawns, only on a declaring plan with a well-formed passing attestation."""
    try:
        fm: Any = yaml.safe_load(fm_text or "")
    except yaml.YAMLError:
        return None, None
    prime = fm.get("prime_exit_criterion") if isinstance(fm, dict) else None
    if not isinstance(prime, dict):
        return None, None
    falsifier = prime.get("falsifier")
    if not isinstance(falsifier, dict) or falsifier.get("mode") != OPERATOR_MODE:
        return None, None

    record: Dict[str, Any] = prime.get("operator_attestation")  # type: ignore[assignment]
    if not isinstance(record, dict):
        return None, "falsifier.mode is operator, but prime_exit_criterion carries no operator_attestation"
    missing = [k for k in _ATTESTATION_KEYS if not str(record.get(k) or "").strip()]
    if missing:
        return None, f"operator_attestation is missing {', '.join(missing)}"
    verdict = str(record["verdict"]).strip()
    if verdict != "pass":
        return None, f"operator_attestation verdict is {verdict!r}, not pass"
    artifact = str(record["artifact"]).strip().replace("\\", "/")
    try:
        run_git(["cat-file", "-e", f"HEAD:{artifact}"])
    except Exception:  # noqa: BLE001 - any failure to resolve is "not committed"
        return None, f"operator_attestation artifact {artifact} is not committed at HEAD"
    ran_against = str(record["ran_against"]).strip()
    try:
        run_git(["cat-file", "-e", f"{ran_against}^{{commit}}"])
    except Exception:  # noqa: BLE001 - any failure to resolve is "not a commit"
        return None, f"operator_attestation ran_against {ran_against} does not resolve to a commit"
    return (
        f"operator-attested by {record['attested_by']} against "
        f"{ran_against[:12]} ({artifact})",
        None,
    )
