"""
coordinator_core.ops.handoff_stamp — JSON-RPC "handoff.stamp" operation.

Purpose: Port of coordinator/bin/stamp-shipped-in.js into the coordinator_core
resident service. Inserts ``shipped_in: <SHA>`` into a handoff's YAML frontmatter
immediately after ``claimed_at:`` (anchored insert; DR-084 new-vocabulary field),
falling back to ``consumed_at:`` for archived-schema records that predate the
rename, or appends to end of frontmatter when neither is present. Idempotent by
default — returns exit_code 0 with applied=False when ``shipped_in`` is already
present. ``force=True`` (paired with a caller-supplied ``sha``) opts out of the
idempotent skip and REPLACES the existing value instead — the provenance-repair
escape added 2026-07-22 after a sibling ``ship-handoff`` call stamped a
concurrent peer session's sha with no authorized way to correct it.

SHA quoting (parity-critical, three guards from stamp-shipped-in.js serializeYamlScalar):
  - SHA contains structural YAML characters (e.g. ``#``) → single-quoted
  - SHA is all-numeric (e.g. ``274671833``)             → YAML parses as integer; quoted
  - SHA matches YAML 1.1 scientific-notation pattern
    (e.g. ``1958e194``)                                 → YAML 1.1 auto-coerce; quoted
All three are activated via ``serialize_yaml_scalar(..., numeric_quoting=True)``
from coordinator_core.frontmatter.primitives.

Exit-code contract (simple dict envelope — NOT the fleet {mode,dry_run,candidates} shape):
  exit_code 0, applied True  — field inserted
  exit_code 0, applied False — field already present (idempotent skip)
  exit_code 1, applied False — error (missing params, bad frontmatter, I/O failure)

Self-registration: importing this module fires ``@register_op("handoff.stamp", _handler)``
as a side-effect. Add this module to coordinator_core/ops/__init__.py to trigger
registration at start_server() time.

P9 WORKTREE DERIVATION: ``_OP_KEY_SCOPE`` keys this op ``"common_dir"``, so
``repo_root`` arrives as ``<worktree>/.git``. All state/handoffs/ paths are built
from ``main_worktree_root(repo_root)`` — never from ``repo_root`` directly (which
would scan .git/state/, always empty).

Spec backlink: coordinator/bin/stamp-shipped-in.js (exact port)

Negative-spec (hard-won):
  - Does NOT git-commit. Pure frontmatter file mutation only.
  - Does NOT validate SHA format — any non-empty string is accepted.
  - Does NOT read ctx.repo_root (None in global service); uses the repo_root arg.
  - Does NOT use the fleet {mode, dry_run, candidate_ids} envelope (this is a
    per-file mutation, not a fleet-batch op).
  - Does NOT let force=True silently resolve a sha of its own — force always
    replaces with the CALLER-SUPPLIED sha param (required regardless of force),
    never with a value this op derives. Resolving-and-overwriting under force
    is the exact hazard (concurrent-peer-sha stamping, 2026-07-22 incident)
    this escape exists to repair, not repeat.

``_repair_archived_shipped_in_handler`` (2026-07-22, provenance-repair verb):

A SEPARATE, deliberately-not-``@register_op``-registered handler for repairing
``shipped_in`` on a handoff that has ALREADY been archived (``archive/handoffs/``).
Exists solely because a 2026-07-22 corpus audit found 8 archived handoffs with a
mis-stamped ``shipped_in`` and no lifecycle verb (ship/consume/supersede/repark/
stamp) can reach ``archive/handoffs/`` — that containment is this file's own
``_handler``'s deliberate restriction (see its allowed_roots note above), and
archival-freezes-the-record is a correct invariant this repair verb does NOT
relax for any other writer. This is a NARROW, SEPARATE door: its own
``allowed_roots = [worktree / "archive" / "handoffs"]`` is a local variable
scoped to this function alone — ``_handler``'s ``state/handoffs/``-only
allowed_roots above is untouched, so every other verb's containment is
byte-identical before and after this addition.

Not registered via ``@register_op``: registering a new op name would require a
matching ``OP_CLASSIFICATION`` entry (DR-208's drift-guard,
``coordinator_core/authz/classification.py``) and an ``_OP_KEY_SCOPE`` entry
(``coordinator_core/op_scopes.py``) purely to satisfy CI plumbing this
single-purpose repair tool does not need — it is called directly in-process
by ``coordinator_core.archive_stamp.cs_repair_archived_shipped_in``, exactly as
``cs_ship_handoff`` calls ``handoff_archive_transition._handler`` directly,
never through the JSON-RPC dispatch surface.

Negative-spec (repair verb, hard-won):
  - NEVER resolves a sha. No scope-path git-log lookup, no branch-tip fallback,
    no ownership-guard path (contrast ``archive_stamp.stamp_shipped_in``, which
    has all three) — the caller must name the exact sha, or pass ``unset=True``
    to clear the field. A repair verb that resolves would reintroduce the
    exact defect (a wrong sha stamped without human review) it exists to clean up.
  - Requires a non-empty ``reason`` on every call, sha-repair or unset alike —
    an archived-record mutation with no recorded justification is strictly
    worse than no repair verb at all. ``reason`` is returned in the response
    for the caller's own audit trail (this handler does not itself persist a
    ledger — recording the disposition is the calling agent's job).
  - Does NOT extend archived-record mutability beyond ``shipped_in`` and
    ``shipped_in_kind`` for THIS door — only its sibling
    ``_repair_archived_deployment_state_handler`` (added 2026-07-26, own
    field set: ``deployment_state``/``continued_into``/``closed_reason``)
    shares the archive/handoffs/ allowed_roots; no other field and no other
    archived-lifecycle verb reaches it. ``archive/handoffs/`` remains closed
    to ``handoff.transition``/``handoff.stamp``/``handoff.stamp_phase``/etc.
    for every field these two narrow repair doors do not name. Archival
    otherwise remains a freeze.
  - ``sha`` and ``unset=True`` are mutually exclusive — supplying both fails
    loud (no write) rather than picking one silently.
  - Does NOT store the caller-supplied ``sha`` verbatim — it is truncated to
    8 chars before write, matching ``stamp_shipped_in``'s own format
    contract. A repair must not be distinguishable from a correct original
    by format alone; a second writer skipping this truncation (the exact
    ``boot_sweep._stamp_shipped_in_besteff`` divergence this workstream
    exists to clean up) is the failure mode this line exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable, Optional

from coordinator_core.artifact_basename import md_fallback_candidates
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root

_LOG = logging.getLogger(__name__)

# shipped_in_kind discriminant (DR-096, DoE-claude 2026-07-26 ruling) — the
# enum tagging WHICH sanctioned resolution produced a shipped_in value. Kept
# here (not just in archive_stamp.py) because this handler is the one
# structural point THREE of the four current writers already converge on:
# `archive_stamp.stamp_shipped_in` (wraps this handler), plus
# `consumed_handoff_stamp.py` and `handoff_ship_archive.py` (both call this
# handler directly, bypassing that wrapper, but still pass `kind` through the
# same param this handler validates). The fourth writer
# (ops.normalize_claimed_frontmatter) still bypasses this handler's RMW path
# entirely (it has no live file to lock through `_handler`, only marker text
# to translate) but no longer keeps its own copy of the value grammar — it
# imports `_SHA_HEX_RE`/`_NO_COMMIT_TOKEN_RE` from `shipped_in_tokens` (the
# leaf module those two regexes were moved to, out of `archive_stamp`, to
# break an import cycle -- state/debt-backlog/DSR-2026-08-13-archive-stamp-
# import-order-drops-an-op-from-the-registry.yaml) and writes
# `shipped_in_kind` in lockstep, same as this handler does.
_SHIPPED_IN_KIND_ENUM = frozenset(
    {"ship-commit", "successor", "scope-derived", "no-commit"}
)


# ---------------------------------------------------------------------------
# Mutate-closure builder — factored out of `_handler` (2026-07-28, ceremony-
# lock-hold-resurrection Row 7) so a caller that needs to run extra
# verification INSIDE the same `locked_rmw` lock hold — the ceremony's
# live-children re-check, `coordinator_core.ops.ceremony.consumed_handoff_
# stamp._stamp_with_live_children_recheck` — can wrap this mutate in its own
# composite callable instead of duplicating the frontmatter-mutation logic.
# `_handler` itself is just the thinnest possible caller of this function;
# behavior for every existing caller (`archive_stamp.stamp_shipped_in`,
# `handoff_ship_archive.py`, this module's own `_handler`) is unchanged.
# ---------------------------------------------------------------------------


def build_stamp_mutate(
    handoff_path_raw: str, sha: str, kind: Optional[str], *, force: bool = False
) -> "tuple[Callable[[str], str], dict]":
    """Build the ``(mutate, state)`` pair `locked_rmw` needs for a
    ``handoff.stamp`` write, without acquiring the lock or performing any
    I/O itself.

    ``mutate`` is a pure ``str -> str`` callable, exactly what `_handler`
    passed to `locked_rmw` before this extraction — same idempotent-skip,
    same force-replace escape, same DR-096 ``shipped_in_kind`` lockstep
    write, same SHA-quoting guards. ``kind`` is NOT validated here (the enum
    check lives in `_handler`, before params reach this point) — callers
    outside `_handler` must validate their own ``kind`` first.

    ``state`` is a dict of single-element lists the closure writes into as
    it runs — ``{"applied": [bool], "replaced": [bool], "prior_value":
    [str|None], "prior_kind": [str|None]}`` — read AFTER `locked_rmw`
    returns (or raises `MutateAbort`, in which case ``state`` still holds
    its untouched defaults).
    """
    _applied = [False]
    _replaced = [False]
    _prior_value: list[Optional[str]] = [None]
    _prior_kind: list[Optional[str]] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"no valid YAML frontmatter block in: {handoff_path_raw} — "
                "stamp-shipped-in.js: file has no valid YAML frontmatter block"
            )
        existing = read_fm_field(split.fm_text, "shipped_in")
        if existing is not None:
            if not force:
                # Idempotent: return unchanged text so locked_rmw skips the write.
                # kind is left untouched here too — see the kind param doc
                # ("left COMPLETELY UNTOUCHED" on the idempotent-skip path):
                # this handler never does a partial write of shipped_in_kind
                # alone when shipped_in itself is not being (re)written.
                return old_text
            # force=True: replace the existing value in place — the explicit
            # provenance-repair escape (see module Negative-spec). numeric_quoting
            # matches the insert path's own SHA guards.
            _prior_value[0] = existing
            new_fm = replace_fm_field(split.fm_text, "shipped_in", sha, numeric_quoting=True)
            if kind is not None:
                # shipped_in_kind is rewritten IN LOCKSTEP with a force-replaced
                # shipped_in (DR-096) — read the prior value from the
                # already-mutated new_fm (identical content to split.fm_text at
                # this key; replace_fm_field only touched shipped_in) so a
                # caller can audit what kind was overwritten, same shape as
                # prior_value above.
                _prior_kind[0] = read_fm_field(new_fm, "shipped_in_kind")
                if _prior_kind[0] is not None:
                    new_fm = replace_fm_field(new_fm, "shipped_in_kind", kind)
                else:
                    new_fm = insert_fm_field(new_fm, "shipped_in_kind", kind, after_key="shipped_in")
            _applied[0] = True
            _replaced[0] = True
            return rebuild(split, new_fm)
        # Insert shipped_in after claimed_at (DR-084 new-vocabulary anchor);
        # archived-schema records that predate the claimed_at rename fall back to
        # anchoring on consumed_at (insert_fm_field falls back to append-at-end
        # itself when neither key is found).
        # numeric_quoting=True activates the three SHA guards from stamp-shipped-in.js:
        #   - structural chars (e.g. '#') — always quoted by _STRUCTURAL_RE
        #   - all-numeric SHA             — _ALL_NUMERIC_RE guard (F0 integer coerce)
        #   - scientific-notation SHA     — _SCIENTIFIC_RE guard (F0 YAML 1.1 float coerce)
        if read_fm_field(split.fm_text, "claimed_at") is not None:
            after_key = "claimed_at"
        else:
            after_key = "consumed_at"
        new_fm = insert_fm_field(
            split.fm_text,
            "shipped_in",
            sha,
            after_key=after_key,
            numeric_quoting=True,
        )
        if kind is not None:
            # shipped_in_kind is inserted IN LOCKSTEP, immediately after the
            # shipped_in it was just anchored on (DR-096) — never as a
            # standalone write.
            new_fm = insert_fm_field(new_fm, "shipped_in_kind", kind, after_key="shipped_in")
        _applied[0] = True
        return rebuild(split, new_fm)

    state = {
        "applied": _applied,
        "replaced": _replaced,
        "prior_value": _prior_value,
        "prior_kind": _prior_kind,
    }
    return _mutate, state


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("handoff.stamp")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.stamp" handler.

    Port of coordinator/bin/stamp-shipped-in.js. Inserts ``shipped_in: <SHA>``
    immediately after ``claimed_at:`` in the handoff's YAML frontmatter (anchored
    insert; DR-084 new-vocabulary field), falling back to ``consumed_at:`` for
    archived-schema records that predate the rename, or appends to end if neither
    is present. Idempotent: returns exit_code 0, applied=False when ``shipped_in``
    is already present.

    Params:
        handoff_path  (str)  — absolute or repo-relative path to the handoff file.
                               Required.
        sha           (str)  — commit SHA (or the sanctioned no-commit token) to
                               stamp as shipped_in value. Required.
        force         (bool) — when True and ``shipped_in`` is already present,
                               REPLACE it with ``sha`` instead of taking the
                               idempotent no-op. Default False. Origin: 2026-07-22
                               incident — a sibling ``ship-handoff`` call stamped a
                               concurrent peer session's sha with no authorized way
                               to repair it (see the module-level Negative-spec
                               entry below).
        kind          (str)  — REQUIRED discriminant for how ``sha`` was resolved;
                               one of ``ship-commit | successor | scope-derived |
                               no-commit`` (DR-096). Written as ``shipped_in_kind:``
                               immediately after ``shipped_in:`` (inserted or
                               replaced in lockstep with it). Omitted (``None``/
                               empty) is REJECTED (exit_code 1, no write) — this
                               is the single choke point DR-096 requires: "a
                               caller that does not say which kind it is writing
                               is exactly how this field acquired five meanings."
                               This handler does NOT guess a kind on the
                               caller's behalf; every caller must name one
                               explicitly, mirroring ``archive_stamp.
                               stamp_shipped_in``'s own keyword-only,
                               no-default ``kind`` parameter. A malformed
                               (non-enum, non-empty) ``kind`` is also rejected
                               (exit_code 1, no write).

    Returns a dict with keys:
        exit_code  (int)   — 0 ok (insert, replace, or skip) / 1 error
        applied    (bool)  — True if field was inserted or replaced; False if
                             already present and force was not set, or on error.
        replaced   (bool)  — True only when force replaced an existing value
                             (present, default False, on every response).
        prior_value (str|None) — the value that was overwritten, when replaced is
                             True; None otherwise. Makes a corrected stamp
                             auditable rather than silent.
        kind       (str|None) — echoes the ``kind`` param verbatim (None if
                             omitted), present on every response (error or not).
        prior_kind (str|None) — the ``shipped_in_kind`` value that was
                             overwritten, when replaced is True and kind was
                             supplied; None otherwise.
        message    (str)   — present on exit_code 0; human-readable outcome description.
        error      (str)   — present on exit_code 1; human-readable reason.

    Exit-code contract:
        exit_code 0, applied True,  replaced False — shipped_in inserted (was absent)
        exit_code 0, applied True,  replaced True  — shipped_in force-replaced (was present)
        exit_code 0, applied False, replaced False — shipped_in already present; no-op (idempotent); message set
        exit_code 1, applied False — error; error field set with reason (includes an
                                     unknown/malformed ``kind``)

    Negative-spec:
      - Does NOT commit. Pure in-place frontmatter file write only.
      - Does NOT validate SHA format or length — any non-empty string is stamped.
        (Deliberately preserved: this handler is a low-level primitive shared by
        callers that intentionally exercise pathological SHA-quoting inputs —
        see test_sha_quoting_hash_in_sha et al. — so SHA/no-commit-token value
        SHAPE validation stays the sole responsibility of
        ``archive_stamp.stamp_shipped_in``, the validated wrapper. Only the
        ``kind`` ENUM is checked here — see the ``kind`` param doc above for why
        that check is safe to add unconditionally.)
      - Does NOT read ctx.repo_root (None in global service); uses repo_root arg.
      - Does NOT use the fleet mode/dry_run/candidate_ids envelope.
      - Does NOT allow force=True without sha — every caller must already have
        supplied a sha to stamp; force never triggers resolution of its own.
        There is no "force + no sha" shape.
      - Does NOT guess/default ``shipped_in_kind`` — ``kind`` is a REQUIRED
        param; an omitted/empty ``kind`` is rejected (exit_code 1, no write)
        exactly like a missing ``sha``. See the ``kind`` param doc above.
    """
    handoff_path_raw: str = params.get("handoff_path") or ""
    sha: str = params.get("sha") or ""
    force: bool = bool(params.get("force", False))
    kind_raw = params.get("kind")
    kind: Optional[str] = kind_raw.strip() if isinstance(kind_raw, str) else None
    if kind == "":
        kind = None

    if not handoff_path_raw:
        return _err("missing required param: handoff_path", kind=kind)
    if not sha:
        return _err("missing required param: sha", kind=kind)
    if kind is None:
        return _err(
            "missing required param: kind (DR-096) — must be one of "
            f"{sorted(_SHIPPED_IN_KIND_ENUM)}; a caller that does not say "
            "which kind it is writing is exactly how this field acquired "
            "five meanings — this handler no longer accepts an omitted kind",
            kind=kind,
        )
    if kind not in _SHIPPED_IN_KIND_ENUM:
        return _err(
            f"rejected unknown kind {kind!r} — must be one of "
            f"{sorted(_SHIPPED_IN_KIND_ENUM)}",
            kind=kind,
        )

    # P9: repo_root is required to derive the worktree root (common_dir scope
    # keying guarantees the command-type invoker always supplies --repo; see
    # ipc.py's _OP_KEY_SCOPE["handoff.stamp"] = "common_dir"). Rejecting up
    # front here — mirroring handoff_transition.py's repo_root-required gate —
    # makes worktree always derivable below and removes the permissive
    # no-containment fallback that previously existed for the
    # absolute-path-with-no-repo_root branch (Review: op-family
    # path-containment sweep, 2026-07-08, Finding 1).
    if repo_root is None:
        return _err(
            "handoff.stamp: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)",
            kind=kind,
        )

    worktree = main_worktree_root(repo_root)

    # Resolve path — if relative, anchor to worktree root derived from repo_root.
    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Containment (Review: op-family path-containment sweep, 2026-07-08): resolved
    # path MUST be under <worktree>/state/handoffs/ — archive/handoffs/ is
    # deliberately NOT an allowed root (this is a mutation verb; mutating an
    # archived handoff is out of scope). See
    # docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4.
    allowed_roots = [worktree / "state" / "handoffs"]
    p = contained_path(p, allowed_roots)
    if p is None:
        return _err(
            f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}",
            kind=kind,
        )

    if not p.is_file():
        return _err(f"handoff not found on disk: {handoff_path_raw}", kind=kind)

    # RMW via locked_rmw — single flock-protected read→mutate→write cycle (DR-212 C1).
    # asyncio.to_thread offloads the blocking I/O + flock off the event loop (DR-212 D3).
    mutate, state = build_stamp_mutate(handoff_path_raw, sha, kind, force=force)

    try:
        await asyncio.to_thread(locked_rmw, p, mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}", kind=kind)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "mutate aborted", kind=kind)
    except OSError as exc:
        return _err(f"cannot read/write handoff file: {exc}", kind=kind)

    _applied = state["applied"]
    _replaced = state["replaced"]
    _prior_value = state["prior_value"]
    _prior_kind = state["prior_kind"]

    if _replaced[0]:
        _LOG.info(
            "handoff.stamp: force-replaced shipped_in in %s — was %r, now %s",
            p, _prior_value[0], sha,
        )
        return {
            "exit_code": 0,
            "applied": True,
            "replaced": True,
            "prior_value": _prior_value[0],
            "kind": kind,
            "prior_kind": _prior_kind[0],
            "message": (
                f"force-replaced shipped_in in {handoff_path_raw}: "
                f"{_prior_value[0]!r} -> {sha}"
            ),
        }
    elif _applied[0]:
        _LOG.debug("handoff.stamp: stamped shipped_in: %s into %s", sha, p)
        # Review: code-reviewer (F6) — include message field for envelope consistency
        # with handoff.transition; callers doing result["message"] must not KeyError.
        return {
            "exit_code": 0,
            "applied": True,
            "replaced": False,
            "prior_value": None,
            "kind": kind,
            "prior_kind": None,
            "message": f"stamped shipped_in: {sha} into {handoff_path_raw}",
        }
    else:
        _LOG.debug(
            "handoff.stamp: shipped_in already present in %s — skipping (idempotent)", p
        )
        # Review: code-reviewer (F6) — include message field for envelope consistency.
        return {
            "exit_code": 0,
            "applied": False,
            "replaced": False,
            "prior_value": None,
            "kind": kind,
            "prior_kind": None,
            "message": f"shipped_in already present in {handoff_path_raw} — skipping (idempotent)",
        }


# ---------------------------------------------------------------------------
# Error-shape helper
# ---------------------------------------------------------------------------


def _err(msg: str, kind: Optional[str] = None) -> dict:
    """Return an exit_code=1 error reply dict.

    Review: code-reviewer (F8) — renamed _error → _err for consistency with sibling
    ops (handoff_transition, memo_transition all use _err).

    kind: echoed verbatim into the response (DR-096) — the ``_handler`` doc
    contract promises ``kind`` is present on every response, error included,
    so a caller can always see what it asked to write even when the write
    itself was rejected. None (the default) covers every call site that
    predates the ``kind`` param, or that fires before ``kind`` is parsed.
    """
    _LOG.warning("handoff.stamp: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg, "kind": kind}


# ---------------------------------------------------------------------------
# _repair_archived_shipped_in_handler — provenance-repair verb for an
# ALREADY-ARCHIVED handoff's shipped_in field. See module docstring for the
# full rationale and negative-spec. Deliberately NOT @register_op-registered
# (see docstring below) — called directly in-process by
# coordinator_core.archive_stamp.cs_repair_archived_shipped_in.
# ---------------------------------------------------------------------------

_SHA_SHAPE_RE = re.compile(r"[0-9a-fA-F]{7,40}")


def _repair_err(msg: str) -> dict:
    """Error-shape helper for the repair verb — own log prefix, same envelope as _err."""
    _LOG.warning("handoff.repair_archived_shipped_in: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


async def _repair_archived_shipped_in_handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """Provenance repair of ``shipped_in`` on a handoff already under archive/handoffs/.

    See module docstring, "``_repair_archived_shipped_in_handler``", for full
    rationale, negative-spec, and why this is NOT ``@register_op``-registered.

    Params:
        handoff_path (str)  — path to the ARCHIVED handoff file (must resolve
                              under ``<worktree>/archive/handoffs/``). Required.
        reason       (str)  — caller-supplied justification for the repair,
                              non-empty. Required on EVERY call — sha-repair or
                              unset alike. Convention (not enforced): prefix
                              with the root cause, e.g. "peer-race: ..." or
                              "incomplete-scope: ...", so a reader of the
                              returned/logged reason can tell a concurrent-
                              session mis-stamp apart from a scope: block that
                              structurally could not see the true witness
                              commit — the two imply different follow-ups, but
                              fixing either underlying cause is explicitly out
                              of scope for this repair verb.
        sha          (str)  — the exact, caller-named sha to stamp. NEVER
                              resolved by this handler (no scope-path lookup,
                              no branch-tip fallback, no ownership guard) —
                              the caller is asserting a specific, already-
                              known-correct value. Mutually exclusive with
                              ``unset=True``. Accepted in 7-40 hex-char shape,
                              but STORED truncated to 8 chars (``sha[:8]``) —
                              matching ``stamp_shipped_in``'s own format
                              contract (``archive_stamp.py``'s
                              ``resolved[:8]``). See Negative-spec: a repair
                              must not be distinguishable from a correct
                              original by format alone.
        unset        (bool) — when True, clears ``shipped_in`` entirely
                              instead of stamping a value. Default False. The
                              honest outcome for archived rows with no
                              recoverable correct sha — Position A does not
                              treat "unset" as a failure state.

    Returns a dict with keys:
        exit_code   (int)        — 0 ok / 1 error (bad params, path escape,
                                   file not found, malformed frontmatter, lock
                                   timeout).
        applied     (bool)       — True if the file was written; False on a
                                   byte-identical no-op (unset with nothing to
                                   clear, or sha repair to the value already
                                   present) or on error.
        unset       (bool)       — echoes the ``unset`` param.
        prior_value (str|None)   — the ``shipped_in`` value before this call
                                   (None if absent). Makes every repair
                                   auditable — the caller always learns what
                                   was overwritten.
        new_value   (str|None)   — the value now in place (None when unset).
        reason      (str)        — echoes the caller-supplied reason, present
                                   on every exit_code 0 response.
        message     (str)        — human-readable outcome description
                                   (exit_code 0 only).
        error       (str)        — human-readable reason (exit_code 1 only).

    Exit-code contract:
        exit_code 1 — missing handoff_path/reason; sha and unset both given
                      (or neither given); malformed sha shape; repo_root
                      missing; handoff_path does not resolve under
                      archive/handoffs/; file not found; no valid frontmatter;
                      lock timeout.
        exit_code 0 — repair applied (insert/replace/clear), or a no-op
                      because the requested state already holds
                      (byte-identical — no write occurs, applied=False).

    Negative-spec: see module docstring's "_repair_archived_shipped_in_handler"
    section. The short version: no resolution, ever; reason always required;
    only ``shipped_in``/``shipped_in_kind`` are touched by THIS door (its
    sibling ``_repair_archived_deployment_state_handler`` owns a disjoint
    field set); every other archived-lifecycle verb is unaffected by this
    addition.
    """
    handoff_path_raw: str = params.get("handoff_path") or ""
    reason: str = (params.get("reason") or "").strip()
    sha_raw = params.get("sha")
    sha: str = sha_raw.strip() if isinstance(sha_raw, str) else ""
    unset: bool = bool(params.get("unset", False))

    if not handoff_path_raw:
        return _repair_err("missing required param: handoff_path")
    if not reason:
        return _repair_err("missing required param: reason (non-empty)")
    if unset and sha:
        return _repair_err(
            "rejected: unset=True and a non-empty sha are mutually exclusive — "
            "supply exactly one"
        )
    if not unset and not sha:
        return _repair_err(
            "missing required param: sha (or pass unset=True to clear "
            "shipped_in instead) — this verb never resolves a sha of its own"
        )
    if sha and not _SHA_SHAPE_RE.fullmatch(sha):
        return _repair_err(
            f"rejected malformed sha {sha!r} (expected 7-40 hex chars)"
        )
    # Format contract (2026-07-22 fix): shipped_in is an 8-char abbreviated
    # sha everywhere else it is written (archive_stamp.stamp_shipped_in
    # stores resolved[:8]) — a 40-char value here was the exact divergence
    # (a second writer skipping the [:8] truncation) this whole workstream
    # exists to clean up. Truncate AFTER shape validation, so a repair is
    # never distinguishable from a correct original by format alone.
    if sha:
        sha = sha[:8]

    if repo_root is None:
        return _repair_err(
            "handoff.repair_archived_shipped_in: repo_root is required "
            "(no founding root available)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Archived-root access belongs to THIS verb only among handoff_stamp.py's
    # OWN handlers — a per-call local, never merged into _handler's
    # state/handoffs/-only allowed_roots above. See module docstring
    # negative-spec. NOTE (2026-08-06, docs/plans/2026-08-06-executing-
    # session-can-discharge-criteria.md chunk C2): this "never merged" claim
    # is now false for the SIBLING op coordinator_core.ops.handoff_correct_body
    # — that op's DR-247 amendment unions state/handoffs/ and archive/handoffs/
    # into its own allowed_roots, bounded by an ownership gate and a
    # terminal-state refusal this file's two repair handlers do not carry.
    # This module's own `_handler` (state/handoffs/-only) and this narrow
    # repair door remain unchanged.
    allowed_roots = [worktree / "archive" / "handoffs"]
    p = contained_path(p, allowed_roots)
    if p is None:
        return _repair_err(
            f"handoff_path does not resolve under archive/handoffs/: {handoff_path_raw!r}"
        )

    if not p.is_file():
        return _repair_err(f"archived handoff not found on disk: {handoff_path_raw}")

    _applied = [False]
    _prior_value: list[Optional[str]] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"no valid YAML frontmatter block in: {handoff_path_raw}"
            )
        existing = read_fm_field(split.fm_text, "shipped_in")
        _prior_value[0] = existing

        if unset:
            if existing is None:
                # Nothing to clear — byte-identical no-op.
                return old_text
            new_fm = remove_fm_field(split.fm_text, "shipped_in")
            _applied[0] = True
            return rebuild(split, new_fm)

        if existing == sha:
            # Already at the requested value — byte-identical no-op.
            return old_text
        if existing is not None:
            new_fm = replace_fm_field(split.fm_text, "shipped_in", sha, numeric_quoting=True)
        else:
            new_fm = insert_fm_field(
                split.fm_text, "shipped_in", sha, after_key="claimed_at", numeric_quoting=True
            )
        _applied[0] = True
        return rebuild(split, new_fm)

    try:
        await asyncio.to_thread(locked_rmw, p, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _repair_err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _repair_err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _repair_err(f"cannot read/write archived handoff file: {exc}")

    new_value = None if unset else sha
    if _applied[0]:
        _LOG.info(
            "handoff.repair_archived_shipped_in: %s %s (was %r, now %r) — reason: %s",
            "cleared" if unset else "repaired", p, _prior_value[0], new_value, reason,
        )
        message = (
            f"cleared shipped_in in {handoff_path_raw} (was {_prior_value[0]!r}) — {reason}"
            if unset else
            f"repaired shipped_in in {handoff_path_raw}: {_prior_value[0]!r} -> {sha} — {reason}"
        )
    else:
        message = (
            f"shipped_in already unset in {handoff_path_raw} — no-op — {reason}"
            if unset else
            f"shipped_in in {handoff_path_raw} already {sha!r} — no-op — {reason}"
        )

    return {
        "exit_code": 0,
        "applied": _applied[0],
        "unset": unset,
        "prior_value": _prior_value[0],
        "new_value": new_value,
        "reason": reason,
        "message": message,
    }


# ---------------------------------------------------------------------------
# _repair_archived_deployment_state_handler — provenance-repair verb for an
# ALREADY-ARCHIVED handoff's deployment_state field. Sibling of
# _repair_archived_shipped_in_handler above (same narrow-door shape, same
# reason-required/no-resolution discipline); see that handler's docstring and
# the module docstring for the shared rationale. Deliberately NOT
# @register_op-registered, for the same DR-208/op_scopes CI-plumbing reason
# as the shipped_in repair verb — called directly in-process by
# coordinator_core.archive_stamp.cs_repair_archived_deployment_state.
#
# Added 2026-07-26 after a DoE-claude cross-repo memo (13 archived handoffs
# stuck at deployment_state: in_flight, hand-edited because ship-handoff's
# state/handoffs/-only containment refuses archive/handoffs/ paths — the
# same problem the shipped_in repair verb solved for shipped_in on
# 2026-07-22, now recurring for deployment_state). The hand-edit itself is
# the evidence for why a REPAIR VERB beats widening ship-handoff's guard:
# 10 of the 13 hand-edited files stamped deployment_state: continued with no
# continued_into, silently producing a record that fails
# handoff-archived.schema.json's own cross-field rule (continued_into
# required-when-continued, landed 2026-07-22). This handler enforces that
# SAME cross-field rule in-process, so a repair through this verb can never
# reproduce that defect — widening ship-handoff's path guard would not have
# caught it, because ship-handoff's own mutation path never re-validates the
# schema's cross-field rules either.
# ---------------------------------------------------------------------------

_ARCHIVED_DEPLOYMENT_STATES = frozenset(
    {"awaiting_gate", "ready_to_fire", "in_flight", "shipped", "continued", "closed"}
)
_CLOSED_REASONS = frozenset({"cancelled", "displaced", "stale"})

# Terminal deployment_states (E1, DoE-claude 2026-07-26/27 follow-up memo).
# A record sitting at one of these three is VALIDLY archived — its lifecycle
# is over, same as the freeze this handler already respects for every other
# field. Repairing a record's deployment_state is legitimate ONLY when the
# record was never validly written in the first place, and that condition is
# mechanically checkable: the 14-handoff corpus this door exists for all
# carry `in_flight` or no deployment_state key at all — both non-terminal.
# This set is deliberately the complement of that: refuse the repair unless
# the CURRENT on-disk state is non-terminal or absent. The property is
# monotone and self-extinguishing by construction — the only permitted
# transition is non-terminal/absent -> terminal, so a record this door just
# repaired immediately leaves its own reachable set (its new state is always
# one of these three) and can never be re-mutated by a second call, whether
# that second call names the same target state or a different one. See
# coordinator_core/test_archive_stamp.py::TestRepairArchivedDeploymentState::
# test_second_call_after_repair_refuses_self_extinguishing for the assertion.
#
# AC13 (2026-08-18) carves ONE narrow exception into this otherwise-
# unconditional set membership check, inside
# _repair_archived_deployment_state_handler._mutate (`moving_off_shipped`):
# `shipped` -> a NON-terminal target is permitted, clearing the shipped-
# implied provenance triple in the same write. `continued` and `closed`
# remain unconditionally terminal — this set's membership is unchanged, only
# one caller's use of it gained a same-write bypass for one specific member.
_TERMINAL_DEPLOYMENT_STATES = frozenset({"shipped", "continued", "closed"})

# Cross-repo continued_into reference shape (e.g. "project-makima:docs/plans/x.md")
# — observed in the live corpus (DoE-claude handoff 2026-07-24_112615_c0bd4682.md).
# A colon-prefixed segment of 3+ word chars, not a URL scheme and not a
# single-letter Windows drive ("C:\..."), signals a sibling-repo path this
# handler has no filesystem access to verify. Detected so it routes to the
# override path (§ continued_into_override) rather than being silently
# treated as "not found" with no explanation, or silently accepted as
# "found" when it was never checked.
_CROSS_REPO_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{2,}:(?!//)")


def _repair_deployment_state_err(msg: str) -> dict:
    """Error-shape helper for the repair verb — own log prefix, same envelope as _err."""
    _LOG.warning("handoff.repair_archived_deployment_state: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


def _resolve_continued_into(worktree: Path, value: str) -> bool:
    """Return True iff ``value`` resolves to a real file under ``worktree``.

    Existence-and-identity check for the ``continued_into`` cross-field, added
    2026-07-26 after a DoE-claude repair session wrote a *guessed* successor
    slug into an archived handoff's ``continued_into`` and this verb accepted
    it silently (rc=0) — the error was only caught by an independent
    cross-check against the successor's real ``handoff_id``. The verb caught
    "no lineage" (the mandatory-when-continued gate above) but not "wrong
    lineage", which is the failure mode that matters: it looks correct
    forever after.

    The live corpus uses several shapes for ``continued_into`` (see
    ``coordinator_core/ops/tests/test_handoff_stamp.py``'s resolution-gap
    tests for concrete examples pulled from DoE-claude's actual archive):
      - a bare synthetic id (``hnd-<slug>-<hex6>``) matching another
        handoff's own ``handoff_id:`` frontmatter field, live or archived
      - a worktree-relative path (``state/handoffs/<file>.md``,
        ``archive/handoffs/2026-07/<file>.md``, ``docs/plans/<file>.md`` —
        a handoff may continue directly into a plan, not just a successor
        handoff)
      - a bare filename with no directory prefix, resolved by searching
        ``archive/handoffs/**`` for a matching basename

    Cross-repo references (``<repo>:<path>``) are NOT resolved here — this
    handler has no filesystem access to a sibling repo's worktree from
    inside a single-repo op. They return False, same as a fabricated value;
    the caller distinguishes the two only via ``continued_into_override``
    (see the handler's own negative-spec) — this function's job is strictly
    "did I find a real file", not "is this reference plausible".

    Does NOT resolve git history — a successor deleted by a distill sweep
    resolves False here even if genuinely real and recoverable from git log.
    That is deliberate: this function embodies the SAME "never resolves of
    its own" discipline as the rest of this repair verb (see module
    docstring's Negative-spec) — the caller who recovered a deleted
    successor's id from git history already did the resolution a human eye
    trusts; re-deriving it here would just be a second, unaudited resolver.
    """
    v = value.strip()
    if not v:
        return False

    if _CROSS_REPO_REF_RE.match(v):
        return False

    p = Path(v)
    candidates = [p if p.is_absolute() else worktree / p]
    if not v.startswith(("state/handoffs/", "archive/handoffs/", "docs/")):
        candidates.append(worktree / "state" / "handoffs" / v)
    for c in candidates:
        if c.is_file():
            return True

    if "/" in v or v.endswith(".md"):
        # Path-shaped but not found at any anchored candidate — last resort,
        # search archive/handoffs/** for a file with this exact basename (a
        # successor cited without its state/-vs-archive/ prefix, or one that
        # has since itself been archived under a different YYYY-MM shard).
        target_name = p.name
        # Bare-slug fallback (2026-07-28 defect fix, same shape as
        # `pickup_assemble._search_dirs_for_basename`): this arm is reached
        # for a path-shaped `v` with NO `.md` suffix (e.g.
        # "state/handoffs/<slug>"), so `target_name` is itself a bare slug —
        # a literal `rglob(target_name)` would never match the `<slug>.md`
        # file actually on disk. Search both forms; narrowed to `.md`
        # deliberately, since every handoff this arm resolves is markdown.
        # Candidate-form generation is delegated to
        # `coordinator_core.artifact_basename.md_fallback_candidates`
        # (shared with `pickup_assemble` and `resolve_swept_baton` — see
        # that module's docstring).
        target_names = md_fallback_candidates(target_name)
        arch_root = worktree / "archive" / "handoffs"
        if arch_root.is_dir():
            for name in target_names:
                for f in arch_root.rglob(name):
                    if f.is_file():
                        return True
        return False

    # id-shaped (no path separator, no .md suffix): search both live and
    # archived handoffs for a frontmatter handoff_id match.
    for root in (worktree / "state" / "handoffs", worktree / "archive" / "handoffs"):
        if not root.is_dir():
            continue
        for f in root.rglob("*.md"):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            split = split_frontmatter(text)
            if split is None:
                continue
            hid = read_fm_field_unquoted(split.fm_text, "handoff_id")
            if hid is not None and hid == v:
                return True
    return False


async def _repair_archived_deployment_state_handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """Provenance repair of ``deployment_state`` (and its state-conditional
    companion fields) on a handoff already under ``archive/handoffs/``.

    See module docstring and the sibling ``_repair_archived_shipped_in_handler``
    for the shared narrow-door rationale.

    Params:
        handoff_path     (str)  — path to the ARCHIVED handoff file (must
                                  resolve under ``<worktree>/archive/handoffs/``).
                                  Required.
        reason            (str)  — caller-supplied justification for the repair,
                                  non-empty. Required on every call — see
                                  ``_repair_archived_shipped_in_handler``'s
                                  ``reason`` docs for the convention (prefix with
                                  root cause).
        deployment_state  (str)  — the target value. Required. Must be one of
                                  handoff-archived.schema.json's own enum
                                  (awaiting_gate, ready_to_fire, in_flight,
                                  shipped, continued, closed) — any other value
                                  is a usage error (exit_code 1), never silently
                                  written.
        continued_into    (str)  — successor handoff id-or-path. REQUIRED when
                                  ``deployment_state == "continued"`` (mirrors
                                  the schema's own cross-field rule — this
                                  handler enforces it BEFORE any write, so a
                                  repair through this verb can never produce
                                  the exact continued-without-continued_into
                                  defect a hand-edit produced). Rejected
                                  (usage error) when supplied for any other
                                  target state. Also RESOLUTION-AND-EXISTENCE
                                  checked (2026-07-26): must name a real file
                                  under the worktree — see
                                  ``_resolve_continued_into`` — unless
                                  ``continued_into_override=True`` is also
                                  supplied. A value that merely *looks*
                                  plausible (a well-formed but fabricated
                                  ``hnd-...`` slug) is rejected exactly like a
                                  missing value; catching "no lineage" but not
                                  "wrong lineage" was the gap this closes.
        continued_into_override (bool) — when True, skips the resolution-and-
                                  existence check on ``continued_into`` and
                                  applies the value as given. For the
                                  genuinely-cannot-verify-locally cases: a
                                  successor deleted by a distill sweep and
                                  recovered from git history, or a cross-repo
                                  continuation (``<repo>:<path>``) this
                                  single-repo handler cannot resolve. Default
                                  False. Rejected (usage error) when supplied
                                  for any target state other than
                                  ``"continued"``. The caller's ``reason``
                                  (already mandatory on every call) is the
                                  record of why the override was needed — see
                                  Negative-spec below for why this is a
                                  bypass-with-audit-trail, not a silent
                                  reintroduction of guessing.
        closed_reason     (str)  — one of cancelled|displaced|stale. REQUIRED
                                  when ``deployment_state == "closed"`` (same
                                  schema cross-field rule). Rejected (usage
                                  error) when supplied for any other target
                                  state.

    Returns a dict with keys:
        exit_code    (int)        — 0 ok / 1 error (bad params, cross-field
                                    violation, unresolved continued_into,
                                    path escape, file not found, malformed
                                    frontmatter, lock timeout, OR the
                                    record's current deployment_state is
                                    already terminal — see Negative-spec).
        applied      (bool)       — True if the file was written; False on a
                                    byte-identical no-op (requested state
                                    already holds, and that state is
                                    non-terminal) or on error.
        prior_state  (str|None)   — the ``deployment_state`` value before this
                                    call (None if absent).
        new_state    (str)        — the value now in place.
        continued_into_verified (bool|None) — True when ``continued_into``
                                    resolved to a real file on disk; False
                                    when it did NOT resolve and the write
                                    proceeded anyway under
                                    ``continued_into_override=True``; None
                                    when the target state is not
                                    ``"continued"`` (field not applicable).
        provenance_cleared (list[str]) — the subset of
                                    ``["shipped_in", "advanced_by",
                                    "advanced_at"]`` actually removed in this
                                    call (AC13, § off-shipped provenance
                                    repair below). Always ``[]`` unless this
                                    call moved the record OFF ``shipped`` to a
                                    non-terminal target.
        reason       (str)        — echoes the caller-supplied reason, present
                                    on every exit_code 0 response.
        message      (str)        — human-readable outcome description
                                    (exit_code 0 only).
        error        (str)        — human-readable reason (exit_code 1 only).

    § off-shipped provenance repair (AC13, 2026-08-18): the one carve-out in
    the terminal-lock below. A record whose ``deployment_state`` is
    ``shipped`` may be repaired to a NON-TERMINAL target
    (``awaiting_gate``/``ready_to_fire``/``in_flight``) — never to
    ``continued``/``closed``, and never restated as ``shipped`` (both of
    those stay refused by the unchanged terminal-lock). This is for the
    deliverable-cascade co-tenancy defect: a record swept to ``shipped`` only
    because it happened to sit alongside a DIFFERENT deliverable's real
    target is lifecycle-correct once un-shipped, but its ``shipped_in``/
    ``advanced_by``/``advanced_at`` still assert ship evidence and an
    advancing deliverable that were never true for THIS record. This call
    clears all three (whichever are present) IN THE SAME WRITE as the
    ``deployment_state`` flip — never as a separate call to
    ``_repair_archived_shipped_in_handler`` — because
    ``schema_validate._cf_shipped_in_required`` fires only while
    ``deployment_state == "shipped"``: a clear-then-flip sequence persists an
    intermediate ``shipped`` record with no ``shipped_in`` (invalid) between
    the two calls, and a flip-then-clear sequence persists an intermediate
    ``shipped_in``-bearing-but-no-longer-``shipped`` record that only
    validates by accident of the validator not checking `shipped_in` absent
    ``deployment_state == "shipped"``. One ``_mutate``/one ``locked_rmw``
    write means neither intermediate is ever the on-disk state — the caller
    (C6) never sequences this by hand.

    Negative-spec (repair verb, hard-won — mirrors
    ``_repair_archived_shipped_in_handler``'s):
      - NEVER resolves ``continued_into`` or ``closed_reason`` of its own
        FOR THE CALLER — the caller must always name the exact successor /
        reason; this verb never guesses or scans for a plausible value to
        fill in on the caller's behalf. What it DOES do (2026-07-26) is
        VERIFY a caller-supplied ``continued_into`` resolves to something
        real — this is existence-checking a claim already made, not
        resolving one the caller left unmade. The two are not the same
        operation: resolving-for-the-caller reintroduces guessing; verifying
        a caller's own claim is the opposite of guessing. The
        ``continued_into_override`` escape exists precisely because
        verification can have real, deliberate exceptions (deleted-and-
        git-recovered, cross-repo) that guessing-for-the-caller never would.
      - Requires a non-empty ``reason`` on every call.
      - REFUSES (exit_code 1) unless the record's CURRENT on-disk
        ``deployment_state`` is non-terminal (``awaiting_gate``,
        ``ready_to_fire``, ``in_flight``) or absent, OR this is the
        off-shipped provenance repair (§ above): current state ``shipped``
        AND the target is non-terminal. Every other terminal case —
        ``shipped`` restated as ``shipped``, and ``shipped``/``continued``/
        ``closed`` targeting ``continued``/``closed`` — still refuses exactly
        as before; the carve-out widens WHERE the door reaches
        (shipped -> non-terminal), never HOW MANY terminal states remain
        reachable-by-repair (still only via that one path). Checked BEFORE
        the idempotent-no-op comparison so a call that merely restates an
        already-terminal, non-carved-out state also refuses rather than
        silently succeeding. This keeps the door self-extinguishing for
        every state but the carve-out: the only legal transitions are
        non-terminal/absent -> terminal (unchanged) and the new
        shipped -> non-terminal repair, so a record this door just repaired
        to a TERMINAL state can never be reached by a second call, same
        target state or not — see ``_TERMINAL_DEPLOYMENT_STATES``'s
        docstring. A record repaired OFF shipped lands back in non-terminal
        territory and is deliberately NOT extinguished — it re-enters normal
        lifecycle progression (a later legitimate ship advances it again).
      - Does NOT extend archived-record mutability to any other field — only
        ``deployment_state``, ``continued_into``, ``closed_reason``, and (on
        the off-shipped provenance-repair path only) ``shipped_in``/
        ``advanced_by``/``advanced_at`` are ever touched, and the latter
        three are only ever CLEARED, never stamped with a caller-supplied
        value — this door still never resolves ship evidence or an advancing
        deliverable of its own. Does NOT extend to any other archived
        lifecycle verb — ``archive/handoffs/`` remains closed to
        ``handoff.transition``/``handoff.stamp``/``handoff.archive_transition``/
        etc. Archival otherwise remains a freeze.
      - Does NOT perform the live-children guard
        (``handoff.has_live_children``) — that guard exists to decide whether
        an ACTIVE handoff is safe to archive; a handoff already sitting in
        ``archive/handoffs/`` has, by definition, already passed (or
        deliberately bypassed) that decision. Re-running it here would be
        re-litigating a decision this verb has no authority to reverse.
    """
    handoff_path_raw: str = params.get("handoff_path") or ""
    reason: str = (params.get("reason") or "").strip()
    target_state: str = (params.get("deployment_state") or "").strip()
    continued_into_raw = params.get("continued_into")
    continued_into: str = continued_into_raw.strip() if isinstance(continued_into_raw, str) else ""
    continued_into_override: bool = bool(params.get("continued_into_override", False))
    closed_reason_raw = params.get("closed_reason")
    closed_reason: str = closed_reason_raw.strip() if isinstance(closed_reason_raw, str) else ""

    if not handoff_path_raw:
        return _repair_deployment_state_err("missing required param: handoff_path")
    if not reason:
        return _repair_deployment_state_err("missing required param: reason (non-empty)")
    if not target_state:
        return _repair_deployment_state_err("missing required param: deployment_state")
    if target_state not in _ARCHIVED_DEPLOYMENT_STATES:
        return _repair_deployment_state_err(
            f"rejected unknown deployment_state {target_state!r} — must be one "
            f"of {sorted(_ARCHIVED_DEPLOYMENT_STATES)}"
        )
    if target_state == "continued" and not continued_into:
        return _repair_deployment_state_err(
            "deployment_state 'continued' requires 'continued_into' (successor "
            "handoff id-or-path) — mirrors handoff-archived.schema.json's own "
            "cross-field rule; this verb never resolves a successor of its own"
        )
    if target_state != "continued" and continued_into:
        return _repair_deployment_state_err(
            f"'continued_into' was supplied but deployment_state is "
            f"{target_state!r}, not 'continued' — rejected rather than silently "
            "written to a state it does not apply to"
        )
    if target_state != "continued" and continued_into_override:
        return _repair_deployment_state_err(
            f"'continued_into_override' was supplied but deployment_state is "
            f"{target_state!r}, not 'continued' — rejected; the override only "
            "applies to the continued_into resolution check"
        )
    if target_state == "closed" and not closed_reason:
        return _repair_deployment_state_err(
            "deployment_state 'closed' requires 'closed_reason' "
            "(cancelled|displaced|stale) — mirrors handoff-archived.schema.json's "
            "own cross-field rule"
        )
    if target_state == "closed" and closed_reason not in _CLOSED_REASONS:
        return _repair_deployment_state_err(
            f"rejected unknown closed_reason {closed_reason!r} — must be one of "
            f"{sorted(_CLOSED_REASONS)}"
        )
    if target_state != "closed" and closed_reason:
        return _repair_deployment_state_err(
            f"'closed_reason' was supplied but deployment_state is "
            f"{target_state!r}, not 'closed' — rejected rather than silently "
            "written to a state it does not apply to"
        )

    if repo_root is None:
        return _repair_deployment_state_err(
            "handoff.repair_archived_deployment_state: repo_root is required "
            "(no founding root available)"
        )

    worktree = main_worktree_root(repo_root)

    # Resolution-and-existence check on continued_into (2026-07-26 fix — see
    # _resolve_continued_into and the handler's own Params/Negative-spec
    # docs above for the full rationale). Runs before the path-containment
    # check below so an unresolved/fabricated continued_into is rejected
    # even if handoff_path itself is fine — a usage error, not an I/O error.
    continued_into_verified: Optional[bool] = None
    if target_state == "continued":
        continued_into_verified = _resolve_continued_into(worktree, continued_into)
        if not continued_into_verified and not continued_into_override:
            return _repair_deployment_state_err(
                f"continued_into {continued_into!r} does not resolve to a real "
                "file under state/handoffs/ or archive/handoffs/ (searched by "
                "path and by handoff_id) — pass continued_into_override=True "
                "(with 'reason' explaining why, e.g. a successor deleted by a "
                "distill sweep and recovered from git history, or a cross-repo "
                "reference) if this is a deliberate, verified exception rather "
                "than a fabricated/guessed value"
            )

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Archived-root access belongs to THIS verb only among handoff_stamp.py's
    # OWN handlers — a per-call local, never merged into _handler's
    # state/handoffs/-only allowed_roots above. Same shape as
    # _repair_archived_shipped_in_handler's own allowed_roots. NOTE
    # (2026-08-06, docs/plans/2026-08-06-executing-session-can-discharge-
    # criteria.md chunk C2): this "never merged" claim is now false for the
    # SIBLING op coordinator_core.ops.handoff_correct_body — see that
    # module's own comment above this file's other repair handler for the
    # full note.
    allowed_roots = [worktree / "archive" / "handoffs"]
    p = contained_path(p, allowed_roots)
    if p is None:
        return _repair_deployment_state_err(
            f"handoff_path does not resolve under archive/handoffs/: {handoff_path_raw!r}"
        )

    if not p.is_file():
        return _repair_deployment_state_err(f"archived handoff not found on disk: {handoff_path_raw}")

    _applied = [False]
    _prior_state: list[Optional[str]] = [None]
    _cleared_provenance: list[list[str]] = [[]]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"no valid YAML frontmatter block in: {handoff_path_raw}"
            )
        existing_state = read_fm_field(split.fm_text, "deployment_state")
        existing_continued_into = read_fm_field(split.fm_text, "continued_into")
        existing_closed_reason = read_fm_field(split.fm_text, "closed_reason")
        _prior_state[0] = existing_state

        # Off-shipped provenance repair (AC13, 2026-08-18): the ONE exception
        # to the terminal-lock below. `shipped` is the sole terminal state a
        # co-tenant cascade casualty can carry FALSELY — `advanced_by`
        # asserts a named deliverable advanced this record, and that
        # assertion is wrong for every co-tenant swept along by another
        # spinoff's ship. `continued`/`closed` carry no such falsifiable
        # claim, so they keep the unconditional terminal-lock; only a
        # shipped -> NON-TERMINAL target counts as "moving off shipped" —
        # shipped -> shipped (E1's existing already-terminal refusal) and
        # shipped -> continued/closed (sideways between two terminal states,
        # not an undo) are both still refused, unchanged, by the check below.
        moving_off_shipped = (
            existing_state == "shipped" and target_state not in _TERMINAL_DEPLOYMENT_STATES
        )

        # Enforced precondition (E1): refuse unless the record's CURRENT
        # on-disk deployment_state is non-terminal or absent, UNLESS this is
        # the off-shipped provenance repair carved out above. A terminal
        # state otherwise means this record was already validly archived —
        # mutating it is the exact harm this door's containment
        # (archive/handoffs/-only) otherwise stays silent about. Checked
        # before the no-op comparison below on purpose: a same-target-as-
        # current call against an already-terminal record must still refuse
        # (exit_code 1), not silently succeed as a no-op — that refusal is
        # what makes the transition self-extinguishing (see
        # _TERMINAL_DEPLOYMENT_STATES docstring) for every state but the
        # carved-out shipped -> non-terminal repair path.
        if existing_state in _TERMINAL_DEPLOYMENT_STATES and not moving_off_shipped:
            raise MutateAbort(
                f"refusing repair: {handoff_path_raw} already carries a "
                f"terminal deployment_state ({existing_state!r}) — this door "
                "only repairs a record whose current deployment_state is "
                "non-terminal (awaiting_gate/ready_to_fire/in_flight) or "
                "absent, OR a 'shipped' record being moved OFF shipped to a "
                "non-terminal target (provenance repair, AC13); a terminal "
                "state means the record was already validly archived and "
                "mutating it is outside this door's reach, by design"
            )

        # Byte-identical no-op: requested state (and its companion field,
        # when applicable) already holds. Unreachable when moving_off_shipped
        # is True — that path requires existing_state ("shipped") != target_state
        # (non-terminal by construction), so the states always differ.
        if (
            existing_state == target_state
            and (target_state != "continued" or existing_continued_into == continued_into)
            and (target_state != "closed" or existing_closed_reason == closed_reason)
        ):
            return old_text

        fm_text = split.fm_text
        if existing_state is not None:
            fm_text = replace_fm_field(fm_text, "deployment_state", target_state)
        else:
            fm_text = insert_fm_field(
                fm_text, "deployment_state", target_state, after_key="kind"
            )

        if target_state == "continued":
            if existing_continued_into is not None:
                fm_text = replace_fm_field(fm_text, "continued_into", continued_into)
            else:
                fm_text = insert_fm_field(
                    fm_text, "continued_into", continued_into, after_key="deployment_state"
                )
        elif target_state == "closed":
            if existing_closed_reason is not None:
                fm_text = replace_fm_field(fm_text, "closed_reason", closed_reason)
            else:
                fm_text = insert_fm_field(
                    fm_text, "closed_reason", closed_reason, after_key="deployment_state"
                )

        # Provenance clear (AC13): moving OFF shipped in this SAME write also
        # clears the provenance triple the shipped state implied —
        # `shipped_in` (ship evidence), `advanced_by`/`advanced_at` (which
        # deliverable advanced this record, and when). All three are false
        # once the record is known to have shipped only as a co-tenant
        # casualty of a DIFFERENT deliverable's cascade. Doing this in the
        # SAME `_mutate` call (one locked_rmw write) rather than a second,
        # separate call to `_repair_archived_shipped_in_handler` is the whole
        # point: `_cf_shipped_in_required` fires only WHILE
        # deployment_state == "shipped", so a caller clearing shipped_in
        # FIRST (while the on-disk deployment_state still reads "shipped")
        # would persist a state the validator rejects; a caller flipping
        # deployment_state first and clearing shipped_in second leaves that
        # same invalid state on disk for the gap between the two writes. This
        # handler never persists either intermediate — the flip and the
        # clear land in one file write — so the ordering question the
        # validator poses does not arise. See
        # test_repair_archived_verbs.py for the two-call trap pinned against
        # `_cf_shipped_in_required` directly.
        _provenance_cleared: list[str] = []
        if moving_off_shipped:
            for _field in ("shipped_in", "advanced_by", "advanced_at"):
                if read_fm_field(fm_text, _field) is not None:
                    fm_text = remove_fm_field(fm_text, _field)
                    _provenance_cleared.append(_field)
        _cleared_provenance[0] = _provenance_cleared

        _applied[0] = True
        return rebuild(split, fm_text)

    try:
        await asyncio.to_thread(locked_rmw, p, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _repair_deployment_state_err(f"lock timeout acquiring file lock: {exc}")
    except MutateAbort as exc:
        return _repair_deployment_state_err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        return _repair_deployment_state_err(f"cannot read/write archived handoff file: {exc}")

    if _applied[0] and target_state == "continued" and not continued_into_verified:
        _LOG.warning(
            "handoff.repair_archived_deployment_state: wrote unverified continued_into "
            "%r into %s under continued_into_override=True — reason: %s",
            continued_into, p, reason,
        )
    if _applied[0]:
        _LOG.info(
            "handoff.repair_archived_deployment_state: repaired %s (was %r, now %r) — reason: %s",
            p, _prior_state[0], target_state, reason,
        )
        message = (
            f"repaired deployment_state in {handoff_path_raw}: "
            f"{_prior_state[0]!r} -> {target_state!r} — {reason}"
        )
        if _cleared_provenance[0]:
            message += (
                f"; cleared shipped provenance ({', '.join(_cleared_provenance[0])})"
            )
    else:
        message = (
            f"deployment_state in {handoff_path_raw} already {target_state!r} "
            f"— no-op — {reason}"
        )

    return {
        "exit_code": 0,
        "applied": _applied[0],
        "prior_state": _prior_state[0],
        "new_state": target_state,
        "continued_into_verified": continued_into_verified,
        "provenance_cleared": _cleared_provenance[0],
        "reason": reason,
        "message": message,
    }
