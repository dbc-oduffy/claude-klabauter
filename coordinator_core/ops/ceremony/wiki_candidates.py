"""
coordinator_core.ops.ceremony.wiki_candidates -- JSON-RPC
"ceremony.wiki_candidates": which wiki pages a /update-docs run hands a model.

Purpose: one deterministic, read-only query (no model call) that joins the plans in the
plan-completion ledger window to the wiki pages citing their spine `writes:` paths, ranks pages
by the highest contributing sizing tshirt, and previews each plan from its ledger `plan_blob`.
Contract: coordinator-content-repo `coordinator/docs/contracts/wiki-update-candidates.md` (reply schema
`wiki-update-candidates`, version 1).

Window, by ledger `completed_at`: `since` param (ISO date/datetime, or a sha that is a
`completing_sha` in the ledger: rows after that row), else the worklist `ack_head` mapped the same
way, else the last `DEFAULT_WINDOW_DAYS` days. A sha the ledger does not know falls back to the
day window and says so in `window.source`. A plan absent from the ledger is a missing row, fixed by
backfill; no history is walked. With no ledger file at all, completed plans in the window come from
one `ls-tree` over docs/plans instead (`_scan_completed_plans`); `ledger_missing` then means the
scan found none.

Spawns, constant in plan count: rev-parse and one `cat-file --batch` over the plan blobs (plus one
`ls-tree` when the ledger file is absent); the wiki pages are read in-process.

Self-registration: importing this module registers the op; `ops/__init__.py` imports it at
start_server() time.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core import plan_completion_ledger as _ledger
from coordinator_core.git.run import run_git
from coordinator_core.ipc import CEREMONY_BUDGET_SECS, register_op
from coordinator_core.lifecycle import git_common_dir, main_worktree_root
from coordinator_core.ops.ceremony import update_docs_worklist as _worklist

SCHEMA = "wiki-update-candidates"
SCHEMA_VERSION = 1
PREVIEW_CHARS = 10000
PAGES_CAP = 10
PLANS_CAP = 20
DEFAULT_WINDOW_DAYS = _ledger.DEFAULT_WINDOW_DAYS
COMPLETED_STATUSES = _ledger.COMPLETED_STATUSES
# A touched path cited by more wiki pages than this is a hub (doctrine, hooks.json, shared
# skills): it matches nearly every plan and would spend the caps on noise. Narrow citations on
# the real corpus sit at 1-3 pages; hubs sit at 20+, so 3 separates the two.
HUB_CITING_PAGES = 3

WIKI_DIRS = ("coordinator/docs/wiki", "docs/wiki")

SOURCE_ACK = "ack_head"
SOURCE_SINCE = "since-param"
SOURCE_DEFAULT = "default-7d"

REASON_LEDGER_MISSING = "ledger_missing"
REASON_LEDGER_EMPTY = "ledger_empty_in_window"
REASON_NO_CITING_PAGE = "no_citing_page"
REASON_HUB_CUT = "hub_cut"

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_EPOCH = "1970-01-01"


def _rank(tshirt: Optional[str]) -> int:
    return _ledger.TSHIRT_RANK.get(tshirt or "", 0)


def _after_sha(rows: List[Dict[str, Any]], sha: str) -> Optional[List[Dict[str, Any]]]:
    """Rows after the last ledger row whose `completing_sha` starts with `sha`; None when none does."""
    sha = sha.lower()
    for i in range(len(rows) - 1, -1, -1):
        if str(rows[i].get("completing_sha", "")).lower().startswith(sha):
            return rows[i + 1 :]
    return None


def _window(root: Path, since: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    """(rows in the window, window.since, window.source)."""
    ack, source = None, SOURCE_DEFAULT
    if since and _SHA_RE.match(since):
        ack, source = since, SOURCE_SINCE
    elif since:
        try:
            return _ledger.read_window(root, since), since, SOURCE_SINCE
        except ValueError:
            pass
    else:
        cached = _worklist._load_cache(_worklist._cache_path(root)).get("ack_head")
        if isinstance(cached, str) and _SHA_RE.match(cached):
            ack, source = cached, SOURCE_ACK
    if ack:
        rows = _after_sha(_ledger.read_window(root, _EPOCH), ack)
        if rows is not None:
            return rows, ack, source
    return _ledger.read_window(root), None, SOURCE_DEFAULT


def _read_blobs(root: Path, blobs: List[str]) -> List[str]:
    """Text of each blob id via one `cat-file --batch`; "" when unreadable."""
    if not blobs:
        return []
    res = run_git(
        ["cat-file", "--batch"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS,
        input="".join(f"{b}\n" for b in blobs).encode("ascii", errors="replace"),
    )
    data, pos = res.stdout_bytes, 0
    texts: List[str] = []
    for _ in blobs:
        eol = data.find(b"\n", pos)
        if eol < 0:
            texts.append("")
            continue
        header = data[pos:eol].split()
        pos = eol + 1
        if len(header) == 3 and header[1] == b"blob":
            size = int(header[2])
            texts.append(data[pos : pos + size].decode("utf-8", errors="replace"))
            pos += size + 1
        else:
            texts.append("")
    return texts


_PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")


_STATUS_RE = re.compile(r"^status:[ \t]*['\"]?(\w+)", re.M)
_TREE_RE = re.compile(r"^\d+ blob ([0-9a-f]{40,64})\t(docs/plans/(?:[^/]+/)*(\d{4}-\d{2}-\d{2})-[^/]+\.md)$")


def _scan_completed_plans(root: Path, since: Optional[str], head: Optional[str]) -> List[Dict[str, Any]]:
    """Ledger-shaped rows for plans at a completed status, from one `ls-tree` plus one cat-file batch.

    Used only when the ledger file is absent. Trap: a pathspec-limited `git log` over the window
    costs ~10s on a busy repo, so the window is applied to the plan filename's `YYYY-MM-DD-` prefix
    over the HEAD tree instead; `completing_sha` is HEAD (the commit the plan is read at).
    """
    start = _ledger._parse_instant(since) if since and not _SHA_RE.match(since) else None
    floor = (start or datetime.now(timezone.utc) - timedelta(days=DEFAULT_WINDOW_DAYS)).date().isoformat()
    res = run_git(["ls-tree", "-r", "HEAD", "--", "docs/plans"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    if not res.ok:
        return []
    blobs: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        m = _TREE_RE.match(line)
        if m and m.group(3) >= floor:
            blobs[m.group(2)] = m.group(1)
    paths = list(blobs)
    rows: List[Dict[str, Any]] = []
    for path, text in zip(paths, _read_blobs(root, [blobs[p] for p in paths])):
        fm = _ledger.frontmatter(text)
        status = _STATUS_RE.search(fm)
        writes = _ledger.spine_writes(text)
        if not status or status.group(1) not in COMPLETED_STATUSES or not writes:
            continue
        rows.append({
            "plan": path, "status": status.group(1), "completing_sha": head,
            "plan_blob": blobs[path], "tshirt": _ledger.tshirt(root, fm), "writes": writes,
            "_text": text,
        })
    return rows


def _citing_pages(root: Path, paths: List[str]) -> Dict[str, List[str]]:
    """{wiki page: touched paths its text cites}, read in-process.

    Trap: a `git grep -F -f -` over ~1100 patterns cost ~800ms of child CPU, the whole of the
    op's overrun of the 500ms bar. A cited path is a run of path characters, so each page is cut
    into such runs once and every suffix starting at a '/' boundary is looked up in a set:
    a citation counts when it begins a run or follows a '/' (`../x`, `/abs/x`) and ends the run
    (`x:12`, `x::sym`, `x#L3` all end it). Trailing sentence dots are stripped.
    """
    if not paths:
        return {}
    wanted = set(paths)
    cites: Dict[str, List[str]] = {}
    for spec in WIKI_DIRS:
        base = root / spec
        if not base.is_dir():
            continue
        for page_path in sorted(base.rglob("*.md")):
            try:
                text = page_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            hit = set()
            for token in set(_PATH_TOKEN.findall(text)):
                if "/" not in token:
                    continue
                token = token.rstrip(".")
                start = 0
                while start >= 0:
                    if token[start:] in wanted:
                        hit.add(token[start:])
                    nxt = token.find("/", start)
                    start = nxt + 1 if nxt >= 0 else -1
            if hit:
                cites[page_path.relative_to(root).as_posix()] = sorted(hit)
    return cites


def _empty_reason(root: Path, chosen: List[Dict[str, Any]], had_citing: bool) -> str:
    """Why `pages` is empty: the first stage of the pipeline that left nothing."""
    if not chosen:
        if not (root / _ledger.LEDGER).exists():
            return REASON_LEDGER_MISSING
        return REASON_LEDGER_EMPTY
    return REASON_HUB_CUT if had_citing else REASON_NO_CITING_PAGE


def wiki_candidates(root: Path, since: Optional[str] = None) -> Dict[str, Any]:
    """The contract reply for the repo at `root`: window, ranked pages, preview total, truncation,
    and `empty_reason` (null when pages is non-empty)."""
    rows, base, source = _window(root, since)
    head_res = run_git(["rev-parse", "HEAD"], cwd=str(root), timeout=CEREMONY_BUDGET_SECS)
    head = head_res.stdout.strip() if head_res.ok else None
    if not (root / _ledger.LEDGER).exists():
        rows = _scan_completed_plans(root, since, head)
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row.get("status") in COMPLETED_STATUSES and row.get("writes") and row.get("plan_blob"):
            latest[str(row.get("plan"))] = row
    chosen = list(latest.values())

    plans: List[Dict[str, Any]] = []
    texts = (
        [str(r["_text"]) for r in chosen] if all("_text" in r for r in chosen)
        else _read_blobs(root, [str(r["plan_blob"]) for r in chosen])
    )
    for row, text in zip(chosen, texts):
        plans.append({
            "path": row["plan"], "commit_sha": row.get("completing_sha"), "tshirt": row.get("tshirt"),
            "preview": _ledger.body(text)[:PREVIEW_CHARS], "writes": list(row["writes"]),
        })

    cited = _citing_pages(root, sorted({p for pl in plans for p in pl["writes"]}))
    had_citing = bool(cited)
    fan_in: Dict[str, int] = {}
    for hit in cited.values():
        for p in hit:
            fan_in[p] = fan_in.get(p, 0) + 1
    cited = {
        page: kept_paths
        for page, hit in cited.items()
        if (kept_paths := [p for p in hit if fan_in[p] <= HUB_CITING_PAGES])
    }
    pages: List[Dict[str, Any]] = []
    for page, paths in cited.items():
        mine = sorted(
            (pl for pl in plans if any(w in paths for w in pl["writes"])),
            key=lambda pl: (-_rank(pl["tshirt"]), pl["path"]),
        )
        if mine:
            pages.append({"page": page, "plans": mine})
    pages.sort(key=lambda pg: (-_rank(pg["plans"][0]["tshirt"]), -len(pg["plans"]), pg["page"]))

    cut_pages = pages[PAGES_CAP:]
    kept = pages[:PAGES_CAP]
    cut_plans = sum(len(pg["plans"]) for pg in cut_pages)
    budget = PLANS_CAP
    for pg in kept:
        take = pg["plans"][:budget]
        cut_plans += len(pg["plans"]) - len(take)
        budget -= len(take)
        pg["plans"] = take
    emptied = [pg for pg in kept if not pg["plans"]]
    kept = [pg for pg in kept if pg["plans"]]

    total = 0
    for pg in kept:
        pg["plans"] = [
            {k: pl[k] for k in ("path", "commit_sha", "tshirt", "preview")} for pl in pg["plans"]
        ]
        total += sum(len(pl["preview"]) for pl in pg["plans"])
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "window": {"since": base, "head": head, "source": source},
        "pages": kept,
        "preview_chars_total": total,
        "truncated": {"pages": len(cut_pages) + len(emptied), "plans": cut_plans},
        "empty_reason": None if kept else _empty_reason(root, chosen, had_citing),
    }


@register_op("ceremony.wiki_candidates")
def _ceremony_wiki_candidates(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.wiki_candidates" handler.

    Params: `since` (ISO date/datetime, or a ledger `completing_sha`; overrides the cached
    `ack_head`), `repo_root` (any path inside the repo to query; default the invoking repo). Both sources
    resolve to the same main worktree root, so --repo, cwd and `repo_root` agree. Returns
    {schema, schema_version, window{since, head, source}, pages[{page, plans[{path, commit_sha,
    tshirt, preview}]}], preview_chars_total, truncated{pages, plans}, empty_reason}. Read-only: writes no file
    and never advances the worklist `ack_head`. Caps: PREVIEW_CHARS, PAGES_CAP, PLANS_CAP.
    """
    p = params or {}
    override = p.get("repo_root")
    if override:
        base = Path(str(override))
        if not base.is_dir():
            raise ValueError(f"ceremony.wiki_candidates: repo_root is not a directory: {override!r}")
    elif repo_root is None:
        raise ValueError(
            "ceremony.wiki_candidates requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    else:
        base = repo_root
    try:
        common = base if base.name == ".git" else git_common_dir(base)
    except RuntimeError as exc:
        raise ValueError(f"ceremony.wiki_candidates: not inside a git repository: {base!r}") from exc
    root = main_worktree_root(common)
    since = str(p["since"]).strip() if p.get("since") else None
    return wiki_candidates(root, since)
