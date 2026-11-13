"""
coordinator_core.ops.ceremony.update_docs_worklist -- JSON-RPC
"ceremony.update_docs_worklist": the incremental /update-docs scan.

Purpose: emit the mechanical half of /update-docs (source drift since the last
acknowledged run, the Phase 8 lineage backstop, the Phase 8b prune
classification) as `decided_by_op`, and only the items an engine cannot decide
as `agent_worklist`. Read-only apart from its own derived cache.

Holds the DR-344 500ms process-time bar at default params with at most ONE git
spawn: an (mtime_ns, size) cache under `os.scandir` re-reads a frontmatter only
when its file changed, drift is one scoped `git diff <acked sha>` against the
working tree, and output is capped. A cold cache re-reads under a process-time
budget (`REREAD_BUDGET_SECS`); a file not yet scanned is never reported prunable.

Does NOT port `compute_curation_status`, the `archive/specs` walk, the `rglob`
source inventory or the project-tracker reconcile of the deleted
`ceremony.update_docs_scan`.

Self-registration: importing this module registers the op; `ops/__init__.py`
imports it at start_server() time.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.lifecycle_constants import SPEC_RIPE_STATUSES, SPEC_SKIP_STATUSES
from coordinator_core.machine_resolver import registry_get
from coordinator_core.memo_corpus import memo_corpus_root

SCHEMA_VERSION = 1

CACHE_KEY = "paths.update_docs_worklist_cache"
"""Settings-home key for the cache root. Unset: the cache lives under the repo's
gitignored `.coordinator-local/cache/` (derived state, rebuildable)."""

CACHE_SUBDIR = "update-docs-worklist"
CACHE_FILE = "state.json"

SOURCE_ROOTS = ("coordinator_core", "coordinator/bin")
DIRECTORY_MD = "coordinator_core/DIRECTORY.md"

PLANS_PRUNE_AGE_DAYS = 14
CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS = 90
TASKS_STATUS_SUPERSEDED = "superseded"

CHANGED_PATH_CAP = 200
PRUNE_ROWS_CAP = 50
WORKLIST_PATHS_CAP = 40
OUTPUT_BYTE_CAP = 65536

REREAD_BUDGET_SECS = 0.25
"""Process time a call may spend re-reading frontmatter before it stops, persists
what it read and reports `cache.complete: false`. Keeps a cold first run under
the 500ms bar."""

HEAD_BYTES = 4096

_CLOSE = re.compile(r"^---[ \t]*$", re.M)
_KEYS = re.compile(r"^(status|supersedes|picked_up_at):(?=[ \t]|$)[ \t]*(.*?)[ \t]*$", re.M)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DAY_NS = 86400e9


def _frontmatter_fields(path: str) -> tuple[str, str, str]:
    """(status, supersedes, picked_up_at) of one file; empty strings when absent."""
    with open(path, "rb") as fh:
        raw = fh.read(HEAD_BYTES)
        if len(raw) == HEAD_BYTES and b"\n---" not in raw[4:]:
            raw += fh.read()
    text = raw.decode("utf-8", "replace")
    if text.startswith("---\n") and "\r" not in text:
        close = _CLOSE.search(text, 4)
        if close:
            vals = {"status": "", "supersedes": "", "picked_up_at": ""}
            seen: set[str] = set()
            for m in _KEYS.finditer(text, 4, close.start()):
                if m.group(1) not in seen:
                    seen.add(m.group(1))
                    vals[m.group(1)] = m.group(2).strip().strip('"').strip("'")
            return vals["status"], vals["supersedes"], vals["picked_up_at"]
    split = split_frontmatter(text)
    if split is None:
        return "", "", ""
    out = []
    for key in ("status", "supersedes", "picked_up_at"):
        value = read_fm_field(split.fm_text, key)
        out.append(value.strip().strip('"').strip("'") if value is not None else "")
    return out[0], out[1], out[2]


def _cache_path(worktree_root: Path) -> Path:
    base = registry_get(CACHE_KEY)
    if not base:
        base = str(worktree_root / ".coordinator-local" / "cache")
    return Path(base) / CACHE_SUBDIR / CACHE_FILE


def _load_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_cache(path: Path, cache: dict) -> bool:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def _git_head(root: Path) -> Optional[str]:
    res = run_git(["rev-parse", "HEAD"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    head = res.stdout.strip() if res.ok else ""
    return head if _SHA_RE.match(head) else None


def _git_drift(root: Path, ack_head: Optional[str]) -> tuple[Optional[str], list[str]]:
    """(baseline sha, changed source paths). One spawn: a scoped tree diff of the acked
    sha against the working tree, so committed and dirty changes both appear. With no
    acked sha, or one git no longer knows, the one spawn re-seeds the baseline and reports
    no drift. Untracked files are not drift here: `git diff` omits them until committed."""
    if not ack_head or not _SHA_RE.match(ack_head):
        return _git_head(root), []
    res = run_git(
        ["diff", "--name-only", "-z", "--no-renames", ack_head, "--", *SOURCE_ROOTS],
        cwd=str(root),
        timeout=CEREMONY_BUDGET_SECS,
    )
    if not res.ok:
        return _git_head(root), []
    return ack_head, sorted(t for t in res.stdout.split("\0") if t)


def _scan_dir(
    abs_dir: Path,
    rel_dir: str,
    cached: dict,
    out: dict,
    started: float,
    stats: dict,
) -> None:
    """stat-only walk of one flat directory; re-read a frontmatter only when
    (mtime_ns, size) differ, and only while the re-read budget lasts."""
    if not abs_dir.is_dir():
        return
    with os.scandir(abs_dir) as it:
        for entry in it:
            if not entry.name.endswith(".md") or not entry.is_file():
                continue
            st = entry.stat()
            key = f"{rel_dir}/{entry.name}"
            rec = cached.get(key)
            if rec and rec[0] == st.st_mtime_ns and rec[1] == st.st_size:
                out[key] = rec
                continue
            if time.process_time() - started > REREAD_BUDGET_SECS:
                stats["complete"] = False
                continue
            try:
                out[key] = [st.st_mtime_ns, st.st_size, *_frontmatter_fields(entry.path)]
            except OSError:
                continue
            stats["reread"] += 1


def _harvested_paths(root: Path, cache: dict) -> set[str]:
    """Paths the distillation log records as harvested, re-parsed only when the
    log's (mtime_ns, size) change."""
    log = root / "state" / "distillation-log.md"
    try:
        st = log.stat()
    except OSError:
        return set()
    sig = [st.st_mtime_ns, st.st_size]
    if cache.get("log_sig") == sig:
        return set(cache.get("harvested", []))
    from coordinator_core.distill._common import parse_distillation_log
    from coordinator_core.distill.harvest_debt import HARVESTED_DISPOSITIONS

    try:
        rows = parse_distillation_log(log.read_text(encoding="utf-8"))
    except OSError:
        return set()
    harvested = sorted({Path(r.path).as_posix() for r in rows if r.disposition in HARVESTED_DISPOSITIONS})
    cache["log_sig"], cache["harvested"] = sig, harvested
    return set(harvested)


def _classify(
    files: dict[str, list],
    arch_rel: str,
    harvested: set[str],
    now: _dt.datetime,
) -> tuple[dict[str, list[str]], dict[str, int], set[str], list[str]]:
    """(prune rows per cohort, totals, lineage-blocked predecessors, held plans)."""
    now_ns = now.timestamp() * 1e9
    blocked: set[str] = set()
    for key, rec in files.items():
        if key.startswith("docs/plans/") and rec[3] and rec[2] not in ("abandoned", "closed"):
            blocked.add(Path(rec[3]).as_posix())

    prune: dict[str, list[str]] = {"plans": [], "crossrepo_archive": [], "tasks": []}
    held: list[str] = []
    arch_prefix = arch_rel + "/"
    for key, (mtime_ns, _size, status, _sup, picked) in files.items():
        if key.startswith("docs/plans/"):
            if status in SPEC_SKIP_STATUSES and (now_ns - mtime_ns) / _DAY_NS >= PLANS_PRUNE_AGE_DAYS:
                if key in blocked:
                    held.append(key)
                elif status in SPEC_RIPE_STATUSES and key not in harvested:
                    continue
                else:
                    prune["plans"].append(key)
        elif key.startswith(arch_prefix):
            if status == "actioned" and picked:
                try:
                    when = _dt.datetime.fromisoformat(picked.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=_dt.timezone.utc)
                if (now - when).total_seconds() / 86400.0 >= CROSSREPO_ARCHIVE_ACTIONED_FLOOR_DAYS:
                    prune["crossrepo_archive"].append(key)
        elif status == TASKS_STATUS_SUPERSEDED:
            prune["tasks"].append(key)
    totals = {cohort: len(rows) for cohort, rows in prune.items()}
    return {c: sorted(rows) for c, rows in prune.items()}, totals, blocked, sorted(held)


def _bound_output(out: dict) -> dict:
    """Halve the capped lists until the serialized result fits OUTPUT_BYTE_CAP."""
    while len(json.dumps(out, separators=(",", ":"))) > OUTPUT_BYTE_CAP:
        changed = out["changed_since_ack"]
        lists = [changed["paths"], *out["decided_by_op"]["prune"].values(), *(w["paths"] for w in out["agent_worklist"])]
        longest = max(lists, key=len)
        if not longest:
            break
        del longest[len(longest) // 2:]
        if longest is changed["paths"]:
            changed["truncated"] = True
    return out


@register_op("ceremony.update_docs_worklist")
def _ceremony_update_docs_worklist(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.update_docs_worklist" handler.

    Params: `ack` (bool, default false) advances the cached baseline to HEAD at the cost
    of one more git spawn; a default call never advances it, so an agent run that dies
    loses nothing. The one exception: a first call, or a cached sha git no longer knows,
    re-seeds the baseline to HEAD and reports no drift.

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree; the handler scans the main worktree.

    Returns {schema_version, ack_head, cache{state, entries, reread, complete},
    changed_since_ack, decided_by_op, agent_worklist}. Writes only its own cache file.
    """
    if repo_root is None:
        raise ValueError(
            "ceremony.update_docs_worklist requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    started = time.process_time()
    root = main_worktree_root(repo_root)
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    ack = bool((params or {}).get("ack", False))

    cache_path = _cache_path(root)
    cache = _load_cache(cache_path)
    head, changed = _git_drift(root, cache.get("ack_head"))
    if ack:
        head = _git_head(root) or head
    baseline_moved = bool(head) and head != cache.get("ack_head")
    if baseline_moved:
        cache["ack_head"] = head

    old_files = cache.get("files", {})
    files: dict[str, list] = {}
    stats = {"reread": 0, "complete": True}
    _scan_dir(root / "docs" / "plans", "docs/plans", old_files, files, started, stats)
    archive = Path(memo_corpus_root(str(root))) / "archive"
    arch_rel = archive.relative_to(root).as_posix()
    _scan_dir(archive, arch_rel, old_files, files, started, stats)
    _scan_dir(root / "tasks", "tasks", old_files, files, started, stats)

    harvested = _harvested_paths(root, cache)
    prune, totals, blocked, held = _classify(files, arch_rel, harvested, now)

    src_changed = [p for p in changed if p.endswith(".py") and p.startswith("coordinator_core/")]
    worklist: list[dict[str, Any]] = []
    if src_changed:
        worklist.append({
            "kind": "doc_drift_review",
            "why": "source changed since the last ack",
            "total": len(src_changed),
            "paths": src_changed[:WORKLIST_PATHS_CAP],
        })
    if held:
        worklist.append({
            "kind": "lineage_held_disposed_plan",
            "why": "disposed but a live successor supersedes-names it",
            "total": len(held),
            "paths": held[:WORKLIST_PATHS_CAP],
        })

    out = {
        "schema_version": SCHEMA_VERSION,
        "ack_head": head,
        "cache": {
            "state": "warm" if old_files else "cold",
            "entries": len(files),
            "reread": stats["reread"],
            "complete": stats["complete"],
        },
        "changed_since_ack": {
            "total": len(changed),
            "paths": changed[:CHANGED_PATH_CAP],
            "truncated": len(changed) > CHANGED_PATH_CAP,
        },
        "decided_by_op": {
            "directory_md_stale": bool(src_changed) and DIRECTORY_MD not in changed,
            "lineage_blocked": len(blocked),
            "prune_totals": totals,
            "prune": {c: rows[:PRUNE_ROWS_CAP] for c, rows in prune.items()},
        },
        "agent_worklist": worklist,
    }

    cache["files"] = files
    if stats["reread"] or baseline_moved:
        if not _store_cache(cache_path, cache):
            out["cache"]["state"] = "unwritable"
    return _bound_output(out)
