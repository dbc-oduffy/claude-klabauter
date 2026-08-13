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

from pathlib import Path

import yaml

DRAFT_REL = "state/strategic/self-description.draft.yaml"

_GENERATED = "generated"


def _stamp_provenance(value):
    """Recursively guarantee every dict encountered carries provenance:"generated".

    Walks lists/dicts inside `value`; any dict missing a `provenance` key (or with a falsy one)
    is stamped `"generated"` in place. Never overwrites an existing non-empty `provenance` value
    — the generator upstream is the source of truth when it has already stamped one.
    """
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
    """Write `fields` to `<repo_root>/state/strategic/self-description.draft.yaml`.

    Idempotent full overwrite: every call replaces the draft file wholesale (generator-owns-draft
    model, DEC-3) — never appends to or merges with a prior draft on disk. Creates
    `state/strategic/` if absent. Guarantees every field dict inside each field-list value of
    `fields` carries `provenance: "generated"` before serializing (defense-in-depth over the
    generator's own upstream stamping) — the walk never touches the top-level `fields` dict
    itself, so no spurious document-root `provenance` key is ever written.

    Args:
        repo_root: guarded, resolved repo root (see coordinator_core.cartography._guard.path_guard).
        fields: dict of generatable-subset fields to serialize (e.g. version_highlights,
            competitors).

    Returns:
        Path to the written draft file.
    """
    # Review: code-reviewer (F1) — stamp only the field-list VALUES (version_highlights /
    # competitors entries), never the top-level `fields` dict itself; the frozen schema has
    # no top-level `provenance` and is additionalProperties:false at the document root.
    for value in fields.values():
        _stamp_provenance(value)

    draft_path = Path(repo_root) / DRAFT_REL
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" disables universal-newline translation — this draft is a
    # byte-contract consumed downstream; without it, Windows text mode
    # silently rewrites every embedded "\n" to "\r\n".
    with open(draft_path, "w", encoding="utf-8", newline="") as f:
        f.write(yaml.safe_dump(fields, sort_keys=False))
    return draft_path
