"""Post-run report over a Workflow run's journal: per-leg cost and executor rows past the wall cap.

The journal carries no timestamps, so wall time comes from each agent transcript's first and last
`timestamp`. Only a transcript head and a bounded tail are read, with no subprocess. A transcript
larger than head + tail makes turns, output tokens and peak context lower bounds (`partial`).
Post-run only: never called from the fire op, a hook or an emitted script.

    python3 -m coordinator_core.workflow_watch.run_report --journal <abs path to journal.jsonl> [--json]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Optional

from coordinator_core.workflow_watch.stall import _tail_text, _transcript_path

EXECUTOR_WALL_CAP_SECONDS = 30 * 60
EXECUTOR_LABEL_PREFIX = "work:"
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 64 * 1024
UNKNOWN = "unknown"


def _started_agents(journal_path: str) -> list[tuple[str, str]]:
    """(agentId, label) of every `started` event, in journal order, one per agent id."""
    seen: dict[str, str] = {}
    try:
        with open(journal_path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                if '"started"' not in raw:
                    continue
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "started":
                    continue
                agent_id = event.get("agentId")
                if not isinstance(agent_id, str) or agent_id in seen:
                    continue
                label = event.get("label")
                seen[agent_id] = label if isinstance(label, str) and label else agent_id
    except OSError:
        return []
    return list(seen.items())


def _head_text(path: str, limit: int) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _events(text: str, drop_first: bool, drop_last: bool) -> list[dict]:
    """Parsed JSON lines; a seek-cut first line and a size-cut last line are dropped."""
    lines = text.split("\n")
    if drop_first and lines:
        lines = lines[1:]
    if drop_last and lines:
        lines = lines[:-1]
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if isinstance(event, dict):
            out.append(event)
    return out


def _epoch(stamp: Any) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _context(usage: dict) -> int:
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _meta(run_dir: str, agent_id: str, transcript: Optional[str]) -> dict:
    name = f"agent-{agent_id}.meta.json"
    candidates = [os.path.join(run_dir, name), os.path.join(run_dir, "subagents", name)]
    if transcript:
        candidates.insert(0, transcript[: -len(".jsonl")] + ".meta.json")
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _agent_row(run_dir: str, agent_id: str, label: str) -> dict:
    row: dict[str, Any] = {
        "label": label, "agent_id": agent_id, "agent_type": UNKNOWN, "model": UNKNOWN,
        "turn1_context": None, "peak_context": None, "output_tokens": None, "turns": None,
        "wall_seconds": None, "partial": False,
    }
    path = _transcript_path(run_dir, agent_id)
    meta = _meta(run_dir, agent_id, path)
    for key, field in (("agent_type", "agentType"), ("model", "model")):
        if isinstance(meta.get(field), str) and meta[field]:
            row[key] = meta[field]
    if path is None:
        return row
    try:
        size = os.stat(path).st_size
    except OSError:
        return row
    if size <= HEAD_BYTES + TAIL_BYTES:
        events = _events(_head_text(path, size), False, False)
    else:
        row["partial"] = True
        events = (_events(_head_text(path, HEAD_BYTES), False, True)
                  + _events(_tail_text(path, TAIL_BYTES), True, False))
    stamps = [t for t in (_epoch(e.get("timestamp")) for e in events) if t is not None]
    if stamps:
        row["wall_seconds"] = round(max(stamps) - min(stamps), 1)
    out_by_msg: dict[str, int] = {}
    for event in events:
        message = event.get("message")
        if event.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        ctx = _context(usage)
        if row["turn1_context"] is None:
            row["turn1_context"] = ctx
        row["peak_context"] = max(row["peak_context"] or 0, ctx)
        ident = message.get("id") if isinstance(message.get("id"), str) else f"n{len(out_by_msg)}"
        out_by_msg[ident] = max(out_by_msg.get(ident, 0), int(usage.get("output_tokens") or 0))
    if out_by_msg:
        row["turns"] = len(out_by_msg)
        row["output_tokens"] = sum(out_by_msg.values())
    return row


def build_report(journal_path: str) -> dict:
    """{"rows": [...], "over_cap": [...]} for one run's journal."""
    run_dir = os.path.dirname(journal_path)
    rows = [_agent_row(run_dir, agent_id, label) for agent_id, label in _started_agents(journal_path)]
    over = [r for r in rows
            if r["label"].startswith(EXECUTOR_LABEL_PREFIX)
            and r["wall_seconds"] is not None and r["wall_seconds"] > EXECUTOR_WALL_CAP_SECONDS]
    return {"wall_cap_seconds": EXECUTOR_WALL_CAP_SECONDS, "rows": rows, "over_cap": over}


def _cell(value: Any, mark: str = "") -> str:
    return UNKNOWN if value is None else f"{value}{mark}"


def render(report: dict) -> str:
    head = ["label", "type", "model", "turn1_ctx", "peak_ctx", "out_tok", "turns", "wall_s"]
    table = [head]
    for r in report["rows"]:
        mark = "+" if r["partial"] else ""
        table.append([r["label"], r["agent_type"], r["model"], _cell(r["turn1_context"]),
                      _cell(r["peak_context"]), _cell(r["output_tokens"], mark),
                      _cell(r["turns"], mark),_cell(r["wall_seconds"])])
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    lines.append("")
    lines.append(f"over {report['wall_cap_seconds']}s (work:* rows):")
    if report["over_cap"]:
        lines += [f"  {r['label']}  {r['wall_seconds']}s" for r in report["over_cap"]]
    else:
        lines.append("  none")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_report", description=__doc__.split("\n")[0])
    parser.add_argument("--journal", required=True, help="absolute path to a run's journal.jsonl")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.journal):
        print(f"journal not found: {args.journal}", file=sys.stderr)
        return 2
    report = build_report(args.journal)
    print(json.dumps(report, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
