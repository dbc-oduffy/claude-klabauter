"""Carry the open harness Task API list through handoff and re-inject it on pickup.

The harness store is `<claude_config_dir()>/tasks/<session-id>/<n>.json`. This module
reads it, renders a `## Open tasks` baton section holding a fenced `yaml tasks` block,
parses that block back, and maps it to an ordered advisory TaskCreate/TaskUpdate list.
The engine never executes the harness calls; unreadable or empty input degrades to [].
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from coordinator_core import _settings_home

KEPT_KEYS = ("id", "subject", "description", "status", "blockedBy")
TASKS_HEADING = "## Open tasks"

_BLOCK_RE = re.compile(r"^```yaml tasks[ \t]*\r?\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)


def _default_store_root() -> Path:
    return _settings_home.claude_config_dir() / "tasks"


def _task_sort_key(task: dict) -> tuple[int, str]:
    raw = str(task.get("id", ""))
    return (int(raw), raw) if raw.isdigit() else (10**12, raw)


def read_open_tasks(session_id: str, store_root: Path | None = None) -> list[dict]:
    """Return the non-completed tasks of one session, numeric id order.

    Unparseable files are dropped, never raised; a missing dir yields []. `blockedBy`
    keeps only ids still carried (an edge to a completed task is already satisfied).
    """
    root = store_root if store_root is not None else _default_store_root()
    sid = str(session_id or "")
    if not sid or "/" in sid or "\\" in sid or sid in (".", ".."):
        return []
    session_dir = Path(root) / sid
    try:
        files = sorted(session_dir.glob("*.json"))
    except OSError:
        return []
    tasks: list[dict] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "id" not in data:
            continue
        if data.get("status") == "completed":
            continue
        tasks.append(data)
    tasks.sort(key=_task_sort_key)
    kept_ids = {str(t["id"]) for t in tasks}
    out: list[dict] = []
    for t in tasks:
        blocked = t.get("blockedBy") or []
        if not isinstance(blocked, list):
            blocked = []
        out.append(
            {
                "id": str(t["id"]),
                "subject": str(t.get("subject") or ""),
                "description": str(t.get("description") or ""),
                "status": str(t.get("status") or "pending"),
                "blockedBy": [str(b) for b in blocked if str(b) in kept_ids],
            }
        )
    return out


def render_tasks_section(tasks: list[dict]) -> str:
    """Render the `## Open tasks` section; [] returns "" so the caller writes nothing."""
    if not tasks:
        return ""
    kept = [{k: t.get(k) for k in KEPT_KEYS} for t in tasks]
    block = yaml.safe_dump(kept, sort_keys=False, allow_unicode=True, width=10**6)
    lines = [TASKS_HEADING, "", "```yaml tasks", block.rstrip("\n"), "```", ""]
    for t in kept:
        blocked = t.get("blockedBy") or []
        suffix = f" (blocked by {', '.join('#' + b for b in blocked)})" if blocked else ""
        lines.append(f"- #{t['id']} [{t['status']}] {t['subject']}{suffix}")
    return "\n".join(lines) + "\n"


def parse_tasks_block(body_text: str) -> list[dict]:
    """Parse the first fenced `yaml tasks` block; absent or malformed returns []."""
    try:
        match = _BLOCK_RE.search(body_text or "")
        if not match:
            return []
        data = yaml.safe_load(match.group(1))
        if not isinstance(data, list):
            return []
        out: list[dict] = []
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                return []
            blocked = item.get("blockedBy") or []
            if not isinstance(blocked, list):
                return []
            out.append(
                {
                    "id": str(item["id"]),
                    "subject": str(item.get("subject") or ""),
                    "description": str(item.get("description") or ""),
                    "status": str(item.get("status") or "pending"),
                    "blockedBy": [str(b) for b in blocked],
                }
            )
        return out
    except Exception:  # noqa: BLE001 -- parse of untrusted baton text must never raise
        return []


def recreate_directives(tasks: list[dict]) -> list[dict]:
    """Map carried tasks to ordered advisory harness steps.

    All TaskCreate steps come first (one per task), then a TaskUpdate per task whose
    status != "pending" or which has blockedBy. `old_id` is the id in the dumped list;
    the acting EM resolves each old id to the id its TaskCreate returned. A harness
    lacking TaskCreate simply does not act on these (advisory: true).
    """
    steps: list[dict] = []
    for t in tasks:
        steps.append(
            {
                "tool": "TaskCreate",
                "old_id": t["id"],
                "args": {"subject": t.get("subject", ""), "description": t.get("description", "")},
                "advisory": True,
            }
        )
    for t in tasks:
        status = t.get("status") or "pending"
        blocked = list(t.get("blockedBy") or [])
        if status == "pending" and not blocked:
            continue
        args: dict = {}
        if status != "pending":
            args["status"] = status
        if blocked:
            args["addBlockedBy"] = blocked
        steps.append({"tool": "TaskUpdate", "old_id": t["id"], "args": args, "advisory": True})
    return steps
