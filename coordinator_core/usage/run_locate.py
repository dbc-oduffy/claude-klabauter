"""Locate the Workflow `wf_` run dir of the run a close leg belongs to (COMPUTE_ONLY, spawn-free)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

DEFAULT_AGENT_DESCRIPTIONS = ("commit", "halt-commit")
_PARAMS_TAIL = "terminal-commit-params.json"
_HEAD_BYTES = 8192


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


def locate_run_dir(
    session_dir: Path,
    run_id: str,
    *,
    agent_descriptions: tuple = DEFAULT_AGENT_DESCRIPTIONS,
) -> Optional[Path]:
    """The wf_ dir whose unsettled agent (journal `started`, no `result`/`failed`) has a
    `description` in agent_descriptions and a transcript head naming
    `scratch/warp/<run_id>/terminal-commit-params.json`. None when absent or ambiguous.
    The journal gates the reads: metas and transcripts are opened only for unsettled agents,
    so a dir whose agents all settled costs one journal read."""
    if not run_id or Path(run_id).name != run_id:
        return None
    needle = f"scratch/warp/{run_id}/{_PARAMS_TAIL}"
    wf_root = Path(session_dir) / "subagents" / "workflows"
    matches = []
    try:
        wf_dirs = [Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()]
    except OSError:
        return None
    for wf in wf_dirs:
        unsettled = _unsettled_agent_ids(wf / "journal.jsonl")
        for agent_id in sorted(unsettled):
            meta_name = f"agent-{agent_id}.meta.json"
            if Path(agent_id).name != agent_id:
                continue
            try:
                meta = json.loads((wf / meta_name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict) or meta.get("description") not in agent_descriptions:
                continue
            head = _head(wf / f"agent-{agent_id}.jsonl", _HEAD_BYTES).replace("\\\\", "/").replace("\\/", "/")
            if needle in head:
                matches.append(wf)
    return matches[0] if len(matches) == 1 else None
