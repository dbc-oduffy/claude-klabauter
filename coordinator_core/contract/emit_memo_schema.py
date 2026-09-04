"""
emit_memo_schema — generates cross-repo-memo.schema.json + archived-memo.schema.json
as JSON Schema PROJECTIONS of claude-klabauter's own cross-repo-memo SSOT, mirroring the
`coordinator_core.contract.cockpit_schema.emit_schema.emit_schemas(out_dir=...)`
precedent that is the SOLE canonical regeneration path for DoE's frozen
Cockpit-contract JSON.

Ownership context (Decision-0, RATIFIED 2026-07-24): DR-210 previously held the
two memo schema JSONs DoE-side permanently ("contract, not implementation").
The 2026-07-24 DoE proposal reverses that, but the reversal is one of
BEHAVIOR-ownership, not a hand-authored-file relocation — claude-klabauter's engine
never consumed the DoE JSON for validation in the first place (memos are
validated cross-field-only, no schema file — see
`coordinator_core.frontmatter.schema_validate.validate_memo_cross_fields`);
the real SSOT for required-ness is
`coordinator_core.ops.fleet._memo_compose` (`_VALID_KINDS`,
`_self_validate_frontmatter_fields`) and `coordinator_core.ops.fleet.
_memo_summary` (`_SUMMARY_MAX_CHARS`), and the cross-field lifecycle rules live
in `coordinator_core.frontmatter.schema_validate._MEMO_CROSS_FIELD_RULES`. So
these two JSON Schema documents are DERIVED artifacts describing that
behavior for DoE's routing hook / the legacy JS `query-records` CLI to
consume — not a second hand-owned copy of static JSON. DoE's hook becomes a
pure consumer of the artifact this module emits, exactly as the
Cockpit-contract hook already is a pure consumer of `emit_schema.emit_schemas`.

CRITICAL — placement. The two JSONs this module writes MUST NOT land in
`coordinator_core/frontmatter/schemas/` (the directory
`coordinator_core.frontmatter.schema_validate.load_schemas()` auto-loads into
`_byGlob`/`_byKind`). `match_schema` resolves `_byKind` FIRST and only falls
through to the glob when no `kind` value matches — since every memo carries a
`kind` post-DEC-1, registering these files into `_byKind` would resolve the
memo schema BEFORE the glob path (and any inbox-glob exclusion) is ever
consulted, silently arming full-shape (`required`) validation against
foreign-authored memos — exactly the invariant
`validate_memo_cross_fields`'s own docstring calls out as forbidden
("cross-field ONLY — memos are foreign-authored; a sender's base-field slip
must never..."). `emit_schemas()` below defaults `out_dir` to THIS module's
own directory (`coordinator_core/contract/`), outside the auto-load tree, and
nothing in this module (or anywhere else — see
`coordinator_core/ops/verify_schema_registry_sync.py` and
`coordinator_core/frontmatter/schema_drift_watch.py`, updated in lockstep)
registers the emitted files into `_byGlob` or `_byKind`. They are
drift-reference / DoE-consumer artifacts only.

Spec backlink: pln-take-ownership-of-the-cross-re-ac97ef § C5 / Decision-0
Negative-spec: this module does NOT vendor a hand-authored static JSON file —
every field description below is assembled in code and re-derived on every
`emit_schemas()` call from the SSOT constants it imports
(`_VALID_KINDS`, `_SUMMARY_MAX_CHARS`); a change to either constant changes
the next emission without a second file to remember to update by hand.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from coordinator_core.ops.fleet.memo_kinds import VALID_KINDS as _VALID_KINDS
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS

# ---------------------------------------------------------------------------
# x-schema-version — bumped independently of DoE's prior vendored "1.0.0"
# pin. This is a NEW emission lineage (generated-from-SSOT, not
# hand-authored-and-vendored); the version literal lives in exactly this one
# place, mirroring cockpit_schema.emit_schema.CONTRACT_VERSION's
# single-literal-source discipline.
# ---------------------------------------------------------------------------
MEMO_SCHEMA_VERSION = "1.8.0"

#: Generator-provenance declaration: emit_schemas() writes both of these
#: fixed tracked artifacts to this module's own directory by default.
GENERATES = [
    {
        "artifact": "coordinator_core/contract/cross-repo-memo.schema.json",
        "stamp_key": "x-schema-version",
        "sources": [
            "coordinator_core/contract/emit_memo_schema.py",
            "coordinator_core/ops/fleet/memo_send.py",
        ],
    },
    {
        "artifact": "coordinator_core/contract/archived-memo.schema.json",
        "stamp_key": "x-schema-version",
        "sources": [
            "coordinator_core/contract/emit_memo_schema.py",
            "coordinator_core/ops/fleet/memo_send.py",
        ],
    },
]

# ---------------------------------------------------------------------------
# x-bump-class / x-bump-note — DoE's bump-class annotation (memo
# 2026-07-27-doe-claude-em-bump-class-shipped-and-a-correction.md), sibling
# keys to x-schema-version. Closed vocabulary per that memo:
# top-level-array-additive | nested-field-additive | major. Non-behavioural
# (an `x-` annotation key changes no record's validity) so it does NOT bump
# MEMO_SCHEMA_VERSION — this pair only records how the CURRENT version
# (1.0.0 -> 1.2.0) changed shape, mirroring DoE's own hand-added values so
# their vendored copy and this emission converge rather than drift again.
# Single definition each, consumed at both emission sites below.
# ---------------------------------------------------------------------------
MEMO_SCHEMA_BUMP_CLASS = "nested-field-additive"
MEMO_SCHEMA_BUMP_NOTE = (
    "1.7.0 -> 1.8.0 declares the optional `actioned_at` closure timestamp on "
    "cross-repo-memo — WHEN a memo went terminal, not merely that it did. The "
    "vocabulary previously carried a closure stamp only for the grandfathered "
    "`action_taken`/`closed` statuses (`action_taken_at`/`closed_at`), while the "
    "live terminal status `actioned` had none, so the mainline "
    "`memo.transition action`/`resolve` write recorded no time at all — every "
    "downstream burn-down reconstruction was left inferring one from "
    "`picked_up_at` (a pickup is not a completion) or file mtime (invents a date "
    "nobody recorded), and `distill.delete_guard._candidate_actioned_date` still "
    "resorts to a per-file `git log --follow` proxy. Requested by example-cockpit-repo "
    "2026-09-04 (example-cockpit-repo/state/memo-outbox/memo-closure-timestamp-missing-"
    "from-cross-repo-memo-summary.md, their commit 8754b2171); "
    "`_apply_action_fields` now stamps it, and cockpit-contract "
    "4.4.0 carries it to CrossRepoMemoSummary. Declared only on cross-repo-memo, "
    "not archived-memo, which declares no lifecycle timestamps at all. "
    "Classified nested-field-additive: one new OPTIONAL property, never required, "
    "no change to any existing required set — every memo valid under 1.7.0 stays "
    "valid, and no backfill is performed (absent means UNKNOWN, never 'still "
    "open'). "
    "1.6.0 -> 1.7.0 (gate-closure-signal-contract, cross-repo/inbox/"
    "2026-08-21-claude-klabauter-em-gate-closure-signal-contract.md, ruled by "
    "eng-director state/subagent-share/f6ed9dc2-6fc9-4804-9952-27e684f5f573/"
    "coordinatoreng-director-ceecede2.md) declares the optional `discharges` "
    "object — our half of the two-part contract whose receiving half landed "
    "on the vendored plan-tasks.schema.json 1.10.0 `external_gate[]."
    "closure_key`. A discharging repo stamps this block on an ORDINARY "
    "cross-repo memo (no new `kind:` enum member — a four-site cross-repo "
    "lockstep on the kind vocabulary was deliberately declined; the waiting "
    "repo finds a discharge by the block, never by `kind`) to let the "
    "waiting repo machine-match its own `external_gate.closure_key` and "
    "propose (never itself perform) the `cleared: true` flip. Presence-"
    "triggered completeness, in the pattern `scoped_to` already sets: supply "
    "`discharges` and all three of `closure_key`/`evidence`/`landed_at` are "
    "required; omit it entirely and nothing fires. `closure_key` repeats the "
    "SAME two-member `kind` enum as plan-tasks.schema.json 1.10.0's "
    "`external_gate[].closure_key` (`deliverable` | `memo-thread`) — the two "
    "must agree, a divergence here is the desync the whole contract exists "
    "to prevent, so this module does not re-derive or re-typed the members' "
    "rationale, only cites the vendored schema as the enum's source. "
    "`evidence` reuses `realized_by`'s validated shape (plan path, 7-64 hex "
    "SHA, or the sentinel 'inline') rather than coining a second evidence "
    "vocabulary. Classified nested-field-additive: one new OPTIONAL "
    "top-level object property, no change to any existing required set — "
    "every memo valid under 1.6.0 stays valid. "
    "1.5.0 -> 1.6.0 catches the constant up to a shape drift: commit "
    "0bf6d576e added `sent_by` to both emitted schemas without moving "
    "MEMO_SCHEMA_VERSION off 1.5.0, so the emitter stamped two different "
    "shapes with one version string until this bump (DoE-claude "
    "2026-08-17 cross-repo memo, DR-097 § 3). Purely additive: one new "
    "optional property, never required — no previously-valid memo becomes "
    "invalid. "
    "1.4.0 -> 1.5.0 added the append-only supersede-disposition field quartet "
    "(`disposition_superseded`, `superseding_note`, `superseding_realized_by`, "
    "`superseded_at`) to both schemas, plus the "
    "`_memo_cf_disposition_superseded_requires_companions` cross-field rule — "
    "records that an already-actioned memo's disposition was later reversed "
    "WITHOUT overwriting the original decision/actioned_note/realized_by "
    "(coordinator_core.ops.memo_transition._handle_supersede). Purely "
    "additive: four new optional properties, never required — no previously-"
    "valid memo carries `disposition_superseded` at all, so none becomes "
    "invalid. "
    "1.3.0 -> 1.4.0 added `superseded_by` to the archived-memo schema "
    "(previously declared only on cross-repo-memo, while the archived "
    "corpus has always carried it on memos superseded before archival). "
    "Purely additive: one new optional property, never required — no "
    "previously-valid archived memo becomes invalid (debt "
    "2026-07-28-archived-memo-schema-omits-superseded-by-1d8a0735e39a.yaml). "
    "(1.2.0 -> 1.3.0 added `space` as a new optional field and widened "
    "`supersedes` from a bare string to string-or-list, both in service of "
    "mechanical inbox-blitz bucketing (a sibling repo's 2026-07-28 proposal). "
    "Purely additive: no previously-valid memo becomes invalid — a "
    "string-valued `supersedes` still validates under the widened oneOf. "
    "1.0.0 -> 1.2.0 adopted campaign_id and in_reply_to, commit 5140d176.)"
)

_KIND_DESCRIPTION = (
    "Sender-declared shape of the memo. When present must be one of: "
    + " | ".join(_VALID_KINDS)
    + ". Absent is valid (reader applies 'ask' default). Validated via a "
    "cross-field rule (schema_validate._memo_cf_kind_enum) rather than a "
    "schema enum so the grandfather cutoff applies consistently — required "
    "at SEND time only (DEC-1: memo_send.py's own gate, not this schema's "
    "`required` array; no retroactive invalidation of the existing corpus)."
)

_SUMMARY_DESCRIPTION = (
    f"One-line memo summary (<= {_SUMMARY_MAX_CHARS} chars). Length enforced "
    "by a cross-field rule (schema_validate._memo_cf_summary_length_cap) for "
    "non-grandfathered memos (created >= the grandfather cutoff) — required "
    "at SEND time only (DEC-1: memo_send.py's own gate, not this schema's "
    "`required` array)."
)

_IN_REPLY_TO_DESCRIPTION = (
    "Optional linkage field: the basename of an inbound memo this memo "
    "replies to. Normalized to a bare basename "
    "at send time and, for a receiver-repo delivery, existence-checked "
    "against the SENDER's own cross-repo/inbox/ or cross-repo/archive/ "
    "before anything is written (coordinator_core.ops.fleet.memo_send."
    "_validate_in_reply_to_exists) — never required, never emitted when "
    "absent. Consumed by coordinator_core.pickup_assemble."
    "_candidate_is_linked (matches by basename or basename-minus-'.md', "
    "case-insensitive) as reply-closure evidence, alongside the existing "
    "prose-citation fallback."
)

_SPACE_DESCRIPTION = (
    "Optional sender-declared thread / problem-space hint, per a sibling "
    "repo's inbox-blitz proposal. EXPLICITLY NON-AUTHORITATIVE: the "
    "receiver may override or ignore it, and nothing validates it against a "
    "controlled vocabulary — it is a grouping hint, not a taxonomy. Its "
    "value is that reconstructing threads from memo bodies is the single "
    "most expensive judgment step in a batch inbox pass, and a "
    "sender-declared hint collapses that step to a GROUP BY (a sibling "
    "repo's 16-memo dominant correspondent was five threads to the sender "
    "and expensively so to the receiver). Consumed by "
    "coordinator_core.ops.fleet.memo_blitz_buckets as the preferred "
    "space key. Never required — the entire pre-2026-07-28 corpus lacks it "
    "and must keep validating."
)

_SUPERSEDES_DESCRIPTION = (
    "Sender-declared supersession — points FROM new TO old. Accepts either a "
    "single memo reference (string, the original 'set on a new memo when "
    "re-issuing a pre-lifecycle memo' shape) OR a list of references, "
    "supporting one memo retiring several earlier ones — the observed shape "
    "of a running thread that ends in a correction. A reference is a memo "
    "basename or topic ref. Sender-declared rather than receiver-inferred "
    "because the sender usually knows: a sibling repo's inbox-blitz sweep "
    "found two of three inferred supersessions were memos whose own bodies "
    "said 'this corrects my earlier one' in prose. memo.send's same-day "
    "re-delivery filename disambiguator (memo_send._redelivery_filename) "
    "slugs the FIRST reference when a list is supplied — the list form does "
    "not change that filename's shape."
)

_TO_REPO_CROSS_REPO_MEMO_DESCRIPTION = (
    "OPTIONAL machine-local registry key of the receiver repo, in "
    "`repos.<key>` form (e.g. repos.doe_claude, repos.claude_klabauter, "
    "repos.example_retrieval_repo — the same key family used fleet-wide for "
    "sibling-repo resolution). `to:` remains the human-readable addressee "
    "and all its existing aliases stay valid; `to_repo` disambiguates "
    "rather than replaces — a memo's `to:` carries a nickname the "
    "receiver may be addressable under (this seat is addressable under "
    "eight of them), so a reader cannot verify by inspection that a memo "
    "landed in the right repo, whereas `to_repo` is machine-checkable "
    "without already knowing the alias mapping. Absent on the entire "
    "pre-2026-07-24 corpus and not yet emitted by claude-klabauter's memo_send — "
    "always optional, never required, a receiver-repo-local extension "
    "consumed by hooks/scripts/validate-frontmatter-schema.py's "
    "routing-mismatch check. Adopted per cross-repo/inbox/2026-07-24-"
    "claude-klabauter-em-central-id-canonical-order.md \"Not asked for, "
    "deliberately\" (PM-ratified)."
)

_SUPERSEDED_BY_DESCRIPTION = (
    "Set by receiver when this memo is superseded by a newer one — points "
    "FROM old TO new. Required by cross-field rule when status=superseded."
)

_SUPERSEDED_BY_ARCHIVED_MEMO_DESCRIPTION = (
    "Sibling of the cross-repo-memo schema's `superseded_by` field, carried "
    "through to archival so a memo superseded before archival still "
    "validates and still describes its own supersession after `git mv` to "
    "cross-repo/archive/. Never required — historical archived memos "
    "predate this field and must keep validating; unlike the cross-repo-memo "
    "side, no cross-field rule runs against archived records, so this is "
    "descriptive only, not enforcement."
)

_TO_REPO_ARCHIVED_MEMO_DESCRIPTION = (
    "OPTIONAL machine-local registry key of the receiver repo, in "
    "`repos.<key>` form (e.g. repos.doe_claude, repos.claude_klabauter, "
    "repos.example_retrieval_repo). Sibling of the cross-repo-memo schema's "
    "`to_repo` field, carried through to archival so a memo bearing it "
    "still validates after `git mv` to cross-repo/archive/. `to:` "
    "remains the human-readable addressee; `to_repo` disambiguates, it "
    "does not replace. Never required — historical archived memos "
    "predate this field and must keep validating. A receiver-repo-local "
    "extension, not (yet) emitted by claude-klabauter's memo_send."
)

_CAMPAIGN_ID_DESCRIPTION = (
    "Optional shared correlation id stamped by memo.send's 1->N fan-out "
    "(DEC-3/C7, per the memo-ownership-and-redesign plan). Present only "
    "on memos delivered via a to:[] fan-out send; identical across every "
    "receiver in the same campaign, persisted to disk on every successful "
    "per-receiver write (coordinator_core.ops.fleet.memo_send."
    "_memo_send_fan_out) so a rag-side compliance query over this field can "
    "distinguish not-yet-acted (delivered, no disposition) from "
    "never-delivered (write failed) receivers. Absent on ordinary "
    "single-receiver memos. Additive field (DEC-1 discipline) — never "
    "required."
)

_DISPOSITION_SUPERSEDED_DESCRIPTION = (
    "Receiver-set marker: this memo's disposition (decision/actioned_note as "
    "originally actioned) was later REVERSED. Append-only correction, "
    "distinct from `superseded_by` (which points to a DIFFERENT memo that "
    "supersedes this whole one): this field records a supersession of the "
    "SAME memo's own disposition, written by "
    "coordinator_core.ops.memo_transition._handle_supersede via the "
    "`--supersede-note`/`--supersede-realized-by` action flags. When true, "
    "requires superseding_note/superseding_realized_by/superseded_at "
    "(cross-field rule "
    "schema_validate._memo_cf_disposition_superseded_requires_companions) and "
    "status must already be actioned or superseded — there is no disposition "
    "to supersede otherwise. The ORIGINAL decision/decision_note/realized_by/"
    "actioned_note are left untouched on disk (never overwritten), so a "
    "reader sees the current (superseding) truth first — these four fields "
    "are anchored immediately after `status`, ahead of the original "
    "disposition fields — with the superseded original readable as history "
    "beneath. Never required — absent on the entire pre-existing corpus and "
    "on every memo whose disposition was never reversed."
)

_SUPERSEDING_NOTE_DESCRIPTION = (
    "Free-text rationale for the disposition reversal. Required by "
    "cross-field rule when disposition_superseded=true."
)

_SUPERSEDING_REALIZED_BY_DESCRIPTION = (
    "Pointer to what realized the reversal: a commit SHA, a memo path/"
    "basename, or a baton reference. Required by cross-field rule when "
    "disposition_superseded=true."
)

_SUPERSEDED_AT_DESCRIPTION = (
    "Timestamp the disposition reversal was recorded. Required by "
    "cross-field rule when disposition_superseded=true."
)

_DISCHARGES_DESCRIPTION = (
    "OPTIONAL — our half of the gate-closure-signal-contract (cross-repo/"
    "inbox/2026-08-21-claude-klabauter-em-gate-closure-signal-contract.md, "
    "ruled by eng-director state/subagent-share/f6ed9dc2-6fc9-4804-9952-"
    "27e684f5f573/coordinatoreng-director-ceecede2.md). Stamped on an "
    "ORDINARY cross-repo memo by the discharging repo — no `kind: "
    "discharge` enum member was minted (a four-site cross-repo lockstep on "
    "the kind vocabulary is real coordination cost the ruling declined to "
    "pay); a waiting repo finds a discharge by this block's presence, never "
    "by `kind`. The waiting repo matches `closure_key` against its own "
    "`external_gate[].closure_key` (plan-tasks.schema.json 1.10.0) and may "
    "PROPOSE the `cleared: true` flip with a citation — it never performs "
    "it from this block alone. Presence-triggered completeness, in the "
    "pattern `scoped_to` already sets: supply `discharges` and all three "
    "members (`closure_key`, `evidence`, `landed_at`) are required; omit it "
    "entirely and nothing fires."
)

_DISCHARGES_CLOSURE_KEY_KIND_DESCRIPTION = (
    "Which identity namespace `id` is drawn from. The SAME two-member enum "
    "as the vendored plan-tasks.schema.json 1.10.0's `external_gate[]."
    "closure_key.kind` — the two must agree; a divergence here is the "
    "desync the whole contract exists to prevent. See that schema's field "
    "description (coordinator_core/frontmatter/schemas/plan-tasks.schema."
    "json) for the member rationale and the four named exclusions, not "
    "re-typed here."
)

_DISCHARGES_CLOSURE_KEY_ID_DESCRIPTION = (
    "The identity value, in the namespace `kind` names. Interpreted within "
    "the discharging (sending) repo — ids are minted per-repo with no repo "
    "qualifier."
)

_DISCHARGES_EVIDENCE_DESCRIPTION = (
    "Where the discharging work landed. Reuses `realized_by`'s validated "
    "shape (a plan path, commit SHA of 7-64 hex chars, or the sentinel "
    "'inline') rather than coining a second evidence vocabulary — see this "
    "schema's `realized_by` property for the shape."
)

_DISCHARGES_LANDED_AT_DESCRIPTION = (
    "Date the discharging work landed (YYYY-MM-DD)."
)

_DISCHARGES_PROPERTY: dict[str, Any] = {
    "type": "object",
    "description": _DISCHARGES_DESCRIPTION,
    "required": ["closure_key", "evidence", "landed_at"],
    "properties": {
        "closure_key": {
            "type": "object",
            "required": ["kind", "id"],
            "additionalProperties": False,
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["deliverable", "memo-thread"],
                    "description": _DISCHARGES_CLOSURE_KEY_KIND_DESCRIPTION,
                },
                "id": {
                    "type": "string",
                    "minLength": 1,
                    "description": _DISCHARGES_CLOSURE_KEY_ID_DESCRIPTION,
                },
            },
        },
        "evidence": {
            "type": "string",
            "description": _DISCHARGES_EVIDENCE_DESCRIPTION,
        },
        "landed_at": {
            "type": "string",
            "format": "date",
            "description": _DISCHARGES_LANDED_AT_DESCRIPTION,
        },
    },
}

_SUPERSEDES_PROPERTY: dict[str, Any] = {
    "oneOf": [
        {"type": "string"},
        {"type": "array", "items": {"type": "string"}},
    ],
    "description": _SUPERSEDES_DESCRIPTION,
}

_SCOPED_TO_PROPERTY: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional structured pin declaring exactly what surface, at exactly "
        "what point-in-time, this memo's decision governs. Presence-triggered "
        "completeness: a cross-field rule requires a well-formed scoped_to "
        "(artifact, exactly one of version/sha, seam) whenever ANY scoped_to "
        "sub-key is supplied — mirrors memo_send.py's own "
        "_validate_scoped_to."
    ),
    "required": ["artifact", "seam"],
    "properties": {
        "artifact": {
            "type": "string",
            "description": "The surface the decision governs (a file, contract, schema, or subsystem name).",
        },
        "version": {
            "type": "string",
            "description": (
                "Version tag of the artifact the decision applies to. Mutually "
                "exclusive with sha — exactly one of the two must be present."
            ),
        },
        "sha": {
            "type": "string",
            "pattern": "^[0-9a-fA-F]{7,40}$",
            "description": (
                "Commit SHA of the artifact the decision applies to (7-40 hex "
                "chars). Mutually exclusive with version — exactly one of the "
                "two must be present."
            ),
        },
        "seam": {
            "type": "string",
            "description": "The seam the decision applies at (the boundary or interface this pin governs).",
        },
    },
    "oneOf": [
        {"required": ["version"], "not": {"required": ["sha"]}},
        {"required": ["sha"], "not": {"required": ["version"]}},
    ],
}


def _build_cross_repo_memo_schema() -> dict[str, Any]:
    """cross-repo-memo.schema.json — applies to `cross-repo/inbox/[0-9]*.md`.

    Field set + cross-field-rule pointers mirror
    `coordinator_core.frontmatter.schema_validate._MEMO_CROSS_FIELD_RULES`
    (the claude-klabauter-native SSOT, ported from DoE's now-superseded
    `bin/lib/schema.js` CROSS_FIELD_RULES['cross-repo-memo']). `required`
    lists only the base structural fields memo_send.py's
    `_self_validate_frontmatter_fields` enforces unconditionally
    (title/from/to/created/status/delivery_mode) — `kind` and `summary` are
    DELIBERATELY excluded per DEC-1 (send-time gate, not a schema-required
    tightening; no retroactive invalidation of the existing corpus).
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://coordinator.local/schemas/cross-repo-memo.schema.json",
        "x-schema-name": "cross-repo-memo",
        "x-schema-version": MEMO_SCHEMA_VERSION,
        "x-bump-class": MEMO_SCHEMA_BUMP_CLASS,
        "x-bump-note": MEMO_SCHEMA_BUMP_NOTE,
        "x-generated-by": "coordinator_core.contract.emit_memo_schema.emit_schemas",
        "applies_to": "cross-repo/inbox/[0-9]*.md",
        "description": (
            "Cross-repo memo — single-surface delivery artifact written by the "
            "sender to the receiver's cross-repo/inbox/. GENERATED PROJECTION, "
            "not hand-vendored: claude-klabauter owns the behavioral SSOT "
            "(coordinator_core.frontmatter.schema_validate._MEMO_CROSS_FIELD_RULES "
            "for lifecycle cross-field rules; coordinator_core.ops.fleet.memo_send "
            "for the send-time required-field/kind/summary gate). Cross-field "
            "rules: in_progress -> picked_up_by, actioned+accepted/partial -> "
            "realized_by shape, action_taken companions, closed companions, "
            "superseded -> superseded_by, central-only -> to, summary length cap, "
            "kind enum, distill_fate=ratification -> in_repo_capture shape, "
            "disposition_superseded=true -> superseding_note/"
            "superseding_realized_by/superseded_at + status already "
            "actioned/superseded. Memos created before the grandfather "
            "cutoff (see `created`) skip cross-field validation entirely."
        ),
        "type": "object",
        "required": ["title", "from", "to", "created", "status", "delivery_mode"],
        "properties": {
            "title": {
                "type": "string",
                "description": "Human-readable label for the memo.",
            },
            "from": {
                "type": "string",
                "description": (
                    "Sender repo or identity. RECEIVER-ROUTING-CRITICAL — used to "
                    "determine delivery target. Do NOT rename to machine: or author:."
                ),
            },
            "to": {
                "type": "string",
                "description": (
                    "Receiver repo or identity. RECEIVER-ROUTING-CRITICAL — used "
                    "for routing and self-receipt detection. Required when "
                    "delivery_mode=central-only (cross-field rule)."
                ),
            },
            "to_repo": {
                "type": "string",
                "description": _TO_REPO_CROSS_REPO_MEMO_DESCRIPTION,
            },
            "created": {
                "type": "string",
                "format": "date",
                "description": (
                    "Date the memo was authored (YYYY-MM-DD). Grandfather cutoff: "
                    "< 2026-05-22 skips cross-field validation."
                ),
            },
            "status": {
                "enum": [
                    "open", "in_progress", "actioned",
                    "reviewed", "action_taken", "closed", "superseded",
                ],
                "description": (
                    "Primary lifecycle: open -> in_progress -> actioned "
                    "(receiver-side in-place edits). Back-compat values retained "
                    "for grandfathered pre-lifecycle memos: reviewed, "
                    "action_taken, closed, superseded."
                ),
            },
            "delivery_mode": {
                "enum": ["receiver-repo", "central-only"],
                "description": (
                    "Single-surface model: memo.send only issues 'receiver-repo'. "
                    "'central-only' is retained for grandfathered pre-2026-05-23 "
                    "memos."
                ),
            },
            "related": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Related artifact paths (optional list of path strings).",
            },
            "supersedes": _SUPERSEDES_PROPERTY,
            "space": {
                "type": "string",
                "description": _SPACE_DESCRIPTION,
            },
            "superseded_by": {
                "type": "string",
                "description": _SUPERSEDED_BY_DESCRIPTION,
            },
            "picked_up_by": {
                "type": "string",
                "description": (
                    "Claim attribution stamped at pickup-start when status flips "
                    "to in_progress. Required by cross-field rule when "
                    "status=in_progress."
                ),
            },
            "sent_by": {
                "type": "string",
                "description": (
                    "Sender's session UUID, stamped at SEND time (never at draft "
                    "time — draft time is not send time). Mirrors picked_up_by on "
                    "the receive path. A durable session UUID, never a resolved "
                    "messaging address (addresses are per-process and a replayed "
                    "one is actively wrong, not merely stale). Optional and never "
                    "required — absent on memos predating this field, and a sender "
                    "that could not resolve its own session id renders an explicit "
                    "unresolved sentinel rather than omitting the field silently."
                ),
            },
            "picked_up_at": {
                "type": "string",
                "description": (
                    "Timestamp when the memo was picked up. Permissive: corpus "
                    "mixes date-time and legacy bare-date."
                ),
            },
            "received_at": {
                "type": "string",
                "description": (
                    "Lifecycle timestamp (convenience metadata; authoritative "
                    "trail is in receiver repo git log)."
                ),
            },
            "received_by": {
                "type": "string",
                "description": "Receiver session identifier.",
            },
            "reviewed_at": {
                "type": "string",
                "description": "Lifecycle timestamp for review phase.",
            },
            "actioned_at": {
                "type": "string",
                "description": (
                    "Closure timestamp for the LIVE terminal status: when "
                    "status went to actioned. Stamped by "
                    "coordinator_core.ops.memo_transition._apply_action_fields on "
                    "the action/resolve write, preserved never overwritten once "
                    "present. Distinct from action_taken_at/closed_at below, which "
                    "belong to the grandfathered action_taken/closed statuses. "
                    "Optional and never required — absent on every memo closed "
                    "before 2026-09-04, where it means UNKNOWN, never 'still "
                    "open'. Permissive on shape: the corpus mixes RFC3339 "
                    "date-time (the op's own stamp) and bare date (hand-authored)."
                ),
            },
            "action_taken_at": {
                "type": "string",
                "description": (
                    "Lifecycle timestamp when action was taken. Required by "
                    "cross-field rule when status=action_taken or status=closed."
                ),
            },
            "closed_at": {
                "type": "string",
                "description": (
                    "Closure timestamp (YYYY-MM-DD). Required by cross-field rule "
                    "when status=closed."
                ),
            },
            "decision": {
                "enum": ["accepted", "declined", "partial", "superseded"],
                "description": (
                    "Decision outcome. Required by cross-field rule when "
                    "status=action_taken or status=closed. Cross-field rule also "
                    "gates realized_by on accepted/partial."
                ),
            },
            "decision_note": {
                "type": "string",
                "description": "Optional free-text rationale populated alongside decision.",
            },
            "actioned_note": {
                "type": "string",
                "description": (
                    "Optional note set by receiver at actioning time (free-text "
                    "rationale or decision summary)."
                ),
            },
            "kind": {
                "type": "string",
                "description": _KIND_DESCRIPTION,
            },
            "summary": {
                "type": "string",
                "description": _SUMMARY_DESCRIPTION,
            },
            "campaign_id": {
                "type": "string",
                "description": _CAMPAIGN_ID_DESCRIPTION,
            },
            "in_reply_to": {
                "type": "string",
                "description": _IN_REPLY_TO_DESCRIPTION,
            },
            "realized_by": {
                "type": "string",
                "description": (
                    "Where the accepted/partial work landed: a plan path, commit "
                    "SHA (7-64 hex chars), or sentinel 'inline'. Required by "
                    "cross-field rule when status=actioned and "
                    "decision=accepted or partial."
                ),
            },
            "distill_fate": {
                "enum": ["ephemeral", "commitment", "ratification"],
                "description": (
                    "Distill-lifecycle classification stamped at the source "
                    "(memo-resolution time). ephemeral: fyi-ack/routine "
                    "coordination, requires neither in_repo_capture nor "
                    "realized_by. commitment: accept/partial that opens a "
                    "realized_by loop. ratification: settles ownership/seam "
                    "permanently — REQUIRES in_repo_capture pointing at an "
                    "in-repo home (cross-field rule); a ~/.claude memory-pointer "
                    "path MUST fail validation. Absent is valid on legacy memos."
                ),
            },
            "in_repo_capture": {
                "type": "string",
                "description": (
                    "In-repo path where a ratification-fated memo's decision was "
                    "durably captured (docs/decisions/, docs/wiki/, "
                    "state/cross-repo-commitments/, or a canonical plan/spec "
                    "path — or the coordinator/docs/... source-repo equivalents). "
                    "Required by cross-field rule when distill_fate=ratification. "
                    "A ~/.claude-rooted path is NOT a valid in-repo home and "
                    "fails validation."
                ),
            },
            "scoped_to": _SCOPED_TO_PROPERTY,
            "disposition_superseded": {
                "type": "boolean",
                "description": _DISPOSITION_SUPERSEDED_DESCRIPTION,
            },
            "superseding_note": {
                "type": "string",
                "description": _SUPERSEDING_NOTE_DESCRIPTION,
            },
            "superseding_realized_by": {
                "type": "string",
                "description": _SUPERSEDING_REALIZED_BY_DESCRIPTION,
            },
            "superseded_at": {
                "type": "string",
                "description": _SUPERSEDED_AT_DESCRIPTION,
            },
            "discharges": _DISCHARGES_PROPERTY,
        },
    }


def _build_archived_memo_schema() -> dict[str, Any]:
    """archived-memo.schema.json — applies to `cross-repo/archive/*.md`.

    The terminal-flipped mirror of cross-repo-memo, once a memo has been
    relayed and archived. Kept intentionally permissive (looser typing on
    `status` — plain string, not an enum) since archived records span the
    full lifecycle history including grandfathered/back-compat statuses.
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://coordinator.local/schemas/archived-memo.schema.json",
        "x-schema-name": "archived-memo",
        "x-schema-version": MEMO_SCHEMA_VERSION,
        "x-bump-class": MEMO_SCHEMA_BUMP_CLASS,
        "x-bump-note": MEMO_SCHEMA_BUMP_NOTE,
        "x-generated-by": "coordinator_core.contract.emit_memo_schema.emit_schemas",
        "applies_to": "cross-repo/archive/*.md",
        # Review: code-reviewer — Finding 1 (P1). A top-level "kinds" field
        # here re-arms the exact _byKind landmine this module's docstring
        # names as CRITICAL to avoid: load_schemas() special-cases BOTH the
        # literal "x-kinds" and "kinds" keys for _byKind registration, and
        # this file (unlike cross-repo-memo.schema.json) is genuinely
        # delivered to the shared schemas_dir that gets load_schemas()'d.
        # Nothing in build_type_to_glob/_TYPE_TO_GLOB_SUPPLEMENTS reads a
        # "kinds" field, so it is dropped outright rather than renamed.
        "description": (
            "Archived cross-repo memo — outbound memos relayed and archived at "
            "cross-repo/archive/. GENERATED PROJECTION, not hand-vendored — see "
            "cross-repo-memo.schema.json's description for the ownership note. "
            "Spec backlink: docs/plans/2026-06-23-deliverable-type-schema-taxonomy.md "
            "§ C1b; pln-take-ownership-of-the-cross-re-ac97ef § C5."
        ),
        "type": "object",
        "required": ["from", "to", "status"],
        "properties": {
            "from": {
                "type": "string",
                "description": "Sender identifier (repo slug or session).",
            },
            "to": {
                "type": "string",
                "description": "Receiver identifier (repo slug or EM label).",
            },
            "to_repo": {
                "type": "string",
                "description": _TO_REPO_ARCHIVED_MEMO_DESCRIPTION,
            },
            "status": {
                "type": "string",
                "description": "Lifecycle status of the memo.",
            },
            "title": {
                "type": "string",
                "description": "Human-readable title.",
            },
            "created": {
                "type": "string",
                "format": "date",
                "description": "Date the memo was authored (YYYY-MM-DD).",
            },
            "related_plan": {
                "type": "string",
                "description": "Repo-relative path to the plan this memo relates to.",
            },
            "related_review": {
                "type": "string",
                "description": "Repo-relative path to the review this memo relates to.",
            },
            "campaign_id": {
                "type": "string",
                "description": _CAMPAIGN_ID_DESCRIPTION,
            },
            "in_reply_to": {
                "type": "string",
                "description": _IN_REPLY_TO_DESCRIPTION,
            },
            # space / supersedes are carried through to archival for the same
            # reason to_repo is: a memo bearing them must keep validating
            # after `git mv` to cross-repo/archive/, and the archived corpus
            # is exactly what a supersession-candidate sweep reads back.
            "space": {
                "type": "string",
                "description": _SPACE_DESCRIPTION,
            },
            "supersedes": _SUPERSEDES_PROPERTY,
            "superseded_by": {
                "type": "string",
                "description": _SUPERSEDED_BY_ARCHIVED_MEMO_DESCRIPTION,
            },
            "scoped_to": {
                **_SCOPED_TO_PROPERTY,
                "description": (
                    "Optional structured pin declaring exactly what surface, at "
                    "exactly what point-in-time, this memo's decision governs. "
                    "Never required — historical archived memos predate this "
                    "field and must keep validating."
                ),
            },
            "disposition_superseded": {
                "type": "boolean",
                "description": _DISPOSITION_SUPERSEDED_DESCRIPTION,
            },
            "superseding_note": {
                "type": "string",
                "description": _SUPERSEDING_NOTE_DESCRIPTION,
            },
            "superseding_realized_by": {
                "type": "string",
                "description": _SUPERSEDING_REALIZED_BY_DESCRIPTION,
            },
            "superseded_at": {
                "type": "string",
                "description": _SUPERSEDED_AT_DESCRIPTION,
            },
            # Declared on BOTH projections, not cross-repo-memo alone: the
            # discharge reader's binding rule is to scan inbox AND archive,
            # because the boot sweep moves actioned memos and an actioned
            # discharge memo is still the discharge record. Omitting it here
            # would leave the block undeclared on exactly the population the
            # reader is required to read, and would restate the 1.5.0->1.6.0
            # shape drift this constant's own bump-note records.
            "discharges": _DISCHARGES_PROPERTY,
        },
    }


# name -> builder. Caller-facing surface for `emit_schemas`; kept as a plain
# module-level dict (not a class registry) since there are exactly two
# entities and no discriminated-union / $defs bundling need (unlike
# cockpit_schema's 28-entity registry).
_MEMO_SCHEMA_BUILDERS: dict[str, Any] = {
    "cross-repo-memo": _build_cross_repo_memo_schema,
    "archived-memo": _build_archived_memo_schema,
}


def _default_out_dir() -> Path:
    """This module's own directory — deliberately NOT
    `coordinator_core/frontmatter/schemas/` (the load_schemas() auto-load
    dir). See module docstring's CRITICAL placement note."""
    return Path(__file__).resolve().parent


def emit_schemas(
    out_dir: str | os.PathLike[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Write cross-repo-memo.schema.json + archived-memo.schema.json to
    `out_dir` (default: this module's own directory,
    `coordinator_core/contract/`). Mirrors
    `cockpit_schema.emit_schema.emit_schemas(out_dir=...)`'s signature and
    write-then-return-in-memory-shapes contract.

    Returns `{name: schema_dict}` for both entities so callers (parity tests,
    the drift-watch/registry-sync gates) don't need to re-read the just-
    written files.

    Negative-spec: never writes into `coordinator_core/frontmatter/schemas/`
    — a caller that passes that path explicitly gets a RuntimeError, not a
    silent auto-load-arming write. This is a deliberate hard stop, not
    advisory: the CRITICAL invariant this module exists to protect
    (foreign memos never resolve to shape-validation) has exactly one
    enforcement point and this is it.

    Caveat (Review: code-reviewer — Finding 4, nit): the guard is a
    best-effort literal resolved-path comparison (`schema_dir.resolve() ==
    forbidden`), not a filesystem-identity check — a symlink whose resolved
    target differs from the literal computed `forbidden` path, or a future
    rename of `frontmatter/schemas/`, would bypass it silently.
    `test_generated_files_not_present_in_auto_load_dir` is the independent
    second backstop (asserts the real dir's contents rather than trusting
    this guard fired).
    """
    schema_dir = Path(out_dir) if out_dir is not None else _default_out_dir()
    forbidden = (Path(__file__).resolve().parent.parent / "frontmatter" / "schemas").resolve()
    if schema_dir.resolve() == forbidden:
        raise RuntimeError(
            f"emit_memo_schema.emit_schemas: refusing to write into {forbidden} "
            "— that is the load_schemas() auto-load dir; registering these "
            "generated memo schemas there would arm _byKind full-shape "
            "validation against every foreign-authored memo carrying a "
            "matching kind:. See this module's docstring CRITICAL note."
        )
    schema_dir.mkdir(parents=True, exist_ok=True)

    emitted: dict[str, dict[str, Any]] = {}
    for name, builder in _MEMO_SCHEMA_BUILDERS.items():
        schema = builder()
        out_path = schema_dir / f"{name}.schema.json"
        out_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", newline="\n")
        emitted[name] = schema
    return emitted


def main(argv: list[str] | None = None) -> None:
    """Runnable entrypoint. Parses argv before touching the filesystem."""
    parser = argparse.ArgumentParser(
        prog="coordinator-emit-memo-schema",
        description=(
            "Emit cross-repo-memo.schema.json + archived-memo.schema.json as "
            "generated projections of claude-klabauter's memo SSOT. See "
            "coordinator_core.contract.emit_memo_schema's module docstring."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help=(
            "Directory to write the emitted schemas to. Defaults to this "
            "module's own directory (coordinator_core/contract/). Passed "
            "straight through to emit_schemas(out_dir=...)."
        ),
    )
    args = parser.parse_args(argv)
    emitted = emit_schemas(out_dir=args.out_dir)
    for name in emitted:
        print(f"emit_memo_schema: wrote {name}.schema.json")


if __name__ == "__main__":
    main()
