"""Halt marker for a sizing run: write it, find the halted run a refire resumes, mark it taken.

The marker is `halted.json` in the run's record dir. It is rewritten to `resumed`, never
unlinked: terminal_commit drops record-dir deletions, so an unlinked marker would stay an
uncommitted tracked deletion. The marker may carry `run_base_sha` (the halted run's review base,
read by `halted_run_base`); `halted_ask_run_for` finds a halted `ask-*` run naming a sizing.
`revised_at` is stamped by `mark_revised` when workflow.revise lands on a halted run and read by `revised_halt`. Stdlib plus the atomic writer; no git. A halt over a split dependency tree stamps `live_breakage` and spawns one detached unimatrix-send alert to the live Group EM (`halt_split_tree`); no other subprocess.

`halted_run_for` skips minted-only shell dirs and re-halts a `resumed` marker whose resumer died unstaged.
Trap: a live resumer still at `minted` looks the same as a dead one, so a second refire in that window re-halts it
(pinned by `test_replay_halted_run_shadow.py::test_resumed_with_a_dead_unstaged_resumer_is_halted_again`).

Accepted residual: two concurrent refires of one halted sizing can both read `halted` before
either rewrites it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.git import call_io_scope
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit.ask_contract import (
    HALT_BLOCKING_FIELD, HALT_BLOCKING_ITEMS_FIELD, HALT_HELD_ROWS_FIELD, HALT_LIVE_BREAKAGE_FIELD, HALT_PLAN_FIELD,
    HALT_RAG_CORPUS_DOWN, RECORD_DIR_ROOT, RUN_DIR_ROOT, record_terminal_judge_at, refire_next_action_for, stamp_run_stage_at,
)
from coordinator_core.ops.dispatch_emit.rag_use import RESTORE_MESSAGE
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import STATUS_HALTED
from coordinator_core.ops.dispatch_emit.halt_split_tree import alert_live_gem, find_split_trees
from coordinator_core.ops.dispatch_emit.halt_refire import is_exit_next_action, is_refire_next_action
from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

HALTED_FILE = "halted.json"
STATUS_RESUMED = "resumed"
STATUS_LANDED = "landed"
STATUS_SUPERSEDED = "superseded"
SUPERSEDED_EVIDENCE_FIELD = "superseded_evidence"
PATH_DISPOSITIONS_FIELD = "path_dispositions"
DISPOSITION_LANDED = "landed"
DISPOSITION_SUPERSEDED = "superseded"
DISPOSITION_SCRATCH = "scratch-abandoned"
DISPOSITION_LOST = "lost"
STATUS_CLOSED = "closed"
HALT_RUN_BASE_FIELD = "run_base_sha"
HALT_REASON_FIELD = "reason"
HALT_RUN_ARTIFACTS_FIELD = "run_artifacts"
REVISED_AT_FIELD = "revised_at"
HALT_RELEASED_FIELD = "released"
HALT_NEXT_ACTION_FIELD = "next_action"
_SHA40 = re.compile(r"[0-9a-f]{40}")
_DATE_PREFIX = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: The `<%Y%m%dT%H%M%S>-<hex6>` tail of a run id; without it a sibling sizing whose stem extends
#: this one's (`foo` vs `foo-slice-1`) shares the `warp-<stem>-` prefix and shadows the newest dir.
_STAMP = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}")


def _norm(sizing_rel: str) -> str:
    return PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()


def _marker_text(record_dir: Path) -> str:
    """`halted.json` text, read through the open `call_io_scope` (one read per path per scope);
    FileNotFoundError when absent. `_write` drops the cached read."""
    raw = call_io_scope.read_bytes(Path(record_dir) / HALTED_FILE)
    if raw is None:
        raise FileNotFoundError(HALTED_FILE)
    return raw.decode("utf-8")


def _read(record_dir: Path) -> Optional[dict]:
    try:
        data = json.loads(_marker_text(record_dir))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_strict(record_dir: Path) -> Optional[dict]:
    """The marker, None when `halted.json` is absent; ValueError when it exists but is unreadable,
    not JSON, or not an object. For hold computations, where a corrupt marker must hold."""
    try:
        data = json.loads(_marker_text(record_dir))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable halt marker in {record_dir}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"halt marker in {record_dir} is not an object")
    return data


UNREADABLE_HALT_RECORD = "unreadable-halt-record"


class UnreadableHaltRecord(ValueError):
    """A halt marker that exists but cannot be read; `reason` names the run dir it came from."""

    def __init__(self, run_id: str, detail: str = ""):
        self.run_id = run_id
        self.reason = f"{UNREADABLE_HALT_RECORD}:{run_id}"
        super().__init__(f"{self.reason} {detail}".strip())


def read_strict_run(record_dir: Path) -> Optional[dict]:
    """`read_strict` raising `UnreadableHaltRecord` (run_id = the record dir's name) on a corrupt marker."""
    try:
        return read_strict(Path(record_dir))
    except ValueError as exc:
        raise UnreadableHaltRecord(Path(record_dir).name, str(exc)) from exc


def _write(record_dir: Path, payload: dict) -> Path:
    target = record_dir / HALTED_FILE
    atomic_write_bytes(target, (json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    call_io_scope.invalidate(path=target)
    from coordinator_core.ops.dispatch_emit import halted_withheld_index

    halted_withheld_index.forget_fresh()
    _track_lookup(Path(record_dir), payload)
    return target


#: Machine-local lookup index: `<LOOKUP_INDEX_DIR>/<sha1(sizing_rel)[:16]>/<run_id>`, one empty file per run whose
#: marker is `halted` or `resumed` for that sizing. `_write` is the one writer of `halted.json`, so every transition
#: (halt, `resumed` rewrite, re-halt, landed, closed, superseded) lands here; a per-run file makes each one a single
#: atomic create or unlink, so peers never lose each other's update. The index answers "does any run hold this
#: sizing?" and which `ask-*` runs to read; supersession by a newer run is still decided from the run dir names.
LOOKUP_INDEX_DIR = ".coordinator-local/halted-run-lookup"
LOOKUP_SENTINEL = ".built-v2"


def _lookup_key(sizing_rel: str) -> str:
    """Keyed on the sizing's stem, so a sizing that moved between `state/sizings/` and its month subdir keeps its runs."""
    return hashlib.sha1(PurePosixPath(_norm(sizing_rel)).stem.encode("utf-8")).hexdigest()[:16]


def _root_of(record_dir: Path) -> Optional[Path]:
    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return None
    return root if root / RECORD_DIR_ROOT / rd.name == rd else None


def _track_lookup(record_dir: Path, payload: dict) -> None:
    """Mirror the marker just written into the lookup index; best-effort, the marker is the truth."""
    sizing = payload.get("sizing_rel")
    root = _root_of(record_dir)
    if not isinstance(sizing, str) or not sizing or root is None or not safe_id(record_dir.name):
        return
    entry = root / LOOKUP_INDEX_DIR / _lookup_key(sizing) / record_dir.name
    try:
        if payload.get("status") in (STATUS_HALTED, STATUS_RESUMED):
            entry.parent.mkdir(parents=True, exist_ok=True)
            entry.touch()
        else:
            entry.unlink(missing_ok=True)
    except OSError:
        pass


def rebuild_lookup_index(repo_root: Path) -> None:
    """Index every `halted` or `resumed` marker under the record root, then write the sentinel. Add-only, so a
    transition recorded mid-scan is never undone by a stale read."""
    root = Path(repo_root)
    warp = root / RECORD_DIR_ROOT
    try:
        names = os.listdir(warp)
    except OSError:
        names = []
    for name in names:
        if not (name.startswith("warp-") or name.startswith("ask-")) or not safe_id(name):
            continue
        marker = _read(warp / name)
        if marker and marker.get("status") in (STATUS_HALTED, STATUS_RESUMED):
            _track_lookup(warp / name, marker)
    sentinel = root / LOOKUP_INDEX_DIR / LOOKUP_SENTINEL
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()


#: A record root with at most this many run dirs is cheap enough to index on the hook path.
INLINE_BUILD_MAX_RUNS = 64


def _small_record_root(repo_root: Path) -> bool:
    """True when the record root holds at most `INLINE_BUILD_MAX_RUNS` run dirs: one listing, no reads."""
    try:
        with os.scandir(Path(repo_root) / RECORD_DIR_ROOT) as it:
            runs = 0
            for entry in it:
                if entry.name.startswith(("warp-", "ask-")):
                    runs += 1
                    if runs > INLINE_BUILD_MAX_RUNS:
                        return False
    except OSError:
        return True
    return True


def _indexed_runs(repo_root: Path, sizing_rel: str, *, build: bool = True):
    """Run ids the lookup index holds for the sizing; the first call on a checkout builds the index.
    With `build=False` an unbuilt index over more than `INLINE_BUILD_MAX_RUNS` runs answers `STALLED` (the caller cannot prove a negative) instead of scanning."""
    base = Path(repo_root) / LOOKUP_INDEX_DIR
    if not (base / LOOKUP_SENTINEL).exists():
        if not build and not _small_record_root(repo_root):
            return STALLED
        rebuild_lookup_index(repo_root)
    try:
        return set(os.listdir(base / _lookup_key(sizing_rel)))
    except OSError:
        return set()


def _warp_stamps(root: Path, prefix: str) -> list:
    """Stamps of the `<prefix><stamp>` dirs under `root`, from one directory listing; [] on an unreadable root."""
    stamps = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(prefix) and _STAMP.fullmatch(entry.name[len(prefix):]) and entry.is_dir():
                    stamps.append(entry.name[len(prefix):])
    except OSError:
        return []
    return stamps


def write_halt_marker(
    record_dir: Path,
    *,
    halted: str,
    sizing_rel: Optional[str],
    plan_rel: Optional[str] = None,
    blocking: Optional[tuple] = None,
    run_base_sha: Optional[str] = None,
    blocking_items: Optional[list] = None,
    reason: Optional[str] = None,
    next_action: Optional[dict] = None,
    held_rows: Optional[list] = None,
) -> Path:
    """Write the `halted` marker into `record_dir` (created if absent); return its path.

    `plan`, the blocking key, the blocking items, the reason, the next_action and the base are written only when given (the base
    only when 40-hex; next_action only when `halt_refire.is_refire_next_action` or `is_exit_next_action` accepts it), so other halts' markers are unchanged.
    """
    payload = {
        "status": STATUS_HALTED,
        "halted": halted,
        "sizing_rel": _norm(sizing_rel) if sizing_rel else None,
    }
    if plan_rel:
        payload[HALT_PLAN_FIELD] = _norm(plan_rel)
    if blocking is not None:
        payload[HALT_BLOCKING_FIELD] = list(blocking)
    if blocking_items is not None:
        payload[HALT_BLOCKING_ITEMS_FIELD] = list(blocking_items)
    if held_rows is not None:
        payload[HALT_HELD_ROWS_FIELD] = list(held_rows)
    if reason:
        payload[HALT_REASON_FIELD] = reason
    accepted = next_action is not None and (is_refire_next_action(next_action) or is_exit_next_action(next_action))
    if not accepted and halted == HALT_RAG_CORPUS_DOWN and sizing_rel:
        next_action, accepted = refire_next_action_for(
            halted, run_id=Path(record_dir).name, sizing_rel=_norm(sizing_rel), detail=RESTORE_MESSAGE
        ), True
    if accepted:
        payload[HALT_NEXT_ACTION_FIELD] = next_action
    if run_base_sha and _SHA40.fullmatch(run_base_sha):
        payload[HALT_RUN_BASE_FIELD] = run_base_sha
    root = _target_root(Path(record_dir))
    splits = find_split_trees(root) if root is not None else []
    if splits:
        payload[HALT_LIVE_BREAKAGE_FIELD] = splits
    prior = _prior_live_breakage(Path(record_dir))
    target = _write(Path(record_dir), payload)
    if splits and splits != prior:
        alert_live_gem(root, Path(record_dir).name, splits)
    stamp_run_stage_at(Path(record_dir), "halted")
    if sizing_rel:
        _schedule_resume_build(Path(record_dir))
    return target


def _prior_live_breakage(record_dir: Path):
    """The `live_breakage` already on this run's marker, or None; a re-halt over the same split does not re-alert."""
    try:
        data = json.loads((record_dir / HALTED_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get(HALT_LIVE_BREAKAGE_FIELD) if isinstance(data, dict) else None


#: Test seam: a callable `(target_root, run_id)` replacing the detached spawn.
_RESUME_SPAWNER = None


def _target_root(record_dir: Path) -> Optional[Path]:
    """The halted run's target root (`<root>/<RECORD_DIR_ROOT>/<run>`); None when the path is too shallow."""
    try:
        return Path(record_dir).parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return None


def _schedule_resume_build(record_dir: Path) -> None:
    """Start `halt_resume_build` detached for the halted run at `record_dir`
    (`<root>/<RECORD_DIR_ROOT or scratch/warp>/<run>`); never raises and never waits.

    Skipped under pytest unless `_RESUME_SPAWNER` is set, so a fixture halt never starts a real process."""
    rd = Path(record_dir)
    root = _target_root(rd)
    if root is None:
        return
    try:
        if _RESUME_SPAWNER is not None:
            _RESUME_SPAWNER(root, rd.name)
            return
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return
        from coordinator_core.ops.ceremony.detached_spawn import spawn_detached

        engine_root = Path(__file__).resolve().parents[3]
        spawn_detached(
            str(engine_root), str(Path(__file__).with_name("halt_resume_build.py")), [str(root), rd.name]
        )
    except Exception:  # noqa: BLE001 -- scheduling is best-effort; the marker is the truth
        pass


def halted_run_base(record_dir: Path) -> Optional[str]:
    """The base sha the halted run recorded in its marker, else None (absent, unreadable, not 40-hex)."""
    marker = _read(Path(record_dir))
    value = marker.get(HALT_RUN_BASE_FIELD) if marker else None
    return value if isinstance(value, str) and _SHA40.fullmatch(value) else None


def halted_ask_run_for(repo_root: Path, sizing_rel: str, *, ignore_superseded: bool = False) -> Optional[str]:
    """Return the newest halted `ask-<stamp>` run naming `sizing_rel`, else None.

    Only ask dirs stamped on or after the sizing filename's YYYY-MM-DD prefix are read (None for an
    undated name), and a `warp-<stem>-*` dir stamped later supersedes the ask run unless
    `ignore_superseded` (the notes-carrying lookup, which resumes nothing).
    """
    norm = _norm(sizing_rel)
    stem = PurePosixPath(norm).stem
    dated = _DATE_PREFIX.match(stem)
    if dated is None:
        return None
    floor = "".join(dated.groups())
    root = Path(repo_root) / RECORD_DIR_ROOT
    asks = [
        name[4:] for name in _indexed_runs(repo_root, norm)
        if name.startswith("ask-") and _STAMP.fullmatch(name[4:]) and name[4:12] >= floor
    ]
    newest_warp = max(_warp_stamps(root, f"warp-{stem}-"), default="") if asks and not ignore_superseded else ""
    for stamp in sorted(asks, reverse=True):
        if stamp < newest_warp and not ignore_superseded:
            return None
        name = f"ask-{stamp}"
        marker = _read(root / name)
        if marker and marker.get("status") == STATUS_HALTED and marker.get("sizing_rel") == norm:
            return name if safe_id(name) else None
    return None


def notes_predecessor(repo_root: Path, sizing_rel: str, *, exclude: Optional[str] = None) -> Optional[str]:
    """Newest prior run of `sizing_rel`, in any terminal state, that holds a live revise note, else None.

    Candidates are `warp-<stem>-<stamp>` dirs and dated `ask-<stamp>` dirs whose `halted.json` or
    `run.json` names the sizing; `exclude` (the run being composed) is skipped. Resumes nothing:
    the caller only reads its notes. One scandir of `scratch/warp`, no git.
    """
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit import run_notes

    norm = _norm(sizing_rel)
    stem = PurePosixPath(norm).stem
    dated = _DATE_PREFIX.match(stem)
    floor = "".join(dated.groups()) if dated else None
    prefix = f"warp-{stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    found = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                name = entry.name
                if name == exclude or not entry.is_dir():
                    continue
                if name.startswith(prefix):
                    stamp = name[len(prefix):]
                elif name.startswith("ask-") and floor is not None:
                    stamp = name[4:]
                    if stamp[:8] < floor:
                        continue
                else:
                    continue
                if _STAMP.fullmatch(stamp) and safe_id(name):
                    found.append((stamp, name))
    except OSError:
        return None
    for _stamp, name in sorted(found, reverse=True):
        rdir = root / name
        bound = (_read(rdir) or {}).get("sizing_rel") or (rl.read_run(rdir) or {}).get("sizing_rel")
        if not isinstance(bound, str) or _norm(bound) != norm:
            continue
        try:
            if run_notes.live(rl.carried_notes(Path(repo_root), name)):
                return name
        except ValueError:
            continue
    return None


def bound_sizing_rel(repo_root: Path, run_id: str) -> Optional[str]:
    """The sizing a run is bound to (its `halted.json`, else its `run.json`), else None. Two small reads of
    one run's record dir; `run_id` must be a safe id."""
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    if not safe_id(run_id):
        return None
    rdir = Path(repo_root) / RECORD_DIR_ROOT / run_id
    bound = (_read(rdir) or {}).get("sizing_rel") or (rl.read_run(rdir) or {}).get("sizing_rel")
    return _norm(bound) if isinstance(bound, str) and bound else None


def _is_shell(run_dir: Path) -> bool:
    """A minted-only dir: no marker and a progress record that never left `minted` (or none).

    Such a dir holds no work to resume or to supersede with, so it never hides an older halt.
    """
    if (run_dir / HALTED_FILE).exists():
        return False
    try:
        stage = json.loads((run_dir / "run-stage.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return isinstance(stage, dict) and stage.get("stage") in (None, "minted")


def halted_run_named(repo_root: Path, run_id: object, sizing_rel: str) -> Optional[str]:
    """`run_id` when it names a `warp-*` or `ask-*` run whose marker is still `halted`, else None.

    A refire carries the run id its halt recorded. A marker bound to another sizing path (the sizing moved or
    was mutated since the halt) is rebound to `sizing_rel` here, so the sizing-keyed lookups downstream resolve
    the same run. One marker read; a run that is not halted answers None and the sizing lookup stands."""
    if not isinstance(run_id, str) or not run_id.startswith(("warp-", "ask-")) or not safe_id(run_id):
        return None
    rd = Path(repo_root) / RECORD_DIR_ROOT / run_id
    marker = _read(rd)
    if marker is None or marker.get("status") != STATUS_HALTED:
        return None
    norm = _norm(sizing_rel)
    old = marker.get("sizing_rel")
    if old != norm:
        _write(rd, {**marker, "sizing_rel": norm})
        _index_call("add_run", rd, halted_withheld(rd), sizing_rel=norm)
        if isinstance(old, str) and old:
            try:
                (Path(repo_root) / LOOKUP_INDEX_DIR / _lookup_key(old) / run_id).unlink(missing_ok=True)
            except OSError:
                pass
    return run_id


def halted_run_to_resume(repo_root: Path, sizing_rel: str, *, build: bool = True, run_id: object = None):
    """The halted run a refire of `sizing_rel` resumes, a `warp-<stem>-*` run or else an `ask-<stamp>`
    one. The submit hook and the emit body both resolve through here so their lookups cannot drift.
    A refire's `run_id` (`halted_run_named`) is tried first, so a sizing path change cannot lose the run.
    A sizing the lookup index holds no run for answers None after one directory listing.
    The hook passes `build=False`: on a checkout whose index is unbuilt it answers the `warp-<stem>-*` halt
    (one directory listing) or else `STALLED`, never a scan of every halt record; the deferred emit, which
    builds, resolves the rest off the hook."""
    if run_id is not None:
        named = halted_run_named(repo_root, run_id, sizing_rel)
        if named is not None:
            return named
    runs = _indexed_runs(repo_root, _norm(sizing_rel), build=build)
    if runs is STALLED:
        return halted_run_for(repo_root, sizing_rel) or STALLED
    if not runs:
        return None
    return halted_run_for(repo_root, sizing_rel) or halted_ask_run_for(repo_root, sizing_rel)


def halted_run_for(repo_root: Path, sizing_rel: str) -> Optional[str]:
    """Return the run id a refire of `sizing_rel` resumes, else None.

    Newest `warp-<stem>-*` dir first (run ids embed %Y%m%dT%H%M%S); a minted-only shell is skipped,
    so a newer unmarked run that got past `minted` still means the halted one was superseded. A
    `resumed` marker whose every newer dir is such a shell (the resuming run died before staging,
    e.g. its halt commit was denied) is rewritten `halted` and returned.
    """
    norm = _norm(sizing_rel)
    prefix = f"warp-{PurePosixPath(norm).stem}-"
    root = Path(repo_root) / RECORD_DIR_ROOT
    names = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                if entry.name.startswith(prefix) and _STAMP.fullmatch(entry.name[len(prefix):]) and entry.is_dir():
                    names.append(entry.name)
    except OSError:
        return None
    skipped = 0
    for name in sorted(names, reverse=True):
        if not safe_id(name):
            return None
        if _is_shell(root / name):
            skipped += 1
            continue
        marker = _read(root / name)
        bound = marker.get("sizing_rel") if marker else None
        if not isinstance(bound, str) or PurePosixPath(_norm(bound)).stem != PurePosixPath(norm).stem:
            return None
        status = marker.get("status")
        if bound != norm and status in (STATUS_HALTED, STATUS_RESUMED):
            marker = {**marker, "sizing_rel": norm}
            _write(root / name, marker)
            _index_call("add_run", root / name, halted_withheld(root / name), sizing_rel=norm)
        if status == STATUS_HALTED:
            return name
        if status == STATUS_RESUMED and skipped:
            _write(root / name, {**marker, "status": STATUS_HALTED})
            _index_call("add_run", root / name, halted_withheld(root / name), sizing_rel=norm)
            return name
        return None
    return None


#: The `rebind_blocker` reason for a recorded emitter that is no longer a live session.
EMITTER_GONE = "emitter-gone"


def recorded_emitter(repo_root: Path, run_id: str) -> Optional[str]:
    """The session id the halted run's emission receipt names, else None (no script, no receipt,
    unreadable, or no session recorded)."""
    from coordinator_core.ops.dispatch_emit.emission_receipt import emission_receipt_path

    script = Path(repo_root) / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    try:
        receipt = json.loads(emission_receipt_path(script).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    recorded = receipt.get("session_id") if isinstance(receipt, dict) else None
    return recorded if recorded and isinstance(recorded, str) else None


def rebind_blocker(repo_root: Path, run_id: str, session_id: str) -> Optional[str]:
    """Return a reason `session_id` must not take the halted run's script path, else None.

    None when the run has no script or receipt, the receipt names `session_id` or no session, or
    the recorded session is live. `EMITTER_GONE` when the recorded session is not live. One
    receipt read, one `session_live` call, no spawn.
    """
    from coordinator_core.session.liveness import session_live

    recorded = recorded_emitter(repo_root, run_id)
    if recorded is None or recorded == session_id:
        return None
    return None if session_live(recorded) else EMITTER_GONE


def _index_call(fn: str, record_dir: Path, *args, **kwargs) -> None:
    """Mirror a marker transition into the halted-withheld index of the checkout holding `record_dir`
    (`<root>/<RECORD_DIR_ROOT>/<run>`). Best-effort: the marker write is the truth, so an index
    failure never fails the transition. Imported lazily: the index module imports this one."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rd = Path(record_dir)
    try:
        getattr(idx, fn)(rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)], rd.name, *args, **kwargs)
    except (OSError, ValueError, IndexError):
        pass


def _live_index_drop(record_dir: Path) -> None:
    """Drop the run's row from the live-run index of the checkout holding `record_dir`, so a run whose
    marker left `halted` stops refusing overlapping asks. Never raises (`index_remove` swallows)."""
    from coordinator_core.ops.dispatch_emit import live_run_writes

    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return
    live_run_writes.index_remove(root, rd.name)


def _withheld_prints(record_dir: Path, withheld: dict) -> dict:
    """Blob id of each withheld path as it stands in the checkout holding `record_dir`; a path that
    is absent or unreadable is left out. The same fingerprint `_record_withheld_prints` records."""
    from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return {}
    return {p: f for p in sorted(withheld) if isinstance(p, str) and (f := fingerprint(root, p)) is not None}


def record_withheld(record_dir: Path, withheld: dict) -> bool:
    """Stamp ``run_artifacts.withheld`` (``{path: reason}``) on a ``halted`` marker so a refire can
    tell product the halt left dirty on purpose from product it never touched; False and no write
    when nothing was withheld or the marker is absent or not halted."""
    marker = _read(Path(record_dir))
    if not withheld or marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, HALT_RUN_ARTIFACTS_FIELD: {"withheld": dict(sorted(withheld.items()))}})
    _index_call(
        "add_run", record_dir, withheld,
        sizing_rel=marker.get("sizing_rel") or "", prints=_withheld_prints(record_dir, withheld),
    )
    return True


def record_resume_stage(record_dir: Path, stage: dict) -> bool:
    """Stamp ``next_action.resume.stage`` (the kept.json pointer) on a ``halted`` marker whose
    ``next_action`` is a refire envelope; False and no write otherwise."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    action = marker.get(HALT_NEXT_ACTION_FIELD)
    if not is_refire_next_action(action):
        return False
    stamped = {**action, "resume": {**action["resume"], "stage": dict(stage)}}
    _write(Path(record_dir), {**marker, HALT_NEXT_ACTION_FIELD: stamped})
    return True


def halted_withheld(record_dir: Path) -> dict:
    """``{path: reason}`` the halt recorded as withheld; empty when absent or malformed."""
    return _withheld_of(_read(Path(record_dir)))


def _withheld_of(marker: Optional[dict]) -> dict:
    arts = marker.get(HALT_RUN_ARTIFACTS_FIELD) if marker else None
    raw = arts.get("withheld") if isinstance(arts, dict) else None
    return {k: v for k, v in raw.items() if isinstance(k, str)} if isinstance(raw, dict) else {}


def fresh_index_entry(record_dir: Path):
    """The `halted_withheld_index` entry of the run owning `record_dir` (`<root>/<RECORD_DIR_ROOT>/<run>`)
    while it is fresh against the marker (`halted_withheld_index.fresh_entry`), else None: the caller
    opens `halted.json`. None for a dir outside that layout."""
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    rd = Path(record_dir)
    try:
        root = rd.parents[len(PurePosixPath(RECORD_DIR_ROOT).parts)]
    except IndexError:
        return None
    if root / RECORD_DIR_ROOT / rd.name != rd:
        return None
    return idx.fresh_entry(root, rd.name)


def halted_withheld_strict(record_dir: Path) -> dict:
    """`halted_withheld` over the strict read: empty when the marker is absent, `UnreadableHaltRecord` when corrupt.
    A run with a fresh `halted_withheld_index` entry (`fresh_index_entry`) is answered without opening its
    marker: `{path: ""}`. Trap: the reasons are empty on that path; a caller needing them reads the marker."""
    entry = fresh_index_entry(record_dir)
    if entry is not None:
        return {p: "" for p in entry.paths}
    return _withheld_of(read_strict_run(record_dir))


def mark_revised(record_dir: Path, at: str) -> bool:
    """Stamp `revised_at` on a `halted` marker; False and no write when it is absent or not halted."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, REVISED_AT_FIELD: at})
    return True


def revised_halt(record_dir: Path) -> Optional[str]:
    """The `revised_at` value while the marker is `halted`, else None. One marker read."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return None
    value = marker.get(REVISED_AT_FIELD)
    return value if isinstance(value, str) and value else None


def record_released(record_dir: Path, sha: str, reason: str) -> bool:
    """Stamp ``released: {reason, sha}`` on a ``halted`` marker, leaving it ``halted`` and resumable;
    False and no write when the marker is absent or not halted."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, HALT_RELEASED_FIELD: {"reason": reason, "sha": sha}})
    return True


def mark_resumed(record_dir: Path) -> bool:
    """Rewrite a `halted` marker as `resumed`; False when no halted marker is present."""
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") != STATUS_HALTED:
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_RESUMED})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    return True


def mark_superseded(record_dir: Path, expected: dict, evidence: dict, dispositions: Optional[dict] = None) -> bool:
    """Rewrite the marker as `superseded` with `evidence` only if it still equals `expected` (the marker the
    caller verified against); False and no write when it is absent, changed, or no longer `halted`.

    `dispositions` (path -> DISPOSITION_*), when given, is also recorded under PATH_DISPOSITIONS_FIELD."""
    marker = _read(Path(record_dir))
    if marker is None or marker != expected or marker.get("status") != STATUS_HALTED:
        return False
    updated = {**marker, "status": STATUS_SUPERSEDED, SUPERSEDED_EVIDENCE_FIELD: evidence}
    if dispositions is not None:
        updated[PATH_DISPOSITIONS_FIELD] = dispositions
    _write(Path(record_dir), updated)
    _live_index_drop(record_dir)
    _record_judge(record_dir)
    return True


def mark_landed(record_dir: Path, sha: str, *, without_verdict: bool = False) -> bool:
    """Rewrite a `halted` or `resumed` marker as `landed` at `sha`; False and no write for any other status.

    A `landed` marker is skipped by `halted_run_for` / `halted_ask_run_for`, so a refire no longer resumes it.
    `without_verdict` stamps `landed_without_verdict: true`; the default writes no such key.
    """
    marker = _read(Path(record_dir))
    if marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    extra = {"landed_without_verdict": True} if without_verdict else {}
    _write(Path(record_dir), {**marker, "status": STATUS_LANDED, "sha": sha, **extra})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    stamp_run_stage_at(Path(record_dir), "landed")
    return True


def mark_closed(record_dir: Path, reason: str, at: str) -> bool:
    """Rewrite a `halted` or `resumed` marker as `closed` with `{reason, at}`: a terminal end for a
    halt that will never land (nothing withheld, owner gone). False and no write for any other status."""
    marker = _read(Path(record_dir))
    if not reason or marker is None or marker.get("status") not in (STATUS_HALTED, STATUS_RESUMED):
        return False
    _write(Path(record_dir), {**marker, "status": STATUS_CLOSED, "closed": {"reason": reason, "at": at}})
    _index_call("remove_run", record_dir)
    _live_index_drop(record_dir)
    _record_judge(record_dir)
    return True


def _record_judge(record_dir: Path) -> None:
    """Record the engine's not_met verdict for a run closed or superseded without one; best-effort."""
    try:
        record_terminal_judge_at(Path(record_dir))
    except (ValueError, OSError):
        pass


def mark_predecessors_landed(repo_root: Path, run_id: str, sha: str) -> list:
    """Stamp each `resumed` predecessor of `run_id` (its run.json `resumed_from` chain) landed at `sha`.

    Returns the run ids stamped. A predecessor not `resumed` (already landed, or halted again) is left
    alone; the walk is cycle-safe and capped at `CARRIED_HOP_CAP` hops. Never raises on a bad link.
    """
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    root = Path(repo_root)
    stamped: list = []
    visited = {run_id}
    cur = run_id
    for _ in range(rl.CARRIED_HOP_CAP):
        try:
            prev = (rl.read_run(record_dir(root, cur)) or {}).get(rl.RESUMED_FROM_FIELD)
            if not (isinstance(prev, str) and safe_id(prev)) or prev in visited:
                break
            marker = _read(record_dir(root, prev))
            if marker is not None and marker.get("status") == STATUS_RESUMED and mark_landed(record_dir(root, prev), sha):
                stamped.append(prev)
        except (OSError, ValueError):
            break
        visited.add(prev)
        cur = prev
    return stamped
