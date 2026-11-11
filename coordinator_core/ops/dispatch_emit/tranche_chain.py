"""Tranche chain state: what a minted inventory spine records about its tranche, and the
`dispatch.emit` request that continues the family at its next tranche."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, Tuple, Union

from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field

SPINE_SOURCE_RE = re.compile(r"^Derived from `([^`\n]+)` by `dispatch\.emit --inventory`", re.MULTILINE)
_DERIVED_FROM_LINE = "derived_from: mise inventory record\n"
_TRANCHE_SUFFIX_RE = re.compile(r"-t\d+$")
_SCRIPT_SUFFIX = ".workflow.mjs"

PathLike = Union[str, Path]


def stall_summary(stall: Optional[dict]) -> str:
    """`<row>: <reason>, ...` for a tranche report's `stall` (`{"rows", "held_by"}`); "" when
    there is no stall."""
    if not stall:
        return ""
    held_by = stall.get("held_by") or {}
    return ", ".join(f"{row}: {held_by.get(row, 'held')}" for row in stall.get("rows") or held_by)


def stamp_tranche_state(
    spine_text: str, row_budget: int, passes_remaining: int, stall: Optional[dict] = None
) -> str:
    """`spine_text` with `tranche_row_budget:` / `tranche_passes_remaining:` added to its
    frontmatter, so the next re-mint and the chain read them from disk. A `stall` (every row
    the pass would emit is held) adds `tranche_stalled: "<rows held_by summary>"`."""
    stamp = f"tranche_row_budget: {int(row_budget)}\ntranche_passes_remaining: {int(passes_remaining)}\n"
    summary = stall_summary(stall)
    if summary:
        stamp += f"tranche_stalled: {json.dumps(summary)}\n"
    return spine_text.replace(_DERIVED_FROM_LINE, _DERIVED_FROM_LINE + stamp, 1)


def stalled_tranche(spine: Path) -> str:
    """The `tranche_stalled` summary the spine records; "" when the pass did not stall."""
    return str(read_frontmatter_field(str(spine), "tranche_stalled") or "").strip()


def _positive_int(spine: Path, field: str) -> Optional[int]:
    raw = str(read_frontmatter_field(str(spine), field) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def recorded_tranche_state(spine: Path) -> Tuple[Optional[int], int]:
    """`(row_budget, passes_remaining)` the spine's frontmatter records; budget None when
    the spine predates the record, passes 0 when absent."""
    raw = str(read_frontmatter_field(str(spine), "tranche_passes_remaining") or "").strip()
    return _positive_int(spine, "tranche_row_budget"), int(raw) if raw.isdigit() else 0


def inventory_record_for(spine: Path, *, family_base: bool = False) -> Optional[Path]:
    """The inventory record a minted spine names beside itself. `family_base` resolves a
    tranche record (`<base>-tN.md`, holding only its own tranche's plans) to the base record
    that still lists every plan, which is what the family's next tranche is cut from."""
    named = SPINE_SOURCE_RE.search(spine.read_text(encoding="utf-8", errors="replace"))
    record = (
        spine.with_name(re.split(r"[\\/]", named.group(1))[-1])
        if named
        else spine.with_name(spine.name.removesuffix(".spine.md") + ".md")
    )
    if family_base:
        base = record.with_name(_TRANCHE_SUFFIX_RE.sub("", record.stem) + record.suffix)
        if base.is_file():
            return base
    return record if record.is_file() else None


def next_tranche_request(root: PathLike, script_path: PathLike) -> Optional[dict]:
    """The `dispatch.emit` params that mint the family's next tranche after the run whose
    script is `script_path`: the base inventory record at the budget the spine recorded.
    None when `script_path` has no inventory spine beside it, or the spine records no
    passes remaining or no budget."""
    script = Path(script_path)
    if not script.name.endswith(_SCRIPT_SUFFIX):
        return None
    spine = script.with_name(script.name.removesuffix(_SCRIPT_SUFFIX) + ".md")
    if not spine.is_file():
        return None
    if str(read_frontmatter_field(str(spine), "derived_from") or "").strip() != "mise inventory record":
        return None
    budget, passes = recorded_tranche_state(spine)
    if budget is None or passes <= 0 or stalled_tranche(spine):
        return None
    record = inventory_record_for(spine, family_base=True)
    if record is None:
        return None
    return {"inventory_path": str(record), "target_root": str(root), "row_budget": budget}


_PART_SCRIPT_RE = re.compile(r"^(.*)-p\d+(\.spine)?$")


def _inventory_spine(script_path: PathLike) -> Optional[Path]:
    script = Path(script_path)
    if not script.name.endswith(_SCRIPT_SUFFIX):
        return None
    stem = script.name.removesuffix(_SCRIPT_SUFFIX)
    # A part script is named `<run>-pN.workflow.mjs` beside `<run>-pN.spine.md`; a tranche
    # script is `<run>.spine.workflow.mjs` beside the same spine.
    spine = script.with_name(stem + (".md" if stem.endswith(".spine") else ".spine.md"))
    if not spine.is_file():
        return None
    if str(read_frontmatter_field(str(spine), "derived_from") or "").strip() != "mise inventory record":
        return None
    return spine


def _part_stamp(spine: Path) -> Tuple[Optional[int], Optional[int]]:
    return _positive_int(spine, "part_index"), _positive_int(spine, "part_count")


def next_part_request(root: PathLike, script_path: PathLike) -> Optional[dict]:
    """The `dispatch.emit` params that mint part N+1 of the whole-set cut after the run whose
    script is `script_path`, from the spine's `part_index` / `part_count` stamp. None for a
    script with no inventory spine, no part stamp, or at the last part."""
    spine = _inventory_spine(script_path)
    if spine is None:
        return None
    index, count = _part_stamp(spine)
    if index is None or count is None or index >= count:
        return None
    record = inventory_record_for(spine)
    if record is None:
        return None
    script = Path(script_path)
    named = _PART_SCRIPT_RE.match(script.name.removesuffix(_SCRIPT_SUFFIX))
    request = {"inventory_path": str(record), "target_root": str(root), "inventory_part": [index + 1, count]}
    if named:
        request["output_path"] = str(
            script.with_name(f"{named.group(1)}-p{index + 1}{named.group(2) or ''}{_SCRIPT_SUFFIX}")
        )
    return request


CHAIN_FILE = "tranche-chain.json"


def write_chain_record(root: PathLike, run_id: str, chained: Optional[str], chain_error: Optional[str]) -> Optional[Path]:
    """Write `{chained, chain_error}` to `<record_dir>/tranche-chain.json`; None when the
    run's record dir cannot be resolved. A file of its own: `write_run_outcome` keeps only
    `RUN_OUTCOME_KEYS` and would drop both keys."""
    from coordinator_core.atomic_replace import atomic_write_bytes
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        path = record_dir(Path(root), run_id) / CHAIN_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, (json.dumps({"chained": chained, "chain_error": chain_error}, indent=2) + "\n").encode("utf-8"))
    except (ValueError, OSError):
        return None
    return path


def _emit_next(root: str, request: dict) -> dict:
    """Run `dispatch.emit` with `request`; the emitted reply, or raise with the engine's error."""
    import asyncio

    from coordinator_core.ipc import dispatch_message

    reply = asyncio.run(dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "dispatch.emit", "params": request, "_origin_worktree": root,
    }))
    if not isinstance(reply, dict) or "result" not in reply:
        raise RuntimeError(f"dispatch.emit failed: {json.dumps((reply or {}).get('error') if isinstance(reply, dict) else reply)}")
    return reply["result"]


def _fire_headless(script: str, root: str, run_id: Optional[str]) -> dict:
    from coordinator_core.ops.workflow_fire import headless

    return headless.start(script, cwd=root, run_id=run_id)


def run_chain(root: PathLike, run_id: str, request: dict, *, emit=_emit_next, fire=_fire_headless) -> dict:
    """Emit the next tranche from `request`, fire its script, and record the outcome beside
    the landed run `run_id`. Never raises: a failure lands in `chain_error`."""
    chained: Optional[str] = None
    error: Optional[str] = None
    try:
        emitted = emit(str(root), request)
        script = emitted.get("path")
        if not script or emitted.get("ok") is False:
            raise RuntimeError(f"emit produced no fireable script: {emitted.get('error') or emitted.get('findings')}")
        chained = str(emitted.get("run_id") or Path(script).name.removesuffix(_SCRIPT_SUFFIX))
        fire(str(script), str(root), chained)
    except Exception as exc:  # noqa: BLE001 -- a chain failure is recorded, never raised
        error = f"{type(exc).__name__}: {exc}"
    write_chain_record(root, run_id, chained, error)
    if error:
        _release_claim(root, run_id)
    return {"chained": chained, "chain_error": error}


def _spawn_detached(root: str, run_id: str, request: dict) -> None:
    """Start the one detached process that runs `run_chain` for this landed run."""
    import os
    import subprocess
    import sys

    command = [sys.executable, "-m", __name__, "--root", root, "--run-id", run_id, "--request", json.dumps(request)]
    env = dict(os.environ, COORDINATOR_WARM="0")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3]) + os.pathsep + env.get("PYTHONPATH", "")
    if os.name == "nt":
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    else:
        kwargs = {"start_new_session": True}
    subprocess.Popen(
        command, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **kwargs,
    )


def _already_chained(root: PathLike, run_id: str) -> bool:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        return (record_dir(Path(root), run_id) / CHAIN_FILE).is_file()
    except (ValueError, OSError):
        return False


def _land_request(root: PathLike, run_id: str, script_path: PathLike) -> Optional[dict]:
    """The next tranche owed after a landing, unless the run already chained or is a part whose
    own terminal commit hands off to the next part."""
    spine = _inventory_spine(script_path)
    if spine is not None:
        index, count = _part_stamp(spine)
        if index is not None and count is not None and index < count:
            return None
    if _already_chained(root, run_id):
        return None
    return next_tranche_request(root, script_path)


def chain_after_land(root: PathLike, run_id: str, script_path: PathLike, *, spawn=None) -> dict:
    """Hand the landed run `run_id` to its family's next tranche: one detached spawn that
    emits and fires it. `{"chained": None, "chain_error": <text> | None, "request": <params> |
    None, "spawned": bool}`; no request (no tranche owed) spawns nothing and writes nothing.
    The detached process records the outcome in `tranche-chain.json`; a failed spawn is
    recorded here. A run that already chained, and a non-last part (its terminal commit chains
    the next part), spawn nothing. Never raises."""
    stalled = _record_stall(root, run_id, script_path)
    if stalled:
        return {"chained": None, "chain_error": stalled, "request": None, "spawned": False}
    return _hand_off(root, run_id, script_path, _land_request, spawn)


def _record_stall(root: PathLike, run_id: str, script_path: PathLike) -> Optional[str]:
    """For a landed tranche spine stamped `tranche_stalled`, write the named `chain_error` to
    `tranche-chain.json` and return it; None for any other script or an already-chained run."""
    script = Path(script_path)
    if not script.name.endswith(_SCRIPT_SUFFIX):
        return None
    spine = script.with_name(script.name.removesuffix(_SCRIPT_SUFFIX) + ".md")
    try:
        if not spine.is_file() or _already_chained(root, run_id):
            return None
        summary = stalled_tranche(spine)
    except OSError:
        return None
    if not summary:
        return None
    error = f"stalled: every row held ({summary})"
    write_chain_record(root, run_id, None, error)
    return error


def chain_after_part(root: PathLike, run_id: str, script_path: PathLike, *, spawn=None) -> dict:
    """Hand the part whose terminal commit just landed to part N+1: the same one detached
    spawn and `tranche-chain.json` record as `chain_after_land`, with no landing involved. A
    part holding the exclusive spawn claim, and the last part, spawn nothing; a stall (failed
    spawn or failed emit) records `chain_error` and releases the claim so a refire retries.
    Never raises."""
    return _hand_off(
        root, run_id, script_path,
        lambda r, rid, s: next_part_request(r, s),
        spawn,
        claim=True,
    )


CLAIM_FILE = "tranche-chain.claim"


def _claim_chain(root: PathLike, run_id: str) -> bool:
    """Create the run's chain claim exclusively; False when a prior hand-off already holds it.
    True when the record dir cannot be resolved: an unclaimable run still chains."""
    import os

    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        path = record_dir(Path(root), run_id) / CLAIM_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        return _take_over_stale_claim(path)
    except (ValueError, OSError):
        return True
    return True


STALE_CLAIM_SECONDS = 900


def _take_over_stale_claim(path: Path) -> bool:
    """A claim older than `STALE_CLAIM_SECONDS` belongs to a detached chain that died before
    `run_chain` released it: refresh it and let this hand-off spawn. A fresh claim stays held."""
    import os
    import time

    try:
        if time.time() - path.stat().st_mtime < STALE_CLAIM_SECONDS:
            return False
        os.utime(path)
    except OSError:
        return False
    return True


def _release_claim(root: PathLike, run_id: str) -> None:
    """Drop the run's chain claim after a stall; a missing claim or dir is nothing to drop."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    try:
        (record_dir(Path(root), run_id) / CLAIM_FILE).unlink(missing_ok=True)
    except (ValueError, OSError):
        return


def _hand_off(root: PathLike, run_id: str, script_path: PathLike, resolve, spawn, claim: bool = False) -> dict:
    try:
        request = resolve(root, run_id, script_path)
    except Exception as exc:  # noqa: BLE001 -- the caller never fails on the chain
        return {"chained": None, "chain_error": f"{type(exc).__name__}: {exc}", "request": None, "spawned": False}
    if request is None:
        return {"chained": None, "chain_error": None, "request": None, "spawned": False}
    if claim and not _claim_chain(root, run_id):
        return {"chained": None, "chain_error": None, "request": request, "spawned": False}
    try:
        (spawn or _spawn_detached)(str(root), run_id, request)
    except Exception as exc:  # noqa: BLE001
        error = f"spawn failed: {type(exc).__name__}: {exc}"
        write_chain_record(root, run_id, None, error)
        if claim:
            _release_claim(root, run_id)
        return {"chained": None, "chain_error": error, "request": request, "spawned": False}
    return {"chained": None, "chain_error": None, "request": request, "spawned": True}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Emit and fire a landed tranche's next tranche.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--request", required=True)
    options = parser.parse_args()
    run_chain(options.root, options.run_id, json.loads(options.request))
