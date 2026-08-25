"""
coordinator_core.ops.schema_drift_gate — JSON-RPC "schema.drift_gate" operation.

Purpose: GATING counterpart to the advisory `makima.schema.vendor_drift` doctor
probe (bin/makima-doctor-probe.py, coordinator_core.frontmatter.schema_drift_watch).
The advisory runs daily and never blocks — by design, per that module's own
negative-spec. This op is the separate, additive surface for the weekly release
boundary: the one cadence point where a divergent vendored schema actually escapes
the repo (a merge to main), so blocking there is cheap (~1/5 days) and meaningful.

Does NOT change the advisory path's fail-open, never-raises behavior in any way —
this op is a thin reduction of the SAME scan_vendored_schema_drift() report to a
pass/fail verdict, called from a different cadence, never a mutation of the
advisory's own contract.

Op-key / contract:
    schema.drift_gate
    params:   {} (no params; scan resolves the DoE clone and vendored dir itself,
               same as the advisory probe)
    response: {ok: bool, status: str, drifted: list[dict], message: str|None}

Gating semantics — ok is False ONLY on a confirmed STATUS_DRIFT verdict:
    DRIFT         -> ok=False, message names every drifted schema + direction.
    MATCH         -> ok=True,  message=None.
    INDETERMINATE -> ok=True,  message notes the check could not run (a release
                     gate blocking on "could not verify" would fail a merge for a
                     reason unrelated to the schemas themselves — e.g. an
                     unreadable DoE clone on the merging machine).
    UNRESOLVED    -> ok=True,  message notes no DoE clone was resolved on this
                     machine (not applicable, same reasoning as INDETERMINATE).
Only a POSITIVELY OBSERVED divergence blocks the gate; inability to check never
does. This mirrors scan_vendored_schema_drift's own status precedence (DRIFT
outranks INDETERMINATE outranks MATCH) and the advisory's "indeterminate is not
evidence of drift" rule — that rule cuts both ways: it is also not evidence
worth blocking a merge over.

Idempotency: read-only comparison against DoE HEAD + the vendored tree on disk;
identical inputs yield the identical verdict, no mutation performed.

Negative-spec:
    - NEVER re-vendors, writes, or mutates anything — reduction only.
    - Does NOT alter scan_vendored_schema_drift or check_schema_drift_advisory —
      both keep their existing never-raises, fail-open contract untouched; this
      op is a new consumer of their existing return shape, not a change to it.
    - Does NOT replace the daily advisory probe — the two run at different
      cadences for different reasons (legibility vs gating) and stay separate.

Spec backlink: cross-repo/inbox/2026-07-23-project-opticon-em-coordinator-doc-new-category-no-validation.md
               coordinator_core/frontmatter/schema_drift_watch.py module docstring.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.frontmatter.schema_drift_watch import (
    STATUS_DRIFT,
    scan_vendored_schema_drift,
)
from coordinator_core.ipc import register_op


def evaluate() -> dict:
    """Reduce scan_vendored_schema_drift()'s report to a {ok, status, drifted,
    message} gating verdict. Pure reduction — no params, no repo_root; the scan
    resolves the DoE clone / vendored dir itself, same as the advisory probe.
    """
    report = scan_vendored_schema_drift()
    status = str(report.get("status") or "")
    drifted = report.get("drifted") or []

    if status == STATUS_DRIFT:
        named = ", ".join(
            f"{d.get('schema')} [{d.get('direction') or 'direction unknown'}]" for d in drifted
        )
        return {
            "ok": False,
            "status": status,
            "drifted": drifted,
            "message": (
                f"{len(drifted)} vendored schema(s) diverge from DoE HEAD: {named}. "
                "Re-vendor before merging (see coordinator_core/frontmatter/schema_drift_watch.py)."
            ),
        }

    message = None
    if status == "INDETERMINATE":
        message = str(report.get("summary") or "vendored-schema drift check could not run")
    elif status == "UNRESOLVED":
        message = str(report.get("summary") or "no DoE clone resolved; drift not determinable")

    return {"ok": True, "status": status, "drifted": drifted, "message": message}


@register_op("schema.drift_gate")
def _handler(params: dict, repo_root: Optional[object] = None) -> dict:
    """JSON-RPC 'schema.drift_gate' handler. Takes no params; repo_root unused
    (scope "none" — see coordinator_core/op_scopes.py)."""
    return evaluate()
