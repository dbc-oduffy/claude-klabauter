"""coordinator_core.ops.fleet._outbox_frontmatter_rules — shared outbox-draft
frontmatter validation rule set.

Purpose: single source of truth for "does this state/memo-outbox/<topic>.md
draft's frontmatter have the shape `cross-repo-memo send` requires" — status
must be 'draft', the required sender-side fields must be present/non-empty
(summary's KEY only), kind (if present) must be a valid enum value, and
scoped_to (if any sub-field is present) must be the complete triple.

Extracted (2026-08-07) from `coordinator/bin/cross-repo-memo.py`'s
`_validate_outbox_frontmatter` / `_scoped_to_errors`, per the cross-repo memo
that named the write-time gap this closes:
cross-repo/inbox/2026-08-07-example-store-repo-em-memo-tool-rejects-the-shape-it-
teaches.md — an outbox draft hand-authored with `status: open` (copied from
the *received*-memo shape surrounding it in cross-repo/inbox/) sat undelivered
for a session because nothing caught the mismatch until the very last step
(`cross-repo-memo send`).

Two write-time consumers now share this ONE rule set instead of drifting
copies:
  - `coordinator/bin/cross-repo-memo.py`'s `_validate_outbox_frontmatter` (send-
    time hard-gate; unchanged error strings/exit codes — see its own
    docstring for the `open` → `draft` normalization now applied BEFORE this
    validator runs, so a hand-authored `status: open` self-heals at send
    time and never reaches this function's status check on that path alone).
  - `coordinator_core.write_guards.nudge_outbox_draft_frontmatter_shape` (new
    write-time advisory guard; fires the moment the buffer is authored,
    not two hundred lines later at send).

Deliberately NOT unified with the two other scoped_to mirrors this module's
predecessor already documented (`coordinator/bin/lib/schema.js:2290`'s
receiver-side check, `coordinator_core/ops/fleet/memo_send.py`'s
`_validate_scoped_to`) — those validate a DIFFERENT wire shape (a nested
`scoped_to` mapping feeding an op's setup-error envelope) for a DIFFERENT
purpose (receiver-side / op-param validation, not this sender-side outbox
draft's flattened `scoped_to_*` frontmatter keys). Collapsing all three into
one shared function was out of scope for the incident this module closes;
that pre-existing three-way duplication is unchanged. This module closes
specifically the CLI-vs-guard duplication the incident named.

Negative-spec:
  - Does NOT validate the DoE inbox schema shape
    (`coordinator/schemas/cross-repo-memo.schema.json`) — that schema's
    `applies_to` glob is `cross-repo/inbox/[0-9]*.md` and must NOT be
    extended to cover `state/memo-outbox/*.md`; outbox drafts and delivered
    inbox memos are two separate lifecycles (see
    `_validate_outbox_frontmatter`'s own docstring in the CLI, unchanged).
  - Does NOT know about `state/memo-outbox/sent/` — callers are responsible
    for scoping to undelivered drafts only; this module validates whatever
    frontmatter dict it is handed.
"""

from __future__ import annotations

import re

#: Required outbox-draft frontmatter fields. `summary`'s KEY must be present
#: (value may be empty at draft time — filled in by `memo.compose`); every
#: other field must be present AND non-empty.
#: `kind` is REQUIRED on a DRAFT (added 2026-08-30). It was absent from this
#: tuple while `memo.send` refused any draft lacking it and `memo.draft`
#: required it at authoring time — so the field was simultaneously optional
#: and mandatory depending on which check a sender reached first, which cost
#: a real sender four refusals to compose one memo. This is the OUTBOX
#: (draft) contract only: the DELIVERED corpus stays lenient per DEC-1
#: (`contract/emit_memo_schema`), which deliberately excludes `kind` to avoid
#: retroactively invalidating existing memos. Draft-time strictness and
#: delivered-time leniency are not in tension — one governs what a sender may
#: newly author, the other what a reader must accept.
OUTBOX_REQUIRED_FIELDS = (
    "title",
    "from",
    "to",
    "created",
    "status",
    "delivery_mode",
    "summary",
    "kind",
)

#: Mirrors the canonical `kind` enum in `coordinator/bin/lib/schema.js:2131`
#: (validKinds).
VALID_KINDS = ("ask", "consult", "fyi", "proposal")

#: scoped_to.sha shape — 7-40 hex chars.
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def scoped_to_errors(
    kind: "str | None", scoped_to: "dict[str, str | None] | None"
) -> list[str]:
    """Validate an outbox draft's flattened scoped_to_* fields under
    presence-triggered completeness.

    Mirrors `cross-repo-memo`'s own `_scoped_to_errors` (kept there too, for
    that CLI's other call sites — e.g. `--scoped-to-*` flag validation at
    dispatch time and self-receipt — which are outside this module's scope;
    see module docstring). scoped_to is OPTIONAL: if no sub-key is set, this
    passes. If ANY sub-key is supplied, the COMPLETE triple is required —
    'artifact' (non-empty str), exactly one of 'version' (non-empty str) or
    'sha' (7-40 hex str), and 'seam' (non-empty str) — else fail loud.

    `scoped_to` here is the FLATTENED dict shape
    ({"artifact": ..., "version": ..., "sha": ..., "seam": ...}), same as the
    CLI's own call sites pass — not the nested op-wire mapping
    `memo_send._validate_scoped_to` validates.

    Returns a list of error strings; empty list = valid (or exempt because
    scoped_to was omitted entirely).
    """
    scoped_to = scoped_to or {}
    artifact = (scoped_to.get("artifact") or "").strip()
    seam = (scoped_to.get("seam") or "").strip()
    version = (scoped_to.get("version") or "").strip()
    sha = (scoped_to.get("sha") or "").strip()
    has_version = bool(version)
    has_sha = bool(sha)
    if not (artifact or seam or has_version or has_sha):
        return []
    sha_well_formed = has_sha and bool(SHA_RE.match(sha))
    problems = []
    if not artifact:
        problems.append("artifact (non-empty string naming the governed surface)")
    if has_version == has_sha:
        problems.append(
            "exactly one of version or sha (currently "
            + ("both set" if has_version and has_sha else "neither set")
            + ")"
        )
    elif has_sha and not sha_well_formed:
        problems.append("sha (must be 7-40 hex chars)")
    if not seam:
        problems.append("seam (non-empty string naming the boundary this pin governs)")
    if not problems:
        return []
    return [
        "scoped_to is incomplete — when any of artifact/version/sha/seam is "
        "set, all of scoped_to_artifact, exactly one of "
        "scoped_to_version/scoped_to_sha, and scoped_to_seam are required. "
        f"Problems found: {'; '.join(problems)}."
    ]


def validate_outbox_frontmatter(fm: dict) -> list[str]:
    """Validate a state/memo-outbox/<topic>.md draft's parsed frontmatter.

    Accepts status == "draft" only — `status: open` (the shape every
    *received* inbox memo carries, and an easy hand-authoring mistake to
    copy) is rejected here, same as before extraction. The CLI's own
    `_cmd_send` now normalizes a `status: open` outbox draft to `draft`
    BEFORE calling this (see cross-repo-memo's `_cmd_send` docstring) — this
    function's status check is what makes an un-normalized `open` visible
    to any OTHER caller (e.g. the write-time advisory guard, which reads the
    draft as authored and never normalizes it).

    summary is allowed to be present-but-empty at draft time (user fills in
    body later via `compose`); only its KEY must be present. All other
    required fields must be present and non-empty.

    kind is OPTIONAL — absent/None is valid (reader applies an 'ask'
    default); only a PRESENT value outside the enum is rejected.

    Returns a list of error strings; empty list = valid.
    """
    errors = []
    if fm.get("status") not in ("draft",):
        errors.append(f"status must be 'draft', got: {fm.get('status')!r}")
    for field in OUTBOX_REQUIRED_FIELDS:
        if field == "summary":
            if field not in fm:
                errors.append(f"required field '{field}' missing")
        else:
            if not fm.get(field):
                errors.append(f"required field '{field}' missing or empty")
    kind = fm.get("kind")
    if kind is not None and kind not in VALID_KINDS:
        errors.append(
            f"kind {kind!r} is not a valid enum value "
            f"(must be one of: {', '.join(VALID_KINDS)}). "
            f"Note: 'ack' is not a kind — acknowledgement is receipt-state."
        )
    errors.extend(
        scoped_to_errors(
            kind,
            {
                "artifact": fm.get("scoped_to_artifact"),
                "version": fm.get("scoped_to_version"),
                "sha": fm.get("scoped_to_sha"),
                "seam": fm.get("scoped_to_seam"),
            },
        )
    )
    return errors
