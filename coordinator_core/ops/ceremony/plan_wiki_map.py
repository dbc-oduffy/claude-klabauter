"""
coordinator_core.ops.ceremony.plan_wiki_map -- JSON-RPC "ceremony.plan_wiki_map".

Purpose: for the update-docs / distill wiki step, map completed plans to the wiki pages their
touched paths affect, so the model step reads only the mapped pages and bounded plan previews
before distill deletes the plans. Read-only, no model call.

All data comes through `weekly_doc_files` (doc_files views when an index resolves, else its
batched git fallback): `plans_completed`, `plan_footprints`, `paths_to_docs` over the union of
footprint paths, one preview batch. A window completing more than `MAX_CANDIDATES` plans is
read for the first MAX_CANDIDATES by plan path and reports `truncated`: narrow the window. Spawn count is constant in plan
count; the fallback shares one `cat-file --batch` between footprints and previews. Impactful = sizing M/L/XL or at least
one mapped wiki page; output is sorted by plan path and capped at `max_plans`.

Self-registration: importing this module registers the op; `ops/__init__.py` imports it at
start_server() time.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops import weekly_doc_files as wdf

SCHEMA_VERSION = 1
DEFAULT_WINDOW_DAYS = 7
MAX_PLANS = 50
MAX_CANDIDATES = 400
IMPACTFUL_TSHIRTS = frozenset({"M", "L", "XL"})
WIKI_PREFIX = "docs/wiki/"
ATLAS_PREFIX = "docs/atlas/"
SOURCE_VIEWS = "doc_files"
SOURCE_FALLBACK = "git-fallback"


def _tshirt(sizing: Any) -> str:
    if isinstance(sizing, dict):
        sizing = sizing.get("tshirt")
    return str(sizing).strip().upper() if sizing else ""


def plan_wiki_map(
    since: str,
    until: str,
    max_plans: int = MAX_PLANS,
    *,
    root: Path,
    index_path: Optional[Any] = None,
) -> Dict[str, Any]:
    """Impactful plans completed in [since, until) with mapped wiki pages and previews.

    Returns {schema_version, source, reason, plans[], truncated[, deleted_plans]}. Each plan
    item: plan, completing_sha, summary, sizing, deleted, wiki_pages, preview, and
    `atlas_systems` only when the mapping names any.
    """
    db = wdf.resolve_index_path(index_path)
    source = SOURCE_VIEWS if db is not None else SOURCE_FALLBACK
    rows = wdf.plans_completed(since, until, index_path=index_path, repo=root)

    by_plan: Dict[str, Dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: (str(r["plan"]), str(r.get("sha") or ""))):
        by_plan.setdefault(str(row["plan"]), row)
    clipped = len(by_plan) > MAX_CANDIDATES
    specs: List[Tuple[str, Optional[str]]] = [
        (plan, by_plan[plan].get("sha") or None) for plan in list(by_plan)[:MAX_CANDIDATES]
    ]

    texts: Optional[List[str]] = wdf._plan_texts(specs, root) if db is None else None
    footprints = wdf.plan_footprints(specs, index_path=index_path, repo=root, texts=texts)
    union = sorted({p for paths in footprints.values() for p in paths})
    docs = wdf.paths_to_docs(union, index_path=index_path, repo=root) if union else {}

    items: List[Dict[str, Any]] = []
    item_specs: List[Tuple[str, Optional[str]]] = []
    item_texts: List[str] = []
    for i, (plan, sha) in enumerate(specs):
        row = by_plan[plan]
        mapped = {d for p in footprints.get(plan, []) for d in docs.get(p, [])}
        wiki = sorted(d for d in mapped if d.startswith(WIKI_PREFIX))
        atlas = sorted(d for d in mapped if d.startswith(ATLAS_PREFIX))
        if _tshirt(row.get("sizing")) not in IMPACTFUL_TSHIRTS and not wiki:
            continue
        item: Dict[str, Any] = {
            "plan": plan,
            "completing_sha": sha,
            "summary": row.get("summary"),
            "sizing": row.get("sizing"),
            "deleted": not (root / plan).exists(),
            "wiki_pages": wiki,
        }
        if atlas:
            item["atlas_systems"] = atlas
        items.append(item)
        item_specs.append((plan, sha))
        if texts is not None:
            item_texts.append(texts[i])

    truncated = clipped or len(items) > max_plans
    items, item_specs, item_texts = items[:max_plans], item_specs[:max_plans], item_texts[:max_plans]
    previews = (
        [t[: wdf.PREVIEW_CAP] for t in item_texts]
        if texts is not None
        else wdf.plan_previews(item_specs, index_path=index_path, repo=root)
    )
    for item, preview in zip(items, previews):
        item["preview"] = preview

    out: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": source,
        "reason": (
            "doc_files views"
            if db is not None
            else "no doc_files index resolved; answered from git and docs/wiki text"
        ),
        "plans": items,
        "truncated": truncated,
    }
    if db is None:
        out["deleted_plans"] = "unavailable_in_fallback"
    return out


@register_op("ceremony.plan_wiki_map")
def _ceremony_plan_wiki_map(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "ceremony.plan_wiki_map" handler.

    Params: `since` / `until` (ISO date; default the last 7 days through tomorrow),
    `max_plans` (default 50). repo_root (injected by ipc.dispatch_message): git_common_dir
    of the originating worktree; the handler reads the main worktree. Writes only the
    wiki-text token cache `weekly_doc_files` keeps.
    """
    if repo_root is None:
        raise ValueError(
            "ceremony.plan_wiki_map requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None."
        )
    p = params or {}
    today = _dt.date.today()
    since = str(p.get("since") or (today - _dt.timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat())
    until = str(p.get("until") or (today + _dt.timedelta(days=1)).isoformat())
    cap = max(1, int(p.get("max_plans", MAX_PLANS)))
    return plan_wiki_map(since, until, cap, root=main_worktree_root(repo_root))
