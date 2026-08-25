"""
coordinator_core.ops.roadmap_link_stubs — JSON-RPC "roadmap.link_stubs" operation.

Purpose: the FIRST op that AUTHORS a roadmap-dependency edge. Writes it on BOTH
endpoints of a `state/handoffs/*.md` roadmap-baton pair: `blocked_by` on the
dependent stub (the one being blocked), `blocks` on its reciprocal — the
dependency stub (the one it is blocked by). Before this op existed, DEF-3
(`docs/plans/2026-08-05-roadmap-graph-enforcement-gap.md` § Problem) meant
`blocked_by` was simultaneously schema-mandatory
(`frontmatter/schema_validate.py:_cf_spinoff_roadmap_requires_graph`) and
un-authorable by any op anywhere in `coordinator_core`.

Authority chain (read before touching this module):
    - `docs/decisions/DR-264-roadmap-link-stubs-frontmatter-mutation-.md`
      (status: accepted, ratified_on 2026-08-05) — extends DR-212's
      `handoff.*` in-place frontmatter-mutation carve-out to this NEW,
      non-`handoff.*`-named op, per DR-212 Invariant 3 ("future ops with
      different semantics or target nouns require their own DR"). DR-264
      restates and satisfies all five DR-212 D2 admission bounds for this
      op's two-file compound-transaction shape — see its "## Decision"
      section for the per-bound argument this module's negative-spec below
      only summarizes.
    - `docs/decisions/DR-247-bounded-body-write-carveout-for-claimed-handoff.md`
      § 3 — the authorship-gate reasoning this op's NO-GATE design rests on
      (see "Authorization basis" below), as realized in
      `coordinator_core.ops.propagate_body`.
    - `coordinator_core/ops/handoff_transition.py`'s array-field write
      helpers (`_replace_fm_array_field` / `_insert_fm_array_field` /
      `_yaml_flow_seq`) — reused here, never hand-rolled. See "List-field
      writes" below.

Authorization basis — NO authorship gate (DR-247 § 3, DR-264 "General
principle"): a roadmap baton's `authoring_session` is a PATH
(`state/roadmap/<id>/`) by construction, from two independent emitters — no
live session id can ever equal it, so an authorship-equality gate here would
only ever produce a false refusal, never a real one. Substitute controls,
per DR-264's decision and this op's own admission bounds: a mandatory
non-empty `reason` argument (the `handoff_stamp.py`
`_repair_archived_shipped_in_handler` archived-repair-door precedent for
requiring one), and a stamped/dated response (echoed `reason`,
`applied_blocked_by`/`applied_blocks`, timestamp, logged via `_LOG.info`) —
the paper trail DR-247 § 3 names as the real control, not the gate.

Unlike `propagate_body.py`'s dirty-tree abort (its own AC12 precondition 1),
this op carries NO dirty-tree check. That abort exists solely to guard
`propagate_body`'s own self-commit (see `propagate_body._rollback_delivery`,
which exists only to undo a failed commit on a tree the abort failed to keep
clean) — this op self-commits nothing (see "No git commit" below), so the
abort's entire justification is absent here. Neither comparable
non-committing DR-212 op (`handoff_transition.py`, `handoff_stamp.py`)
carries one either. Keeping it would make this op refuse to run in the EM's
normal working state (dirty tree mid-session, commit deferred per DR-212
D2(iv)) — a control that can only ever produce a false refusal, the exact
failure mode this op's no-authorship-gate design already declines elsewhere.

No git commit (DR-212 Invariant 4, DR-247 § (vi), DR-264 § (iv)): this op is
a FILESYSTEM MUTATION ONLY. `propagate_body.py`'s self-commit is a SEPARATE,
independently-PM-ruled divergence (2026-08-01 cross-machine-durability
ruling, per that op's own docstring) — it is not a generalizable consequence
of DR-247 § 3, and this op does not inherit it. Roadmap batons live under
`state/handoffs/*.md`, inside DR-212's governed noun; commit timing remains
the EM session's responsibility.

List-field writes (2026-07-13 lesson,
`state/lessons/2026-07-13-makima-frontmatter-write-list-fields-via-51feff6964c3.yaml`):
`replace_fm_field`/`insert_fm_field` route every value through
`serialize_yaml_scalar`, which single-quotes any string containing `[`/`]`,
turning an array into a YAML STRING. This op therefore writes `blocked_by`/
`blocks` exclusively via `handoff_transition.py`'s raw flow-sequence helpers
(`_replace_fm_array_field` when the key line is already present,
`_insert_fm_array_field` when it is absent — `_replace_fm_array_field` is
REPLACE-ONLY, a regex `.sub` that silently no-ops when the key line is
missing, so this op branches on key presence rather than assuming the
schema-mandated key is always there).

Field allowlist IS the substitute guard control (F12): `block_consumed_
handoff_edit.py` (write_guards) protects a claimed/consumed handoff's
frontmatter from a hand-edit; that guard is TOOL-level (PreToolUse on
Edit/Write) and this op's UDS invocation never passes through it — so this
op is an UNGATED path onto the same files that guard protects. The narrow
two-field allowlist below (`blocked_by`/`blocks`, roadmap batons only) is
what makes that acceptable: it is the reason behind the plan's own
Anti-scope bullet ("do NOT let `roadmap.link_stubs` become a general
frontmatter writer"). Widening this op's field set — or letting it touch any
`kind` other than `roadmap-baton` — silently reopens the surface that guard
exists to close. Do not extend it without a fresh DR (DR-264's own
"Implementation" section repeats this warning).

Refusal set (AC7):
    - Either endpoint fails to resolve to an existing stub, anywhere in the
      corpus this op can see.
    - An endpoint that resolves ONLY in the archived corpus
      (`archive/handoffs/`) — DR-212 D4 forbids writing `archive/` outright
      ("Write archive/ — that is DR-211's noun; not this carve-out's"), so an
      archived-only resolution is a REFUSAL, never a write target, even
      though `run_check_mode`'s own resolution (which this op deliberately
      mirrors for consistency) legitimately reads both corpora.
    - A self-edge (an endpoint blocked-by/blocks itself).
    - Endpoints whose resolved stubs carry a DIFFERENT `roadmap_id` than the
      one named in the invocation — resolution is corpus-wide (not
      pre-filtered by `roadmap_id`) specifically so this mismatch can be
      reported as its own refusal, distinct from "does not exist at all".
    - The target is not a `kind: roadmap-baton` (or its live pre-rename
      aliases — `coordinator_core.frontmatter.baton_class.
      kind_values_for_canonical`) record.
    - The write would create a cycle in the roadmap_id's own `blocked_by`
      graph.
    - NOT a refusal: the edge already exists on BOTH endpoints — that is an
      idempotent no-op, `exit_code: 0`, `applied: False`.

Repair, not skip (DR-212 D2(i)): a HALF-present edge (one direction already
written, the other missing — exactly the state a crashed or interrupted
prior invocation of this same op can leave) is NOT treated as "already
exists". The missing side is written; per-file idempotence obliges
convergence, not a silent no-op on a partial state.

Negative-spec:
    - Does NOT gate on `authoring_session` — see "Authorization basis" above.
    - Does NOT modify body content on either file — frontmatter-only, per
      DR-264 (iii)'s two-field allowlist.
    - Does NOT touch any path outside `state/handoffs/` — never
      `archive/handoffs/` (see refusal set above).
    - Does NOT issue a git commit of any kind, self-scoped or otherwise —
      see "No git commit" above.
    - Does NOT let `edges.txt` (or any file outside the two resolved
      `state/handoffs/*.md` targets) participate in resolution or the write —
      the sole edge source this op reads is live corpus frontmatter,
      mirroring `run_check_mode`'s own Anti-scope-pinned edge source.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import yaml

from coordinator_core.frontmatter.baton_class import kind_values_for_canonical
from coordinator_core.frontmatter.primitives import (
    _fm_key_line_pattern,
    rebuild,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import (
    LOCK_TIMEOUT_SECS,
    LockTimeout,
    MutateAbort,
    _acquire_flock,
    _lock_dir,
    _plat_unlock,
    locked_rmw,
)
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.ops.handoff_transition import (
    _insert_fm_array_field,
    _replace_fm_array_field,
)

_LOG = logging.getLogger(__name__)

# Same vendored schema every other state/handoffs/ writer in this package
# validates against (handoff_transition.py's own _SCHEMA_PATH) — a roadmap
# baton IS a handoff record, not a separate schema family.
_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent / "frontmatter" / "schemas" / "handoff.schema.json"
)

# Mirrors number_stubs.py's own _ROADMAP_BATON_KIND_WHERE derivation —
# `kind in (...)` covering the canonical `roadmap-baton` value plus any
# still-live retired pre-rename spelling(s), derived at import time rather
# than hand-authored. See coordinator_core/frontmatter/baton_class.py and
# coordinator_core/tests/test_baton_class_is_the_only_membership_set.py.
_ROADMAP_BATON_KIND_WHERE = "kind in ({})".format(
    ",".join(kind_values_for_canonical("roadmap-baton"))
)


def _err(msg: str) -> dict:
    _LOG.warning("roadmap.link_stubs: %s", msg)
    return {"exit_code": 1, "applied": False, "error": msg}


def _validate_fm(fm_text: str) -> list:
    """Parse fm_text as YAML and validate against the vendored handoff schema.

    Same post-mutation gate `handoff_transition.py`'s own `_validate_fm`
    applies — a round-trip through `schema_validate` confirming the written
    `blocked_by`/`blocks` value is an ARRAY, not a `serialize_yaml_scalar`-
    quoted string (AC6), and that nothing else about the record regressed.
    Returns a (possibly empty) list of error dicts; empty means valid.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]
    return validate_frontmatter(fm_dict, _SCHEMA_PATH)


# ---------------------------------------------------------------------------
# Corpus resolution
# ---------------------------------------------------------------------------


def _collect_roadmap_batons(worktree: Path) -> Tuple[Dict[str, dict], Dict[str, dict]]:
    """Return (live_by_stub_id, archived_by_stub_id) for ALL roadmap-baton
    records this worktree's corpus carries — NOT pre-filtered by roadmap_id.

    Corpus-wide (not roadmap_id-scoped) deliberately: the roadmap_id-mismatch
    refusal (AC7 / staff-eng F8, refusal (3)) needs to tell "does not exist
    anywhere" apart from "exists, but under a different roadmap_id" — a
    roadmap_id-scoped query cannot distinguish the two, it would just report
    the second case as the first.
    """
    live_records = query_records("handoff", worktree, where=_ROADMAP_BATON_KIND_WHERE)
    arch_records = query_records("handoff-archived", worktree, where=_ROADMAP_BATON_KIND_WHERE)

    live_by_id: Dict[str, dict] = {}
    for rec in live_records:
        stub_id = (rec.get("frontmatter") or {}).get("stub_id")
        if stub_id:
            live_by_id[stub_id] = rec

    arch_by_id: Dict[str, dict] = {}
    for rec in arch_records:
        stub_id = (rec.get("frontmatter") or {}).get("stub_id")
        if stub_id:
            arch_by_id[stub_id] = rec

    return live_by_id, arch_by_id


def _resolve_endpoint(
    stub_id: str,
    roadmap_id: str,
    live_by_id: Dict[str, dict],
    arch_by_id: Dict[str, dict],
    label: str,
) -> Tuple[Optional[dict], Optional[str]]:
    """Resolve one endpoint's live record, or a human-readable refusal reason.

    Returns (record, None) on success, or (None, reason) on refusal — see
    module docstring "Refusal set" for the four distinct non-existence/
    wrong-corpus/wrong-roadmap refusal shapes this distinguishes.
    """
    live_rec = live_by_id.get(stub_id)
    arch_rec = arch_by_id.get(stub_id)

    if live_rec is None and arch_rec is None:
        return None, (
            f"{label} stub_id {stub_id!r} does not resolve to an existing "
            f"roadmap-baton stub anywhere in the corpus — roadmap.link_stubs "
            f"never invents stubs"
        )

    if live_rec is None:
        return None, (
            f"{label} stub_id {stub_id!r} resolves only in the archived "
            f"corpus (archive/handoffs/) — refusing: DR-212 D4 forbids "
            f"writing archive/, that is DR-211's noun, not this carve-out's"
        )

    fm = live_rec.get("frontmatter") or {}
    rec_roadmap_id = fm.get("roadmap_id")
    if rec_roadmap_id != roadmap_id:
        return None, (
            f"{label} stub_id {stub_id!r} resolves in the live corpus but "
            f"carries roadmap_id {rec_roadmap_id!r}, not the invocation's "
            f"roadmap_id {roadmap_id!r} — refusing a cross-roadmap edge"
        )

    return live_rec, None


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value:
        return [str(value)]
    return []


def _reachable_via_blocked_by(
    start_stub_id: str,
    roadmap_id: str,
    live_by_id: Dict[str, dict],
    arch_by_id: Dict[str, dict],
) -> Set[str]:
    """DFS over the roadmap_id's existing `blocked_by` edges from
    start_stub_id, traversing live AND archived-corpus nodes.

    Returns the set of stub_ids `start_stub_id` is already, directly or
    transitively, blocked by. Used to detect whether adding a NEW
    `dependent blocked_by dependency` edge would close a cycle: if
    `dependent_stub_id` is already reachable from `dependency_stub_id` this
    way, `dependency` already (transitively) depends on `dependent`, and the
    new edge would complete a loop.

    Consults `arch_by_id` when a node isn't found in `live_by_id` (Finding
    3) — an archived stub is refused as a WRITE target elsewhere (see
    refusal set), but a chain routing through one must still count as a
    graph node for cycle detection, or a real cycle could go undetected by
    silently treating the archived node as a dead end.
    """
    seen: Set[str] = set()
    stack = [start_stub_id]
    while stack:
        cur = stack.pop()
        rec = live_by_id.get(cur)
        if rec is None:
            rec = arch_by_id.get(cur)
        if rec is None:
            continue
        fm = rec.get("frontmatter") or {}
        if fm.get("roadmap_id") != roadmap_id:
            continue
        for blocker in _as_list(fm.get("blocked_by")):
            if blocker not in seen:
                seen.add(blocker)
                stack.append(blocker)
    return seen


# ---------------------------------------------------------------------------
# Single-endpoint array-field write
# ---------------------------------------------------------------------------


def _write_edge_field(
    path: Path, repo_root: Path, field: str, other_stub_id: str,
) -> Tuple[bool, Optional[str]]:
    """Ensure `other_stub_id` is present in `path`'s frontmatter `field`
    array (`blocked_by` or `blocks`) — inserts if the key is absent
    (`_insert_fm_array_field`), replaces if present
    (`_replace_fm_array_field`); never hand-rolled (see module docstring
    "List-field writes"). Returns (applied, error) — applied=False on a
    byte-identical idempotent no-op (`other_stub_id` already present, never
    an error).
    """
    _state: Dict[str, Any] = {"applied": False}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {path}")

        fm = split.fm_text
        try:
            fm_dict = yaml.safe_load(fm) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"YAML parse error in frontmatter: {exc}")

        current_raw = fm_dict.get(field)
        # Canonical key-line boundary check (same rule _replace_fm_array_field/
        # _insert_fm_array_field themselves route through internally) — a
        # bare substring match on f"{field}:" would be fooled by CRLF or a
        # coincidental prefix; _fm_key_line_pattern is the single reviewed
        # boundary-lookahead rule for this (staff-eng Finding D, cited in
        # handoff_transition.py's own array-field helpers).
        key_present = _fm_key_line_pattern(field).search(fm) is not None
        current_list = _as_list(current_raw)

        if other_stub_id in current_list:
            _state["applied"] = False
            return old_text  # byte-identical -> locked_rmw skips the write

        new_list = current_list + [other_stub_id]

        if key_present:
            fm = _replace_fm_array_field(fm, field, new_list)
        else:
            # staff-eng F7: _replace_fm_array_field is REPLACE-ONLY and
            # silently no-ops when the key line is absent. A roadmap baton
            # always carries blocked_by/blocks per schema
            # (_cf_spinoff_roadmap_requires_graph requires both), so this
            # branch should not fire in practice, but this op's spec must
            # not rely on that unstated invariant holding.
            anchor = "blocks" if field == "blocked_by" else "roadmap_id"
            fm = _insert_fm_array_field(fm, field, new_list, anchor)

        errors = _validate_fm(fm)
        if errors:
            raise MutateAbort(
                f"roadmap.link_stubs: post-mutation frontmatter validation "
                f"failed for {path}: {format_validation_errors(errors)}"
            )

        _state["applied"] = True
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return False, f"target not found on disk: {path}"
    except LockTimeout as exc:
        return False, f"timed out waiting for file lock on {path}: {exc}"
    except MutateAbort as exc:
        return False, str(exc.args[0]) if exc.args else "mutate aborted"
    except OSError as exc:
        return False, f"cannot read/write {path}: {exc}"

    return _state["applied"], None


@contextlib.contextmanager
def _roadmap_id_lock(
    roadmap_id: str, repo_root: Path, timeout: float = LOCK_TIMEOUT_SECS,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock scoped to *roadmap_id* for the
    whole read-corpus -> cycle-check -> write-both sequence (Finding 2).

    `locked_rmw`'s own lock only ever covers a single call's single target
    file, momentarily -- two concurrent invocations building opposite-
    direction edges can each pass their own cycle check against a stale
    snapshot before either writes, producing the exact cycle the refusal set
    promises to prevent. This lock closes that TOCTOU window by serialising
    the whole sequence per roadmap_id.

    Distinct sidecar from either endpoint's own `locked_rmw` lock: this key
    is derived from a virtual, never-materialised path under
    `state/roadmap/<roadmap_id>/`, never either resolved stub's real path,
    so acquiring it cannot self-deadlock against the per-file locks
    `_write_edge_field` takes later in the same held sequence.

    `timeout` only bounds how long a waiter blocks trying to ACQUIRE this
    outer lock. Once held, the holder can itself block up to
    `LOCK_TIMEOUT_SECS` on each of the two inner `_write_edge_field` ->
    `locked_rmw` calls, so the effective worst-case serialised wait for a
    second caller on the same roadmap_id is closer to 2x `LOCK_TIMEOUT_SECS`
    than 1x -- not a correctness bug (fails closed, matches existing
    `LockTimeout` semantics elsewhere), just worth naming so a future caller
    reasoning about contention doesn't have to re-derive it.

    The key is canonicalised via `os.path.realpath` before hashing, matching
    `locked_write._lock_key`'s own `sha1(os.path.realpath(target))`
    convention, so this lock's correctness does not silently depend on every
    caller supplying an identically-formatted `repo_root` string. In practice
    `ipc.py` already resolves `repo_root` through a canonical table before
    ever calling this handler, so this is defense-in-depth: without it, a
    future direct caller passing a trailing-slash or symlinked-worktree-alias
    `repo_root` would silently defeat this lock's cross-process serialisation
    with no error, since the virtual path never materialises on disk for
    `os.path.realpath` to resolve against a symlink target -- realpath still
    normalises the non-existent leaf's parent chain, which is what collapses
    the alias.
    """
    virtual_target = repo_root / "state" / "roadmap" / roadmap_id / ".link_stubs.lock"
    key = hashlib.sha1(os.path.realpath(str(virtual_target)).encode()).hexdigest()
    lock_path = _lock_dir(repo_root) / f"roadmap-link-{key}.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        _acquire_flock(lock_fd, timeout)
        try:
            yield
        finally:
            _plat_unlock(lock_fd)
    finally:
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# Synchronous handler body (run off the event loop via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_link_stubs(
    *,
    roadmap_id: str,
    dependent_stub_id: str,
    dependency_stub_id: str,
    reason: str,
    repo_root: Path,
    worktree: Path,
) -> dict:
    """Synchronous resolve -> check -> write sequence for `roadmap.link_stubs`.

    Runs entirely off the event loop (see `_handler`'s
    `await asyncio.to_thread(...)` call) -- `_collect_roadmap_batons` reads
    the whole corpus synchronously and `_write_edge_field` -> `locked_rmw`
    polls a cross-process flock with `time.sleep` for up to
    `LOCK_TIMEOUT_SECS`; neither may run directly in an async body (DR-212
    D3, restated unchanged by DR-264). The whole sequence, from the corpus
    read through both writes, is held under `_roadmap_id_lock` (Finding 2).
    """
    with _roadmap_id_lock(roadmap_id, repo_root):
        live_by_id, arch_by_id = _collect_roadmap_batons(worktree)

        dependent_rec, err_msg = _resolve_endpoint(
            dependent_stub_id, roadmap_id, live_by_id, arch_by_id, "dependent",
        )
        if err_msg is not None:
            return _err(err_msg)
        dependency_rec, err_msg = _resolve_endpoint(
            dependency_stub_id, roadmap_id, live_by_id, arch_by_id, "dependency",
        )
        if err_msg is not None:
            return _err(err_msg)

        assert dependent_rec is not None and dependency_rec is not None  # narrowed above

        dependent_fm = dependent_rec.get("frontmatter") or {}
        dependency_fm = dependency_rec.get("frontmatter") or {}

        already_blocked_by = dependency_stub_id in _as_list(dependent_fm.get("blocked_by"))
        already_blocks = dependent_stub_id in _as_list(dependency_fm.get("blocks"))

        if already_blocked_by and already_blocks:
            _LOG.info(
                "roadmap.link_stubs: no-op — %s already blocked_by %s and %s "
                "already blocks %s (reason: %s)",
                dependent_stub_id, dependency_stub_id, dependency_stub_id,
                dependent_stub_id, reason,
            )
            return {
                "exit_code": 0,
                "applied": False,
                "applied_blocked_by": False,
                "applied_blocks": False,
                "reason": reason,
                "message": (
                    f"{dependent_stub_id} already blocked_by {dependency_stub_id} "
                    f"and {dependency_stub_id} already blocks {dependent_stub_id} "
                    f"— idempotent no-op"
                ),
            }

        # Cycle check (AC7) — only meaningful while the edge is not already
        # fully present (handled above): would completing this edge close a
        # loop in roadmap_id's own blocked_by graph? If dependent_stub_id is
        # already reachable FROM dependency_stub_id via existing blocked_by
        # edges, dependency already (transitively) depends on dependent, and
        # adding "dependent blocked_by dependency" would complete a cycle.
        # This whole check, and both writes below, run under the
        # roadmap_id-scoped lock acquired above (Finding 2) so no concurrent
        # invocation's snapshot can go stale underneath it.
        reachable_from_dependency = _reachable_via_blocked_by(
            dependency_stub_id, roadmap_id, live_by_id, arch_by_id,
        )
        if dependent_stub_id in reachable_from_dependency:
            return _err(
                f"refusing: writing blocked_by {dependency_stub_id} onto "
                f"{dependent_stub_id} would create a dependency cycle — "
                f"{dependency_stub_id} is already (transitively) blocked_by "
                f"{dependent_stub_id} in roadmap_id {roadmap_id!r}"
            )

        allowed_roots = [worktree / "state" / "handoffs"]

        def _resolve_write_path(rec: dict, label: str) -> Tuple[Optional[Path], Optional[str]]:
            raw_path = rec.get("path") or ""
            p = Path(raw_path)
            if not p.is_absolute():
                p = worktree / p
            resolved = contained_path(p, allowed_roots)
            if resolved is None:
                return None, (
                    f"{label} record's resolved path escapes state/handoffs/ "
                    f"(or targets archive/handoffs/, which this op never "
                    f"touches): {raw_path!r}"
                )
            return resolved, None

        dependent_path, path_err = _resolve_write_path(dependent_rec, "dependent")
        if path_err is not None:
            return _err(path_err)
        dependency_path, path_err = _resolve_write_path(dependency_rec, "dependency")
        if path_err is not None:
            return _err(path_err)

        assert dependent_path is not None and dependency_path is not None

        applied_blocked_by, write_err = _write_edge_field(
            dependent_path, repo_root, "blocked_by", dependency_stub_id,
        )
        if write_err is not None:
            return _err(f"failed writing blocked_by onto {dependent_stub_id}: {write_err}")

        applied_blocks, write_err = _write_edge_field(
            dependency_path, repo_root, "blocks", dependent_stub_id,
        )
        if write_err is not None:
            return _err(
                f"blocked_by was written onto {dependent_stub_id}, but writing "
                f"blocks onto {dependency_stub_id} failed: {write_err} — the "
                f"edge is now HALF-present; re-invoking roadmap.link_stubs with "
                f"the same params will REPAIR the missing side (DR-212 D2(i))"
            )

        ts = datetime.now(timezone.utc).isoformat()
        _LOG.info(
            "roadmap.link_stubs: %s blocked_by %s (applied=%s), %s blocks %s "
            "(applied=%s) at %s (reason: %s)",
            dependent_stub_id, dependency_stub_id, applied_blocked_by,
            dependency_stub_id, dependent_stub_id, applied_blocks, ts, reason,
        )
        return {
            "exit_code": 0,
            "applied": bool(applied_blocked_by or applied_blocks),
            "applied_blocked_by": applied_blocked_by,
            "applied_blocks": applied_blocks,
            "reason": reason,
            "message": (
                f"linked: {dependent_stub_id} blocked_by {dependency_stub_id}, "
                f"{dependency_stub_id} blocks {dependent_stub_id} "
                f"(roadmap_id={roadmap_id})"
            ),
        }


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("roadmap.link_stubs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "roadmap.link_stubs" handler.

    Writes a reciprocal roadmap-dependency edge on two `state/handoffs/*.md`
    roadmap-baton files: `blocked_by` on the dependent stub, `blocks` on the
    dependency stub. See module docstring for authority chain, authorization
    basis, refusal set, and negative-spec.

    Params:
        roadmap_id         (str) — the roadmap both endpoints must belong to.
                                    Required, non-empty.
        dependent_stub_id  (str) — the stub_id being blocked (gains a
                                    `blocked_by` entry naming
                                    dependency_stub_id). Required, non-empty.
        dependency_stub_id (str) — the stub_id being depended upon (gains a
                                    `blocks` entry naming dependent_stub_id).
                                    Required, non-empty.
        reason              (str) — mandatory, non-empty caller-supplied
                                    justification (no-authorship-gate
                                    substitute control — see "Authorization
                                    basis"). Echoed back on every exit_code 0
                                    response.

    Returns a dict with keys:
        exit_code           (int)  — 0 ok (applied or idempotent no-op) / 1
                                      refused (see `error`).
        applied              (bool) — True iff at least one of the two files
                                      was actually written (False on a full
                                      idempotent no-op — the edge already
                                      existed on both endpoints).
        applied_blocked_by   (bool) — True iff the dependent's blocked_by was
                                      written this call.
        applied_blocks       (bool) — True iff the dependency's blocks was
                                      written this call.
        reason               (str)  — echoes the caller-supplied reason
                                      (exit_code 0 only).
        message              (str)  — human-readable outcome (exit_code 0
                                      only).
        error                (str)  — human-readable refusal reason
                                      (exit_code 1 only).
    """
    roadmap_id: str = (params.get("roadmap_id") or "").strip()
    dependent_stub_id: str = (params.get("dependent_stub_id") or "").strip()
    dependency_stub_id: str = (params.get("dependency_stub_id") or "").strip()
    reason: str = (params.get("reason") or "").strip()

    if not roadmap_id:
        return _err("missing required param: roadmap_id (non-empty)")
    if not dependent_stub_id:
        return _err("missing required param: dependent_stub_id (non-empty)")
    if not dependency_stub_id:
        return _err("missing required param: dependency_stub_id (non-empty)")
    if not reason:
        return _err(
            "missing required param: reason (non-empty) — this op carries no "
            "authorship gate (DR-247 § 3, DR-264 General principle); the "
            "mandatory reason is the substitute audit-trail control"
        )
    if dependent_stub_id == dependency_stub_id:
        return _err(
            f"self-edge refused: dependent_stub_id and dependency_stub_id "
            f"are both {dependent_stub_id!r} — a stub cannot block/be-"
            f"blocked-by itself"
        )

    if repo_root is None:
        return _err(
            "roadmap.link_stubs: repo_root is required (no founding root "
            "available — handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    # asyncio.to_thread for DR-212 D3 async-loop mandate (Finding 1):
    # _run_link_stubs reads the whole corpus synchronously and, via
    # _write_edge_field -> locked_rmw, polls a cross-process flock with
    # time.sleep for up to LOCK_TIMEOUT_SECS -- none of that may run
    # directly in this async body's await-free execution.
    return await asyncio.to_thread(
        _run_link_stubs,
        roadmap_id=roadmap_id,
        dependent_stub_id=dependent_stub_id,
        dependency_stub_id=dependency_stub_id,
        reason=reason,
        repo_root=repo_root,
        worktree=worktree,
    )
