"""
coordinator_core.ops.schema_drift_gate — JSON-RPC "schema.drift_gate" operation.

Purpose: GATING counterpart to the advisory `claude-klabauter.schema.vendor_drift` doctor
probe (bin/claude-klabauter-doctor-probe.py, coordinator_core.frontmatter.schema_drift_watch).
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

Gating semantics (PM ruling, 2026-09) — `divergence_kind` is read per drifted
entry and DISCRIMINATES blocking from advisory, no longer a blanket
STATUS_DRIFT -> ok=False:
    DRIFT, any entry divergence_kind == "shape"
                  -> ok=False, message names every SHAPE-drifted schema +
                     direction (a validation-shape delta is the one class
                     that can silently corrupt a consumer's parse).
    DRIFT, every entry divergence_kind in {"prose-only", None}, no "shape"
                  -> ok=True, message notes the drift is prose-only/unclassified
                     and therefore advisory, not blocking.
    MATCH         -> ok=True,  message=None.
    INDETERMINATE -> ok=True,  message notes the check could not run (a release
                     gate blocking on "could not verify" would fail a merge for a
                     reason unrelated to the schemas themselves — e.g. an
                     unreadable DoE clone on the merging machine).
    UNRESOLVED    -> ok=True,  message notes no DoE clone was resolved on this
                     machine (not applicable, same reasoning as INDETERMINATE).
Only a POSITIVELY OBSERVED shape divergence blocks the gate; inability to check
never does, and neither does a prose-only divergence. This mirrors
scan_vendored_schema_drift's own status precedence (DRIFT outranks
INDETERMINATE outranks MATCH) and the advisory's "indeterminate is not evidence
of drift" rule — that rule cuts both ways: it is also not evidence worth
blocking a merge over.

Idempotency: read-only comparison against DoE HEAD + the vendored tree on disk;
identical inputs yield the identical verdict, no mutation performed.

Negative-spec:
    - NEVER re-vendors, writes, or mutates anything — reduction only.
    - Does NOT alter scan_vendored_schema_drift or check_schema_drift_advisory —
      both keep their existing never-raises, fail-open contract untouched; this
      op is a new consumer of their existing return shape, not a change to it.
    - Does NOT replace the daily advisory probe — the two run at different
      cadences for different reasons (legibility vs gating) and stay separate.
    - A drifted entry with no `divergence_kind` key (an older advisory build,
      per scan_vendored_schema_drift's own doc) is treated as non-shape —
      never blocks on absence of evidence, same fail-open posture as
      INDETERMINATE/UNRESOLVED above.

Spec backlink: cross-repo/inbox/2026-07-23-example-cockpit-repo-em-coordinator-doc-new-category-no-validation.md
               coordinator_core/frontmatter/schema_drift_watch.py module docstring.
               docs/plans/2026-09-22-inbox-blitz-bundled-xs-s-fixes-2026-09-11.md (P143-T35).
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.frontmatter.schema_drift_watch import (
    STATUS_DRIFT,
    scan_vendored_schema_drift,
)
from coordinator_core.ipc import register_op

_DIVERGENCE_KIND_SHAPE = "shape"


def evaluate() -> dict:
    """Reduce scan_vendored_schema_drift()'s report to a {ok, status, drifted,
    schemas_dir_rung, schemas_dir_degrade_reason, message} gating verdict. Pure
    reduction — no params, no repo_root; the scan resolves the DoE clone /
    vendored dir itself, same as the advisory probe.

    `schemas_dir_rung`/`schemas_dir_degrade_reason` pass the scan's own
    rung-2-observability fields (schema_drift_watch._resolve_scan_schemas_dir_with_reason)
    through verbatim, and a non-None degrade_reason is folded into `message`
    on a blocking (shape) verdict — a caller reading only `ok`/`message` must
    be able to tell "compared against source" from "fell back to the mirror's
    own copies" apart, since those two produce very different drift counts
    (see state/bug-backlog/2026-09-09-the-drift-scan-has-the-right-rung-and-
    falls-through-it-silently.yaml).

    `divergence_kind` gates whether a DRIFT verdict blocks at all — see module
    docstring's Gating semantics section.
    """
    report = scan_vendored_schema_drift()
    status = str(report.get("status") or "")
    drifted = report.get("drifted") or []
    schemas_dir_rung = report.get("schemas_dir_rung")
    schemas_dir_degrade_reason = report.get("schemas_dir_degrade_reason")

    if status == STATUS_DRIFT:
        shape_drifted = [d for d in drifted if d.get("divergence_kind") == _DIVERGENCE_KIND_SHAPE]

        if shape_drifted:
            named = ", ".join(
                f"{d.get('schema')} [{d.get('direction') or 'direction unknown'}]"
                for d in shape_drifted
            )
            message = (
                f"{len(shape_drifted)} vendored schema(s) diverge in SHAPE from DoE HEAD: "
                f"{named}. Re-vendor before merging "
                "(see coordinator_core/frontmatter/schema_drift_watch.py)."
            )
            if schemas_dir_degrade_reason is not None:
                message = (
                    f"{message} Compared against the mirror's own copies, not the engine "
                    f"source tree ({schemas_dir_degrade_reason})."
                )
            return {
                "ok": False,
                "status": status,
                "drifted": drifted,
                "schemas_dir_rung": schemas_dir_rung,
                "schemas_dir_degrade_reason": schemas_dir_degrade_reason,
                "message": message,
            }

        named = ", ".join(d.get("schema") for d in drifted)
        message = (
            f"{len(drifted)} vendored schema(s) diverge from DoE HEAD in prose only "
            f"(no shape divergence): {named}. Advisory only — not blocking."
        )
        return {
            "ok": True,
            "status": status,
            "drifted": drifted,
            "schemas_dir_rung": schemas_dir_rung,
            "schemas_dir_degrade_reason": schemas_dir_degrade_reason,
            "message": message,
        }

    message = None
    if status == "INDETERMINATE":
        message = str(report.get("summary") or "vendored-schema drift check could not run")
    elif status == "UNRESOLVED":
        message = str(report.get("summary") or "no DoE clone resolved; drift not determinable")

    return {
        "ok": True,
        "status": status,
        "drifted": drifted,
        "schemas_dir_rung": schemas_dir_rung,
        "schemas_dir_degrade_reason": schemas_dir_degrade_reason,
        "message": message,
    }


@register_op("schema.drift_gate")
def _handler(params: dict, repo_root: Optional[object] = None) -> dict:
    """JSON-RPC 'schema.drift_gate' handler. Takes no params; repo_root unused
    (scope "none" — see coordinator_core/op_scopes.py)."""
    return evaluate()
