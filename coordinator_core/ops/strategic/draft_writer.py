"""
coordinator_core.ops.strategic.draft_writer — writes the strategic self-description draft.

Purpose: writes the generator-produced `fields` dict (version_highlights + competitors, and
whatever future generatable-subset fields land in later chunks) to the ONE sanctioned draft
path, `<repo_root>/state/strategic/self-description.draft.yaml`. This is the sole write surface
of the strategic.generate op — no other module in coordinator_core.ops.strategic touches disk.

HARDENED (C4): the draft path is a hardcoded module constant (`DRAFT_REL`) — never derived from
or joined against any canonical-file name. Every emitted field (each dict inside every list value
of `fields`) is guaranteed to carry `provenance: "generated"`: the generator already stamps this
upstream, but this module stamps any missing marker too (defense-in-depth) so a caller regression
upstream can never silently produce an unmarked field. The stamping walk is scoped to the
field-list entries (`fields.values()`) only — never the top-level `fields` dict itself, which has
no `provenance` property in the frozen schema and is additionalProperties:false at the document
root. Re-runs REPLACE the draft wholesale (generator-owns-draft model, plan DEC-3 pinned) — never
merged, never appended.

Negative-spec: this module has NO code path that opens, reads-to-mutate, or writes the
human-ratified canonical `self-description.yaml` — no function here accepts, derives, or
constructs that filename. The only path this module ever opens for writing is `DRAFT_REL`
resolved under the caller-supplied `repo_root`. Reconciling draft into canonical is a separate,
human-invoked ceremony (coordinator:strategic-self-description-refresh), never this op.

Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C1 (stub) / § C4 (hardening)
"""

from __future__ import annotations

GENERATES = []  # writes state/strategic/self-description.draft.yaml (DRAFT_REL); git ls-files shows only self-description.yaml and self-description.draft.yaml.archived tracked -- the live draft itself is not a currently-tracked artifact

from pathlib import Path

import yaml

DRAFT_REL = "state/strategic/self-description.draft.yaml"

_GENERATED = "generated"


def _stamp_provenance(value):
    if isinstance(value, dict):
        if "provenance" not in value or not value["provenance"]:
            value["provenance"] = _GENERATED
        for v in value.values():
            _stamp_provenance(v)
    elif isinstance(value, list):
        for item in value:
            _stamp_provenance(item)
    return value


def write_draft(repo_root: Path, fields: dict) -> Path:
    for value in fields.values():
        _stamp_provenance(value)

    draft_path = Path(repo_root) / DRAFT_REL
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    with open(draft_path, "w", encoding="utf-8", newline="") as f:
        f.write(yaml.safe_dump(fields, sort_keys=False))
    return draft_path
