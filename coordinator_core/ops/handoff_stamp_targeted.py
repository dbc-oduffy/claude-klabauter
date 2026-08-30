"""
coordinator_core.ops.handoff_stamp_targeted — the targeted, single-record
composition of ``archive-stamp-cli ship-handoff`` (``mode="stamp_only"``),
WITHOUT the corpus-wide ``housekeeping.cycle`` fan-in
``coordinator_core.archive_stamp._call_handoff_archive_transition`` pays on
every call.

Purpose: `handoff_archive_transition._handler`'s own ``do_stamp_only`` block
(see that module, "stamp_only: guard has cleared — stamp in place, NO git
mv") already operates on exactly ONE handoff path per call — it never reads
the live corpus, never opens the archive index, and never calls
`compute_terminal_set` (confirmed by C1 of
`docs/plans/2026-08-30-the-stamp-stops-paying-for-a-sweep-that.md`). Every
corpus-wide read this module exists to avoid paying lives in
`housekeeping/cycle.py`, reached only via `archive_stamp.
_call_handoff_archive_transition`'s per-verb call into `housekeeping.cycle`
— a file this module never imports and a call this module never makes. This
is a rewrite of the CALL SHAPE (skip the cycle), not a simplification of the
per-record CONTRACT `handoff_archive_transition.py` already enforces.

Scope (C2 of the governing plan — ship/stamp_only ONLY): reads ONE file,
applies the stamp-only contract preconditions below in memory, writes back
atomically in a SINGLE `locked_rmw` lock hold (one read, one write — the
pre-existing path pays two separate `locked_rmw` calls, one via
`handoff.stamp` for `shipped_in` and a second via `handoff_transition._ship`
for the `deployment_state` flip). chain/supersede modes are C3's job, not
this module's.

Contract preserved from `handoff_archive_transition.py` (C1's table; item
numbers below cite that table's own numbering):
  - Item 1: containment — live-only under ``state/handoffs/`` (this mode
    never widens to ``ARCHIVE_ROOT_SUBDIRS``; that widening is supersede-only,
    C3's job).
  - Item 5/11 shape (the do_stamp twin's AC6/AC6b/AC7 handling), reproduced
    for stamp_only from `handoff_archive_transition.py`'s own
    ``do_stamp_only`` block: a caller-supplied ``sha`` that the idempotency
    guard would silently retain-over is refused loudly (AC6), a same-commit
    re-stamp is a legitimate no-op (AC6b), and "nothing was ever there" is
    distinguished from "a stamp attempt no-opped, retaining a prior value"
    (AC7).
  - Item 6: Position A — never-guess-a-branch-tip refusal. Refuses the
    ``deployment_state: shipped`` flip when ``shipped_in`` would be left
    unset after the stamp attempt. This module never resolves a sha of its
    own (no scope-path git-log walk, no branch-tip fallback) — that
    resolution, when needed, is the CALLER's job, upstream of this module,
    exactly as C1 confirms no scope-derivation lives inside
    `handoff_archive_transition.py`'s own per-record body either.
  - Item 7 (STALE DOCSTRING, confirmed by C1): no live-children/holder guard
    gates ``stamp_only`` — none has existed since the 2026-08-28 deletion.
    This module reproduces that (guard-free) shape; it does not resurrect one.
  - Item 8: no git mv. The record stays in ``state/handoffs/`` for the async
    archival sweep (the cadence step, per the governing plan's Problem
    section) — this module never touches ``archive/handoffs/``.

Reused, never re-derived (per the governing plan's C2 body: "the SHA-quoting
guards are reused and never re-derived"):
  - `coordinator_core.ops.handoff_stamp.build_stamp_mutate` — the exact
    ``shipped_in``/``shipped_in_kind`` insert-or-force-replace closure,
    including its three parity-critical SHA-quoting guards (structural
    chars, all-numeric, scientific-notation) ported from
    stamp-shipped-in.js. NOT reimplemented here.
  - `coordinator_core.ops.handoff_transition.build_ship_mutate` — the exact
    ``deployment_state: shipped`` + ``pickup_ready: false`` flip closure,
    including its post-mutation schema-validation gate. NOT reimplemented
    here.

Both closures are pure ``str -> str`` callables operating on the SAME
frontmatter text shape, so they are composed sequentially inside ONE
combined mutate closure passed to a SINGLE `locked_rmw` call — this is the
"reads ONE file ... writes back atomically" the governing plan's C2 body
names, and the concrete mechanism by which this module avoids the two
separate lock-hold/read/write round trips the pre-existing
`stamp_shipped_in()` + `handoff_transition._ship()` composition pays.

C3 addendum (chain/supersede — move without commit, docs/plans/2026-08-30-the-
stamp-stops-paying-for-a-sweep-that.md chunk C3): `chain_archive_handoff` and
`supersede_archive_handoff` extend this module with the two remaining
per-record modes `handoff_archive_transition._handler` implements (item table
1a, 3, 4, 9-17 of that plan's C1). Both are `async def` — unlike
`ship_stamp_only` above, they call `ops.fleet._common.archive_and_commit`,
itself async (`_commit_via_head_spine` lands the move's commit without a
`git commit` spawn; see that function's own docstring). REUSE, NOT RE-DERIVE:
both compose `handoff_archive_transition.py`'s own private per-mode helpers
(`_current_deployment_state`, `_supersede_continued`,
`_handoff_live_holder_session`, `_commit_retained_supersede_flip`,
`_sha_canonically_matches`, `_current_shipped_in`, `_TERMINAL_DEPLOYMENT_STATES`)
function-locally, exactly as that module's own do_stamp block reuses
`coordinator_core.archive_stamp.stamp_shipped_in` function-locally — the
Reconciliation section of the governing plan is explicit that this file's
per-record CONTRACT is not being simplified, only its CALL SHAPE (skip
`archive_stamp._call_handoff_archive_transition`'s `housekeeping.cycle`
fan-in, C4's job, not this module's).

Negative-spec (hard-won, ported forward from `handoff_archive_transition.py`):
  - Does NOT resolve a sha of its own. ``sha=None`` (or empty) means "the
    caller has nothing to supply this call" — exactly like a
    scope-derivation attempt that found no commit — never an invitation to
    walk git history or fall back to a branch tip.
  - Does NOT git-mv. ``stamp_only`` never moves the file; a later sweep
    (the cadence step) archives it.
  - Does NOT walk any directory and does NOT spawn git. The only I/O this
    module performs is: read ONE file's frontmatter text, optionally write
    it back once (`locked_rmw`'s own git-free flock protocol — see
    `coordinator_core.lifecycle.git_common_dir`'s own negative-spec: "no
    subprocess spawn at all").
  - Does NOT gate on live children or a live claim holder — no such guard
    exists for ``stamp_only`` (C1 item 7; the module docstring this file's
    sibling ``handoff_archive_transition.py`` still carries at its own
    top-of-file is stale on this point and is NOT reproduced here).
  - Does NOT accept a mode other than the one it implements. This module has
    no ``mode`` param at all — it IS ``mode="stamp_only"``, nothing else.
    chain/stamp_shipped/supersede stay `handoff_archive_transition.py`'s
    (and, per the governing plan, C3's targeted-path job for chain/supersede).

Spec backlink: coordinator_core/ops/handoff_archive_transition.py's
``do_stamp_only`` block (the per-record contract this module reproduces,
without that module's per-call `housekeeping.cycle` fan-in).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.handoff_stamp import build_stamp_mutate
from coordinator_core.ops.handoff_transition import build_ship_mutate
from coordinator_core.shipped_in_tokens import _NO_COMMIT_TOKEN_RE, _SHA_HEX_RE
from coordinator_core.wire_paths import rel_id as _wire_rel_id


def _final_stamp_value(resolved: str) -> str:
    """Storage-form transform applied to a value about to be written as
    ``shipped_in`` — truncates a hex SHA to the module's own 8-char format
    contract, but leaves the sanctioned no-commit token verbatim.

    Reproduces ``coordinator_core.archive_stamp._final_stamp_value``
    verbatim — this module never calls `archive_stamp.stamp_shipped_in`
    (that wrapper resolves a sha via scope-paths/branch-tip, which this
    targeted path deliberately never does — see module Negative-spec), but
    a caller-supplied ``sha`` must still land in the SAME 8-char-truncated
    storage form every other writer uses, or a legitimate same-commit
    re-stamp (AC6b) would never canonically match what is actually on disk.
    """
    if _NO_COMMIT_TOKEN_RE.fullmatch(resolved):
        return resolved
    return resolved[:8]


def _sha_canonically_matches(supplied: str, prior_value: str) -> bool:
    """True when ``supplied`` and ``prior_value`` name the SAME commit.

    Reproduces ``coordinator_core.ops.handoff_archive_transition.
    _sha_canonically_matches`` verbatim (case-insensitive prefix compare, the
    stored ``shipped_in`` being the 8-char `_final_stamp_value`-truncated
    form of a caller's full-length ``sha``) — duplicated here rather than
    imported so this module does not pull in
    `handoff_archive_transition.py`'s own module-level imports
    (`ops.ceremony.git_native`, `ops.fleet._common.archive_and_commit`/
    `Move`), which this targeted path has no use for and does not want to
    pay the import cost of. This is a small, pure comparator (not one of the
    SHA-QUOTING guards the governing plan names as never-re-derive) — see
    that module's own docstring for the full AC6/AC6b rationale this
    reproduces.
    """
    a, b = supplied.strip().lower(), prior_value.strip().lower()
    if not a or not b:
        return False
    if len(a) < len(b):
        return False
    return a.startswith(b)


def _err(msg: str) -> dict:
    """Return an exit_code=1 envelope, key set matching
    ``handoff_archive_transition._err`` (AC11 uniformity, reproduced here so
    a caller cannot tell which module actually served the response)."""
    return {
        "exit_code": 1,
        "mode": "stamp_only",
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


def _usage_error(msg: str) -> dict:
    """Return an exit_code=2 envelope, key set matching
    ``handoff_archive_transition._usage_error``."""
    return {
        "exit_code": 2,
        "mode": "stamp_only",
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


class _StampOnlyRefusal(Exception):
    """Internal signal raised from inside the combined mutate closure to
    abort the write and report a specific, already-formatted error dict —
    distinct from `MutateAbort`, whose single string arg cannot carry the
    AC6/Position-A/warnings-list distinctions this module's several refusal
    shapes need. Caught immediately around the `locked_rmw` call; never
    escapes this module."""

    def __init__(self, envelope: dict):
        super().__init__(envelope.get("error"))
        self.envelope = envelope


def ship_stamp_only(
    handoff_path: str,
    repo_root: Optional[Path],
    *,
    sha: Optional[str] = None,
    kind: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Targeted, zero-corpus-read composition of
    ``handoff.archive_transition``'s ``mode="stamp_only"``.

    Params mirror that op's own ``sha``/``kind``/``force`` params exactly
    (see its docstring) — this function implements ONLY that one mode, so it
    has no ``mode``/``continued_into``/``successor_path``/``restage_src``
    params (those belong to modes this module does not implement).

    Returns the SAME envelope shape ``handoff_archive_transition._handler``
    returns for ``mode="stamp_only"`` — this is C4's contract to verify
    byte-for-byte; this module's own job is to reproduce it, not to
    reshape it.
    """
    handoff_path_raw = (handoff_path or "").strip()
    if not handoff_path_raw:
        return _usage_error("'handoff_path' is required")
    if repo_root is None:
        return _err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )
    if force and not (sha and sha.strip()):
        return _usage_error(
            "'force' requires 'sha' — force must never trigger its own "
            "resolution (see archive_stamp.stamp_shipped_in's Negative-spec)"
        )

    stamp_sha = sha.strip() if isinstance(sha, str) and sha.strip() else None

    # Kind default/override — mirrors `handoff_archive_transition._handler`'s
    # own `stamp_kind_override` resolution exactly (this module has no
    # scope-derivation path of its own — see Negative-spec — so 'scope-
    # derived' is never actually threaded into a stamp_mutate call here; it
    # only appears as the computed-but-unused default when `sha` is absent).
    if kind is not None:
        if stamp_sha is None:
            return _usage_error(
                "'kind' requires 'sha' — an explicit kind override only makes "
                "sense paired with the sha it describes (mirrors "
                "archive_stamp.stamp_shipped_in's own kind/sha cross-validation)"
            )
        if kind not in ("ship-commit", "successor"):
            return _usage_error(
                f"unsupported kind override {kind!r} for this op "
                "— must be 'ship-commit' or 'successor' when paired with an "
                "explicit sha ('no-commit' and 'scope-derived' have no "
                "explicit-override call shape here)"
            )
        stamp_kind: Optional[str] = kind
    else:
        stamp_kind = "ship-commit" if stamp_sha else "scope-derived"

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Containment (C1 item 1) — live-only, same as every mode except supersede.
    allowed_roots = [worktree / "state" / "handoffs"]
    contained = contained_path(p, allowed_roots)
    if contained is None:
        return _usage_error(f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}")

    if not contained.is_file():
        return _err(f"handoff not found on disk: {handoff_path_raw}")

    rel_id = _wire_rel_id(contained, worktree)

    warnings: list = []
    stamped_holder = {"value": False}

    # DR-096 scope-derivation-selection surfacing (mirrors
    # `handoff_archive_transition._handler`'s own unconditional warning,
    # computed once ahead of the do_stamp_only block, for ANY call with no
    # `sha` — same wording, same trigger, reproduced here byte-for-byte so
    # the returned envelope does not move for a no-sha call). This module
    # never actually resolves 'scope-derived' itself (see Negative-spec);
    # the warning fires purely on the kind DEFAULTING to it.
    if stamp_kind == "scope-derived":
        warnings.append(
            f"shipped_in_kind selected: scope-derived (legacy write-time "
            f"strategy, retired as preferred per DR-096) — no --sha was "
            f"supplied to this 'stamp_only' call for {rel_id}"
        )

    stamp_mutate = None
    stamp_state: Optional[dict] = None
    if stamp_sha is not None:
        # Shape validation + storage-form truncation — mirrors
        # `archive_stamp.stamp_shipped_in`'s own override validation exactly
        # (this module never calls that function — see Negative-spec — but
        # a caller-supplied `sha` still owes the SAME shape check and the
        # SAME 8-char truncated storage form every other writer uses).
        is_no_commit_token = bool(_NO_COMMIT_TOKEN_RE.fullmatch(stamp_sha))
        is_hex = bool(_SHA_HEX_RE.fullmatch(stamp_sha))
        if not (is_no_commit_token or is_hex) or (is_no_commit_token and not is_hex):
            # Malformed shape, OR a syntactically-no-commit-token value paired
            # with kind in {'ship-commit', 'successor'} (both REQUIRE a hex
            # sha per stamp_shipped_in's own cross-validation — this module
            # never accepts kind='no-commit', so a no-commit-token override
            # is always a mismatch here). `handoff_archive_transition._handler`
            # never surfaces the underlying validation reason in the envelope
            # (only via stamp_shipped_in's own stderr print) — it reports a
            # uniform "stamp transport failure" for any outcome.exit_code!=0,
            # reproduced verbatim here.
            warnings.append(
                f"stamp_shipped_in exited 1 for {rel_id} — stamp transport failure"
            )
            out = _err(
                f"stamp transport failure for {rel_id}: stamp_shipped_in "
                f"exited 1 — --stamp-only aborted, nothing else mutated by "
                "this call; retry once the underlying failure is resolved "
                "(pass --sha to retry with an explicit override once "
                "resolved, if appropriate)"
            )
            out["warnings"] = warnings
            return out
        stamp_mutate, stamp_state = build_stamp_mutate(
            handoff_path_raw, _final_stamp_value(stamp_sha), stamp_kind, force=force
        )

    ship_mutate, ship_state = build_ship_mutate(handoff_path_raw)

    def _mutate(old_text: str) -> str:
        text = old_text
        before_split = split_frontmatter(text)
        if before_split is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {handoff_path_raw}")
        before_shipped_in = read_fm_field_unquoted(before_split.fm_text, "shipped_in")
        before_shipped_in = None if before_shipped_in in (None, "null", "") else before_shipped_in

        if stamp_mutate is not None:
            text = stamp_mutate(text)
            assert stamp_state is not None
            applied = stamp_state["applied"][0]
            prior_value = stamp_state["prior_value"][0]
            if prior_value is None:
                # handoff.stamp's own idempotent-skip branch never populates
                # prior_value on the true "already present, not forced"
                # no-op — only its force-replace branch does. Fall back to
                # the pre-mutation on-disk value, safe here because a skip
                # is a non-write (mirrors archive_stamp.stamp_shipped_in's
                # own `_read_current_shipped_in` fallback).
                prior_value = before_shipped_in
            if not applied and stamp_sha and not (
                prior_value is not None and _sha_canonically_matches(stamp_sha, prior_value)
            ):
                # AC6 — a supplied --sha the idempotency guard would silently
                # retain-over must never be discarded quietly.
                raise _StampOnlyRefusal(
                    _err(
                        f"refusing to discard supplied --sha {stamp_sha!r} for {rel_id}: "
                        f"shipped_in is already present (prior_value={prior_value!r}) "
                        "and does not canonically match the supplied sha — pass "
                        "--force to overwrite it"
                    )
                )

        after_split = split_frontmatter(text)
        if after_split is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {handoff_path_raw}")
        after_shipped_in = read_fm_field_unquoted(after_split.fm_text, "shipped_in")
        after_shipped_in = None if after_shipped_in in (None, "null", "") else after_shipped_in

        if after_shipped_in is None or after_shipped_in == before_shipped_in:
            # AC7 — distinguish "nothing was ever there" from "a stamp
            # attempt no-opped, retaining a prior value".
            if before_shipped_in is None:
                warnings.append(
                    f"stamp_shipped_in resolved no commit for {rel_id}'s scope: "
                    "paths — shipped_in left unset (Position A: no branch-tip "
                    "fallback)"
                )
            else:
                warnings.append(
                    f"stamp_shipped_in retained prior value {before_shipped_in!r} "
                    f"for {rel_id} — nothing new was written"
                )
            stamped_holder["value"] = False
        else:
            stamped_holder["value"] = True

        # Position A refusal (C1 item 6): never flip deployment_state:shipped
        # when shipped_in would be left unset.
        if after_shipped_in is None:
            raise _StampOnlyRefusal(
                _err(
                    f"stamp_only: refusing to flip deployment_state:shipped for "
                    f"{rel_id} — no shipped_in could be resolved from its scope: "
                    "paths, and no --sha was supplied to `archive-stamp-cli "
                    "ship-handoff`. Pass --sha <SHA> to resolve/override "
                    "shipped_in explicitly (Position A never guesses a "
                    "branch-tip sha — see module docstring)."
                )
            )

        text = ship_mutate(text)
        return text

    try:
        locked_rmw(contained, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        return _err(f"lock timeout acquiring file lock: {exc}")
    except OSError as exc:
        return _err(f"cannot read/write handoff file: {exc}")
    except _StampOnlyRefusal as exc:
        out = dict(exc.envelope)
        out["stamped"] = stamped_holder["value"]
        out["warnings"] = warnings
        return out
    except MutateAbort as exc:
        out = _err(str(exc.args[0]) if exc.args else "mutate aborted")
        out["stamped"] = stamped_holder["value"]
        out["warnings"] = warnings
        return out

    return {
        "exit_code": 0,
        "mode": "stamp_only",
        "stamped": stamped_holder["value"],
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "moved": False,
        "warnings": warnings,
        "message": (
            f"stamped {rel_id} (deployment_state: shipped) — retained in "
            "state/handoffs/ for later archival sweep"
        ),
    }


# ---------------------------------------------------------------------------
# C3 — chain / supersede: move without commit (archive_and_commit does the
# commit — see module docstring § C3 addendum).
# ---------------------------------------------------------------------------


def _chain_err(msg: str) -> dict:
    """Return an exit_code=1 envelope for mode='chain' (key set matches
    `handoff_archive_transition._err`, mode hardcoded since this function
    implements exactly one mode)."""
    return {
        "exit_code": 1,
        "mode": "chain",
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


def _chain_usage_error(msg: str) -> dict:
    """Return an exit_code=2 envelope for mode='chain' (key set matches
    `handoff_archive_transition._usage_error`)."""
    out = _chain_err(msg)
    out["exit_code"] = 2
    return out


def _supersede_err(msg: str) -> dict:
    """Return an exit_code=1 envelope for mode='supersede' (key set matches
    `handoff_archive_transition._err`)."""
    return {
        "exit_code": 1,
        "mode": "supersede",
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "retain_kind": None,
        "moved": False,
        "warnings": [],
        "error": msg,
        "message": None,
    }


def _supersede_usage_error(msg: str) -> dict:
    """Return an exit_code=2 envelope for mode='supersede' (key set matches
    `handoff_archive_transition._usage_error`)."""
    out = _supersede_err(msg)
    out["exit_code"] = 2
    return out


async def chain_archive_handoff(
    handoff_path: str,
    repo_root: Optional[Path],
) -> dict:
    """Targeted, zero-corpus-read composition of
    ``handoff.archive_transition``'s ``mode="chain"``.

    No stamp (mode='chain' never stamps — see
    `handoff_archive_transition.py`'s own module docstring mode table): the
    ONLY work here is the terminal-state precondition (plan C1 item 3) and
    the git-mv (item 4). Returns the SAME envelope shape `_handler` returns
    for ``mode="chain"`` — C4's contract to verify byte-for-byte.
    """
    from coordinator_core.ops.fleet._common import (
        Move,
        archive_and_commit,
        handoff_archive_dest,
    )
    from coordinator_core.ops.handoff_archive_transition import (
        _TERMINAL_DEPLOYMENT_STATES,
        _current_deployment_state,
    )

    handoff_path_raw = (handoff_path or "").strip()
    if not handoff_path_raw:
        return _chain_usage_error("'handoff_path' is required")
    if repo_root is None:
        return _chain_err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Containment (C1 item 1) — live-only, chain never widens (item 1a is
    # supersede-only).
    allowed_roots = [worktree / "state" / "handoffs"]
    contained = contained_path(p, allowed_roots)
    if contained is None:
        return _chain_usage_error(f"handoff_path escapes state/handoffs/: {handoff_path_raw!r}")

    if not contained.is_file():
        return _chain_err(f"handoff not found on disk: {handoff_path_raw}")

    rel_id = _wire_rel_id(contained, worktree)

    # Terminal-state precondition (C1 item 3) — refuse the move outright when
    # the candidate's on-disk deployment_state is not already terminal. chain
    # stamps nothing itself, so this is the only tooth keeping a non-terminal
    # baton out of archive/handoffs/.
    current_deployment_state = _current_deployment_state(contained)
    if current_deployment_state not in _TERMINAL_DEPLOYMENT_STATES:
        return _chain_err(
            f"refusing to archive {rel_id}: deployment_state is "
            f"{current_deployment_state!r}, not terminal (must be one of "
            f"{sorted(_TERMINAL_DEPLOYMENT_STATES)}) — mode='chain' does "
            "not stamp a terminal state on this baton. Reach a terminal "
            "state first: mode='stamp_shipped' (-> deployment_state: "
            "shipped), mode='supersede' with continued_into=<successor> "
            "(-> deployment_state: continued), or a direct "
            "handoff.transition close call (-> deployment_state: closed) "
            "— then retry this archival."
        )

    # git-mv (C1 item 4) — ONE-element Move batch through archive_and_commit,
    # never a bare os.replace (see module docstring § C3 addendum / the
    # governing plan's Reconciliation section: a bare rename is committed by
    # nothing, since the cadence sweep reads state/handoffs/ from disk).
    dest = handoff_archive_dest(worktree, contained)
    move = Move(src=contained, dst=dest, candidate_id=rel_id, restage_src=False)
    subject = f"archive handoff: {rel_id}\n\nVia handoff.archive_transition (mode=chain)."
    acted, failed = await archive_and_commit(worktree, [move], subject)

    moved = bool(acted) and not failed
    warnings: list = []
    move_failure_reason: Optional[str] = None
    if failed:
        move_failure_reason = failed[0].get("reason") or "git mv failed, no reason reported"
        warnings.append(f"not archived: {move_failure_reason}")

    if moved:
        message = f"archived {rel_id} to {_wire_rel_id(dest, worktree)}"
    elif move_failure_reason:
        message = f"{rel_id}: not archived: {move_failure_reason}"
    else:
        message = f"{rel_id}: archival did not complete this call"

    out = {
        "exit_code": 0,
        "mode": "chain",
        "stamped": False,
        "superseded": False,
        "retained": False,
        "retain_reason": None,
        "moved": moved,
        "warnings": warnings,
        "message": message,
    }
    if failed:
        out["failed"] = failed
    return out


async def supersede_archive_handoff(
    handoff_path: str,
    repo_root: Optional[Path],
    *,
    continued_into: str,
    sha: Optional[str] = None,
    kind: Optional[str] = None,
    force: bool = False,
) -> dict:
    """Targeted, zero-corpus-read composition of
    ``handoff.archive_transition``'s ``mode="supersede"``.

    Reuses `handoff_archive_transition.py`'s own private per-record helpers
    function-locally (`_supersede_continued`, `_handoff_live_holder_session`,
    `_commit_retained_supersede_flip`, `_current_deployment_state`,
    `_current_shipped_in`, `_sha_canonically_matches`,
    `_TERMINAL_DEPLOYMENT_STATES`) — NOT reimplemented here (plan C1 items
    9-17). Returns the SAME envelope shape `_handler` returns for
    ``mode="supersede"``.
    """
    from coordinator_core.archival import claimed_or_shipped_at_path
    from coordinator_core.archive_stamp import stamp_shipped_in
    from coordinator_core.ops.fleet._common import (
        ARCHIVE_ROOT_SUBDIRS,
        Move,
        archive_and_commit,
        handoff_archive_dest,
    )
    from coordinator_core.ops.handoff_archive_transition import (
        _TERMINAL_DEPLOYMENT_STATES,
        _commit_retained_supersede_flip,
        _current_deployment_state,
        _current_fm_field,
        _current_shipped_in,
        _handoff_live_holder_session,
        _sha_canonically_matches as _ha_sha_canonically_matches,
        _supersede_continued,
    )

    handoff_path_raw = (handoff_path or "").strip()
    continued_into = (continued_into or "").strip()
    stamp_sha = sha.strip() if isinstance(sha, str) and sha.strip() else None
    stamp_force = bool(force)
    kind_raw = kind.strip() if isinstance(kind, str) and kind.strip() else None

    if not continued_into:
        return _supersede_usage_error(
            "mode 'supersede' requires 'continued_into' (successor handoff "
            "id-or-path) — DR-084 retires the consumed+abandoned expression; "
            "an automated writer may only stamp deployment_state:continued on "
            "positive succession proof"
        )

    if kind_raw is not None:
        if not stamp_sha:
            return _supersede_usage_error(
                "'kind' requires 'sha' — an explicit kind override only makes "
                "sense paired with the sha it describes"
            )
        if kind_raw not in ("ship-commit", "successor"):
            return _supersede_usage_error(
                f"unsupported kind override {kind_raw!r} for this op "
                "— must be 'ship-commit' or 'successor' when paired with an "
                "explicit sha"
            )
        stamp_kind = kind_raw
    else:
        stamp_kind = "ship-commit" if stamp_sha else "scope-derived"

    if stamp_force and not stamp_sha:
        return _supersede_usage_error(
            "'force' requires 'sha' — force must never trigger its own "
            "resolution (see archive_stamp.stamp_shipped_in's Negative-spec)"
        )

    if not handoff_path_raw:
        return _supersede_err("'handoff_path' is required")
    if repo_root is None:
        return _supersede_err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p

    # Containment (C1 item 1a) — supersede is the ONE mode admitting an
    # already-archived path under ARCHIVE_ROOT_SUBDIRS in addition to
    # state/handoffs/.
    live_root = worktree / "state" / "handoffs"
    allowed_roots = [live_root] + [worktree / sub for sub in ARCHIVE_ROOT_SUBDIRS]
    contained = contained_path(p, allowed_roots)
    if contained is None:
        return _supersede_usage_error(
            "handoff_path escapes state/handoffs/ and every known archive dir "
            f"({', '.join(ARCHIVE_ROOT_SUBDIRS)}): {handoff_path_raw!r}"
        )

    if not contained.is_file():
        return _supersede_err(f"handoff not found on disk: {handoff_path_raw}")

    is_archived_target = contained_path(contained, [live_root]) is None

    rel_id = _wire_rel_id(contained, worktree)

    # DR-242 gate (C1 item 9) — reachable directly, bypassing every
    # wrapper-level claimed_or_shipped_at_path check.
    if not claimed_or_shipped_at_path(str(contained)):
        return _supersede_err(
            f"mode='supersede' refused: {rel_id} was never claimed or shipped "
            "(DR-242: a successor-named child is not evidence of succession; "
            "nothing to supersede)"
        )

    # closed-baton-is-terminal gate (C1 item 10).
    if _current_deployment_state(contained) == "closed":
        closed_reason = _current_fm_field(contained, "closed_reason")
        return _supersede_err(
            f"mode='supersede' refused: {rel_id} is deployment_state: "
            f"closed (closed_reason: {closed_reason}) — a closed baton "
            "is terminal and is not superseded; if the closure was "
            "wrong, reopen it first"
        )

    warnings: list = []
    stamped = False
    superseded = False

    # do_stamp (C1 item 11) — unconditional, before the guard, mirrors
    # `handoff_archive_transition.py`'s own do_stamp block for supersede.
    before = _current_shipped_in(contained)
    outcome = await asyncio.to_thread(
        stamp_shipped_in,
        str(contained),
        kind=stamp_kind,
        allow_branch_tip_fallback=False,
        sha=stamp_sha,
        force=stamp_force,
    )
    if outcome.exit_code != 0:
        warnings.append(
            f"stamp_shipped_in exited {outcome.exit_code} for {rel_id} "
            "— stamp transport failure"
        )
        out = _supersede_err(
            f"stamp transport failure for {rel_id}: stamp_shipped_in "
            f"exited {outcome.exit_code} — archival aborted, nothing "
            "else mutated by this call; retry once the underlying "
            "failure is resolved (pass --sha to retry with an explicit "
            "override once resolved, if appropriate)"
        )
        out["warnings"] = warnings
        return out
    elif not outcome.applied and stamp_sha and not (
        outcome.prior_value is not None
        and _ha_sha_canonically_matches(stamp_sha, outcome.prior_value)
    ):
        out = _supersede_err(
            f"refusing to discard supplied --sha {stamp_sha!r} for {rel_id}: "
            f"shipped_in is already present (prior_value="
            f"{outcome.prior_value!r}) and does not canonically match the "
            "supplied sha — pass --force to overwrite it"
        )
        out["warnings"] = warnings
        return out
    else:
        after = _current_shipped_in(contained)
        if after is None or after == before:
            if outcome.prior_value is None:
                warnings.append(
                    f"stamp_shipped_in resolved no commit for {rel_id}'s scope: "
                    "paths — shipped_in left unset (Position A: no branch-tip "
                    "fallback)"
                )
            else:
                warnings.append(
                    f"stamp_shipped_in retained prior value "
                    f"{outcome.prior_value!r} for {rel_id} — nothing new was "
                    "written"
                )
        else:
            stamped = True

    # Supersede status flip (C1 item 12/13/14) — BEFORE the live-holder
    # retain ground, PM ruling: a live claim holder is irrelevant to whether
    # the predecessor is superseded.
    supersede_res = await asyncio.to_thread(
        _supersede_continued, contained, continued_into, repo_root
    )
    if supersede_res.get("exit_code") != 0:
        out = _supersede_err(
            f"supersede failed: {supersede_res.get('error', 'unknown error')}"
        )
        out["stamped"] = stamped
        out["warnings"] = warnings
        return out
    warnings.extend(supersede_res.get("warnings") or [])
    superseded = True

    # Archived-predecessor stamp-in-place (C1 item 15) — DONE the moment the
    # status flip lands; no git-mv to perform, no guard question to ask.
    if is_archived_target:
        return {
            "exit_code": 0,
            "mode": "supersede",
            "stamped": stamped,
            "superseded": superseded,
            "retained": False,
            "retain_reason": None,
            "moved": False,
            "warnings": warnings,
            "message": (
                f"superseded {rel_id} in place — already archived, no move needed"
            ),
        }

    # Holder-liveness retain ground (C1 item 16) — supersede-only.
    live_holder_session = await asyncio.to_thread(
        _handoff_live_holder_session, contained, repo_root
    )
    if live_holder_session is not None:
        retain_reason = (
            f"predecessor retained — claim holder {live_holder_session} is live"
        )
        flip_status = await asyncio.to_thread(
            _commit_retained_supersede_flip,
            worktree,
            rel_id,
            supersede_res["written_text"],
            warnings,
        )
        return {
            "exit_code": 0,
            "mode": "supersede",
            "stamped": stamped,
            "superseded": superseded,
            "retained": True,
            "retain_reason": retain_reason,
            "retain_kind": "live-holder",
            "moved": False,
            "warnings": warnings,
            "error": None,
            "message": f"superseded {rel_id}; {retain_reason} ({flip_status})",
        }

    # Terminal-state precondition (C1 item 3) — the status flip above just
    # wrote deployment_state:continued, a terminal state, so this ordinarily
    # passes; kept as the same tooth `_handler` applies uniformly.
    current_deployment_state = _current_deployment_state(contained)
    if current_deployment_state not in _TERMINAL_DEPLOYMENT_STATES:
        out = _supersede_err(
            f"refusing to archive {rel_id}: deployment_state is "
            f"{current_deployment_state!r}, not terminal (must be one of "
            f"{sorted(_TERMINAL_DEPLOYMENT_STATES)}) — mode='supersede' does "
            "not stamp a terminal state on this baton. Reach a terminal "
            "state first: mode='stamp_shipped' (-> deployment_state: "
            "shipped), mode='supersede' with continued_into=<successor> "
            "(-> deployment_state: continued), or a direct "
            "handoff.transition close call (-> deployment_state: closed) "
            "— then retry this archival."
        )
        out["stamped"] = stamped
        out["superseded"] = superseded
        out["warnings"] = warnings
        return out

    # git-mv (C1 item 4) — ONE-element Move batch through archive_and_commit,
    # restage_src=True: do_stamp/do_supersede both wrote `contained` on disk
    # ahead of this move.
    dest = handoff_archive_dest(worktree, contained)
    move = Move(src=contained, dst=dest, candidate_id=rel_id, restage_src=True)
    subject = f"archive handoff: {rel_id}\n\nVia handoff.archive_transition (mode=supersede)."
    acted, failed = await archive_and_commit(worktree, [move], subject)

    moved = bool(acted) and not failed
    move_failure_reason: Optional[str] = None
    if failed:
        move_failure_reason = failed[0].get("reason") or "git mv failed, no reason reported"
        warnings.append(f"not archived: {move_failure_reason}")

    if moved:
        message = f"archived {rel_id} to {_wire_rel_id(dest, worktree)}"
    elif move_failure_reason:
        message = f"superseded {rel_id}; not archived: {move_failure_reason}"
    else:
        message = f"superseded {rel_id}; archival did not complete this call"

    out = {
        "exit_code": 0,
        "mode": "supersede",
        "stamped": stamped,
        "superseded": superseded,
        "retained": False,
        "retain_reason": None,
        "moved": moved,
        "warnings": warnings,
        "message": message,
    }
    if failed:
        out["failed"] = failed
    return out
