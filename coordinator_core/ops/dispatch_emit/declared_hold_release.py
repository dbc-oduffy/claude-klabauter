"""Declaration-only hold release: a halted holder none of whose hunks for a path survive loses that hold.

`absent_paths(root, run_id)` names the withheld paths of a halted run whose kept diff over its
`run_base_sha` has no hunk present in HEAD or the worktree. `release_absent(root, paths, ...)` releases
each such hold by moving it from the marker's `run_artifacts.withheld` to `released_paths` and records it under `released_holds` in the
holder's `kept.json`, replacing the manual edit.

Invariants: fail closed. A missing base sha, an unreadable or missing kept blob, a diff past the match
bound, an unreadable marker, or a single present hunk leaves the hold standing. One batched `git
cat-file` per holder, none when no holder withholds a candidate path. Never raises. Trap: a hunk
counts present when its added text is in HEAD or the worktree, or its removed text is gone from either.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

RELEASED_HOLDS_KEY = "released_holds"
RELEASE_REASON = "declaration-only hold: hunks absent from worktree and HEAD"
RELEASE_STATUS = "work lost, restore pending"
RELEASE_BY = "dispatch landing (declared_hold_release)"


def _norm(path: object) -> str:
    text = str(path or "").replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def hunks_all_absent(base: str, kept: str, *texts: str) -> bool:
    """True when no block `kept` adds over or removes from `base` is present in any of `texts`.

    Whitespace-insensitive. False past the diff's match bound (undecidable: the hold stands).
    """
    from coordinator_core.ops.dispatch_emit.halted_hold_release import _squash
    from coordinator_core.ops.dispatch_emit.run_hunk_claims import changed_opcodes, exceeds_match_bound

    base_lines, kept_lines = base.splitlines(), kept.splitlines()
    base_b = [line.encode("utf-8", "replace") for line in base_lines]
    kept_b = [line.encode("utf-8", "replace") for line in kept_lines]
    if exceeds_match_bound(base_b, kept_b):
        return False
    squashed = [_squash(t) for t in texts]
    for tag, i1, i2, j1, j2 in changed_opcodes(base_b, kept_b):
        if tag in ("insert", "replace"):
            added = _squash("\n".join(kept_lines[j1:j2]))
            if added and any(added in s for s in squashed):
                return False
        if tag in ("delete", "replace"):
            removed = _squash("\n".join(base_lines[i1:i2]))
            if removed and any(removed not in s for s in squashed):
                return False
    return True


def _kept_entries(record: Path) -> list:
    """`[(chunk, path, blob)]` of every kept chunk path, in chunk then path order."""
    from coordinator_core.ops.dispatch_emit import kept_work

    out = []
    for chunk, entry in sorted(kept_work.read(record)["chunks"].items()):
        paths = entry.get("paths") if isinstance(entry, dict) else None
        if isinstance(paths, dict):
            out += [
                (chunk, _norm(p), b) for p, b in sorted(paths.items()) if isinstance(b, str) and b
            ]
    return out


def absent_paths(root: Path, run_id: str, candidates: Optional[Iterable[str]] = None) -> list:
    """`[{chunk, path, blob}]` for each withheld path of the halted `run_id` (limited to `candidates`
    when given) whose kept hunks are all absent from HEAD and the worktree; empty on any doubt."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.halted_hold_release import _base_sha, _blob_batch

        root = Path(root)
        record = record_dir(root, run_id)
        marker = halted_run._read(record)
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            return []
        base_sha = _base_sha(marker)
        if not base_sha:
            return []
        withheld = {_norm(p) for p in halted_run.halted_withheld_strict(record)}
        if candidates is not None:
            withheld &= {_norm(p) for p in candidates}
        entries = [e for e in _kept_entries(record) if e[1] in withheld]
        by_path: dict = {}
        for chunk, path, blob in entries:
            by_path.setdefault(path, []).append((chunk, blob))
        if not by_path:
            return []
        specs = [f"HEAD:{p}" for p in by_path] + [f"{base_sha}:{p}" for p in by_path]
        specs += [b for blobs in by_path.values() for _c, b in blobs]
        blobs = _blob_batch(root, list(dict.fromkeys(specs)))
        if blobs is None:
            return []
        out = []
        for path, kept_blobs in by_path.items():
            if _worktree_unreadable(root, path):
                continue
            head = (blobs.get(f"HEAD:{path}") or b"").decode("utf-8", "replace")
            base = (blobs.get(f"{base_sha}:{path}") or b"").decode("utf-8", "replace")
            work = _worktree_text(root, path)
            if all(
                blobs.get(b) is not None
                and hunks_all_absent(base, blobs[b].decode("utf-8", "replace"), head, work)
                for _c, b in kept_blobs
            ):
                out += [{"chunk": c, "path": path, "blob": b} for c, b in kept_blobs]
        return out
    except Exception:  # noqa: BLE001 -- unreadable evidence holds
        return []


def _worktree_unreadable(root: Path, path: str) -> bool:
    target = root / path
    return target.exists() and not target.is_file()


def _worktree_text(root: Path, path: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _record_released(record: Path, released: list, now: str) -> None:
    """Move `released` entries out of `kept.json` chunks into a `released_holds` record; keeps other keys."""
    from coordinator_core.ops.dispatch_emit.kept_work import KEPT_FILE
    from coordinator_core.session.claimed_write import replace_text

    target = record / KEPT_FILE
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    chunks = doc.get("chunks") if isinstance(doc.get("chunks"), dict) else {}
    for item in released:
        entry = chunks.get(item["chunk"])
        paths = entry.get("paths") if isinstance(entry, dict) else None
        if isinstance(paths, dict):
            for key in [k for k in paths if _norm(k) == item["path"]]:
                del paths[key]
            if not paths and "return" not in entry:
                del chunks[item["chunk"]]
    doc["chunks"] = chunks
    doc.setdefault("plan_gate", None)
    log = doc.get(RELEASED_HOLDS_KEY) if isinstance(doc.get(RELEASED_HOLDS_KEY), list) else []
    log.append({
        "at": now, "status": RELEASE_STATUS, "reason": RELEASE_REASON, "by": RELEASE_BY,
        "paths": [dict(item) for item in released],
    })
    doc[RELEASED_HOLDS_KEY] = log
    replace_text(target, json.dumps(doc, sort_keys=True, indent=2) + "\n")


def _release_path(root: Path, run_id: str, path: str, reason: str) -> bool:
    """Move `path` from halted `run_id`'s `run_artifacts.withheld` to the marker's `released_paths`;
    True once released (a repeat release is False). Never raises: any failure leaves the hold standing."""
    try:
        from coordinator_core.ops.dispatch_emit import halted_run
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.halted_hold_release import RELEASED_PATHS_FIELD

        rdir = record_dir(Path(root), run_id)
        marker = halted_run.read_strict(rdir)
        if not marker or marker.get("status") != halted_run.STATUS_HALTED:
            return False
        rel = _norm(path)
        released = marker.get(RELEASED_PATHS_FIELD)
        released = dict(released) if isinstance(released, dict) else {}
        artifacts = marker.get("run_artifacts") if isinstance(marker.get("run_artifacts"), dict) else {}
        withheld = artifacts.get("withheld") if isinstance(artifacts.get("withheld"), dict) else {}
        keys = [k for k in withheld if _norm(k) == rel]
        if rel in released or not keys:
            return False
        released[rel] = {"reason": reason, "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        marker["run_artifacts"] = {**artifacts, "withheld": {k: v for k, v in withheld.items() if k not in keys}}
        marker[RELEASED_PATHS_FIELD] = released
        halted_run._write(rdir, marker)
        return True
    except Exception:  # noqa: BLE001
        return False


def release_run(root: Path, run_id: str, candidates: Optional[Iterable[str]] = None) -> list:
    """Release `run_id`'s declaration-only holds and record them; the sorted released paths. Never raises."""
    try:
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        root = Path(root)
        found = absent_paths(root, run_id, candidates)
        done = []
        for path in sorted({f["path"] for f in found}):
            if _release_path(root, run_id, path, RELEASE_REASON):
                done.append(path)
        if done:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                _record_released(record_dir(root, run_id), [f for f in found if f["path"] in done], now)
            except Exception:  # noqa: BLE001 -- the release already landed; report it, the log is best-effort
                pass
        return done
    except Exception:  # noqa: BLE001 -- release unknown: every hold stands
        return []


def release_absent(root: Path, paths: Iterable[str], *, own_run_id: Optional[str] = None) -> dict:
    """Release every other halted run's declaration-only hold on `paths`; `{run_id: [released paths]}`.

    Reads the halted-withheld index (no spawn when no other run withholds a candidate). Never raises.
    """
    try:
        from coordinator_core.ops.dispatch_emit import halted_run, halted_withheld_index
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

        root = Path(root)
        wanted = {_norm(p) for p in paths}
        out: dict = {}
        halted_withheld_index.withheld_by_path(root)  # builds the index once; `run_ids` reads it bare
        for run_id in halted_withheld_index.run_ids(root):
            if run_id == own_run_id:
                continue
            if not wanted & {_norm(p) for p in halted_run.halted_withheld(record_dir(root, run_id))}:
                continue
            done = release_run(root, run_id, wanted)
            if done:
                out[run_id] = done
        return out
    except Exception:  # noqa: BLE001
        return {}
