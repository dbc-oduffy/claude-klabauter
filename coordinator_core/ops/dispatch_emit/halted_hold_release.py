"""When a halted (or resumed) run stops holding its declared write paths against another run's commit.

`release_reason` answers "does this run still hold against the run now committing?"; `release_calls`
names the acts that would free a hold; `mark_plan_origins_landed` rewrites the halted runs a plan
landing just finished. Stdlib only at module top (land_run and terminal_commit cold-import pins);
the one git spawn is `_kept_at_head`'s batched `cat-file`, reached only when no cheaper release applied.

Invariants: a run with no halt marker (a live fresh-token peer) is never released here. A `resumed`
marker can be a run executing again, so only supersession and a landed successor release it, never
a shared sizing or deliverable alone. The recorded releases (criterion-met, phantom) are only read
from a `released` record on a `halted` marker; whoever shows the condition writes the record, never
this module.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
import time
from pathlib import Path
from typing import Optional

RELEASE_SUPERSEDED = "sizing-superseded"
RELEASE_MARKER_SUPERSEDED = "marker-superseded"
RELEASE_SUCCESSOR = "successor-run"
RELEASE_PLAN_LANDING = "plan-landing"
RELEASE_SUCCESSOR_LANDED = "successor-landed"
RELEASE_RECORDED_LANDED = "recorded-landed"
RELEASE_SIZING_SHIPPED = "sizing-shipped"
RELEASE_KEPT_AT_HEAD = "kept-at-head"
RELEASE_CRITERION_MET = "criterion-met-at-head"
RELEASE_OWNER_NOT_LIVE = "owner-not-live"
_LEGACY_RELEASE_PHANTOM = "phantom-no-own-hunk-owner-gone"
RELEASE_WITHHELD_CLEAN = "withheld-clean-at-head"
_RECORDED_RELEASES = frozenset({RELEASE_CRITERION_MET, RELEASE_OWNER_NOT_LIVE, _LEGACY_RELEASE_PHANTOM})

#: Marker field `{path: {reason, at}}`: the halted run no longer holds those paths. Written only by
#: `declared_hold_release._release_path`; read by `released_path_reasons` and its consumers.
RELEASED_PATHS_FIELD = "released_paths"


def released_path_reasons(root: Path, run_id: str) -> dict:
    """`{path: reason}` the halted run's marker releases per path; empty when none or the marker is unreadable."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run

        marker = halted_run._read(_record(Path(root), run_id)) or {}
        raw = marker.get(RELEASED_PATHS_FIELD)
        if not isinstance(raw, dict):
            return {}
        return {
            str(p).replace("\\", "/").removeprefix("./"): str(v.get("reason"))
            for p, v in raw.items() if isinstance(v, dict) and v.get("reason")
        }
    except Exception:  # noqa: BLE001 -- unreadable: nothing is released
        return {}


def without_released_paths(root: Path, holds) -> list:
    """`holds` (`(path, holder_run_id)` pairs) minus those whose holder released that path per path."""
    memo: dict = {}
    out = []
    for path, holder in holds:
        if holder not in memo:
            memo[holder] = released_path_reasons(root, holder)
        if str(path).replace("\\", "/").removeprefix("./") not in memo[holder]:
            out.append((path, holder))
    return out


def _halted_sizing(root: Path, run_id: str, marker: dict) -> str:
    from coordinator_core.ops import warp_run_deliverable as wrd

    sizing = wrd.record_sizing_rel(root, run_id)
    if sizing:
        return sizing
    value = marker.get("sizing_rel")
    return wrd._norm(value) if isinstance(value, str) else ""


def _marker_superseded(marker: Optional[dict]) -> bool:
    from coordinator_core.ops.dispatch_emit import halted_run

    return bool(marker) and marker.get("status") == halted_run.STATUS_SUPERSEDED


def _sizing_superseded(root: Path, run_id: str, marker: dict) -> bool:
    from coordinator_core.ops import warp_run_deliverable as wrd

    sizing = _halted_sizing(root, run_id, marker)
    return bool(sizing) and wrd.sizing_scalar(root, sizing, "status") == "superseded"


def supersession_reason(root: Path, run_id: str) -> Optional[str]:
    """`RELEASE_MARKER_SUPERSEDED` or `RELEASE_SUPERSEDED` when `run_id` is superseded, else None. The one
    supersession test: `_release_reason` and `kept_work.held_paths` both decide through its two halves. A
    sizing supersedes only a halted/resumed marker. Marker and sizing reads only, no spawn; never raises
    (an unreadable record is not superseded)."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        marker = halted_run._read(record_dir(Path(root), run_id))
        if _marker_superseded(marker):
            return RELEASE_MARKER_SUPERSEDED
        if marker and marker.get("status") in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
            if _sizing_superseded(Path(root), run_id, marker):
                return RELEASE_SUPERSEDED
    except Exception:  # noqa: BLE001 -- unknown: not superseded, the hold stands
        return None
    return None


def _successor_landed(root: Path, run_id: str, sizing: str) -> bool:
    """A later-stamped record dir binding `sizing` reads `landed`, or its spent token record carries a `landed` dict."""
    from coordinator_core.ops import warp_run_deliverable as wrd
    from coordinator_core.ops.dispatch_emit import halted_run, run_token, stopped_runs
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    own = stopped_runs._stamp_epoch(run_id)
    if own is None or not sizing:
        return False
    base = Path(root) / RECORD_DIR_ROOT
    try:
        with os.scandir(base) as entries:
            names = sorted(e.name for e in entries if e.is_dir())
    except OSError:
        return False
    for name in names:
        stamp = stopped_runs._stamp_epoch(name)
        if name == run_id or stamp is None or stamp <= own:
            continue
        if wrd._norm(str(wrd._read_json(base / name / "run.json").get("sizing_rel") or "")) != sizing:
            continue
        marker = halted_run._read(base / name)
        if marker is not None and marker.get("status") == halted_run.STATUS_LANDED:
            return True
        receipt = run_token._read(run_token._record_path(root, name, spent=True))
        if receipt is not None and isinstance(receipt.get("landed"), dict):
            return True
    return False


_SCOPE: contextvars.ContextVar = contextvars.ContextVar("halted_hold_release_scope", default=None)


@contextlib.contextmanager
def release_scope():
    """One terminal commit's release computation: `release_reason` is evaluated once per (halted run, committing
    run, committing plan) and `prefetch_kept_at_head` blobs are shared. Nested entry reuses the outer scope.
    The state dies with the scope: nothing is cached across commits. Within one commit HEAD moves only at the
    landing, so an answer read after it is stale toward holding longer, never toward freeing a run."""
    if _SCOPE.get() is not None:
        yield _SCOPE.get()
        return
    from coordinator_core.git import call_io_scope

    token = _SCOPE.set({"memo": {}, "blobs": None, "specs": set(), "prefetched": False, "seen_root": None, "seen": {}, "scope_seen": {}})
    try:
        if call_io_scope.scope_open():
            yield _SCOPE.get()
        else:
            with call_io_scope.call_io_scope():
                yield _SCOPE.get()
    finally:
        _SCOPE.reset(token)


@contextlib.contextmanager
def blob_scope(root: Path):
    """A `release_scope()` that also memoises `_blob_batch` reads for `root`, so a claim lookup over many
    halted runs reads each git object once. Trap: HEAD is frozen for the block."""
    with release_scope() as state:
        owns = state["seen_root"] is None
        if owns:
            state["seen_root"] = str(root)
        try:
            yield state
        finally:
            if owns:
                state["seen_root"] = None
                state["seen"] = {}


def _blob_batch(root: Path, specs: list) -> Optional[dict]:
    """`{spec: bytes | None}` for each git object spec; None when git fails. One `git cat-file --batch`
    spawn for the specs a live `blob_scope` has not already read, none when it holds them all."""
    state = _SCOPE.get()
    if state is None:
        return _blob_fetch(root, specs)
    if state["seen_root"] != str(root):
        if state["seen_root"] is not None:
            return _blob_fetch(root, specs)
        seen = state["scope_seen"].setdefault(str(root), {})
    else:
        seen = state["seen"]
    missing = [s for s in dict.fromkeys(specs) if s not in seen]
    if missing:
        fetched = _blob_fetch(root, missing)
        if fetched is None:
            return None
        seen.update(fetched)
    return {s: seen[s] for s in specs}


def prefetch_kept_blobs(root: Path, runs: list) -> None:
    """Read the blobs every `(record, marker)` in `runs` needs in one spawn, into the live `blob_scope`
    (a no-op outside one). Never raises: a run that cannot be planned is read on demand later."""
    state = _SCOPE.get()
    if state is None or state["seen_root"] != str(root):
        return
    union: set = set()
    specs: list = []
    for record, marker in runs:
        with contextlib.suppress(Exception):  # unreadable: that run is checked on its own
            union.update(_withheld_keys(record))
        try:
            plan = _kept_plan(record)
            if plan is not None:
                specs.extend(_kept_specs(plan[0], plan[1], _base_sha(marker)))
        except Exception:  # noqa: BLE001 -- planned again, and held, by kept_at_head_paths
            continue
    state["memo"][("withheld-union", str(root))] = frozenset(union)
    state["memo"].pop(("dirty-withheld", str(root)), None)
    if specs:
        _blob_batch(root, specs)


def _withheld_keys(record: Path) -> set:
    from coordinator_core.ops.dispatch_emit import halted_run
    from coordinator_core.session.claim_index import _normalize_key

    return {_normalize_key(p) for p in halted_run.halted_withheld_strict(record)}


def _dirty_withheld(root: Path, state: dict) -> Optional[frozenset]:
    """Dirty paths among every prefetched halted run's withheld paths, from ONE index walk shared by all
    runs of the scope (`own_left_behind` over the union; its dirtiness and sibling-claim filter are per
    path). None when no union was prefetched or the walk failed: the caller tests the run alone."""
    key = ("dirty-withheld", str(root))
    if key not in state["memo"]:
        union = state["memo"].get(("withheld-union", str(root)))
        dirty = None
        if union:
            from coordinator_core.ops.ceremony import commit_companion

            with contextlib.suppress(Exception):  # unreadable index: each run holds
                dirty = frozenset(commit_companion.own_left_behind(root, {"-": sorted(union)}, "-", ()))
        state["memo"][key] = dirty
    return state["memo"][key]


def _record(root: Path, run_id: str) -> Path:
    """`ask_contract.record_dir`, memoised per release scope: its symlink-safe resolve costs ~0.2ms and
    a hold computation asks for the same run's record dozens of times."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    state = _SCOPE.get()
    if state is None:
        return record_dir(Path(root), run_id)
    key = ("record", str(root), run_id)
    if key not in state["memo"]:
        state["memo"][key] = record_dir(Path(root), run_id)
    return state["memo"][key]


def release_reason(
    root: Path, halted_run_id: str, committing_run_id: Optional[str], committing_plan: Optional[str] = None,
) -> Optional[str]:
    """Why `halted_run_id` no longer holds against `committing_run_id`'s commit, else None (it holds).

    A `landed` marker always releases; `halted`/`resumed` release per the rules above, and a shipped
    sizing frees either. Any other marker state (none) is None. Never raises: an unreadable record holds.
    Inside `release_scope()` the answer, None from the except arm included, is memoised per key.
    """
    state = _SCOPE.get()
    if state is None:
        return _release_reason(root, halted_run_id, committing_run_id, committing_plan)
    key = ("release", str(root), halted_run_id, committing_run_id, committing_plan)
    if key not in state["memo"]:
        state["memo"][key] = _release_reason(root, halted_run_id, committing_run_id, committing_plan)
    return state["memo"][key]


def _release_reason(
    root: Path, halted_run_id: str, committing_run_id: Optional[str], committing_plan: Optional[str],
) -> Optional[str]:
    try:
        from coordinator_core.ops import warp_run_deliverable as wrd
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD

        root = Path(root)
        marker = halted_run._read(_record(root, halted_run_id))
        status = marker.get("status") if marker else None
        if status == halted_run.STATUS_LANDED:
            return RELEASE_RECORDED_LANDED
        if _marker_superseded(marker):
            return RELEASE_MARKER_SUPERSEDED
        if status not in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
            return None
        released = marker.get(halted_run.HALT_RELEASED_FIELD)
        if status == halted_run.STATUS_HALTED and isinstance(released, dict):
            reason = released.get("reason")
            if reason in _RECORDED_RELEASES:
                return reason
        sizing = _halted_sizing(root, halted_run_id, marker)
        sizing_status = wrd.sizing_scalar(root, sizing, "status") if sizing else ""
        if sizing_status == "superseded":
            return RELEASE_SUPERSEDED
        if sizing_status == "shipped":
            return RELEASE_SIZING_SHIPPED
        if status == halted_run.STATUS_RESUMED:
            return RELEASE_SUCCESSOR_LANDED if _successor_landed(root, halted_run_id, sizing) else None
        if committing_run_id and sizing:
            other = wrd.record_sizing_rel(root, committing_run_id)
            if other and other == sizing:
                return RELEASE_SUCCESSOR
            deliverable = wrd.sizing_scalar(root, sizing, "deliverable_id")
            if other and deliverable and wrd.sizing_scalar(root, other, "deliverable_id") == deliverable:
                return RELEASE_SUCCESSOR
        plan = marker.get(HALT_PLAN_FIELD)
        if committing_plan and isinstance(plan, str) and wrd._norm(plan) == wrd._norm(committing_plan):
            return RELEASE_PLAN_LANDING
        if _kept_at_head(root, _record(root, halted_run_id), marker):
            return RELEASE_KEPT_AT_HEAD
        if _withheld_clean(root, _record(root, halted_run_id)):
            return RELEASE_WITHHELD_CLEAN
    except Exception:
        return None
    return None


def _withheld_clean(root: Path, record: Path) -> bool:
    """True when the halt withholds something and none of its withheld paths is dirty in the worktree.
    Dirtiness is `commit_companion.own_left_behind`'s index-identity test (zero spawn), the one
    `terminal_commit._dirty_held_by_halted_run` applies. Fail-closed: an unreadable record or index holds."""
    from coordinator_core.ops.ceremony import commit_companion
    from coordinator_core.ops.dispatch_emit import halted_run

    try:
        withheld = list(halted_run.halted_withheld_strict(record))
        if not withheld:
            return False
        state = _SCOPE.get()
        if state is not None and state["seen_root"] == str(root):
            dirty = _dirty_withheld(root, state)
            keys = _withheld_keys(record)
            if dirty is not None and keys <= state["memo"][("withheld-union", str(root))]:
                return not (keys & dirty)
        return not commit_companion.own_left_behind(root, {"-": withheld}, "-", ())
    except Exception:  # noqa: BLE001 -- unreadable: the hold stands
        return False


def held_paths(
    root: Path, halted_run_id: str, committing_run_id: Optional[str], committing_plan: Optional[str] = None,
) -> frozenset:
    """The normalized paths `halted_run_id` still holds against `committing_run_id`'s commit.

    Empty when `release_reason` frees the run; otherwise the paths its halt record withholds. A held
    path lands whole or not at all (DR-462): this names no hunk subset. Fail-closed: an error computing the
    release or the normalized set falls back to the raw halt-record withheld set, never to empty; only an
    unreadable halt record itself propagates. Spawns what `release_reason` spawns (its `_kept_at_head`
    batched `cat-file`, when no cheaper release applied). Inside `release_scope()` the answer is memoised
    per key; an unreadable halt record is not memoised.
    """
    state = _SCOPE.get()
    if state is None:
        with release_scope():
            return _held_paths_scoped(root, halted_run_id, committing_run_id, committing_plan)
    key = ("held", str(root), halted_run_id, committing_run_id, committing_plan)
    if key not in state["memo"]:
        state["memo"][key] = _held_paths_scoped(root, halted_run_id, committing_run_id, committing_plan)
    return state["memo"][key]


def _held_paths_scoped(
    root: Path, halted_run_id: str, committing_run_id: Optional[str], committing_plan: Optional[str],
) -> frozenset:
    try:
        if release_reason(root, halted_run_id, committing_run_id, committing_plan) is not None:
            return frozenset()
    except Exception:  # noqa: BLE001 -- release unknown: the run still holds
        pass
    try:
        from coordinator_core.ops import warp_run_deliverable as wrd
        from coordinator_core.ops.dispatch_emit import halted_run

        record = _record(Path(root), halted_run_id)
        held = frozenset(wrd.halted_withheld_paths(Path(root), halted_run_id)) - set(
            released_path_reasons(Path(root), halted_run_id)
        )
        marker = halted_run._read(record)
        if held and marker:
            with contextlib.suppress(Exception):  # unknown: the hold stands
                held -= {wrd._norm(p) for p in kept_at_head_paths(Path(root), record, marker)}
        return held
    except Exception as exc:  # noqa: BLE001 -- normalizer unavailable: the raw record still holds
        from coordinator_core.ops.dispatch_emit import halted_run

        if isinstance(exc, halted_run.UnreadableHaltRecord):
            raise
        return frozenset(
            p.replace("\\", "/").removeprefix("./")
            for p in halted_run.halted_withheld_strict(_record(Path(root), halted_run_id))
        )


def _own_paths(root: Path, run_id: str) -> Optional[tuple]:
    """`(declared, prefixes)` of the run's manifest, or None when the manifest is unreadable or declares nothing."""
    from coordinator_core.ops import warp_run_deliverable as wrd

    declared, prefixes, unreadable = wrd._manifest_declared(run_id, Path(root))
    if unreadable or (not declared and not prefixes):
        return None
    return tuple(declared), tuple(prefixes)


def _is_halted_holder(root: Path, owner: str, run_id: str) -> Optional[bool]:
    """True when `owner` is a halted/resumed run that still holds against `run_id`; False when its hold is
    released; None when its marker is missing, unreadable or in any other state (treated as live)."""
    from coordinator_core.ops.dispatch_emit import halted_run

    marker = halted_run._read(_record(Path(root), owner))
    if not marker or marker.get("status") not in (halted_run.STATUS_HALTED, halted_run.STATUS_RESUMED):
        return False if release_reason(root, owner, run_id, None) is not None else None
    return release_reason(root, owner, run_id, None) is None


def _review_green(root: Path, run_id: str) -> bool:
    """The run's outcome record carries an inline review with no incomplete chunk and its review sidecar resolves."""
    from coordinator_core.ops.dispatch_emit import ask_contract
    from coordinator_core.ops.dispatch_emit.halted_run_landed import _review_record

    outcome = ask_contract.read_run_outcome(Path(root), run_id)
    if not outcome or outcome.get("incomplete_chunks"):
        return False
    return _review_record(Path(root), run_id, None) is not None


def _covers(path: str, declared: tuple, prefixes: tuple) -> bool:
    return path in declared or any(path == pre or path.startswith(pre + "/") for pre in prefixes)


def halted_only_withholders(root: Path, run_id: str) -> Optional[list]:
    """Sorted ids of the other runs withholding `run_id`'s declared paths, when every one is a halted/resumed
    run still holding and `run_id` is reviewed-green; else None.

    None for: a review that is not green, an unreadable or empty manifest, a holder with no halted marker
    (live or unreadable), or no halted withholder at all. In process, no spawn. Never raises.
    """
    try:
        own = _own_paths(root, run_id)
        if own is None or not _review_green(root, run_id):
            return None
        from coordinator_core.ops.dispatch_emit.halted_withheld_index import withheld_by_path

        owners = {o for p, o in withheld_by_path(Path(root)).items() if o != run_id and _covers(p, *own)}
        holders = []
        for owner in sorted(owners):
            holds = _is_halted_holder(root, owner, run_id)
            if holds is None:
                return None
            if holds:
                holders.append(owner)
        return holders or None
    except Exception:  # noqa: BLE001 -- unknown claim: the refusal stands
        return None


def residue_paths(root: Path, run_id: str) -> frozenset:
    """Paths other halted runs withhold that are not `run_id`'s own declared paths: residue a tree-wide
    invariant should not charge to `run_id`. Empty on any error (nothing is excused unread)."""
    try:
        from coordinator_core.ops.dispatch_emit.halted_withheld_index import withheld_by_path

        own = _own_paths(root, run_id) or ((), ())
        out = set()
        for path, owner in withheld_by_path(Path(root)).items():
            if owner != run_id and not _covers(path, *own) and _is_halted_holder(root, owner, run_id):
                out.add(path)
        return frozenset(out)
    except Exception:  # noqa: BLE001
        return frozenset()


def _squash(text: str) -> str:
    return "".join(text.split())


def _blob_fetch(root: Path, specs: list) -> Optional[dict]:
    from coordinator_core.git.run import run_git

    specs = list(dict.fromkeys(specs))
    proc = run_git(["cat-file", "--batch"], cwd=str(root), input="".join(s + "\n" for s in specs).encode())
    if proc.returncode != 0:
        return None
    out, data, pos = proc.stdout_bytes, {}, 0
    for spec in specs:
        end = out.find(b"\n", pos)
        if end < 0:
            return None
        header = out[pos:end].split()
        pos = end + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            data[spec] = out[pos:pos + size]
            pos += size + 1
        else:
            data[spec] = None
    return data


def _hunks_present(base: str, kept: str, head: str) -> bool:
    """Every block the kept text adds over `base` sits in `head` (whitespace-insensitive, so a reflow still
    matches), and every block it removes is gone from `head`."""
    state = _SCOPE.get()
    if state is None:
        return _hunks_present_uncached(base, kept, head)
    key = ("hunks", base, kept, head)
    if key not in state["memo"]:
        state["memo"][key] = _hunks_present_uncached(base, kept, head)
    return state["memo"][key]


def _hunks_present_uncached(base: str, kept: str, head: str) -> bool:
    """The bounded diff is `run_hunk_claims.changed_opcodes` (prefix/suffix trim). Trap: past its bound it
    returns one coarse span without signalling, so `exceeds_match_bound` is checked first and holds."""
    from coordinator_core.ops.dispatch_emit.run_hunk_claims import changed_opcodes, exceeds_match_bound

    base_lines, kept_lines = base.splitlines(), kept.splitlines()
    squashed_head = _squash(head)
    base_b = [line.encode("utf-8", "replace") for line in base_lines]
    kept_b = [line.encode("utf-8", "replace") for line in kept_lines]
    if exceeds_match_bound(base_b, kept_b):
        return False
    for tag, i1, i2, j1, j2 in changed_opcodes(base_b, kept_b):
        if tag in ("insert", "replace"):
            added = _squash("\n".join(kept_lines[j1:j2]))
            if added and added not in squashed_head:
                return False
        if tag in ("delete", "replace"):
            removed = _squash("\n".join(base_lines[i1:i2]))
            if removed and removed in squashed_head and removed not in _squash("\n".join(kept_lines[j1:j2])):
                return False
    return True


def _base_sha(marker: dict) -> str:
    value = marker.get("run_base_sha")
    return value if isinstance(value, str) and value else ""


def _kept_specs(paths: list, kept: dict, base_sha: str) -> list:
    specs = [f"HEAD:{p}" for p in paths] + [kept[p] for p in paths]
    return specs + ([f"{base_sha}:{p}" for p in paths] if base_sha else [])


def _kept_plan(record: Path):
    """`(paths, kept)` for the halt at `record`, or None when nothing is withheld, the record is unreadable, or no
    withheld path has a kept blob."""
    from coordinator_core.ops.dispatch_emit import halted_run, kept_work

    try:
        withheld = halted_run.halted_withheld_strict(record)
    except halted_run.UnreadableHaltRecord:
        return None
    if not withheld:
        return None
    kept: dict = {}
    for chunk in kept_work.read(record)["chunks"].values():
        paths = chunk.get("paths") if isinstance(chunk, dict) else None
        if isinstance(paths, dict):
            kept.update({p: b for p, b in paths.items() if isinstance(b, str) and b})
    # Trap: kept_fingerprints is keyed by normalised path and unions chunk and post_checkpoint blobs;
    # only a path with no chunk entry here and exactly one union blob has a single kept content to
    # compare against HEAD, otherwise it stays held.
    for p, blobs in kept_work.kept_fingerprints(record).items():
        if p not in kept and len(blobs) == 1:
            kept[p] = next(iter(blobs))
    paths = sorted(p for p in withheld if p in kept)
    return (paths, kept) if paths else None


def prefetch_kept_at_head(root: Path, run_ids) -> None:
    """Read every halted run's kept/HEAD/base blobs in one `git cat-file --batch` and stash them in the active
    `release_scope()`, so `kept_at_head_paths` spawns nothing for those runs. No scope: no-op. A failed batch
    holds every run's kept-at-head check for the rest of the scope, prefetched or not (`_scoped_blobs`). Never
    raises."""
    state = _SCOPE.get()
    if state is None:
        return
    try:
        from coordinator_core.ops.dispatch_emit import halted_run

        specs: list = []
        for run_id in run_ids:
            record = _record(Path(root), run_id)
            marker = halted_run._read(record)
            if not marker or marker.get("status") != halted_run.STATUS_HALTED:
                continue
            plan = _kept_plan(record)
            if plan is not None:
                specs += _kept_specs(plan[0], plan[1], _base_sha(marker))
        if not specs:
            return
        specs = list(dict.fromkeys(specs))
        blobs = _blob_batch(Path(root), specs)
        state["prefetched"] = True
        state["blobs"] = blobs
        state["specs"] = set(specs) if blobs is not None else set()
    except Exception:  # noqa: BLE001 -- no prefetch: each run falls back to its own batch
        return


_PREFETCH_FAILED = object()


def _scoped_blobs(specs: list):
    """The prefetched blobs when every spec was prefetched; `_PREFETCH_FAILED` when the prefetch batch failed
    (the run stays held); None when the caller must batch for itself."""
    state = _SCOPE.get()
    if state is None or not state.get("prefetched"):
        return None
    if state["blobs"] is None:
        return _PREFETCH_FAILED
    if all(s in state["specs"] for s in specs):
        return state["blobs"]
    return None


def kept_at_head_paths(root: Path, record: Path, marker: dict) -> set:
    """The withheld paths of the halt at `record` whose kept content is already at HEAD: blob-equal, or the
    kept diff against `run_base_sha` already present. A path with no kept blob, a missing object, or an
    unreadable record is absent (still held). One git spawn for the whole run, none when nothing is withheld
    or the run's blobs were prefetched in the active `release_scope()`. Inside a scope the answer is memoised per
    record: `release_reason` and `warp_run_deliverable.halted_run_write_claims` both ask, and the hunk differ
    behind it costs process time."""
    state = _SCOPE.get()
    key = ("kept-paths", str(root), str(record))
    if state is None:
        return _kept_at_head_paths(root, record, marker)
    if key not in state["memo"]:
        state["memo"][key] = frozenset(_kept_at_head_paths(root, record, marker))
    return set(state["memo"][key])


def _kept_at_head_paths(root: Path, record: Path, marker: dict) -> set:
    from coordinator_core.ops.dispatch_emit import kept_work

    plan = _kept_plan(record)
    if plan is None:
        return set()
    paths, kept = plan
    base_sha = _base_sha(marker)
    specs = _kept_specs(paths, kept, base_sha)
    blobs = _scoped_blobs(specs)
    if blobs is _PREFETCH_FAILED:
        return set()
    if blobs is None:
        blobs = _blob_batch(root, specs)
    if blobs is None:
        return set()
    released: set = set()
    for p in paths:
        head, keep = blobs[f"HEAD:{p}"], blobs[kept[p]]
        if head is None or keep is None:
            continue
        if head == keep or kept_work.checkin_form(root, p, keep) == head:
            released.add(p)
        elif base_sha and _hunks_present(
            (blobs[f"{base_sha}:{p}"] or b"").decode("utf-8", "replace"),
            keep.decode("utf-8", "replace"), head.decode("utf-8", "replace"),
        ):
            released.add(p)
    return released


def _kept_at_head(root: Path, record: Path, marker: dict) -> bool:
    """True when the halt withholds something and every withheld path is `kept_at_head_paths`."""
    from coordinator_core.ops.dispatch_emit import halted_run

    state = _SCOPE.get()
    key = ("kept", str(root), str(record))
    if state is not None and key in state["memo"]:
        return state["memo"][key]
    try:
        withheld = set(halted_run.halted_withheld_strict(record))
    except halted_run.UnreadableHaltRecord:
        return False
    result = bool(withheld) and kept_at_head_paths(root, record, marker) == withheld
    if state is not None:
        state["memo"][key] = result
    return result


def release_calls(root: Path, halted_run_id: str) -> tuple:
    """Register lines naming the acts that free `halted_run_id`'s hold; the sizing line only when a sizing resolves."""
    lines = []
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        root = Path(root)
        sizing = _halted_sizing(root, halted_run_id, halted_run._read(record_dir(root, halted_run_id)) or {})
    except Exception:
        sizing = ""
    if sizing:
        lines.append(f"sizing.supersede {{sizing_path: {sizing}, note: <why the run is no longer owed>}}")
    lines.append(f"dispatch.record_halted_run_landed {{run_id: {halted_run_id}, sha: <landing sha>}}")
    return tuple(lines)


def _covered(declared: tuple, prefixes: tuple, landed: set) -> bool:
    """Every declared path is landed and every declared prefix has a landed path under it."""
    if not declared and not prefixes:
        return False
    if any(p not in landed for p in declared):
        return False
    return all(any(path == pre or path.startswith(pre + "/") for path in landed) for pre in prefixes)


def mark_plan_origins_landed(root: Path, plan_rel: str, sha: str, landed_paths, *, exclude_run: Optional[str]) -> list:
    """Rewrite as `landed` at `sha` each in-window halted run, other than `exclude_run`, whose marker names `plan_rel`
    and whose declared writes `landed_paths` fully cover; returns `[{run_id, recorded}]`.

    A partial landing leaves the marker `halted` (a landed marker is skipped by `halted_run_for`, so its owed rows
    could no longer resume). Never raises; the rewritten halted.json is left uncommitted.
    """
    done: list = []
    try:
        from coordinator_core.ops import warp_run_deliverable as wrd
        from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
        from coordinator_core.ops.dispatch_emit import halted_run, run_close, run_token, stopped_runs
        from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, RECORD_DIR_ROOT

        base = Path(root)
        plan = wrd._norm(plan_rel)
        landed = {p for p in (wrd._norm(x) for x in landed_paths) if p}
        moment = time.time()
        with os.scandir(base / RECORD_DIR_ROOT) as entries:
            names = sorted(e.name for e in entries if e.is_dir())
        for run_id in names:
            stamp = stopped_runs._stamp_epoch(run_id)
            if run_id == exclude_run or stamp is None or moment - stamp > run_token.TTL_SECONDS:
                continue
            record = base / RECORD_DIR_ROOT / run_id
            marker = halted_run._read(record)
            if marker is None or marker.get("status") != halted_run.STATUS_HALTED:
                continue
            named = marker.get(HALT_PLAN_FIELD)
            if not isinstance(named, str) or wrd._norm(named) != plan:
                continue
            declared, prefixes, unreadable = wrd._manifest_declared(run_id, base)
            if unreadable or not _covered(declared, prefixes, landed):
                continue
            if not halted_run.mark_landed(record, sha):
                continue
            if (base / contract.status_rel(run_id)).is_file():
                run_close.sibling_release(base, run_id, sha)
            done.append({"run_id": run_id, "recorded": f"{RECORD_DIR_ROOT}/{run_id}/{halted_run.HALTED_FILE}"})
    except Exception:
        return done
    return done
