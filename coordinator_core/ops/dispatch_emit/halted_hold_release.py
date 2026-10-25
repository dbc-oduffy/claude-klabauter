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

import os
import time
from pathlib import Path
from typing import Optional

RELEASE_SUPERSEDED = "sizing-superseded"
RELEASE_SUCCESSOR = "successor-run"
RELEASE_PLAN_LANDING = "plan-landing"
RELEASE_SUCCESSOR_LANDED = "successor-landed"
RELEASE_RECORDED_LANDED = "recorded-landed"
RELEASE_SIZING_SHIPPED = "sizing-shipped"
RELEASE_KEPT_AT_HEAD = "kept-at-head"
RELEASE_CRITERION_MET = "criterion-met-at-head"
RELEASE_PHANTOM = "phantom-no-own-hunk-owner-gone"
_RECORDED_RELEASES = frozenset({RELEASE_CRITERION_MET, RELEASE_PHANTOM})


def _halted_sizing(root: Path, run_id: str, marker: dict) -> str:
    from coordinator_core.ops import warp_run_deliverable as wrd

    sizing = wrd.record_sizing_rel(root, run_id)
    if sizing:
        return sizing
    value = marker.get("sizing_rel")
    return wrd._norm(value) if isinstance(value, str) else ""


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


def release_reason(
    root: Path, halted_run_id: str, committing_run_id: Optional[str], committing_plan: Optional[str] = None,
) -> Optional[str]:
    """Why `halted_run_id` no longer holds against `committing_run_id`'s commit, else None (it holds).

    A `landed` marker always releases; `halted`/`resumed` release per the rules above, and a shipped
    sizing frees either. Any other marker state (none) is None. Never raises: an unreadable record holds.
    """
    try:
        from coordinator_core.ops import warp_run_deliverable as wrd
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import HALT_PLAN_FIELD, record_dir

        root = Path(root)
        marker = halted_run._read(record_dir(root, halted_run_id))
        status = marker.get("status") if marker else None
        if status == halted_run.STATUS_LANDED:
            return RELEASE_RECORDED_LANDED
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
        if _kept_at_head(root, record_dir(root, halted_run_id), marker):
            return RELEASE_KEPT_AT_HEAD
    except Exception:
        return None
    return None


def _squash(text: str) -> str:
    return "".join(text.split())


def _blob_batch(root: Path, specs: list) -> Optional[dict]:
    """`{spec: bytes | None}` for each git object spec, one `git cat-file --batch` spawn; None when git fails."""
    from coordinator_core.git.run import run_git

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
    import difflib

    base_lines, kept_lines = base.splitlines(), kept.splitlines()
    squashed_head = _squash(head)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, base_lines, kept_lines, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            added = _squash("\n".join(kept_lines[j1:j2]))
            if added and added not in squashed_head:
                return False
        if tag in ("delete", "replace"):
            removed = _squash("\n".join(base_lines[i1:i2]))
            if removed and removed in squashed_head and removed not in _squash("\n".join(kept_lines[j1:j2])):
                return False
    return True


def kept_at_head_paths(root: Path, record: Path, marker: dict) -> set:
    """The withheld paths of the halt at `record` whose kept content is already at HEAD: blob-equal, or the
    kept diff against `run_base_sha` already present. A path with no kept blob, a missing object, or an
    unreadable record is absent (still held). One git spawn for the whole run, none when nothing is withheld."""
    from coordinator_core.ops.dispatch_emit import halted_run, kept_work

    withheld = halted_run.halted_withheld(record)
    if not withheld:
        return set()
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
    if not paths:
        return set()
    base_sha = marker.get("run_base_sha")
    base_sha = base_sha if isinstance(base_sha, str) and base_sha else ""
    specs = [f"HEAD:{p}" for p in paths] + [kept[p] for p in paths]
    specs += [f"{base_sha}:{p}" for p in paths] if base_sha else []
    blobs = _blob_batch(root, specs)
    if blobs is None:
        return set()
    released: set = set()
    for p in paths:
        head, keep = blobs[f"HEAD:{p}"], blobs[kept[p]]
        if head is None or keep is None:
            continue
        if head == keep:
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

    withheld = set(halted_run.halted_withheld(record))
    return bool(withheld) and kept_at_head_paths(root, record, marker) == withheld


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
