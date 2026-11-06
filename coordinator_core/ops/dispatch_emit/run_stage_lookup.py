"""Hook-readable `{run_id, stage}` lookup for a SubagentStart child of a Workflow run.

A SubagentStart payload carries `agent_id`, `agent_type`, `cwd`, `session_id`,
`transcript_path` and `prompt_id`. `session_id` + `agent_id` reach the child's Workflow run: the
run is the `wf_` dir under `<projects>/<slug>/<session_id>/subagents/workflows/` whose
`journal.jsonl` carries the `started` event for that agent id, and its `label` is the stage.
`run_id` is that `wf_` dir name. Spawns nothing; every read fails to None.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional

LOOKUP_FILENAME = "run-stage-lookup.json"


def _key(payload: Mapping[str, Any]) -> str:
    for name in ("agent_id", "session_id"):
        value = payload.get(name)
        if isinstance(value, str) and value and Path(value).name == value:
            return value
    return ""


def record(run_dir: str | Path, key: str, run_id: str, stage: str) -> None:
    """Persist `{run_id, stage}` under `key` (an agent id or session id) in `run_dir`; never raises."""
    try:
        path = Path(run_dir) / LOOKUP_FILENAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data[key] = {"run_id": run_id, "stage": stage}
        tmp = path.with_name(f"{LOOKUP_FILENAME}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def _recorded(wf: Path, key: str) -> Optional[dict]:
    try:
        data = json.loads((wf / LOOKUP_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hit = data.get(key) if isinstance(data, dict) else None
    if isinstance(hit, dict) and isinstance(hit.get("run_id"), str) and isinstance(hit.get("stage"), str):
        return {"run_id": hit["run_id"], "stage": hit["stage"]}
    return None


def _journal_stage(journal: Path, agent_id: str) -> Optional[str]:
    needle = f'"{agent_id}"'
    try:
        with open(journal, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if needle not in raw or '"started"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(event, dict) and event.get("type") == "started"
                        and event.get("agentId") == agent_id):
                    label = event.get("label")
                    return label if isinstance(label, str) and label else agent_id
    except OSError:
        return None
    return None


def resolve(
    payload: Any, *, projects_root: Optional[Path] = None
) -> Optional[tuple[Path, dict]]:
    """(run dir, `{run_id, stage}`) of the Workflow child a SubagentStart `payload` describes, else None.

    A record written by `record` wins; otherwise the child's `started` journal event decides.
    No `agent_id` means no journal join (a session id alone names no child).
    """
    try:
        if not isinstance(payload, Mapping):
            return None
        key = _key(payload)
        session_id = payload.get("session_id")
        if not key or not isinstance(session_id, str):
            return None
        from coordinator_core.usage.run_locate import session_dir_for

        session_dir = session_dir_for(session_id, projects_root)
        if session_dir is None:
            return None
        wf_root = session_dir / "subagents" / "workflows"
        wf_dirs = sorted(
            (Path(e.path) for e in os.scandir(wf_root) if e.name.startswith("wf_") and e.is_dir()),
            key=lambda p: p.name,
        )
        for wf in wf_dirs:
            hit = _recorded(wf, key)
            if hit:
                return wf, hit
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or key != agent_id:
            return None
        for wf in wf_dirs:
            stage = _journal_stage(wf / "journal.jsonl", agent_id)
            if stage is not None:
                return wf, {"run_id": wf.name, "stage": stage}
    except OSError:
        return None
    return None


def read(payload: Any, *, projects_root: Optional[Path] = None) -> Optional[dict]:
    """`{run_id, stage}` of the Workflow child a SubagentStart `payload` describes, else None."""
    found = resolve(payload, projects_root=projects_root)
    return found[1] if found else None
