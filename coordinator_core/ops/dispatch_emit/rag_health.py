"""Compose-time read of example-retrieval-repo's persisted per-repo health verdict.

`repo_verdict(repo_root)` answers one question: does example-retrieval-repo's own record for THIS repo say
semantic search is not ok? It reads `<settings home>/example-retrieval-repo/served-repos.json` (repo root ->
slug) and `corpus-state.json` (`repos.<slug>.corpus_health` lanes, plus the `semantic_search`
verdict block when example-retrieval-repo persists it) and returns one entry dict, or None when healthy.
Two file reads, one stat, no subprocess, no network. Any failure is a `health_unreadable` entry
and an unmapped repo or slug is `health_unknown_repo`: the reader never returns None for a case it
could not judge. A stale `checked_at` is reported as `stale_since`, never a filter.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE = "project_health"
SEMANTIC_LANE = "semantic_served_set"
_INDEX_MARKER = ".index_job.json"
NEXT_ACTION = "ask the example-retrieval-repo peer over unimatrix for project_health of this repo"


def _entry(
    verdict: str,
    reason: str,
    lanes: Optional[List[Dict[str, str]]] = None,
    stale_since: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "source": SOURCE,
        "verdict": verdict,
        "lanes": lanes or [],
        "reason": reason,
        "stale_since": stale_since,
        "next_action": NEXT_ACTION,
    }


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _slug_for(repo_root: str, served: Dict[str, Any]) -> Optional[str]:
    want = _norm(repo_root)
    for row in served.get("repos", []):
        if isinstance(row, dict) and isinstance(row.get("root"), str) and _norm(row["root"]) == want:
            slug = row.get("slug")
            return slug if isinstance(slug, str) and slug else None
    return None


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _index_mtime(rag_home: Path, slug: str) -> Optional[float]:
    try:
        return (rag_home / "indexes" / slug / "root" / _INDEX_MARKER).stat().st_mtime
    except OSError:
        return None


def _oldest(stamps: List[Any]) -> Optional[str]:
    dated = [(e, s) for s in stamps if (e := _epoch(s)) is not None]
    return min(dated)[1] if dated else None


def _semantic_block(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    block = record.get("semantic_search")
    if not isinstance(block, dict):
        return None
    verdict = block.get("verdict")
    if not isinstance(verdict, str) or verdict == "ok":
        return None
    detail = block.get("reason") or "semantic search not ok"
    if "indexed_files" in block and "eligible_files" in block:
        detail = f"{detail} ({block['indexed_files']}/{block['eligible_files']} files indexed)"
    return _entry(
        verdict,
        f"semantic_search {verdict}: {detail}",
        [{"lane": "semantic_search", "state": verdict, "reason": str(detail)}],
        block.get("checked_at") if isinstance(block.get("checked_at"), str) else None,
    )


def _verdict_for(record: Dict[str, Any], rag_home: Path, slug: str) -> Optional[Dict[str, Any]]:
    semantic = _semantic_block(record)
    if semantic is not None:
        return semantic
    health = record.get("corpus_health")
    if not isinstance(health, dict):
        return _entry("health_unreadable", f"corpus-state.json carries no corpus_health for {slug}")
    bad: List[Dict[str, str]] = []
    stamps: List[Any] = []
    for name in sorted(health):
        lane = health[name]
        state = lane.get("state") if isinstance(lane, dict) else None
        if state == "ok":
            stamps.append(lane.get("checked_at"))
            continue
        reason = str(lane.get("reason", "")) if isinstance(lane, dict) else ""
        bad.append({"lane": name, "state": str(state), "reason": reason})
        if isinstance(lane, dict):
            stamps.append(lane.get("checked_at"))
    if bad:
        decisive = next((b for b in bad if b["lane"] == SEMANTIC_LANE), None)
        if decisive is not None:
            verdict = decisive["state"]
        else:
            verdict = "degraded" if any(b["state"] == "degraded" for b in bad) else "unknown"
        lead = decisive or bad[0]
        reason = f"{lead['lane']} {lead['state']}: {lead['reason']}".rstrip(": ")
        if len(bad) > 1:
            reason += f" (+{len(bad) - 1} more non-ok lanes)"
        return _entry(verdict, reason, bad, _oldest(stamps))
    oldest = _oldest(stamps)
    index_mtime = _index_mtime(rag_home, slug)
    oldest_epoch = _epoch(oldest)
    if oldest_epoch is not None and index_mtime is not None and oldest_epoch < index_mtime:
        return _entry("stale", f"all lanes ok but last checked {oldest}, before the index last changed", [], oldest)
    return None


def repo_verdict(repo_root: Any, rag_home: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The repo's non-ok example-retrieval-repo health entry, or None when every lane is ok and fresh.

    Staleness is judged against the index marker's mtime, not the clock.
    """
    try:
        if rag_home is None:
            from coordinator_core._settings_home import settings_home

            rag_home = settings_home() / "project-rag"
        served = json.loads((rag_home / "served-repos.json").read_text(encoding="utf-8"))
        state = json.loads((rag_home / "corpus-state.json").read_text(encoding="utf-8"))
        slug = _slug_for(str(repo_root), served)
        if slug is None:
            return _entry("health_unknown_repo", f"{repo_root} is not in example-retrieval-repo's served-repos.json")
        record = state.get("repos", {}).get(slug)
        if not isinstance(record, dict):
            return _entry("health_unknown_repo", f"{slug} has no record in example-retrieval-repo's corpus-state.json")
        return _verdict_for(record, rag_home, slug)
    except Exception as exc:  # noqa: BLE001 - a reader failure is an entry, never a failed compose
        return _entry("health_unreadable", f"example-retrieval-repo health unreadable: {type(exc).__name__}: {exc}")
