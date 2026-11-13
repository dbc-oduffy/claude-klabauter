"""
coordinator_core.ops.ceremony.update_docs_worklist -- JSON-RPC
"ceremony.update_docs_worklist": the incremental /update-docs scan.

Purpose: emit the mechanical half of /update-docs (source drift since the last
acknowledged run, the Phase 8 lineage backstop, the Phase 8b prune
classification including archived handoffs) as `decided_by_op`, only the items an
engine cannot decide as `agent_worklist`, and a per-phase `phase_verdicts` map
(`agent` | `op` | `noop`) over every /update-docs phase. Read-only apart from its
own derived cache. A retired-term hit (a doc line naming a term the repo's
`docs/retired-terms.yaml` declares removed) is an `agent_worklist` item of kind
`retired_term_hit`: `paths` are `file:line term (removed DATE[: note])`, `hits` the
structured records. A malformed manifest is a `retired_terms_manifest_invalid` item.

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
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.distill.prompt_tier import split_to_tier
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.lifecycle_constants import SPEC_RIPE_STATUSES, SPEC_SKIP_STATUSES
from coordinator_core.machine_resolver import registry_get
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.session.output_tree_layout import is_month_dir
from coordinator_core.session import record_homes
from coordinator_core.updatedocs.retired_terms import RetiredTermsManifestError, scan_retired_terms_report

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

ARCHIVED_HANDOFFS_KEEP_NEWEST = 10
"""Newest archived handoffs (by filename order) kept unconditionally."""

HANDOFF_EXEMPT_KINDS = frozenset({"roadmap-baton", "spinoff-roadmap"})

BLOCKED_PASS_START_SECS = 0.12
"""Process time after which the one corpus-wide `blocked_by_dependents_many` pass is not
started; every unclassified handoff is then `indeterminate`, never prunable."""

LESSONS_OUTBOX = "state/lessons-outbox"

AGENT = "agent"
OP = "op"
NOOP = "noop"

UPDATE_DOCS_PHASE_IDS = (
    "0", "1-2b", "3", "4", "5", "6", "7", "8", "8b", "9", "9b", "10", "10b", "11", "11a", "11b",
    "11g", "11c", "11d", "12", "11f", "11h", "11h2", "11i", "11j", "11k", "11m", "11n", "11o",
    "11p", "13", "14", "15",
)
"""The /update-docs phase table (coordinator-content-repo coordinator/commands/update-docs.md, 33 rows), one
id per row in table order, `1-2b` ASCII-hyphenated, owner suffixes dropped."""

RETIRED_TERM_KIND = "retired_term_hit"
RETIRED_MANIFEST_KIND = "retired_terms_manifest_invalid"

_DRIFT_KINDS = frozenset({"doc_drift_review", RETIRED_TERM_KIND, RETIRED_MANIFEST_KIND})
_DRIFT_PHASES = {"1-2b": _DRIFT_KINDS, "3": _DRIFT_KINDS, "7": _DRIFT_KINDS}
_LINEAGE_PHASES = {"8": "lineage_held_disposed_plan"}
_NOOP_PHASES = frozenset({"4", "5", "10b", "11b"})
_SIBLING_PHASES = frozenset({"11a", "12", "13"})
_LESSONS_PHASE = "6"

_CLOSE = re.compile(r"^---[ \t]*$", re.M)
_KEYS = re.compile(r"^(status|supersedes|picked_up_at):(?=[ \t]|$)[ \t]*(.*?)[ \t]*$", re.M)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_KIND_RE = re.compile(r"^kind:[ \t]*(.*?)[ \t]*$", re.M)
_ANCESTRY_RE = re.compile(
    r"^(?:predecessor_handoff|predecessor|additional_predecessors|forked_from|continued_into):"
    r"[^\n]*(?:\n[ \t]+[^\n]*)*",
    re.M,
)
_MD_NAME_RE = re.compile(r"[A-Za-z0-9._\-]+\.md")
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


def _handoff_fields(path: str, want_refs: bool) -> tuple[str, int, list[str]]:
    """(kind, has_blocked_by, ancestry basenames) of one handoff or plan. `kind` and the
    ancestry names come from the frontmatter only; `has_blocked_by` is 1 when the key text
    appears anywhere in it, an over-approximation that only widens the cache signature."""
    with open(path, "rb") as fh:
        raw = fh.read(HEAD_BYTES)
        if len(raw) == HEAD_BYTES and b"\n---" not in raw[4:]:
            raw += fh.read()
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n")
    if text.startswith("---\n"):
        close = _CLOSE.search(text, 4)
        text = text[4:close.start()] if close else text[4:]
    else:
        text = ""
    m = _KIND_RE.search(text)
    kind = m.group(1).strip().strip('"').strip("'") if m else ""
    refs: list[str] = []
    if want_refs:
        refs = sorted({n for blk in _ANCESTRY_RE.finditer(text) for n in _MD_NAME_RE.findall(blk.group(0))})
    return kind, int("blocked_by" in text), refs


def _walk_md(abs_dir: Path, *, recurse: bool, month_dirs_only: bool = False) -> list[tuple[str, str, int, int]]:
    """(rel-to-abs_dir path, abs path, mtime_ns, size) for every `.md` under abs_dir; stat-only."""
    out: list[tuple[str, str, int, int]] = []
    if not abs_dir.is_dir():
        return out
    stack = [(str(abs_dir), "")]
    while stack:
        cur, rel = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    if entry.name.endswith(".md") and entry.is_file():
                        st = entry.stat()
                        out.append((rel + entry.name, entry.path, st.st_mtime_ns, st.st_size))
                    elif recurse and entry.is_dir(follow_symlinks=False) and (not month_dirs_only or is_month_dir(entry.name)):
                        stack.append((entry.path, rel + entry.name + "/"))
        except OSError:
            continue
    return out


def _scan_handoff_files(
    root: Path, cached: dict, started: float, stats: dict
) -> tuple[dict[str, list], bool]:
    """Per-file [mtime_ns, size, kind, has_blocked_by, ancestry names] for archived handoffs
    and archive/completed (ancestry names not read), live handoffs and plans, keyed by
    repo-relative path. Re-reads only changed files while the process-time budget lasts;
    the bool is False when any file was left unread."""
    complete = True
    out: dict[str, list] = {}
    sources = (
        ("archive/handoffs", True, False, False),
        ("archive/completed", True, False, False),
        (record_homes.home_rel("handoffs"), True, True, True),
        ("docs/plans", False, False, True),
    )
    for rel_dir, recurse, month_only, want_refs in sources:
        for sub, abs_path, mtime_ns, size in _walk_md(root / rel_dir, recurse=recurse, month_dirs_only=month_only):
            key = f"{rel_dir}/{sub}"
            rec = cached.get(key)
            if rec and rec[0] == mtime_ns and rec[1] == size:
                out[key] = rec
                continue
            if time.process_time() - started > REREAD_BUDGET_SECS:
                complete = False
                stats["complete"] = False
                continue
            try:
                out[key] = [mtime_ns, size, *_handoff_fields(abs_path, want_refs)]
            except OSError:
                complete = False
                continue
            stats["handoff_reread"] += 1
    return out, complete


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
    """(prune rows per cohort, totals, lineage-blocked predecessors, held plans). The
    `archived_handoffs` cohort is empty here; `_classify_archived_handoffs` fills it."""
    now_ns = now.timestamp() * 1e9
    blocked: set[str] = set()
    for key, rec in files.items():
        if key.startswith("docs/plans/") and rec[3] and rec[2] not in ("abandoned", "closed"):
            blocked.add(Path(rec[3]).as_posix())

    prune: dict[str, list[str]] = {"plans": [], "crossrepo_archive": [], "tasks": [], "archived_handoffs": []}
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


def _classify_archived_handoffs(
    root: Path,
    hfiles: dict[str, list],
    scan_complete: bool,
    cache: dict,
    started: float,
    stats: dict,
) -> tuple[list[str], dict[str, int]]:
    """(prune rows, {total, prune, keep, indeterminate}) over every archive/handoffs file.

    KEEP: the newest `ARCHIVED_HANDOFFS_KEEP_NEWEST` by filename, a roadmap-exempt `kind`, a
    basename named by a live plan or handoff ancestry field, or a live `blocked_by` dependent.
    PRUNE only when the one corpus pass answered `none`; a file never read, a pass not run
    and any `indeterminate` reply are all `indeterminate`, never prunable."""
    arch = "archive/handoffs/"
    names = sorted((k for k in hfiles if k.startswith(arch)), key=lambda k: (Path(k).name, k), reverse=True)
    counts = {"total": len(names), "prune": 0, "keep": 0, "indeterminate": 0}
    referenced = {n for k, rec in hfiles.items() if k.startswith((record_homes.home_rel("handoffs") + "/", "docs/plans/")) for n in rec[4]}
    candidates: list[str] = []
    for i, key in enumerate(names):
        rec = hfiles[key]
        if i < ARCHIVED_HANDOFFS_KEEP_NEWEST or rec[2] in HANDOFF_EXEMPT_KINDS or Path(key).name in referenced:
            counts["keep"] += 1
        else:
            candidates.append(key)

    states: dict[str, str] = {}
    if candidates and scan_complete:
        holders = sorted(k for k, rec in hfiles.items() if rec[3] and not k.startswith("docs/plans/"))
        sig = hashlib.blake2b(
            repr([(k, hfiles[k][0], hfiles[k][1]) for k in sorted(set(holders) | set(names))]).encode(),
            digest_size=16,
        ).hexdigest()
        memo = cache.get("blocked_by")
        if isinstance(memo, dict) and memo.get("sig") == sig:
            states = memo.get("states", {})
        elif time.process_time() - started <= BLOCKED_PASS_START_SECS:
            from coordinator_core.ops.handoff_children import blocked_by_dependents_many

            replies = blocked_by_dependents_many([str(root / k) for k in candidates], root)
            states = {k: (replies.get(str(root / k)) or {}).get("state", "indeterminate") for k in candidates}
            cache["blocked_by"] = {"sig": sig, "states": states}
            stats["handoff_reread"] += 1
    prune_rows: list[str] = []
    for key in candidates:
        state = states.get(key, "indeterminate")
        if state == "none":
            prune_rows.append(key)
            counts["prune"] += 1
        elif state == "dependents":
            counts["keep"] += 1
        else:
            counts["indeterminate"] += 1
    return sorted(prune_rows), counts


def _phase_verdicts(changed_total: int, worklist: list[dict[str, Any]], outbox_pending: bool) -> dict[str, str]:
    """`agent` only for a phase holding an agent_worklist item; `op` for a phase an op, probe
    or script runs; `noop` when nothing is owed. A phase a sibling run owns is never `agent`."""
    kinds = {w["kind"] for w in worklist}
    clean = changed_total == 0
    verdicts: dict[str, str] = {}
    for pid in UPDATE_DOCS_PHASE_IDS:
        if pid in _DRIFT_PHASES:
            verdicts[pid] = AGENT if _DRIFT_PHASES[pid] & kinds else (NOOP if clean else OP)
        elif pid in _LINEAGE_PHASES:
            verdicts[pid] = AGENT if _LINEAGE_PHASES[pid] in kinds else OP
        elif pid == _LESSONS_PHASE:
            verdicts[pid] = OP if outbox_pending else NOOP
        elif pid in _NOOP_PHASES:
            verdicts[pid] = NOOP
        elif pid in _SIBLING_PHASES:
            verdicts[pid] = NOOP if clean else OP
        else:
            verdicts[pid] = OP
    return verdicts


def _outbox_pending(root: Path) -> bool:
    try:
        with os.scandir(root / LESSONS_OUTBOX) as it:
            return any(e.name.endswith((".yaml", ".yml")) for e in it)
    except OSError:
        return False


def _bound_phase_paths(worklist: list[dict[str, Any]]) -> None:
    """Keep only the first tier-sized group of each worklist row's named paths (the input a
    per-phase agent reads); a cut row gains `paths_truncated` and its `hits` follow `paths`."""
    for w in worklist:
        groups = split_to_tier(w["paths"], lambda p: len(p.encode("utf-8")) + 3)
        if len(groups) > 1:
            w["paths"] = groups[0]
            w["paths_truncated"] = True
            if "hits" in w:
                w["hits"] = w["hits"][: len(w["paths"])]


def _bound_output(out: dict) -> dict:
    """Bound each worklist row's paths to the long-prompt tier, then halve the capped lists
    until the serialized result fits OUTPUT_BYTE_CAP."""
    _bound_phase_paths(out["agent_worklist"])
    while len(json.dumps(out, separators=(",", ":"))) > OUTPUT_BYTE_CAP:
        changed = out["changed_since_ack"]
        lists = [changed["paths"], *out["decided_by_op"]["prune"].values(), *(w["paths"] for w in out["agent_worklist"])]
        longest = max(lists, key=len)
        if not longest:
            break
        del longest[len(longest) // 2:]
        if longest is changed["paths"]:
            changed["truncated"] = True
    for w in out["agent_worklist"]:
        if "hits" in w:
            w["hits"] = w["hits"][: len(w["paths"])]
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
    changed_since_ack, decided_by_op, agent_worklist, phase_verdicts}. Writes only its own
    cache file. `decided_by_op.archived_handoffs` holds the uncapped prune/keep/indeterminate
    counts (summing to `total`); `prune.archived_handoffs` lists the prune rows, capped.
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
    stats = {"reread": 0, "handoff_reread": 0, "complete": True}
    _scan_dir(root / "docs" / "plans", "docs/plans", old_files, files, started, stats)
    archive = Path(memo_corpus_root(str(root))) / "archive"
    arch_rel = archive.relative_to(root).as_posix()
    _scan_dir(archive, arch_rel, old_files, files, started, stats)
    _scan_dir(root / "tasks", "tasks", old_files, files, started, stats)

    harvested = _harvested_paths(root, cache)
    prune, totals, blocked, held = _classify(files, arch_rel, harvested, now)
    hfiles, h_complete = _scan_handoff_files(root, cache.get("hfiles", {}), started, stats)
    prune["archived_handoffs"], handoff_counts = _classify_archived_handoffs(
        root, hfiles, h_complete, cache, started, stats
    )
    totals["archived_handoffs"] = len(prune["archived_handoffs"])

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

    try:
        retired, retired_complete = scan_retired_terms_report(root)
    except RetiredTermsManifestError as exc:
        worklist.append({
            "kind": RETIRED_MANIFEST_KIND,
            "why": str(exc),
            "total": 1,
            "paths": [],
        })
    else:
        if retired:
            worklist.append({
                "kind": RETIRED_TERM_KIND,
                "why": "doc line names a term its repo's retired-terms manifest declares removed"
                + ("" if retired_complete else " (scan cut at its time budget: more docs unscanned)"),
                "complete": retired_complete,
                "total": len(retired),
                "paths": [
                    f"{h['file']}:{h['line']} {h['term']} (removed {h['removed']})"
                    + (f": {h['note']}" if h["note"] else "")
                    for h in retired[:WORKLIST_PATHS_CAP]
                ],
                "hits": retired[:WORKLIST_PATHS_CAP],
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
            "archived_handoffs": handoff_counts,
            "prune": {c: rows[:PRUNE_ROWS_CAP] for c, rows in prune.items()},
        },
        "agent_worklist": worklist,
        "phase_verdicts": _phase_verdicts(len(changed), worklist, _outbox_pending(root)),
    }

    cache["files"] = files
    cache["hfiles"] = hfiles
    if stats["reread"] or stats["handoff_reread"] or baseline_moved:
        if not _store_cache(cache_path, cache):
            out["cache"]["state"] = "unwritable"
    return _bound_output(out)
