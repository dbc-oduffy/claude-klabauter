"""Locate the Workflow `wf_` run dir of the run a close leg belongs to (COMPUTE_ONLY, spawn-free)."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_AGENT_DESCRIPTIONS = ("commit", "halt-commit")
_PARAMS_TAIL = "terminal-commit-params.json"
_HEAD_BYTES = 8192
_RUN_STAMP = re.compile(r"\d{8}T\d{6}")
_FLOOR_MARGIN_S = 86400


def projects_root(env: Optional[dict] = None) -> Path:
    """Claude projects root: `$CLAUDE_CONFIG_DIR`, else `$CLAUDE_HOME`, else `~/.claude`, plus `projects`."""
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR") or env.get("CLAUDE_HOME")
    root = Path(base) if base else Path.home() / ".claude"
    return root / "projects"


def session_dir_for(session_id: str, root: Optional[Path] = None) -> Optional[Path]:
    """`<root>/<slug>/<session_id>` for the one slug dir that holds the session; None when absent or ambiguous."""
    if not session_id or Path(session_id).name != session_id:
        return None
    root = projects_root() if root is None else Path(root)
    hits = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                cand = Path(entry.path) / session_id
                if entry.is_dir() and cand.is_dir():
                    hits.append(cand)
    except OSError:
        return None
    return hits[0] if len(hits) == 1 else None


def _unsettled_agent_ids(journal: Path) -> set:
    started, settled = set(), set()
    try:
        text = journal.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    for line in text.splitlines():
        if '"agentId"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        agent_id = ev.get("agentId")
        kind = ev.get("type")
        if not agent_id:
            continue
        if kind == "started":
            started.add(agent_id)
        elif kind in ("result", "failed"):
            settled.add(agent_id)
    return started - settled


def _head(path: Path, nbytes: int) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(nbytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _run_floor(run_id: str) -> Optional[float]:
    """Epoch seconds before which no wf_ dir can belong to the run: the `YYYYMMDDTHHMMSS` stamp in
    the run id less a day (any timezone). None when the id carries no stamp."""
    m = _RUN_STAMP.search(run_id)
    if m is None:
        return None
    try:
        stamp = datetime.strptime(m.group(0), "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return stamp.timestamp() - _FLOOR_MARGIN_S


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


#: (wf_root, run_id, descriptions) -> the wf dir this process resolved. Process-local: a separate
#: close-job process starts empty and scans once. Re-verified on every hit, never trusted.
_FOUND: dict = {}


def _names_run(wf: Path, needle: str, agent_descriptions: tuple) -> bool:
    """True when `wf` holds an unsettled agent of a listed description whose transcript head names `needle`."""
    for agent_id in sorted(_unsettled_agent_ids(wf / "journal.jsonl")):
        if not isinstance(agent_id, str) or Path(agent_id).name != agent_id:
            continue
        try:
            meta = json.loads((wf / f"agent-{agent_id}.meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict) or meta.get("description") not in agent_descriptions:
            continue
        head = _head(wf / f"agent-{agent_id}.jsonl", _HEAD_BYTES).replace("\\\\", "/").replace("\\/", "/")
        if needle in head:
            return True
    return False


def locate_run_dir(
    session_dir: Path,
    run_id: str,
    *,
    agent_descriptions: tuple = DEFAULT_AGENT_DESCRIPTIONS,
    wf_id: Optional[str] = None,
) -> Optional[Path]:
    """The wf_ dir whose unsettled agent (journal `started`, no `result`/`failed`) has a
    `description` in agent_descriptions and a transcript head naming
    `scratch/warp/<run_id>/terminal-commit-params.json`. None when absent or ambiguous.

    A keyed lookup reads one journal and no other run's: `wf_id` (a `wf_` dir name the caller
    recorded for the run; speculative: no caller passes it yet, the intended one is the close job) and, failing that, the dir this process already resolved for the run
    are each verified in place and returned or refused, never widened to a scan. Without a key
    the journal gates the reads (metas and transcripts open only for unsettled agents), and a run
    id carrying a timestamp skips, on a stat alone, every dir whose journal predates the run."""
    if not run_id or Path(run_id).name != run_id:
        return None
    needle = f"scratch/warp/{run_id}/{_PARAMS_TAIL}"
    wf_root = Path(session_dir) / "subagents" / "workflows"
    memo_key = (str(wf_root), run_id, agent_descriptions)
    if wf_id is not None:
        keyed = wf_root / wf_id if wf_id.startswith("wf_") and Path(wf_id).name == wf_id else None
        return keyed if keyed is not None and _names_run(keyed, needle, agent_descriptions) else None
    known = _FOUND.get(memo_key)
    if known is not None:
        if _names_run(known, needle, agent_descriptions):
            return known
        _FOUND.pop(memo_key, None)
    try:
        wf_dirs = [Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()]
    except OSError:
        return None
    floor = _run_floor(run_id)
    matches = [
        wf for wf in wf_dirs
        if (floor is None or _mtime(wf / "journal.jsonl") >= floor) and _names_run(wf, needle, agent_descriptions)
    ]
    if len(matches) != 1:
        return None
    _FOUND[memo_key] = matches[0]
    return matches[0]
