"""Post-run report over a Workflow run's journal: per-leg cost and executor rows past the wall cap.

The journal carries no timestamps, so wall time comes from each agent transcript's first and last
`timestamp`. Token fields, model and est. USD come from a full transcript read
(usage.agent_usage.scan_run); context, turns and wall read only a head and bounded tail, so on a
larger transcript turns and peak context are lower bounds (`partial`). No subprocess.
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

from coordinator_core.usage.agent_usage import agent_meta, scan_run, started_agents as _started_agents
from coordinator_core.usage.price_table import TOKEN_FIELDS
from coordinator_core.workflow_watch.stall import _tail_text, _transcript_path

EXECUTOR_WALL_CAP_SECONDS = 30 * 60
EXECUTOR_LABEL_PREFIX = "work:"
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 64 * 1024
UNKNOWN = "unknown"


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


def _agent_row(run_dir: str, agent_id: str, label: str) -> dict:
    row: dict[str, Any] = {
        "label": label, "agent_id": agent_id, "agent_type": UNKNOWN, "model": UNKNOWN,
        "turn1_context": None, "peak_context": None, "turns": None,
        "wall_seconds": None, "partial": False,
    }
    path = _transcript_path(run_dir, agent_id)
    meta = agent_meta(run_dir, agent_id, path)
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
    seen_msgs: set[str] = set()
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
        ident = message.get("id") if isinstance(message.get("id"), str) else f"n{len(seen_msgs)}"
        seen_msgs.add(ident)
    if seen_msgs:
        row["turns"] = len(seen_msgs)
    return row


def build_report(journal_path: str) -> dict:
    """{"rows", "over_cap", "by_model", "run_total"} for one run's journal.

    Token sums, usd and model come from usage.agent_usage.scan_run (full transcript read);
    context, turns and wall time stay head+tail bounded.
    """
    run_dir = os.path.dirname(journal_path)
    usage = scan_run(run_dir)
    by_id = {a["agent_id"]: a for a in usage["agents"]}
    rows = []
    for agent_id, label in _started_agents(journal_path):
        row = _agent_row(run_dir, agent_id, label)
        scanned = by_id.get(agent_id)
        if scanned is not None:
            for key in (*TOKEN_FIELDS, "total_tokens", "usd", "unpriced", "speed"):
                row[key] = scanned[key]
            row["model"] = scanned["model"] or row["model"]
        rows.append(row)
    over = [r for r in rows
            if r["label"].startswith(EXECUTOR_LABEL_PREFIX)
            and r["wall_seconds"] is not None and r["wall_seconds"] > EXECUTOR_WALL_CAP_SECONDS]
    return {"wall_cap_seconds": EXECUTOR_WALL_CAP_SECONDS, "rows": rows, "over_cap": over,
            "by_model": usage["by_model"], "run_total": usage["run_total"]}


def _cell(value: Any, mark: str = "") -> str:
    return UNKNOWN if value is None else f"{value}{mark}"


def _usd(value: Any) -> str:
    return UNKNOWN if value is None else f"${value:.4f}"


def render(report: dict) -> str:
    head = ["label", "type", "model", "turn1_ctx", "peak_ctx", "in", "out", "cache_r", "cache_w",
            "est_$", "turns", "wall_s"]
    table = [head]
    for r in report["rows"]:
        mark = "+" if r["partial"] else ""
        has = "input_tokens" in r
        table.append([r["label"], r["agent_type"], r["model"], _cell(r["turn1_context"]),
                      _cell(r["peak_context"]),
                      *(_cell(r.get(f) if has else None) for f in TOKEN_FIELDS),
                      _usd(r.get("usd")) + ("*" if r.get("unpriced") else ""),
                      _cell(r["turns"], mark), _cell(r["wall_seconds"])])
    widths = [max(len(row[i]) for row in table) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    total = report.get("run_total") or {}
    lines.append("")
    lines.append(f"run total: {total.get('total_tokens', 0)} tok, {_usd(total.get('usd'))}"
                 + (f" ({total['unpriced_tokens']} tok unpriced, * rows)"
                    if total.get("unpriced_tokens") else ""))
    for model, b in sorted((report.get("by_model") or {}).items()):
        lines.append(f"  {model}: {b['agents']} agent(s), {b['total_tokens']} tok, {_usd(b['usd'])}")
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
