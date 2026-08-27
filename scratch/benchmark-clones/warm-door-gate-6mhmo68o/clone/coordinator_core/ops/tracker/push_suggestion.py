"""
coordinator_core.ops.tracker.push_suggestion — tracker.push_suggestion op.

Purpose: sat-06's producer-facing write op (C4) — the one seam cockpit (or any
other process that can spawn `coordinator-invoke`) uses to push ONE
sovereign-tracker item-event bundle into this fleet. Resolves ownership via
`coordinator_core.tracker_holder.write_root_for`, then routes:

  - LOCAL target (resolved root == the caller's own worktree) -> a direct,
    same-repo write through `coordinator_core.tracker_store.append_event`.
  - PEER target (resolved root != the caller's own worktree) -> a DR-338
    delivery envelope committed into the peer's `cross-repo/inbox/`, never a
    direct cross-tree `open(write_root_for(...))` write. That shape was
    explicitly declined by the designated store's own EM (DR-338 §1 "three
    candidate shapes") and forbidden by DR-338 D4.

Transport (CORRECTED 2026-08-19, see the plan's own § CORRECTION): this op is
directly callable across a process boundary via
`coordinator/bin/coordinator-invoke` — request/response, registered across
all five surfaces (AC5). Cockpit spawns the trampoline naming this op, passes
`repo_root` + the item-event bundle, and reads the exit code. No inbound file
drop exists or is consumed here (retired).

Ownership routing exception (cockpit `scope: "fleet-authored"` payloads):
route on `event["authority"]["emitter_repo"]`, NOT `event["repo"]` —
Cockpit's designated authoritative emitter can differ from the payload's
subject repo. Every other payload routes on `event["repo"]` directly. Both
resolve through `tracker_holder.write_root_for`'s existing owner-qualified-
slug / `None` / `""`-rejected three-valued contract — `""` is cockpit's
legacy "no owner" encoding and is translated to `None` here BEFORE it ever
reaches `write_root_for` (its own docstring requires exactly this
translation at the caller).

D2a — destination-agnosticism, THE RATIFICATION CONDITION DR-338 binds to
this handler (Ratification Status table, "RATIFIED", 2026-08-20):
this module and its resolution chain (`tracker_holder.write_root_for` /
`holder_repo_root`) name NO destination-specific identifier anywhere — the
peer-delivery target is resolved SOLELY by reading the `tracker.holder_repo`
registry key (for an unowned/holder-routed event) or the wire-supplied
owner-qualified slug (for a stated-owner event) and following either through
the `repos.*` registry family. Repointing `tracker.holder_repo` at a
different `repos.*` member requires NO change to this file — see
`coordinator_core/ops/tracker/tests/test_push_suggestion.py`'s
`test_d2a_no_destination_specific_literal_in_module_source`, the guard test
DR-338's impl-slice obligation requires land WITH this handler.

Envelope shape (D2(6)): the delivered envelope carries the addressed
`owning_repo`, the caller-supplied event bundle VERBATIM, and — F3 (the Director of Engineering
boundary review, 2026-08-20) — a PASSTHROUGH `sensitivity_tier` frontmatter
field when the caller's event carries one (see `_compose_envelope`). This
module does not REQUIRE or VALIDATE any destination-specific declaration
field, and invents no default when one is absent; it only lifts through
what the caller already supplied. Per-destination admission terms are the
destination's own annex (D2(6)'s two-layer note) and a successor
destination inherits the mechanism, never a predecessor's terms — the
passthrough names no destination and its presence does not depend on which
destination is configured, so it starts working with no code change the
moment an upstream producer supplies the field. An undeclared/malformed
EVENT (missing `id`/`observed_at`, the fields `tracker_store.append_event`
itself requires) still fails loud at emit time via `tracker_store`'s own
validation for the local-write arm; the peer arm defers content-shape
refusal to the receiver's own apply step, consistent with D1(b) — this op
only enforces the DELIVERY mechanics (D2(1)-(5), (7)), never a
destination's payload vocabulary.

Classification: MUTATING, on the merits — every call performs a real write,
either `tracker_store.append_event` (local arm) or a committed file write
into a peer repo's `cross-repo/inbox/` (peer arm). DR-208 §5 affirmation (the
mutating-op-author checklist, all five answered against this module's own
handler body):
  1. Opens any file for write (including append)?                 YES —
     `tracker_store.append_event` (local arm, via `locked_rmw`) and
     `_write_envelope_file`'s `os.open(..., O_CREAT | O_EXCL | O_WRONLY)`
     (peer arm).
  2. Calls any git write command (`git add`, `git commit`, ...)?    YES —
     `_commit_envelope` (peer arm only; the local arm never spawns git).
  3. Enqueues any state mutation (queue/backlog/similar)?           No.
  4. Invokes a subprocess that may do any of the above?             YES —
     `_commit_envelope`'s `subprocess.run(["git", ...])` calls (peer arm).
  5. I/O behavior conditional (reads under some paths, writes under
     others)?                                                       YES —
     the local/peer fork itself IS conditional I/O routing.
  A clean YES to (1), (2), (4) and (5) — MUTATING by construction, not a
  conservative-by-default label. Authority:
  docs/decisions/DR-338-tracker-event-delivery-cross-tree-write.md § 2.

Rejection contract (C12 — sidecar review's open question 3): the transport is
a synchronous cross-process op call (module docstring § Transport above), so
an exit code is the ONLY signal a subprocess caller (cockpit) gets. Three
DISTINCT, LOUD failure classes, each its own exception type, each marked
`caller_facing_validation = True` so `coordinator_core.ipc._handler_exception_error`
surfaces the exception's OWN message (INVALID_PARAMS, -32602) instead of the
generic, class-name-only "Internal error: <ClassName>" collapse an
unclassified exception gets — that collapse is exactly the silent-catch-all
AC17 forbids, since it makes every one of these three read identically to a
caller. None of the three ever wraps or is wrapped by another.

F1 (the Director of Engineering boundary review, 2026-08-20): `caller_facing_validation` alone only
gets a caller as far as "one of these three, not an unclassified internal
error" — `_exit_code_for_response` (a shared CLI surface out of this op's
`writes:` scope) collapses ALL of them to the same exit code 1, so a
subprocess caller (cockpit) cannot tell malformed/unauthorized/schema-stale
apart from the exit code alone. Each class therefore also carries a pinned
`refusal_class` string (`_REFUSAL_CLASS_MALFORMED` / `_UNAUTHORIZED` /
`_SCHEMA_STALE`) folded into `str(self)` by `PushSuggestionRefused.__str__`
— since `ipc._handler_exception_error` sets `error["message"] = str(exc)`
for any `caller_facing_validation` exception, that discriminator survives
onto the dispatched JSON-RPC response at a fixed path
(`error["message"]`'s leading `refusal_class=<value>: ` token), provable via
`dispatch_message`, not exception-type introspection:
  - `PushSuggestionMalformedError` — the request or event fails shape/schema
    validation (missing/wrong-typed `event`, `event.scope == "fleet-authored"`
    missing `authority.emitter_repo`, a D3 `repo_root` mismatch, a non-int
    `contract_version`, or a caller-supplied `event.id` — claude-klabauter mints the
    event id itself, RULING 2026-08-20 C4; see `_mint_event_id`). Multiply
    inherits `ValueError` (existing handler-level callers already assert
    `ValueError`) AND `PushSuggestionRefused` (existing
    `_resolve_owning_repo` callers already assert `PushSuggestionRefused`) —
    a narrowing of both pre-existing types, never a widening. C1 (review-
    driven correction, 2026-08-20) removed the addressing-key requirement
    entirely — `_deliver_envelope` now addresses on the minted `event["id"]`,
    never a caller-supplied field, so an event with neither `item_id` nor
    `event_id` behaves identically on both arms.
  - `PushSuggestionUnauthorizedError` — the request names an owning-repo slug
    that does not resolve to a `repos.*` registry member. Reuses C5's own
    refusal (`tracker_holder._resolve_repos_key_for_slug`'s "is not a known
    repos.* member" `RuntimeError`) by catching it at this op's boundary and
    re-raising loud and distinguishable — never wrapping a DIFFERENT
    `tracker_holder` rung (holder-role unset, `repos.<key>` unset, path not
    cloned, brightline) that is an operator/config fact, not a caller-input
    fact, and stays an unclassified `RuntimeError` propagated verbatim.
  - `PushSuggestionSchemaStaleError` — the request's own `contract_version`
    (optional wire field, `params.contract_version`) names a version older
    than this op's own `_CONTRACT_VERSION`. Absence is treated as current
    (mirrors `tracker_transitions.py`'s own documented "absence == version 1"
    convention for `schema_version`), never rejected.

Spec backlink: docs/plans/2026-08-18-sat-06-cockpit-consumption-seam.md
  § Tasks C4, C12, § CORRECTION ("cockpit cannot reach a claude-klabauter op" conflates
  two transports), § Acceptance Criteria AC2/AC5/AC10/AC17.
Spec backlink: docs/decisions/DR-338-tracker-event-delivery-cross-tree-write.md
  § D1/D2/D2a/D4 — the delivery carve-out this op's peer arm discharges.
Spec backlink: docs/decisions/DR-214-send-class-cross-tree-write-boundary.md
  § D2(2)/(3)/(7) — inherited verbatim per DR-338 §D2's own citation (not a
  novel admission bound; `memo.send`'s `_commit_delivered_memo` is this
  module's precedent for the all-hooks-off commit mechanism, ported down to
  this op's narrower single-envelope shape rather than reused directly —
  `memo.send` also owns memo-specific concerns, schema validation, fan-out,
  summary caps, that have no counterpart in a tracker-event envelope).

Negative-spec — a future editor must NOT:
  - Add a direct cross-tree write (`open(write_root_for(...))` against a
    PEER target) — DR-338 D4 forbids shape (a) explicitly; the resolver's
    answer only ADDRESSES a peer delivery, it never opens a path for one.
    A LOCAL target (resolved root == caller's own worktree) still writes
    directly via `tracker_store.append_event` — that crossing is not
    cross-tree and is not DR-338's subject.
  - Hardcode any destination's literal `repos.*` key name, or any path
    naming a specific destination, anywhere in this module or its
    resolution chain — the D2a condition this docstring opens with. The
    destination is resolved SOLELY through the `tracker.holder_repo`
    registry key or the wire-supplied owner-qualified slug.
  - Add a per-destination declaration-field requirement (e.g. requiring a
    specific classification/sensitivity key on the event) — that is the
    destination's own annex (D2(6)), never this mechanism's bound.
  - Batch multiple events into one delivery invocation — D2(1), single
    envelope per invocation.
  - Change `tracker_store`'s signature or add a new parameter to any of its
    entrypoints — this op is a caller, never a co-owner of that module.
  - Import the underlying sovereign-tracker append/read module by any name
    other than `coordinator_core.tracker_store` (AC11's literal-token trap
    is about a DIFFERENT module name — see C3's own negative-spec — this
    module legitimately imports `tracker_store` directly, unlike
    `render_status`/`completion_policy`, because it IS the write seam).
  - Collapse `PushSuggestionMalformedError`/`UnauthorizedError`/
    `SchemaStaleError` into one generic exception, or drop the
    `caller_facing_validation = True` marker from any of the three — that
    marker is the only thing standing between a caller reading this op's own
    remediation text and reading the identical, unhelpful "Internal error:
    <ClassName>" every unclassified exception collapses to (C12/AC17).
  - Re-classify a `tracker_holder` rung OTHER than the non-member-slug
    refusal as `PushSuggestionUnauthorizedError` — the holder-role-unset /
    `repos.<key>`-unset / path-not-cloned / brightline rungs are operator
    misconfiguration, not a caller-supplied unauthorized identifier, and
    stay unclassified `RuntimeError`s propagated verbatim.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.git_native import check_ignore
from coordinator_core.ops.emit._slug import machine_slug
from coordinator_core.ops.fleet._common import (
    _make_git_env,
    check_repo_root,
    main_worktree_root,
)
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core import tracker_holder
from coordinator_core import tracker_store


#: F1 (the Director of Engineering boundary review, 2026-08-20) — the three C12 refusal classes'
#: stable, machine-readable discriminators, pinned as module constants so a
#: rename is caught by `test_refusal_class_values_are_pinned`. This is the
#: contract cockpit (or any process-boundary caller) builds against: the
#: value folded into `error["message"]`'s leading `refusal_class=<value>: `
#: token by `PushSuggestionRefused.__str__` below — see that class's
#: docstring for why this is the field that survives to the wire.
_REFUSAL_CLASS_MALFORMED = "malformed"
_REFUSAL_CLASS_UNAUTHORIZED = "unauthorized"
_REFUSAL_CLASS_SCHEMA_STALE = "schema_stale"
#: C3 (review-driven correction, 2026-08-20) — the D2(4) peer-delivery
#: collision, addressed on the minted event id after C1, is now evidence of
#: a successful prior delivery, not a malformed request; cockpit must be
#: able to treat it as success-on-retry. Pinned alongside the three C12
#: classes so a rename is caught by the same `test_refusal_class_values_
#: are_pinned` this joins.
_REFUSAL_CLASS_DUPLICATE_DELIVERY = "duplicate_delivery"


class PushSuggestionRefused(RuntimeError):
    """Raised when a `tracker.push_suggestion` request is refused before any
    write lands — a malformed event shape or a peer-delivery admission bound
    (D2(4) collision, D2(7) gitignore) failing. Distinguishes a REFUSAL
    (nothing was written) from a `TrackerStoreError`/`TrackerStoreDuplicateIdError`
    the local-write arm may also raise (propagated verbatim, not wrapped).

    The common umbrella for every refusal this module raises. C12's three
    named, distinguishable classes (`PushSuggestionMalformedError`,
    `PushSuggestionUnauthorizedError`, `PushSuggestionSchemaStaleError`,
    below) all subclass this — but not every `PushSuggestionRefused` is one
    of the three (the D2(4)/D2(7) peer-delivery admission refusals above are
    deliberately left unclassified; see module docstring § Rejection
    contract).

    F1 (the Director of Engineering boundary review, 2026-08-20): a subprocess caller's ONLY signal
    is the JSON-RPC error object printed to stdout — `_exit_code_for_response`
    collapses every non-structural-pin error to exit 1, so the three C12
    classes are NOT distinguishable by exit code alone. `refusal_class`
    (None on this base — the D2(7) unclassified refusal carries none) is
    folded into `str(self)` by the override below, which is the only field
    of the exception `ipc._handler_exception_error` preserves onto the wire
    (`error.message = str(exc)`, length-bounded) — so the discriminator is
    reachable at a FIXED path (`error["message"]`'s leading
    `refusal_class=<value>: ` token) in the dispatched response, not just to
    an in-process Python caller inspecting the exception's type.

    C3 (review-driven correction, 2026-08-20): `caller_facing_validation`
    is set True on THIS base class, not only the three C12 subclasses below.
    `ipc._handler_exception_error` only preserves an exception's own message
    when that flag is set — leaving it unset on the base collapsed the
    D2(4)/D2(7) refusals to the generic "Internal error: PushSuggestionRefused"
    shape at the wire, contradicting this module's own docstring claim that
    they carry "its own distinguishable message". `refusal_class` stays
    `None` on the base regardless — unclassified means no discriminator
    token, never a lost message."""

    refusal_class: Optional[str] = None
    caller_facing_validation = True

    def __str__(self) -> str:
        base = super().__str__()
        if self.refusal_class:
            return f"refusal_class={self.refusal_class}: {base}"
        return base


class PushSuggestionMalformedError(PushSuggestionRefused, ValueError):
    """C12 class 1/3 — MALFORMED: the request or event fails shape/schema
    validation (missing/wrong-typed `event`, `fleet-authored` scope missing
    `authority.emitter_repo`, a D3 `params.repo_root` mismatch, a non-int
    `contract_version`). Raised BEFORE any ownership resolution or write is
    attempted. No addressing-key requirement remains (C1, review-driven
    correction, 2026-08-20) — `_deliver_envelope` addresses the peer-arm
    filename on the minted `event["id"]`, never a caller-supplied field.

    Multiply inherits `ValueError` (pre-existing handler-level callers of
    this module already assert `pytest.raises(ValueError, ...)`) and
    `PushSuggestionRefused` (pre-existing `_resolve_owning_repo` callers
    already assert `pytest.raises(PushSuggestionRefused, ...)`) — narrows
    both, widens neither. `caller_facing_validation = True` (module
    docstring § Rejection contract) makes this class's own message the one
    `coordinator_core.ipc._handler_exception_error` surfaces.

    `refusal_class = _REFUSAL_CLASS_MALFORMED` (F1) — pinned so a rename is
    caught by `test_refusal_class_values_are_pinned`."""

    caller_facing_validation = True
    refusal_class = _REFUSAL_CLASS_MALFORMED


class PushSuggestionUnauthorizedError(PushSuggestionRefused):
    """C12 class 2/3 — UNAUTHORIZED: the request names an owning-repo slug
    that does not resolve to a `repos.*` registry member — reuses C5's own
    `tracker_holder._resolve_repos_key_for_slug` "is not a known repos.*
    member" refusal, re-raised at this op's boundary loud and
    distinguishable (see `_push_suggestion_sync`). Never raised for any
    OTHER `tracker_holder` failure rung (those are operator misconfiguration,
    not a caller-supplied unauthorized identifier — module docstring
    negative-spec).

    `refusal_class = _REFUSAL_CLASS_UNAUTHORIZED` (F1)."""

    caller_facing_validation = True
    refusal_class = _REFUSAL_CLASS_UNAUTHORIZED


class PushSuggestionSchemaStaleError(PushSuggestionRefused):
    """C12 class 3/3 — SCHEMA-STALE: the request's own `params.contract_version`
    names a version older than this op's own `_CONTRACT_VERSION` — the
    request was composed against a superseded wire contract for this op.
    Absence of `contract_version` is treated as current, never rejected
    (mirrors `tracker_transitions.py`'s documented "absence == version 1"
    convention for `schema_version`).

    `refusal_class = _REFUSAL_CLASS_SCHEMA_STALE` (F1)."""

    caller_facing_validation = True
    refusal_class = _REFUSAL_CLASS_SCHEMA_STALE


class PushSuggestionDuplicateDeliveryError(PushSuggestionRefused):
    """C3 (review-driven correction, 2026-08-20) — D2(4) PEER-DELIVERY
    COLLISION: `_deliver_envelope` addresses the delivered envelope's
    filename on the minted `event["id"]` (C1), so a collision on that path
    means an envelope carrying this exact id has already been delivered —
    evidence of a successful PRIOR delivery, never a malformed request.
    Cockpit (or any peer-arm caller) can treat this refusal as
    success-on-retry rather than something to work around.

    Distinct from the three C12 classes (malformed/unauthorized/
    schema-stale, all raised BEFORE any write is attempted) — this fires
    only on the peer arm, only after `_write_envelope_file` finds the exact
    target path already exists.

    `refusal_class = _REFUSAL_CLASS_DUPLICATE_DELIVERY`."""

    caller_facing_validation = True
    refusal_class = _REFUSAL_CLASS_DUPLICATE_DELIVERY


#: This op's own wire-contract version (module docstring § Rejection
#: contract, C12 class 3/3). Bump on any breaking change to the
#: `params`/`event` shape this handler requires; a caller supplying an older
#: `contract_version` is refused loud via `PushSuggestionSchemaStaleError`
#: rather than silently misinterpreted against the new shape.
_CONTRACT_VERSION = 1


# ---------------------------------------------------------------------------
# Ownership resolution (routes on the event payload, never a separate wire param)
# ---------------------------------------------------------------------------


def _resolve_owning_repo(event: dict) -> Optional[str]:
    """Resolve the `owning_repo` value to hand `tracker_holder.write_root_for`,
    reading it from the event payload itself (see module docstring § Ownership
    routing exception).

    `scope == "fleet-authored"` routes on `event["authority"]["emitter_repo"]`;
    every other payload routes on `event["repo"]`. An empty-string owner
    (cockpit's legacy "no owner" encoding) is translated to `None` here —
    `write_root_for` itself refuses a bare `""` (AC7) and requires the
    caller to have already made this translation.
    """
    if not isinstance(event, dict):
        raise PushSuggestionMalformedError(
            f"tracker.push_suggestion: event must be an object, got "
            f"{type(event).__name__}"
        )

    scope = event.get("scope")
    if scope == "fleet-authored":
        authority = event.get("authority")
        if not isinstance(authority, dict) or not authority.get("emitter_repo"):
            raise PushSuggestionMalformedError(
                "tracker.push_suggestion: event.scope == 'fleet-authored' "
                "requires a non-empty event.authority.emitter_repo naming "
                "the authoritative producer — the payload's own 'repo' is "
                "NOT used for this scope (cockpit's authoritative emitter "
                "can differ from the subject repo)"
            )
        owning = authority["emitter_repo"]
    else:
        owning = event.get("repo")

    if owning == "":
        return None
    if owning is not None and not isinstance(owning, str):
        raise PushSuggestionMalformedError(
            f"tracker.push_suggestion: resolved owning repo must be a "
            f"string or null, got {owning!r}"
        )
    return owning


# ---------------------------------------------------------------------------
# Peer-delivery envelope (DR-338 D1(a)/D2 — never a direct cross-tree write)
# ---------------------------------------------------------------------------


def _compose_envelope(owning_repo: Optional[str], event: dict) -> str:
    """Compose the delivered envelope's bytes — schema-valid YAML frontmatter
    (`kind`, `owning_repo`, `delivered_at`, optionally `sensitivity_tier`)
    plus the event bundle VERBATIM as a JSON body.

    F3 (the Director of Engineering boundary review, 2026-08-20) — PASSTHROUGH ONLY, never a
    hardcode or a default: if the caller's `event` carries a
    `sensitivity_tier` key, its value is lifted onto the envelope
    frontmatter verbatim; if absent, nothing is emitted for it. This names
    no destination anywhere (D2a stays intact) and does not condition on
    which destination is configured — the CURRENT destination's admission
    term for this field lives in its own annex (D2(6)), never here. See
    module docstring § Envelope shape.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    owning_line = f'owning_repo: "{owning_repo}"' if owning_repo else "owning_repo: null"
    frontmatter_lines = [
        "---",
        "kind: sovereign-tracker-event",
        owning_line,
        f'delivered_at: "{now}"',
    ]
    sensitivity_tier = event.get("sensitivity_tier")
    if sensitivity_tier is not None:
        frontmatter_lines.append(f"sensitivity_tier: {json.dumps(sensitivity_tier)}")
    frontmatter_lines += ["---", ""]
    frontmatter = "\n".join(frontmatter_lines)
    body = json.dumps(event, indent=2, sort_keys=True)
    return frontmatter + body + "\n"


def _write_envelope_file(target_path: Path, content: str) -> None:
    """O_EXCL exclusive create — D2(4) fail-loud on collision, never a
    clobbering overwrite. Mirrors `memo.send`'s `_write_memo_file` (same
    newline="\\n" discipline: this write lands in a foreign, possibly-
    Windows-cloned working tree, and Python text mode would otherwise
    translate "\\n" to "\\r\\n" there regardless of the receiver's own
    .gitattributes)."""
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


#: Deliverable this op ships under, stamped as a trailer on every delivery
#: commit so a receiver can attribute a file that appeared in their tree
#: without any of their operators running anything.
_DELIVERABLE_ID = "dlv-sat-06"


def _unstage(runner, rel_path: str) -> None:
    """Drop *rel_path* from the RECEIVER's index after a failed delivery commit.

    Leaves the envelope written-but-untracked, which is exactly the state the
    detached/bare/unborn-HEAD arm above already documents as safe for the
    receiver's own sweep — so both failure arms converge on one state instead
    of diverging. Without this, a failed commit strands our file STAGED in a
    foreign repo's index, where the receiver's next pathspec-less commit lands
    it inside an unrelated commit attributed to them: a cross-tree write
    arriving in their history without their action, which is the harm DR-214's
    admission discipline exists to prevent.

    `git commit` failing on `index.lock` contention is the ORDINARY outcome in
    a receiver running its own concurrent sessions under this box's documented
    50-70 concurrent-LLM load norm, not an exceptional one — this arm is on the
    expected path.

    Best-effort and never raises: if the unstage itself fails there is nothing
    further this op can safely do inside a repo it does not own, and raising
    here would mask the original delivery failure the caller needs to see.
    """
    try:
        runner(["restore", "--staged", "--", rel_path])
    except OSError:
        pass


def _delivery_commit_message(rel_path: str) -> str:
    """Delivery commit subject plus attribution trailers.

    A delivery lands a file in a repo whose operators did not run anything;
    without trailers they cannot tell which session or deliverable put it
    there. `Session-Id` is read from the environment rather than resolved, so
    it is omitted rather than guessed when absent.
    """
    lines = [
        f"cross-repo: deliver sovereign-tracker event {rel_path}",
        "",
        f"Deliverable-Id: {_DELIVERABLE_ID}",
    ]
    session_id = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if session_id:
        lines.append(f"Session-Id: {session_id}")
    return "\n".join(lines)


def _commit_envelope(receiver_repo_path: Path, rel_path: str) -> dict:
    """Stage+commit ONLY the just-delivered envelope in the RECEIVER repo,
    all-hooks-off (D2(3)) — `-c core.hooksPath=<empty-tmpdir>`, NEVER
    `--no-verify` (does not bypass `prepare-commit-msg`). No foreign branch
    creation: a detached/bare/unborn HEAD leaves the file written but
    uncommitted for the receiver's own sweep. Never raises — every failure
    arm returns `{"committed": False, "reason": <str>}`.
    """
    env = _make_git_env()

    def _run(args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(receiver_repo_path), *args],
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=env,
            **no_console_creationflags(),
        )

    try:
        head_check = _run(["symbolic-ref", "-q", "HEAD"])
    except OSError as exc:
        return {"committed": False, "reason": f"could not read receiver HEAD: {exc}"}
    if head_check.returncode != 0:
        return {
            "committed": False,
            "reason": "no active branch (detached HEAD, bare repo, or unborn HEAD)",
        }

    try:
        add_result = _run(["add", "--", rel_path])
    except OSError as exc:
        return {"committed": False, "reason": f"could not run git add: {exc}"}
    if add_result.returncode != 0:
        return {"committed": False, "reason": f"git add failed: {add_result.stderr.strip()}"}

    try:
        with tempfile.TemporaryDirectory() as empty_hooks_dir:
            commit_result = subprocess.run(
                [
                    "git", "-C", str(receiver_repo_path),
                    "-c", f"core.hooksPath={empty_hooks_dir}",
                    "-c", "commit.gpgsign=false",
                    "commit", "-m",
                    _delivery_commit_message(rel_path),
                    "--", rel_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL,
                env=env,
                **no_console_creationflags(),
            )
    except OSError as exc:
        _unstage(_run, rel_path)
        return {"committed": False, "reason": f"could not run git commit: {exc}"}

    if commit_result.returncode != 0:
        combined = (commit_result.stdout + commit_result.stderr).lower()
        if (
            "nothing to commit" in combined
            or "nothing added to commit" in combined
            or "no changes added to commit" in combined
        ):
            # Idempotent no-op — the path is already committed as-is.
            return {"committed": True, "reason": None}
        _unstage(_run, rel_path)
        return {
            "committed": False,
            "reason": f"git commit failed: {commit_result.stderr.strip()}",
        }
    return {"committed": True, "reason": None}


def _deliver_envelope(target_root: Path, owning_repo: Optional[str], event: dict) -> dict:
    """Peer-delivery arm: compose + write + commit ONE envelope into
    `target_root`'s `cross-repo/inbox/` — DR-338 D1(a)/D2, never a direct
    cross-tree write (D4). `target_root` is always registry-resolved by
    `tracker_holder.write_root_for` before this function is ever called
    (D2(2), registry-enumerated receiver only — never wire-derived).

    Addressed on the MINTED `event["id"]`, never a caller-supplied field
    (C1, review-driven correction, 2026-08-20). `_push_suggestion_sync`
    assigns `event["id"]` before routing, so it is guaranteed present here
    — asserted, not defaulted. Two reasons this is the only correct
    addressing key:

      - Idempotency, date-independently. `event["id"]` is itself
        content-derived and (with `idempotency_key` supplied) stable across
        a same-payload retry — see `_mint_event_id`. Addressing the
        filename on it means a retry re-derives the SAME filename and
        `O_EXCL` refuses the duplicate delivery regardless of which UTC day
        each attempt lands on. The prior `{today}-...-{item_id}` shape
        broke exactly this across a midnight boundary: same payload, same
        key, different date prefix, different filename, no collision, a
        second envelope silently delivered.
      - Confinement. The caller-supplied `item_id`/`event_id` this filename
        used to be built from received no path-hostile treatment beyond
        `.strip()` — a value like `"../../../../escaped"` composes real
        `..` path segments that `mkdir(parents=True)` and `os.open` both
        resolve, verified to escape the receiver repository entirely.
        `event["id"]` is machine-minted with a fixed, grammar-constrained
        shape (`evt-<machine_slug>-<12 hex>`, see `_mint_event_id`) and was
        never caller-supplied text in the first place — a caller-supplied
        `event.id` is refused loud before this function is ever reached
        (the handler's `if "id" in event` check). This is not a sanitizer
        bolted onto a caller-controlled string; it is removing the
        caller-controlled string from the path entirely. See also the
        `target_path.resolve()` confinement assertion below, which exists
        so a future editor cannot reintroduce a caller-controlled path
        component here without an explicit, loud failure.
    """
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise PushSuggestionRefused(
            "tracker.push_suggestion: _deliver_envelope reached with no "
            "minted event['id'] — _push_suggestion_sync must mint it "
            "before routing; refusing rather than composing a delivery "
            "path from an absent addressing key"
        )
    filename = f"tracker-event-{event_id}.md"
    rel_path = f"cross-repo/inbox/{filename}"
    target_path = target_root / "cross-repo" / "inbox" / filename

    # Confinement: the composed path must resolve inside the receiver's
    # inbox and nowhere else. Hardens against a future editor reintroducing
    # a caller-controlled path component into `filename` above — this
    # assertion, not just the minted-id addressing, is what a regression
    # trips (P1-2, review-driven correction, 2026-08-20).
    inbox_root = (target_root / "cross-repo" / "inbox").resolve()
    if target_path.resolve().parent != inbox_root:
        raise PushSuggestionRefused(
            f"tracker.push_suggestion: composed delivery path "
            f"{target_path} resolves outside the receiver's inbox "
            f"{inbox_root} — refusing; nothing was written"
        )

    # D2(7) gitignore delivery guard — refuse-on-ignored, BEFORE any write.
    # `check_ignore`'s own contract is three-valued: 0 = ignored,
    # 1 = evaluated and not ignored, >=2 = could not evaluate. Admitting on
    # anything but 1 (review-driven correction, 2026-08-20) means a
    # receiver mid-rebase or holding an `index.lock` — ordinary at this
    # box's documented load norm — no longer silently loses the guard.
    ignore_result = check_ignore(target_root, [rel_path])
    if ignore_result.returncode == 0:
        raise PushSuggestionRefused(
            f"tracker.push_suggestion: delivery path {rel_path!r} is "
            f"gitignored in the receiver repo {target_root} — refusing "
            "(D2(7)); nothing was written"
        )
    if ignore_result.returncode != 1:
        raise PushSuggestionRefused(
            f"tracker.push_suggestion: could not evaluate gitignore state "
            f"for delivery path {rel_path!r} in the receiver repo "
            f"{target_root} (check-ignore exit {ignore_result.returncode}) "
            "— refusing rather than admitting on an unevaluated guard "
            "(D2(7)); nothing was written"
        )

    content = _compose_envelope(owning_repo, event)
    try:
        _write_envelope_file(target_path, content)
    except FileExistsError as exc:
        raise PushSuggestionDuplicateDeliveryError(
            f"tracker.push_suggestion: delivery target {target_path} "
            "already exists — refusing to clobber (D2(4) fail-loud on "
            "collision). Addressed on the minted event id, this is "
            "evidence of a successful prior delivery, not a malformed "
            "request; safe to treat as success-on-retry."
        ) from exc

    outcome = _commit_envelope(target_root, rel_path)
    return {
        "delivered": "peer",
        "path": str(target_path),
        "committed": outcome["committed"],
        "commit_reason": outcome["reason"],
    }


def _write_local(worktree: Path, event: dict) -> dict:
    """Local arm: the target IS the caller's own repo — a direct, same-repo
    write through `tracker_store.append_event`. Not a cross-tree crossing
    and not DR-338's subject (see module docstring negative-spec)."""
    stored = tracker_store.append_event(dict(event), repo_root=worktree)
    return {"delivered": "local", "event": stored}


def _mint_event_id(event: dict, idempotency_key: Optional[str] = None) -> str:
    """Mint this event's `id` — claude-klabauter mints, the caller never supplies one
    (RULING 2026-08-20, C4). DR-241 bound (i) requires the event id be
    content-derived AND carry a global-uniqueness guarantee; an id minted by
    a foreign, cross-language process is not something this op can affirm
    that for.

    Follows the landed `tracker_entities.py::_mint_event_id` shape:
    `evt-<machine_slug()>-<digest12>`, `digest12` a SHA-256 hexdigest prefix
    (12 hex chars) over the canonical JSON of the caller's event content
    plus a per-event nonce folded in — without a nonce, two structurally-
    identical events pushed back to back would re-derive the SAME id and
    collide against `tracker_store.append_event`'s own-shard duplicate guard
    (see that sibling function's docstring for the full "why a nonce"
    argument, which applies here unchanged). The `<machine>-` component
    supplies the global-uniqueness half of bound (i); the digest supplies
    the content-derived half.

    F5 (the Director of Engineering boundary review, 2026-08-20) — idempotency under retry: the
    transport gives a spawned-subprocess caller ONLY an exit code (module
    docstring § Transport), so an ambiguous failure after
    `tracker_store.append_event` has already committed is indistinguishable
    from one before it, and the caller's correct response to that ambiguity
    is to retry. A wall-clock nonce makes that retry re-mint a DIFFERENT id
    for the SAME logical event and double-append. When the caller supplies
    `idempotency_key` (its own event id — the same field it is forbidden to
    send as `id`, see `PushSuggestionMalformedError`), that value is folded
    into the digest IN PLACE OF the wall-clock nonce, so a same-payload,
    same-key retry re-derives the SAME id and collides on
    `tracker_store.append_event`'s own duplicate-id guard instead of
    double-appending. Bound (i) stays satisfied either way — the DIGEST
    stays content-derived (P3-3, review-driven correction, 2026-08-20: it
    is the *id*, not the digest, that is machine-qualified — the
    `<machine>-` prefix carries that half, applied outside the digest
    below); `idempotency_key` is an INPUT to the digest's derivation, never
    a stored key (the caller-supplied `id` refusal is unchanged — different
    field, different role). Absent `idempotency_key`, today's wall-clock
    nonce behaviour is unchanged. Two machines pushing an identical payload
    with an identical key therefore produce the SAME digest and DIFFERENT
    ids — correct, and the reason the digest/id distinction above matters.

    P3-1 (review-driven correction, 2026-08-20): the two nonce arms are
    tagged (`("k", idempotency_key)` vs `("t", timestamp)`) so they cannot
    alias onto the same digest input — without the tag, a caller whose
    `idempotency_key` happened to equal an ISO-8601 microsecond timestamp
    would collide with the wall-clock arm's derivation for that instant.
    """
    if idempotency_key:
        nonce: Any = ("k", idempotency_key)
    else:
        nonce = (
            "t",
            datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="microseconds"
            ),
        )
    canonical = json.dumps((event, nonce), sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"evt-{machine_slug()}-{digest}"


#: The exact, stable substring `tracker_holder._resolve_repos_key_for_slug`
#: raises for a slug with no `repo_slug.<slug>` registry index entry (C5's
#: own non-member-slug refusal) — the ONE `tracker_holder` rung this op
#: reclassifies as `PushSuggestionUnauthorizedError` (module docstring
#: negative-spec: every OTHER rung is operator misconfiguration, not a
#: caller-supplied unauthorized identifier, and stays unclassified).
_NON_MEMBER_SLUG_MARKER = "is not a known repos.* member"


def _push_suggestion_sync(
    worktree: Path, event: dict, idempotency_key: Optional[str] = None
) -> dict:
    """Synchronous body of the handler — resolves ownership, routes
    local-vs-peer, and performs the write. Run via `asyncio.to_thread` from
    the async JSON-RPC handler (blocking file/subprocess I/O).

    `idempotency_key` (F5, optional wire field) — folded into `_mint_event_id`
    in place of the wall-clock nonce when supplied, see that function's
    docstring."""
    owning_repo = _resolve_owning_repo(event)
    event = dict(event)
    event["id"] = _mint_event_id(event, idempotency_key=idempotency_key)
    try:
        target_root = tracker_holder.write_root_for(owning_repo=owning_repo, repo_root=worktree)
    except RuntimeError as exc:
        if _NON_MEMBER_SLUG_MARKER in str(exc):
            raise PushSuggestionUnauthorizedError(
                f"tracker.push_suggestion: {exc}"
            ) from exc
        raise

    if target_root.resolve() == worktree.resolve():
        return _write_local(worktree, event)
    return _deliver_envelope(target_root, owning_repo, event)


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("tracker.push_suggestion")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """tracker.push_suggestion — resolve ownership for one sovereign-tracker
    item-event bundle and route it: local write for a local target, DR-338
    delivery envelope for a peer target. See module docstring for the full
    contract.

    Wire contract:
        params: {
            "event": dict — the item-event bundle (required),
            "repo_root": str (optional — D3 consistency check only),
            "contract_version": int (optional — C12 schema-stale check;
                absence treated as current, see module docstring).
            "idempotency_key": str (optional, F5 — folded into the minted
                event id's digest in place of the wall-clock nonce so a
                same-payload retry collides on tracker_store's duplicate-id
                guard instead of double-appending; see _mint_event_id).
        }
        ->      `_write_local`'s `{"delivered": "local", "event": ...}` or
                `_deliver_envelope`'s `{"delivered": "peer", "path": ...,
                "committed": bool, "commit_reason": str | None}`.

    `repo_root` handler arg is the git common dir (`_OP_KEY_SCOPE:
    "common_dir"`); the worktree root is derived via
    `main_worktree_root(repo_root)` — never from `params.repo_root`.

    Raises (C12's three named, distinguishable, caller-facing classes —
    module docstring § Rejection contract):
        RuntimeError — repo_root is None (engine misconfiguration; production
            always supplies it via `_OP_KEY_SCOPE='common_dir'`). Not one of
            the three — this is a local engine wiring fault, not a rejected
            inbound request.
        PushSuggestionMalformedError — event missing/not a dict, a D3
            `repo_root` mismatch, a non-int `contract_version`, or any other
            shape/schema validation failure (subclasses `ValueError` and
            `PushSuggestionRefused`).
        PushSuggestionUnauthorizedError — the resolved owning-repo slug is
            not a known `repos.*` member (C5's refusal, reclassified here).
        PushSuggestionSchemaStaleError — `params.contract_version` names a
            version older than `_CONTRACT_VERSION`.
        PushSuggestionRefused — a peer-delivery admission bound refused
            (D2(4) collision, D2(7) gitignore) — NOT one of the three C12
            classes, its own distinguishable message.
        TrackerStoreError / TrackerStoreDuplicateIdError — propagated
            verbatim from `tracker_store.append_event` (local arm).
        RuntimeError — propagated verbatim from `tracker_holder.write_root_for`
            on any failure rung OTHER than the non-member-slug refusal
            (holder unset, not cloned, etc.) — operator misconfiguration,
            not one of the three C12 classes.
    """
    if repo_root is None:
        raise RuntimeError(
            "tracker.push_suggestion: repo_root is None — "
            "_OP_KEY_SCOPE='common_dir' should always supply it in production; "
            "test fixtures must supply an explicit value"
        )
    common_dir = Path(repo_root)
    worktree = main_worktree_root(common_dir)

    # D3: optional repo_root consistency check (contract §3.3 doctrine).
    mismatch = check_repo_root(params.get("repo_root"), common_dir)
    if mismatch:
        raise PushSuggestionMalformedError(f"tracker.push_suggestion: {mismatch}")

    event = params.get("event")
    if not isinstance(event, dict):
        raise PushSuggestionMalformedError(
            f"tracker.push_suggestion: params.event must be an object, got "
            f"{type(event).__name__}"
        )

    # claude-klabauter mints the event id (RULING 2026-08-20, C4) — a caller-supplied
    # `id` is refused loud, never silently overwritten (which would leave
    # the caller believing its id is the stored key).
    if "id" in event:
        raise PushSuggestionMalformedError(
            "tracker.push_suggestion: event must not carry a caller-"
            "supplied 'id' — claude-klabauter mints the event id itself (DR-241 "
            "bound (i)); refusing rather than silently overwriting it"
        )

    # C12 class 3/3 — schema-stale: an explicit, older contract_version is
    # refused loud; absence is treated as current (module docstring).
    contract_version = params.get("contract_version")
    if contract_version is not None:
        if not isinstance(contract_version, int) or isinstance(contract_version, bool):
            raise PushSuggestionMalformedError(
                f"tracker.push_suggestion: params.contract_version must be "
                f"an int, got {type(contract_version).__name__}"
            )
        if contract_version < _CONTRACT_VERSION:
            raise PushSuggestionSchemaStaleError(
                f"tracker.push_suggestion: params.contract_version="
                f"{contract_version} is superseded — this op is on "
                f"contract_version={_CONTRACT_VERSION}; the caller must be "
                "rebuilt against the current wire contract"
            )

    # F5 (the Director of Engineering boundary review, 2026-08-20) — optional idempotency key,
    # folded into the minted id's digest in place of the wall-clock nonce so
    # a same-payload retry collides on tracker_store's duplicate-id guard
    # instead of double-appending (see _mint_event_id).
    idempotency_key = params.get("idempotency_key")
    if idempotency_key is not None and (
        not isinstance(idempotency_key, str) or not idempotency_key.strip()
    ):
        raise PushSuggestionMalformedError(
            f"tracker.push_suggestion: params.idempotency_key must be a "
            f"non-empty string when supplied, got {idempotency_key!r}"
        )

    return await asyncio.to_thread(
        _push_suggestion_sync, worktree, event, idempotency_key
    )
