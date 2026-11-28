"""Engine-held agent_type-to-phase map and the example-game-repo gate-metrics row reader.

Example-Game-Repo rows carry agent_type but no phase; the map lives only here. No subprocess and no git spawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional

REQUIRED_PHASES = ("sizing", "planning", "review")

GATE_METRICS_TAIL_BYTES = 4 * 1024 * 1024
GATE_METRICS_DECISIONS = ("deny", "clear", "retrieval")
GATE_METRICS_FIELDS = ("ts", "agent_id", "run_id", "session_id", "agent_type", "decision", "tool")

AGENT_TYPE_PHASE = MappingProxyType(
    {
        "coordinator:sizing-scout": "sizing",
        "coordinator:ue-corpus-checker": "planning",
        "coordinator:plan-author": "planning",
        "coordinator:plan-reviewer": "review",
        "coordinator:code-reviewer": "review",
        "coordinator:staff-eng": "review",
        "coordinator:executor": "execution",
    }
)

_BARE_PHASE = MappingProxyType({k.rsplit(":", 1)[-1]: v for k, v in AGENT_TYPE_PHASE.items()})


def phase_of(agent_type: Optional[str]) -> Optional[str]:
    """Phase of an agent type; None for null, empty or unmapped, which is never a pass."""
    if not isinstance(agent_type, str) or not agent_type.strip():
        return None
    name = agent_type.strip()
    if name in AGENT_TYPE_PHASE:
        return AGENT_TYPE_PHASE[name]
    if ":" in name:
        return None
    return _BARE_PHASE.get(name)


def _tail_lines(path: Path, max_bytes: int) -> list[str]:
    with path.open("rb") as fh:
        size = fh.seek(0, 2)
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def read_gate_metrics(root: Any, run_id: str, max_bytes: int = GATE_METRICS_TAIL_BYTES) -> list[dict]:
    """Rows of the run from the bounded tail of the metrics log; malformed lines are skipped."""
    path = Path(root) / ".coordinator-local" / "ue-research-gate-metrics.jsonl"
    try:
        lines = _tail_lines(path, max_bytes)
    except OSError:
        return []
    rows: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("run_id") != run_id:
            continue
        if row.get("decision") not in GATE_METRICS_DECISIONS:
            continue
        rows.append({field: row.get(field) for field in GATE_METRICS_FIELDS})
    return rows
