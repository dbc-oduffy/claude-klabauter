"""Section porter — InitiativeSummary (envelope key: ``initiatives``).

Emits one InitiativeSummary record per ``state/initiatives/*.yaml`` file under the central
state root that carries the two hard-required string fields (``id``, ``label``). Missing/blank
``id`` or ``label`` quarantines to ``malformed_records.initiatives``, as does a file that fails
to read/parse. The ``status`` field is coerced: only the four canonical Zod ``InitiativeStatus``
values (``active | paused | shipped | abandoned``) pass through; every other on-disk value
coerces to ``null`` at emit rather than hard-rejecting — the YAML on-disk schema may drift from
the Zod emission enum without a coordinated update (D9). ``owner`` and ``description`` are
present-as-null when absent or non-string. Graceful-absent: no ``initiatives`` dir → ([], []).

Provenance is a ``coordinator_artifact`` envelope with ``ref: null`` (D1/D9 — not git-backed);
Source A's ProvenanceEnvelope superRefine enforces that bidirectional invariant.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.15,
  InitiativeSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P18
Spec backlink: cross-repo/inbox/2026-07-05-initiative-govern-sweep-shape-outcome.md § Ask 1 (canonical enum, PM-ratified 2026-07-04)

goals[] staging (2.13.0, D24): each record carries a transient ``_goal_ids`` key — the
ratified ``goals: [goal-id, ...]`` id-array parsed from the initiative YAML (DR-207,
initiative.schema.json). This section CANNOT resolve those ids into full Goal records
itself (the collect() spine contract gives a section no access to another section's
output) — resolution against ``envelope["goals_current"]`` and the final ``goals``
nesting happen in a post-collect enricher (resolvers.py ``_stamp_initiative_goals``),
which also POPS ``_goal_ids`` before the wire write (the InitiativeSummary schema is
``.strict()`` / ``additionalProperties: false`` — a leaked staging key would reject).
This section MUST NOT read ``goals-log.*.jsonl`` itself; that derivation lives exactly
once, in the shared context-free reader ``coordinator_core/goals/wire_read.py``
(``read_and_collapse``) — not duplicated as a second glob/parse/collapse loop here or
anywhere else.
Spec backlink: pln-claude-klabauter-artifact-emit-2-13-0-go-e1f844 § C3
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.yaml_flat import parse_goal_ids as _parse_goal_ids
from coordinator_core.yaml_flat import simple_yaml_load as _simple_yaml_load
from coordinator_core.yaml_flat import unquote as _unquote  # noqa: F401 — re-exported for tests

# Valid Zod InitiativeStatus values (D9 — ONLY these four canonical values; other on-disk values
# coerce to null). PM-ratified 2026-07-04 per cross-repo/inbox/2026-07-05-initiative-govern-sweep-shape-outcome.md § Ask 1.
_VALID_ZOD_STATUS = frozenset({"active", "paused", "shipped", "abandoned"})


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for InitiativeSummary from ``state/initiatives/*.yaml``."""
    ini_dir = ctx.central_state_root / "initiatives"

    records: list[dict] = []
    malformed: list[dict] = []

    if not ini_dir.is_dir():
        return records, malformed

    for fpath in sorted(ini_dir.glob("*.yaml")):
        fname = fpath.name
        rel_path = f"state/initiatives/{fname}"
        try:
            content = fpath.read_text(encoding="utf-8")
            fm = _simple_yaml_load(content)
        except Exception as e:  # noqa: BLE001 — parity with bash bare-except quarantine
            malformed.append({"path": rel_path, "reason": f"parse error: {e}"})
            continue
        goal_ids = fm.pop("_goal_ids", [])

        id_val = fm.get("id")
        label_val = fm.get("label")
        if not isinstance(id_val, str) or not id_val:
            malformed.append({"path": rel_path, "reason": "missing required field: id"})
            continue
        if not isinstance(label_val, str) or not label_val:
            malformed.append({"path": rel_path, "reason": "missing required field: label"})
            continue

        raw_status = fm.get("status")
        status_val = raw_status if raw_status in _VALID_ZOD_STATUS else None

        owner_val = fm.get("owner")
        desc_val = fm.get("description")

        records.append(
            {
                "repo": ctx.repo_name,
                # Hardcoded to "." on the current
                # single-coordinator-root invariant (unlike goals.py, which reads this
                # field from disk per-record). The `_stamp_initiative_goals` join in
                # resolvers.py scopes on (repo, coordinator_root_path) match; a future
                # multi-coordinator-root setup would need this value sourced from disk
                # here too, or the join silently fails to resolve initiatives declared
                # against a non-"." root.
                "coordinator_root_path": ".",
                "id": id_val,
                "label": label_val,
                "provenance": ctx.provenance(
                    "coordinator_artifact", path=rel_path, derivation="parsed"
                ),
                "owner": owner_val if isinstance(owner_val, str) else None,
                "status": status_val,
                "description": desc_val if isinstance(desc_val, str) else None,
                # Staging key (D24) — resolved into full Goal records and popped by
                # envelope._stamp_initiative_goals; must never reach the .strict() wire.
                "_goal_ids": goal_ids,
            }
        )

    return records, malformed
