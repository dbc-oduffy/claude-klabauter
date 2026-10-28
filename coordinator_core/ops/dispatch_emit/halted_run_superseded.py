"""``dispatch.close_halted_run_superseded``: closes a halted run whose withheld paths later work overwrote.

Params: ``run_id`` (string, required: the halted run). Fail-closed: the op writes only when the marker
is ``halted`` (not resumed, landed or live) and withholds at least one path; the run-stage record is
``halted`` with a parseable ``last_activity_at`` that resolves to a commit (the halt anchor); every
withheld path classifies as exactly one of: ``landed`` (HEAD blob equals the kept blob), ``superseded`` (a commit in
``<halt_anchor>..HEAD`` touched it and a HEAD blob exists that differs from the kept blob) or ``scratch-abandoned``
(under ``scratch/``, untouched since the halt, absent from both the anchor tree and HEAD) or ``lost`` (the kept blob
is absent from the object store: recorded with a reason, no superseding commit needed, and a run with a lost path
closes even with no superseded path). Any path that classifies
as none refuses naming every such path; so does a run with no superseded path (all landed, or landed plus scratch:
``dispatch.record_halted_run_landed`` records those). Any gap leaves marker and index unchanged.
On success the marker is rewritten ``status: superseded`` with ``superseded_evidence: {path: sha}``
(superseded paths only: the newest post-halt commit touching the path) and ``path_dispositions`` (every path),
and the run leaves the halt-withheld index. It never marks a run landed. Three git spawns: ``rev-list`` (anchor),
``log --name-only``, ``cat-file --batch-check`` (HEAD, kept and anchor blobs in one batch).
Legacy route (anchored at the commit that first added halted.json when one exists, else the halt anchor): taken ONLY when the halt has neither a kept file nor a withheld-prints file (written before both); a
present but unreadable, malformed or blob-less one refuses fail-closed. A withheld path whose newest post-halt touch
is a deletion refuses, evidence sha or not. It takes ``evidence_shas`` (list of full shas,
required): each must be an ancestor of HEAD and touch a withheld path in ``<halt_anchor>..HEAD``, none may have
deleted a withheld path, and between them they cover every withheld path; ``superseded_evidence`` records
``{path: evidence sha}`` for the touched paths; an untouched ``scratch/`` path needs no evidence sha and costs one
extra ``cat-file --batch-check`` spawn (anchor and HEAD absence); there is no landed class. ``memo_id`` (optional string) is echoed in the legacy reply only. A halt with a kept
file keeps the gate above and ignores ``evidence_shas``.
With no run-stage record the anchor is the newest commit touching the run's ``halted.json`` (one extra ``log`` spawn).
Trap: the anchor is the halt, never ``run_base_sha``; base..HEAD would count the run's own pre-halt commits.
Reply: ``{ok, run_id, evidence, dispositions, recorded}`` (legacy adds ``legacy: true`` and ``memo_id``) or ``{ok: false, error}``.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root

OP_NAME = "dispatch.close_halted_run_superseded"

_SHA40 = re.compile(r"[0-9a-f]{40}")


def _refuse(error: str) -> dict:
    return {"ok": False, "error": error}


def _halt_anchor(root: Path, run_id: str) -> tuple:
    """``(anchor_sha, None)`` or ``(None, reason)``: the last commit at or before the halt stamp."""
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit import ask_contract
    from coordinator_core.ops.dispatch_emit.halted_hunk_attribution import _iso_epoch

    stage = ask_contract.read_run_stage(root, run_id)
    if not stage:
        from coordinator_core.ops.dispatch_emit import halted_run

        rel = (ask_contract.record_dir(root, run_id) / halted_run.HALTED_FILE).relative_to(root).as_posix()
        proc = run_git(["log", "-1", "--format=%H", "--", rel], cwd=str(root))
        sha = proc.stdout.strip() if proc.returncode == 0 else ""
        if _SHA40.fullmatch(sha):
            return sha, None
        return None, f"{run_id} has no run-stage record and no commit of its halt record; the halt is unanchored"
    if stage.get("stage") != "halted":
        return None, f"{run_id} has no halted run-stage record; the halt is unanchored"
    epoch = _iso_epoch(stage.get("last_activity_at"))
    if epoch is None:
        return None, f"{run_id} run-stage record has no parseable last_activity_at; the halt is unanchored"
    proc = run_git(["rev-list", "-1", f"--before={int(epoch)}", "HEAD"], cwd=str(root))
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    if not _SHA40.fullmatch(sha):
        return None, f"no commit at or before the halt of {run_id} resolves; the halt is unanchored"
    return sha, None


def _newest_touch(root: Path, base: str, paths: list) -> Optional[dict]:
    """``{path: [(sha, status), ...]}`` for the paths touched in ``base..HEAD``, newest first (status ``D`` is a
    deletion); None when git fails. One ``log`` spawn."""
    from coordinator_core.git.run import run_git

    proc = run_git(
        ["-c", "core.quotepath=off", "log", "--name-status", "--no-renames", "--format=%H", f"{base}..HEAD", "--", *paths],
        cwd=str(root),
    )
    if proc.returncode != 0:
        return None
    wanted, found, current = set(paths), {}, None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if _SHA40.fullmatch(line):
            current = line
            continue
        status, _, path = line.partition("\t")
        if current and path in wanted:
            found.setdefault(path, []).append((current, status[:1]))
    return found


def _blob_ids(root: Path, specs: list) -> Optional[list]:
    """The object id per spec (None when missing), one ``cat-file --batch-check``; None when git fails."""
    from coordinator_core.git.run import run_git

    proc = run_git(["cat-file", "--batch-check"], cwd=str(root), input="".join(s + "\n" for s in specs).encode())
    if proc.returncode != 0:
        return None
    lines = proc.stdout_bytes.decode("utf-8", "replace").splitlines()
    if len(lines) != len(specs):
        return None
    out = []
    for line in lines:
        parts = line.rsplit(" ", 2)
        out.append(parts[0] if len(parts) == 3 and parts[1] == "blob" else None)
    return out


_LOST_REASON = "kept blob absent from the object store; no content left to protect"
_SCRATCH_PREFIX = "scratch/"
_SCRATCH_REASON = "under scratch/, never tracked at halt or HEAD"


def _is_scratch_path(p: str) -> bool:
    """Under ``scratch/`` by its normalized spelling: ``scratch/../src/x`` names a path outside scratch/, and git
    does not resolve ``..`` in a ``<rev>:<path>`` spec, so it would read as absent everywhere."""
    return p.startswith(_SCRATCH_PREFIX) and "\\" not in p and posixpath.normpath(p) == p


def _classify_paths(paths: list, touched: dict, head: dict, keep: dict, base: dict, anchor: str) -> tuple:
    """``(dispositions, unclassified)``: ``{path: {disposition, blob|sha|reason}}`` for the classified paths and
    ``["path (reason)", ...]`` for the rest. ``head``/``base`` map a path to its blob id at HEAD / the halt anchor
    (None when absent); a path missing from ``head`` was not read (legacy touched path: never landed or scratch).
    ``keep`` maps a path to its withheld blob (empty on the legacy route, which has no landed class)."""
    from coordinator_core.ops.dispatch_emit import halted_run

    done, bad = {}, []
    for p in paths:
        h, k = head.get(p), keep.get(p)
        if h is not None and h == k:
            done[p] = {"disposition": halted_run.DISPOSITION_LANDED, "blob": h}
        elif keep and k is None:
            done[p] = {"disposition": halted_run.DISPOSITION_LOST, "reason": _LOST_REASON}
        elif p in touched:
            if p in head and h is None:
                bad.append(f"{p} (absent at HEAD; deletion is not verified as supersession)")
            else:
                done[p] = {"disposition": halted_run.DISPOSITION_SUPERSEDED, "sha": touched[p][0][0]}
        elif _is_scratch_path(p) and p in head and h is None and base.get(p) is None:
            done[p] = {"disposition": halted_run.DISPOSITION_SCRATCH, "reason": _SCRATCH_REASON}
        else:
            bad.append(f"{p} (no commit after the halt ({anchor[:12]}) touched it, and it is neither landed nor untracked scratch)")
    return done, bad


def _refuse_unclassified(bad: list) -> dict:
    return _refuse(f"cannot classify {len(bad)} withheld path(s), the run is not closed: " + "; ".join(sorted(bad)))


def _refuse_without_supersession(done: dict) -> Optional[dict]:
    """None when some path is superseded or lost; else the refusal (a superseded close claims a later commit replaced work)."""
    from coordinator_core.ops.dispatch_emit import halted_run

    if any(d["disposition"] in (halted_run.DISPOSITION_SUPERSEDED, halted_run.DISPOSITION_LOST) for d in done.values()):
        return None
    scratch = sorted(p for p, d in done.items() if d["disposition"] == halted_run.DISPOSITION_SCRATCH)
    if not scratch:
        landed = sorted(done)
        return _refuse(
            f"HEAD's blob of {landed[0]} equals the withheld blob; the run's work landed, record it landed with dispatch.record_halted_run_landed"
        )
    return _refuse(
        f"no withheld path was superseded; scratch-only {', '.join(scratch)} is not closable as superseded: "
        "record the landed rest with dispatch.record_halted_run_landed"
    )


def _evidence_shas(params: dict) -> tuple:
    """``(sorted unique 40-hex shas, None)`` or ``(None, reason)`` from ``params["evidence_shas"]``."""
    raw = params.get("evidence_shas")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None, "a legacy halt record (no kept blobs) needs evidence_shas: the commits that shipped the withheld paths"
    shas = []
    for item in raw:
        if not isinstance(item, str) or not _SHA40.fullmatch(item.strip().lower()):
            return None, f"evidence_shas entry {item!r} is not a full 40-hex commit sha"
        shas.append(item.strip().lower())
    return sorted(set(shas)), None


def _all_ancestors_of_head(root: Path, shas: list) -> Optional[str]:
    """None when every sha is a commit reachable from HEAD, else the reason; one ``rev-list`` spawn."""
    from coordinator_core.git.run import run_git

    proc = run_git(["rev-list", "--no-walk=unsorted", *shas, "--not", "HEAD"], cwd=str(root))
    if proc.returncode != 0:
        return "an evidence sha does not resolve to a commit; supersession cannot be verified"
    stray = proc.stdout.split()
    if stray:
        return f"evidence sha {stray[0][:12]} is not an ancestor of HEAD"
    return None


def _first_record_anchor(root: Path, run_id: str) -> tuple:
    """``(sha, None)`` of the commit that first added the run's ``halted.json`` (the halt as first recorded), or
    ``(None, reason)`` when none is committed. A later revision of the record or run-stage stamp can postdate the shipping commits."""
    from coordinator_core.git.run import run_git
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_run

    rel = (ask_contract.record_dir(root, run_id) / halted_run.HALTED_FILE).relative_to(root).as_posix()
    proc = run_git(["log", "--diff-filter=A", "--format=%H", "--", rel], cwd=str(root))
    shas = proc.stdout.split() if proc.returncode == 0 else []
    if shas and _SHA40.fullmatch(shas[-1]):
        return shas[-1], None
    return None, f"{run_id} has no commit that added its halt record; the halt is unanchored"


def _legacy_close(root: Path, record: Path, marker: dict, run_id: str, paths: list, params: dict) -> dict:
    """Close a halt that predates kept blobs: every withheld path must be touched in ``<anchor>..HEAD`` and
    covered by a caller-named evidence commit that is an ancestor of HEAD, with HEAD's blob differing from the
    blob at the anchor. Three git spawns."""
    from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index

    shas, why = _evidence_shas(params)
    if shas is None:
        return _refuse(why)
    anchor, _ = _first_record_anchor(root, run_id)
    if anchor is None:
        anchor, why = _halt_anchor(root, run_id)
        if anchor is None:
            return _refuse(why)
    why = _all_ancestors_of_head(root, shas)
    if why:
        return _refuse(why)
    touching = _newest_touch(root, anchor, paths)
    if touching is None:
        return _refuse(f"git log over {anchor[:12]}..HEAD failed; retry")
    scratchy = [p for p in paths if p not in touching and _is_scratch_path(p)]
    probed = scratchy + [p for p in paths if p in touching]
    ids = _blob_ids(root, [f"HEAD:{p}" for p in probed] + [f"{anchor}:{p}" for p in probed])
    if ids is None:
        return _refuse("git cat-file failed; retry")
    head_all = dict(zip(probed, ids))
    base_all = dict(zip(probed, ids[len(probed):]))
    head = {p: head_all[p] for p in scratchy}
    base = {p: base_all[p] for p in scratchy}
    dispositions, bad = _classify_paths(paths, touching, head, {}, base, anchor)
    if bad:
        return _refuse_unclassified(bad)
    why = _refuse_without_supersession(dispositions)
    if why:
        return why
    given = set(shas)
    evidence = {}
    for p in paths:
        if p not in touching:
            continue
        if touching[p][0][1] == "D":
            return _refuse(f"newest touch of withheld {p} ({touching[p][0][0][:12]}) deleted it; a deletion is not verified as supersession")
        hit = next((s for s, _ in touching[p] if s in given), None)
        if hit is None:
            return _refuse(f"no evidence sha touches withheld {p}; name the commit that shipped it")
        deleted = next((s for s, st in touching[p] if s in given and st == "D"), None)
        if deleted:
            return _refuse(f"evidence sha {deleted[:12]} deleted withheld {p}; a deletion is not shipping it")
        if head_all[p] == base_all[p]:
            return _refuse(f"HEAD's blob of withheld {p} equals its blob at the halt ({anchor[:12]}); touched then reverted is not shipped")
        evidence[p] = hit
        dispositions[p]["sha"] = hit
    unused = sorted(given - {s for p in paths for s, _ in touching.get(p, ())})
    if unused:
        return _refuse(f"evidence sha {unused[0][:12]} touches no withheld path in {anchor[:12]}..HEAD")
    if not halted_run.mark_superseded(record, marker, evidence, dispositions):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    halted_withheld_index.remove_run(root, run_id)
    reply = {
        "ok": True, "run_id": run_id, "evidence": evidence, "dispositions": dispositions, "legacy": True,
        "recorded": (record / halted_run.HALTED_FILE).relative_to(root).as_posix(),
    }
    memo = params.get("memo_id")
    if isinstance(memo, str) and memo:
        reply["memo_id"] = memo
    return reply


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "dispatch.close_halted_run_superseded": verify supersession evidence, then close the run."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit import ask_contract, halted_hold_release, halted_run, halted_withheld_index, kept_work
    from coordinator_core.ops.dispatch_emit.halted_run_landed import _live_successor
    from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

    if repo_root is None:
        return _refuse(f"{OP_NAME} requires repo_root")
    params = params if isinstance(params, dict) else {}
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id or not safe_id(run_id):
        return _refuse(f"{OP_NAME} requires a safe string run_id")
    root = main_worktree_root(Path(repo_root))
    try:
        record = ask_contract.record_dir(root, run_id)
        marker = halted_run.read_strict_run(record)
        withheld = halted_run.halted_withheld_strict(record)
    except halted_run.UnreadableHaltRecord as exc:
        return _refuse(f"{exc.reason}: halt marker unreadable; {run_id} is not closed superseded")
    except ValueError as exc:
        return _refuse(str(exc))
    if marker is None:
        return _refuse(f"{run_id} has no halt record")
    status = marker.get("status")
    if status != halted_run.STATUS_HALTED:
        return _refuse(f"{run_id} is {status}; only a halted run closes superseded")
    successor = _live_successor(root, run_id)
    if successor:
        return _refuse(f"{run_id} has a live successor {successor}; wait for it or stop it")
    if not withheld:
        return _refuse(f"{run_id} withholds no paths; there is nothing to supersede")
    plan = halted_hold_release._kept_plan(record)
    if plan is None:
        if not any((record / f).exists() for f in (kept_work.KEPT_FILE, WITHHELD_PRINTS_FILE)):
            return _legacy_close(root, record, marker, run_id, sorted(withheld), params)
        return _refuse(f"{run_id} kept or withheld-prints file is present but yields no kept blob for a withheld path; supersession cannot be verified")
    kept = plan[1]
    missing = sorted(p for p in withheld if p not in kept)
    if missing:
        return _refuse(f"{run_id} has no kept blob for withheld {missing[0]}; supersession cannot be verified")
    paths = sorted(withheld)
    anchor, why = _halt_anchor(root, run_id)
    if anchor is None:
        return _refuse(why)
    touched = _newest_touch(root, anchor, paths)
    if touched is None:
        return _refuse(f"git log over {anchor[:12]}..HEAD failed; retry")
    n = len(paths)
    ids = _blob_ids(root, [f"HEAD:{p}" for p in paths] + [kept[p] for p in paths] + [f"{anchor}:{p}" for p in paths])
    if ids is None:
        return _refuse("git cat-file failed; retry")
    dispositions, bad = _classify_paths(
        paths, touched, dict(zip(paths, ids[:n])), dict(zip(paths, ids[n:2 * n])), dict(zip(paths, ids[2 * n:])), anchor,
    )
    if bad:
        return _refuse_unclassified(bad)
    why = _refuse_without_supersession(dispositions)
    if why:
        return why
    evidence = {p: d["sha"] for p, d in dispositions.items() if d["disposition"] == halted_run.DISPOSITION_SUPERSEDED}
    if not halted_run.mark_superseded(record, marker, evidence, dispositions):
        return _refuse(f"{run_id} halt marker changed under the write; retry")
    halted_withheld_index.remove_run(root, run_id)
    return {
        "ok": True, "run_id": run_id, "evidence": evidence, "dispositions": dispositions,
        "recorded": (record / halted_run.HALTED_FILE).relative_to(root).as_posix(),
    }
