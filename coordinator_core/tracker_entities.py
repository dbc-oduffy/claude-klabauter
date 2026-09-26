"""
coordinator_core.tracker_entities — sovereign-tracker entity/event vocabulary.

Purpose: the closed set of entity-event kinds this plan introduces, the
reserved-project guard, the `item.id` minting scheme, and a structurally
status-free `item` constructor — the payload shapes that ride on top of
`coordinator_core.tracker_store`'s frozen `append_event`/`read_events` pair.
This module mints and validates PAYLOADS; it never touches the store's
locking, sharding, or ordering machinery.

Spec backlink: docs/plans/2026-08-05-sat-02-sovereign-tracker-relational-
spine.md § Decision (DEC-13 through DEC-24), § Acceptance Criteria
(AC1/AC2/AC7/AC8/AC9/AC13/AC14/AC17), § Tasks C1/C2/C4.

C2 adds the emission layer proper: functions that build a payload (via the
C1 constructors above), stamp it into a storable event, and make exactly
ONE `tracker_store.append_event` call each — plus the DEC-24 foreign-repo
refusal for membership-edge emission.

C4 adds `item_person` emission over DEC-18's three-part
`(item_id, person_id, role)` natural key: a closed `role` enum
(`ITEM_PERSON_ROLES`) validated by both C1 payload constructors, and an
exact-duplicate-triple write-time refusal (AC9) in `emit_item_person_added`
— the one case DEC-18's key cannot enforce via `append_event`'s own event-id
uniqueness, since every call mints a distinct event id from the DEC-19
`applied_at` nonce regardless of payload repetition.

Negative-spec:
  - Do NOT modify `coordinator_core/tracker_store.py`. Its seven-stub
    interface is pinned across this plan (AC10); a need to change it is a
    re-plan signal, not a scope expansion.
  - Do NOT ever construct or emit an `unassigned` membership EDGE (an
    `item_project_added`/`item_project_retracted` event naming
    `RESERVED_PROJECT_ID`). The reserved project ROW stays real and
    addressable (DEC-22, AC16) — it is the edge that is never stored, not
    the row. The unassigned state is a read-time fold result only (DEC-13),
    never a write-time artifact — not as an optimization, not as a cache,
    not "just for the first release".
  - Do NOT resolve `repo_root` against this repo's own tree, and do NOT
    derive it from `__file__` — every entrypoint here mirrors
    `tracker_store`'s caller-supplied-`repo_root` discipline (DEC-11).
  - Do NOT add an `item_merge` (DEC-17). Item ids are permanent-for-life;
    a merge would rewrite historical events citing the losing id, which
    DR-241 Invariant 3 forbids outright. If item-merge is ever wanted, it
    lands as a *supersession* event (`item_superseded_by`) folded at read
    time, never an in-place rewrite — see
    docs/decisions/DR-224-succession-resolves-a-dead-holder-node-supersede-not-release.md
    for this repo's settled `supersede`-not-`merge`/`release` vocabulary.
  - Do NOT call `mint_deliverable_id.mint` for item ids — it is
    time/pid/random-derived and fails DR-241 bound (i)'s content-derivation
    requirement. Use `mint_item_id` in this module instead (DEC-16).
  - Do NOT embed a machine slug in `item.id`. DR-241 bound (i)'s
    machine-qualification requirement governs the entity EVENT id
    (DEC-20, chunk C2), not this permanent-for-life payload field.
  - Do NOT stamp `applied_at` at any site other than `_stamp_applied_at`
    (DEC-19) — a per-call-site `datetime.now(...)` is exactly the
    "rule someone must remember to maintain" this design exists to avoid.
  - Do NOT conflate the entity EVENT id (`evt-<machine>-<digest12>`,
    DEC-20, minted by `_mint_event_id`) with `item.id` (DEC-16, minted by
    `mint_item_id`). They are different ids serving different contracts.
  - Do NOT stretch `tracker_store.fold_observed_set`/`resolve_observed_set`
    across the repo boundary to answer the DEC-24 foreign-repo question.
    cockpit was explicit that doing so makes one repo's fold depend on
    peer repos' bytes, which is the blindness rather than the cure — the
    within-repo `item_created` presence check in `_require_local_item`
    is the whole obligation.
  - Do NOT silently skip a foreign-repo `item_project` edge (DEC-24,
    AC17). Emission must RAISE — a silent skip turns a spec violation
    into a silent data-loss bug.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from coordinator_core import tracker_store
from coordinator_core.ops.ceremony.completion_entry import _slug_from_title
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.tracker_id_grammar import is_item_id

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "item_created",
        "project_created",
        "item_project_added",
        "item_project_retracted",
        "item_closure_fidelity_set",
        "item_person_added",
        "item_person_retracted",
        "person_created",
        "person_alias_added",
        "person_alias_retracted",
        "person_merged",
    }
)
"""The closed set of entity-event kinds this plan introduces. Nothing else
rides in a sovereign-tracker event's ``kind`` field for this plan's entities."""

RESERVED_PROJECT_ID = "unassigned"
"""The reserved project row's id (DEC-22). The ROW is real and addressable
whether or not any item currently folds to it (AC16) — this constant names
only the identity. The corresponding membership EDGE must never be stored;
see the module negative-spec above."""

ITEM_PERSON_ROLES: frozenset[str] = frozenset(
    {"assignee", "watcher", "raised_by", "mentioned"}
)
"""The closed role enum for `item_person` (C4 deliverable #3). Nothing else
rides in an `item_person_added`/`item_person_retracted` payload's `role`
field for this plan."""

CLOSURE_FIDELITY_VALUES: frozenset[str] = frozenset(
    {"auto-observable", "verify-with-effort", "human-attested"}
)
"""The closed classification enum for `item_closure_fidelity_set` (C3
deliverable, per DR-closure-fidelity-tier-axis.md D1/D4). Nothing else rides
in an `item_closure_fidelity_set` payload's `closure_fidelity` field for
this plan. No retraction counterpart exists (D1): a re-classification emits
a fresh `item_closure_fidelity_set` event with a different value rather than
a retract-then-add pair — see the emitter docstring below."""

_ITEM_ID_SLUG_MAX = 32
_ITEM_ID_NONCE_HEX_LEN = 6
_ITEM_ID_DIGEST_LEN = 12


class TrackerEntityError(Exception):
    """Raised for a malformed or contract-violating tracker-entity operation.

    Actual raise sites (Review: code-reviewer c2a5a195 Finding 4 — the prior
    docstring named a ``status``-field case ``item_created`` cannot even
    raise for, since it has no ``status`` kwarg at all; that path raises a
    bare ``TypeError`` instead):
      - ``reject_reserved_project`` — create/retract/rename of the reserved
        ``RESERVED_PROJECT_ID`` project or edge.
      - ``reject_invalid_role`` — an ``item_person`` payload with a role
        outside the closed ``ITEM_PERSON_ROLES`` enum.
      - ``_require_local_item`` — a membership-edge emission naming an item
        this repo did not create (DEC-24).
      - ``emit_item_person_added`` — an exact-duplicate ``(item_id,
        person_id, role)`` triple already ADDED (AC9).
      - ``mint_item_id`` — a malformed ``created_at``, an empty slug, a
        malformed explicit ``nonce``, or a minted id that fails the
        ``[a-z0-9-]``-only charset guard.
    """


def reject_reserved_project(project_id: str, *, action: str) -> None:
    """Guard shared by every project-entity write path (AC2).

    Raises ``TrackerEntityError`` if *project_id* is ``RESERVED_PROJECT_ID`` —
    the reserved row may never be created, retracted, or renamed by an
    ordinary event. The row's existence and addressability (DEC-22, AC16) is
    a property of the read-time fold, never of a write to this project id.

    Exported (not a leading-underscore private) so any future
    project-retraction/rename event constructor — this plan's C1
    ``EVENT_KINDS`` defines no such kind — is required to route through this
    one guard rather than re-deriving the reserved-id check.
    """
    if project_id == RESERVED_PROJECT_ID:
        raise TrackerEntityError(
            f"cannot {action} the reserved project {RESERVED_PROJECT_ID!r} — "
            "it is a real, addressable row (DEC-22) but is never created, "
            "retracted, or renamed by an ordinary event"
        )


def project_created(project_id: str, *, name: str) -> dict:
    """Construct a ``project_created`` event payload.

    Raises ``TrackerEntityError`` if *project_id* is ``RESERVED_PROJECT_ID``
    (AC2) — the reserved row is never minted by an ordinary create event.
    """
    reject_reserved_project(project_id, action="create")
    return {"kind": "project_created", "project_id": project_id, "name": name}


def mint_item_id(
    title: str,
    body: str,
    created_at: str,
    *,
    nonce: str | None = None,
) -> str:
    """Mint a permanent-for-life ``item.id`` per DEC-16.

    Format: ``itm-<YYYYMMDD>-<slug<=32>-<nonce6>-<digest12>``, charset
    ``[a-z0-9-]`` only — git-trailer-safe by construction (AC7): no
    whitespace, colon, or newline can appear in the output.

    *created_at* must be an ISO-8601 string beginning ``YYYY-MM-DD`` (the
    date component is taken verbatim from its first 10 characters and
    digits-only-joined into ``YYYYMMDD``); the full string is also folded
    into the digest input unchanged.

    *nonce* defaults to ``secrets.token_hex(3)`` (6 hex chars) — minted
    once per call and folded into the digest input. This is what fixes the
    exact-input-collision defect (AC8): two mints of identical
    ``(title, body, created_at)`` — same machine, same second — would
    otherwise produce a byte-identical id, which no digest length alone can
    fix. Passing an explicit *nonce* is for deterministic testing only;
    ordinary callers must never pass one, or they reintroduce the exact
    collision this parameter exists to prevent.

    ``digest12`` is a SHA-256 hexdigest prefix (12 chars) over
    ``json.dumps((title, body, created_at, nonce), sort_keys=True)`` — the
    canonical serialization of the four-tuple.

    Does NOT embed a machine slug (DEC-16) and does NOT call
    ``mint_deliverable_id.mint`` (time/pid/random-derived, fails DR-241
    bound (i)).
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}", created_at):
        raise TrackerEntityError(
            f"created_at {created_at!r} does not start with an ISO-8601 "
            "YYYY-MM-DD date — cannot mint an item id from it"
        )
    date_part = created_at[:10].replace("-", "")
    # truncation to _ITEM_ID_SLUG_MAX can re-expose a '-' at the new
    slug = _slug_from_title(title)[:_ITEM_ID_SLUG_MAX].strip("-")
    if not slug:
        raise TrackerEntityError(
            f"title {title!r} slugs to an empty string — cannot mint an "
            "item id with no slug segment"
        )
    if nonce is not None:
        if not re.match(rf"^[0-9a-f]{{{_ITEM_ID_NONCE_HEX_LEN}}}$", nonce):
            raise TrackerEntityError(
                f"explicit nonce {nonce!r} must be exactly "
                f"{_ITEM_ID_NONCE_HEX_LEN} lowercase hex characters"
            )
    resolved_nonce = nonce if nonce is not None else secrets.token_hex(3)

    canonical = json.dumps(
        (title, body, created_at, resolved_nonce), sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :_ITEM_ID_DIGEST_LEN
    ]

    item_id = f"itm-{date_part}-{slug}-{resolved_nonce}-{digest}"
    if not is_item_id(item_id):
        raise TrackerEntityError(
            f"minted item id {item_id!r} violates the item.id grammar "
            "(tracker_id_grammar.ITEM_ID_PATTERN)"
        )
    return item_id


def item_created(item_id: str, *, title: str, body: str, created_at: str) -> dict:
    return {
        "kind": "item_created",
        "id": item_id,
        "title": title,
        "body": body,
        "created_at": created_at,
    }


def item_project_added(item_id: str, project_id: str) -> dict:
    """Construct an ``item_project_added`` membership-edge payload.

    Refuses, at construction time, to record an edge naming
    ``RESERVED_PROJECT_ID`` (DEC-13/DEC-22) — the unassigned edge is NEVER
    stored; it is a read-time fold result only. AC3/AC4 exist to catch a
    write path that would emit one.
    """
    reject_reserved_project(project_id, action="add a real edge to")
    return {
        "kind": "item_project_added",
        "item_id": item_id,
        "project_id": project_id,
    }


def item_project_retracted(item_id: str, project_id: str) -> dict:
    """Construct an ``item_project_retracted`` membership-edge payload.

    Refuses, at construction time, to record a retraction naming
    ``RESERVED_PROJECT_ID`` — there is never a real edge to that id to
    retract (DEC-13/DEC-22).
    """
    reject_reserved_project(project_id, action="retract a real edge from")
    return {
        "kind": "item_project_retracted",
        "item_id": item_id,
        "project_id": project_id,
    }


def reject_invalid_role(role: str, *, action: str) -> None:
    """Guard shared by both `item_person` payload constructors (C4
    deliverable #3).

    Raises ``TrackerEntityError`` unless *role* is one of the closed
    ``ITEM_PERSON_ROLES`` enum values — ``assignee`` / ``watcher`` /
    ``raised_by`` / ``mentioned``. Nothing else may ride in a ``role``
    field for this plan's `item_person` events.
    """
    if role not in ITEM_PERSON_ROLES:
        raise TrackerEntityError(
            f"cannot {action} item_person with role {role!r} — role must be "
            f"one of {sorted(ITEM_PERSON_ROLES)!r}"
        )


def reject_invalid_closure_fidelity(closure_fidelity: str, *, action: str) -> None:
    """Guard for `item_closure_fidelity_set` payload construction (C3, per
    DR-closure-fidelity-tier-axis.md D1).

    Raises ``TrackerEntityError`` unless *closure_fidelity* is one of the
    closed ``CLOSURE_FIDELITY_VALUES`` enum values — ``auto-observable`` /
    ``verify-with-effort`` / ``human-attested``. Mirrors
    ``reject_invalid_role``'s shape.
    """
    if closure_fidelity not in CLOSURE_FIDELITY_VALUES:
        raise TrackerEntityError(
            f"cannot {action} item_closure_fidelity_set with "
            f"closure_fidelity {closure_fidelity!r} — closure_fidelity must "
            f"be one of {sorted(CLOSURE_FIDELITY_VALUES)!r}"
        )


def item_closure_fidelity_set(item_id: str, closure_fidelity: str) -> dict:
    """Construct an ``item_closure_fidelity_set`` payload (C3, per
    DR-closure-fidelity-tier-axis.md D1).

    A single-valued classification event, not a membership add/discard pair
    — a re-classification emits a fresh event carrying a different value;
    there is no retraction counterpart (D1: "no retraction counterpart by
    design"). The fold takes the latest (by ``applied_at``) as current.

    Raises ``TrackerEntityError`` if *closure_fidelity* is outside the
    closed ``CLOSURE_FIDELITY_VALUES`` enum.
    """
    reject_invalid_closure_fidelity(closure_fidelity, action="set")
    return {
        "kind": "item_closure_fidelity_set",
        "item_id": item_id,
        "closure_fidelity": closure_fidelity,
    }


def item_person_added(item_id: str, person_id: str, role: str) -> dict:
    """Construct an ``item_person_added`` payload.

    Natural key ``(item_id, person_id, role)`` (DEC-18) — one person may
    hold several simultaneous roles on one item (e.g. ``assignee`` and
    ``raised_by``); a two-part key would force a lossy choice between them.
    ``person_id`` is null-tolerant until sat-05's registry lands.

    Raises ``TrackerEntityError`` if *role* is outside the closed
    ``ITEM_PERSON_ROLES`` enum.
    """
    reject_invalid_role(role, action="add")
    return {
        "kind": "item_person_added",
        "item_id": item_id,
        "person_id": person_id,
        "role": role,
    }


def item_person_retracted(item_id: str, person_id: str, role: str) -> dict:
    """Construct an ``item_person_retracted`` payload over the same
    ``(item_id, person_id, role)`` natural key (DEC-18).

    Raises ``TrackerEntityError`` if *role* is outside the closed
    ``ITEM_PERSON_ROLES`` enum.
    """
    reject_invalid_role(role, action="retract")
    return {
        "kind": "item_person_retracted",
        "item_id": item_id,
        "person_id": person_id,
        "role": role,
    }


ALIAS_NAMESPACES: frozenset[str] = frozenset(
    {"transcript_name", "email", "git_author", "display", "github", "github_id"}
)
"""The closed alias-namespace enum for `person_alias_added`/
`person_alias_retracted`. Nothing else rides in an alias payload's
``namespace`` field for this plan."""


def reject_invalid_namespace(namespace: str, *, action: str) -> None:
    """Guard shared by both alias payload constructors.

    Raises ``TrackerEntityError`` unless *namespace* is one of the closed
    ``ALIAS_NAMESPACES`` enum values — ``transcript_name`` / ``email`` /
    ``git_author`` / ``display`` / ``github`` / ``github_id``. Exported (not a
    leading-underscore private) so any future alias-related constructor
    routes through this one guard rather than re-deriving the namespace
    check, mirroring ``reject_invalid_role``'s shape.
    """
    if namespace not in ALIAS_NAMESPACES:
        raise TrackerEntityError(
            f"cannot {action} alias with namespace {namespace!r} — namespace "
            f"must be one of {sorted(ALIAS_NAMESPACES)!r}"
        )


def normalize_alias(namespace: str, raw_value: str) -> str:
    stripped = raw_value.strip()
    if namespace in ("email", "git_author", "github"):
        return stripped.casefold()
    return stripped


def mint_person_id() -> str:
    return str(uuid.uuid4())


def person_created(person_id: str, *, display_name: str) -> dict:
    return {
        "kind": "person_created",
        "person_id": person_id,
        "display_name": display_name,
    }


def person_alias_added(person_id: str, namespace: str, raw_value: str) -> dict:
    """Construct a ``person_alias_added`` payload.

    Carries *raw_value* UNCHANGED as provenance alongside the DEC-44
    normalized value (``normalized_value``), which is the identity used
    for alias resolution. Both keys are present — this is not an
    either/or.

    Raises ``TrackerEntityError`` if *namespace* is outside the closed
    ``ALIAS_NAMESPACES`` enum.
    """
    reject_invalid_namespace(namespace, action="add")
    return {
        "kind": "person_alias_added",
        "person_id": person_id,
        "namespace": namespace,
        "raw_value": raw_value,
        "normalized_value": normalize_alias(namespace, raw_value),
    }


def person_alias_retracted(person_id: str, namespace: str, raw_value: str) -> dict:
    """Construct a ``person_alias_retracted`` payload.

    Same raw/normalized dual-key shape as ``person_alias_added`` — the
    retraction must be matchable against the exact original add either by
    provenance (``raw_value``) or by identity (``normalized_value``).

    Raises ``TrackerEntityError`` if *namespace* is outside the closed
    ``ALIAS_NAMESPACES`` enum.
    """
    reject_invalid_namespace(namespace, action="retract")
    return {
        "kind": "person_alias_retracted",
        "person_id": person_id,
        "namespace": namespace,
        "raw_value": raw_value,
        "normalized_value": normalize_alias(namespace, raw_value),
    }


def person_merged(from_id: str, into_id: str, actor: str) -> dict:
    return {
        "kind": "person_merged",
        "from_id": from_id,
        "into_id": into_id,
        "actor": actor,
    }


def _stamp_applied_at() -> str:
    """Mint one microsecond-precision ISO-8601 UTC ``applied_at`` stamp.

    The SOLE minting site for every entity event's ``applied_at`` (DEC-19).
    ``read_events`` sorts on ``(applied_at, observed_at, id)`` with
    ``sequence`` deliberately excluded; ``fold_observed_set``'s
    second-granularity ``time.strftime`` stamp is the only other timestamp
    minted anywhere in this store, and at second granularity two
    same-process entity events on the same ``(item, project)`` pair within
    one wall-clock second would tie and fall through to `id` (a content
    digest) for ordering — silently reversing a real-time-ordered pair
    (AC13). Microsecond precision, minted from exactly one call site, is
    what makes emission order and read-back order agree.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _mint_event_id(kind: str, item_id_or_pair: object, payload: dict, applied_at: str) -> str:
    canonical = json.dumps(
        (kind, item_id_or_pair, payload, applied_at), sort_keys=True
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[
        :_ITEM_ID_DIGEST_LEN
    ]
    return f"evt-{machine_slug()}-{digest}"


def _emit(payload: dict, *, item_id_or_pair: object, repo_root: Path) -> dict:
    kind = payload["kind"]
    applied_at = _stamp_applied_at()
    event_id = _mint_event_id(kind, item_id_or_pair, payload, applied_at)

    event = dict(payload)
    if kind == "item_created" and "id" in event:
        event["item_id"] = event.pop("id")
    event["id"] = event_id
    event["applied_at"] = applied_at
    event["observed_at"] = applied_at

    return tracker_store.append_event(event, repo_root=repo_root)


def _require_local_item(item_id: str, *, repo_root: Path) -> None:
    """Refuse a membership-edge emission for an item this repo did not
    create (DEC-24, AC17).

    cockpit settled items as REPO-SCOPED: an item lives in the repo where
    the task happens, so a repo's fold over its own items is complete by
    construction, and a membership edge naming an item absent from this
    repo's own ``item_created`` events is malformed data, not a topology to
    support. Raises ``TrackerEntityError`` — never a silent skip, which
    would turn a spec violation into a silent data-loss bug.

    Answers this within-repo only, by re-reading this repo's own
    ``tracker_store.read_events`` output for a matching ``item_created``
    record. Does NOT stretch ``fold_observed_set``/``resolve_observed_set``
    across the repo boundary — cockpit was explicit that doing so would
    make one repo's fold depend on peer repos' bytes, which is the
    blindness rather than the cure.
    """
    for event in tracker_store.read_events(repo_root=repo_root):
        if event.get("kind") == "item_created" and event.get("item_id") == item_id:
            return
    raise TrackerEntityError(
        f"item {item_id!r} was not created in this repo's own event stream "
        "(DEC-24) — a foreign-repo item_project edge is refused, not "
        "silently skipped"
    )


def emit_item_created(
    item_id: str, *, title: str, body: str, created_at: str, repo_root: Path
) -> dict:
    payload = item_created(item_id, title=title, body=body, created_at=created_at)
    return _emit(payload, item_id_or_pair=item_id, repo_root=repo_root)


def emit_project_created(project_id: str, *, name: str, repo_root: Path) -> dict:
    """Build a ``project_created`` payload (C1) and append it as one event.

    Inherits ``project_created``'s ``RESERVED_PROJECT_ID`` refusal (AC2) —
    no separate guard is needed here.
    """
    payload = project_created(project_id, name=name)
    return _emit(payload, item_id_or_pair=project_id, repo_root=repo_root)


def emit_item_project_added(item_id: str, project_id: str, *, repo_root: Path) -> dict:
    _require_local_item(item_id, repo_root=repo_root)
    payload = item_project_added(item_id, project_id)
    return _emit(
        payload, item_id_or_pair=(item_id, project_id), repo_root=repo_root
    )


def emit_item_project_retracted(item_id: str, project_id: str, *, repo_root: Path) -> dict:
    _require_local_item(item_id, repo_root=repo_root)
    payload = item_project_retracted(item_id, project_id)
    return _emit(
        payload, item_id_or_pair=(item_id, project_id), repo_root=repo_root
    )


def emit_item_closure_fidelity_set(
    item_id: str, closure_fidelity: str, *, repo_root: Path
) -> dict:
    """Build an ``item_closure_fidelity_set`` payload (C1) and append it as
    one event (C3, per DR-closure-fidelity-tier-axis.md D1).

    Raises ``TrackerEntityError`` before emitting if *closure_fidelity* is
    outside the closed ``CLOSURE_FIDELITY_VALUES`` enum (inherited from
    ``item_closure_fidelity_set``).

    No retraction counterpart exists (D1) — a re-classification is a fresh
    call to this same emitter with a different *closure_fidelity*, folded
    last-write-wins by ``applied_at``, not a retract-then-add pair. This
    mirrors ``fold_membership``'s own precedent for folding a single
    current value, not the ``item_project_added``/``item_project_retracted``
    add/discard shape.
    """
    payload = item_closure_fidelity_set(item_id, closure_fidelity)
    return _emit(payload, item_id_or_pair=item_id, repo_root=repo_root)


def _item_person_edge_present(
    item_id: str, person_id: str | None, role: str, *, repo_root: Path
) -> bool:
    """Fold this repo's own `item_person_added`/`item_person_retracted`
    events restricted to ONE `(item_id, person_id, role)` triple (DEC-18),
    in `read_events`' own ratified order, and return whether that triple is
    currently ADDED.

    Does NOT reach into `tracker_projection` — that module imports
    `RESERVED_PROJECT_ID` from this one, so importing back would create a
    cycle. Mirrors `_require_local_item`'s direct `read_events` scan
    instead, restricted to the one triple this call cares about rather than
    folding every item's membership.
    """
    present = False
    for event in tracker_store.read_events(repo_root=repo_root):
        if (
            event.get("item_id") == item_id
            and event.get("person_id") == person_id
            and event.get("role") == role
        ):
            if event.get("kind") == "item_person_added":
                present = True
            elif event.get("kind") == "item_person_retracted":
                present = False
    return present


def emit_item_person_added(
    item_id: str, person_id: str | None, role: str, *, repo_root: Path
) -> dict:
    """Build an ``item_person_added`` payload (C1) and append it as one
    event.

    Raises ``TrackerEntityError`` before emitting if *role* is outside the
    closed ``ITEM_PERSON_ROLES`` enum (inherited from ``item_person_added``),
    or if the exact ``(item_id, person_id, role)`` triple is already
    ADDED (AC9) — DEC-18's natural key is enforced here at write time, since
    ``append_event``'s own event-id uniqueness check cannot catch it (each
    call mints a distinct event id via the DEC-19 ``applied_at`` nonce).
    Two DIFFERENT roles for the same ``(item_id, person_id)`` pair (e.g.
    ``assignee`` and ``raised_by``) are unaffected and admitted
    simultaneously.

    Concurrency note (Review: code-reviewer c2a5a195 Finding 3): the
    duplicate-triple check below is check-then-append, not atomic —
    ``_item_person_edge_present`` reads lock-free via ``read_events``, then
    this function's own ``_emit``/``append_event`` call takes the store's
    lock separately. Two concurrent processes racing on the identical
    triple can both observe "absent" before either appends, so this
    guarantee is same-process-only. Not closed here: closing it would need
    a store-side primitive, and ``tracker_store.py``'s seven-stub interface
    is pinned across this plan (AC10) — a need to change it is a re-plan
    signal. Cross-process duplicate rejection for `item_person` should be
    decided in sat-05 (the person registry), not bolted onto this guard.
    """
    if _item_person_edge_present(item_id, person_id, role, repo_root=repo_root):
        raise TrackerEntityError(
            f"duplicate item_person edge: ({item_id!r}, {person_id!r}, "
            f"{role!r}) is already added (DEC-18 natural key)"
        )
    payload = item_person_added(item_id, person_id, role)
    return _emit(
        payload,
        item_id_or_pair=(item_id, person_id, role),
        repo_root=repo_root,
    )


def emit_item_person_retracted(
    item_id: str, person_id: str | None, role: str, *, repo_root: Path
) -> dict:
    payload = item_person_retracted(item_id, person_id, role)
    return _emit(
        payload,
        item_id_or_pair=(item_id, person_id, role),
        repo_root=repo_root,
    )


# already imports `RESERVED_PROJECT_ID` from this module, so the reverse


def _alias_owner(
    namespace: str, normalized_value: str, *, repo_root: Path
) -> str | None:
    owner: str | None = None
    for event in tracker_store.read_events(repo_root=repo_root):
        if (
            event.get("namespace") != namespace
            or event.get("normalized_value") != normalized_value
        ):
            continue
        if event.get("kind") == "person_alias_added":
            person_id = event.get("person_id")
            if not isinstance(person_id, str):
                raise TrackerEntityError(
                    f"malformed person_alias_added event {event.get('id')!r}: "
                    f"person_id={person_id!r} must be a string"
                )
            owner = person_id
        elif event.get("kind") == "person_alias_retracted":
            owner = None
    return owner


def _person_merge_map(*, repo_root: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for event in tracker_store.read_events(repo_root=repo_root):
        if event.get("kind") != "person_merged":
            continue
        from_id = event.get("from_id")
        into_id = event.get("into_id")
        if not (isinstance(from_id, str) and isinstance(into_id, str)):
            raise TrackerEntityError(
                f"malformed person_merged event {event.get('id')!r}: "
                f"from_id={from_id!r}, into_id={into_id!r} must both be "
                "strings"
            )
        mapping[from_id] = into_id
    return mapping


def _resolves_to(start_id: str, target_id: str, merge_map: dict[str, str]) -> bool:
    current = start_id
    visited: set[str] = set()
    while current in merge_map and current not in visited:
        visited.add(current)
        current = merge_map[current]
        if current == target_id:
            return True
    return False


def emit_person_created(person_id: str, *, display_name: str, repo_root: Path) -> dict:
    payload = person_created(person_id, display_name=display_name)
    return _emit(payload, item_id_or_pair=person_id, repo_root=repo_root)


def emit_person_alias_added(
    person_id: str, namespace: str, raw_value: str, *, repo_root: Path
) -> dict:
    """Build a ``person_alias_added`` payload (C1) and append it as one
    event.

    Raises ``TrackerEntityError`` before emitting if *namespace* is outside
    the closed ``ALIAS_NAMESPACES`` enum (inherited from
    ``person_alias_added``), or if the normalized ``(namespace,
    normalized_value)`` pair is already claimed by a DIFFERENT
    ``person_id`` (AC3) — the error message names both ids. Checked via
    ``_alias_owner`` prior to the single ``append_event`` call, so a
    colliding alias never reaches the store at all.
    """
    payload = person_alias_added(person_id, namespace, raw_value)
    existing_owner = _alias_owner(
        namespace, payload["normalized_value"], repo_root=repo_root
    )
    if existing_owner is not None and existing_owner != person_id:
        raise TrackerEntityError(
            f"alias collision: ({namespace!r}, {payload['normalized_value']!r}) "
            f"is already registered to person {existing_owner!r}, cannot "
            f"register it to person {person_id!r}"
        )
    return _emit(
        payload,
        item_id_or_pair=(namespace, payload["normalized_value"]),
        repo_root=repo_root,
    )


def emit_person_alias_retracted(
    person_id: str, namespace: str, raw_value: str, *, repo_root: Path
) -> dict:
    """Build a ``person_alias_retracted`` payload (C1) and append it as one
    event.

    Raises ``TrackerEntityError`` before emitting if *namespace* is outside
    the closed ``ALIAS_NAMESPACES`` enum (inherited from
    ``person_alias_retracted``). No collision guard applies here — a
    retraction only removes an existing claim, it cannot create one.
    """
    payload = person_alias_retracted(person_id, namespace, raw_value)
    return _emit(
        payload,
        item_id_or_pair=(namespace, payload["normalized_value"]),
        repo_root=repo_root,
    )


def emit_person_merged(from_id: str, into_id: str, actor: str, *, repo_root: Path) -> dict:
    merge_map = _person_merge_map(repo_root=repo_root)
    if from_id in merge_map:
        raise TrackerEntityError(
            f"cannot merge person {from_id!r} into {into_id!r} — {from_id!r} "
            f"is already retired by an existing person_merged tombstone "
            f"(DEC-43 idempotency)"
        )
    if into_id == from_id or _resolves_to(into_id, from_id, merge_map):
        raise TrackerEntityError(
            f"cannot merge person {from_id!r} into {into_id!r} — {into_id!r} "
            f"already resolves back to {from_id!r} through the existing "
            f"merge chain, which would close a cycle (DEC-42)"
        )
    payload = person_merged(from_id, into_id, actor)
    return _emit(payload, item_id_or_pair=from_id, repo_root=repo_root)
