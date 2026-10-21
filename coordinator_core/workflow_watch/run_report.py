"""Post-run report over a Workflow run's journal: per-leg cost and executor rows past the wall cap.

The journal carries no timestamps, so wall time comes from each agent transcript's first and last
`timestamp`. Token fields, model and est. USD come from a full transcript read
(usage.agent_usage.scan_run); context, turns and wall read only a head and bounded tail, so on a
larger transcript turns and peak context are lower bounds (`partial`). No subprocess.
Never called from the fire op, a hook or an emitted script; the one in-process caller is
workflow_watch's terminal path (a background Monitor process, post-run by definition).

`write_record` writes run-cost.json beside the journal: phase_split, executors and a shrink-only
ratchet against run_cost_ratchet.json (`--tighten` lowers a ceiling a run beat, never raises one).

    python3 -m coordinator_core.workflow_watch.run_report --journal <abs path to journal.jsonl> [--json] [--record] [--tighten]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from typing import Any, Optional

from coordinator_core.ops.dispatch_emit.executor_economy import (
    MEASURED_BASELINE,
    RATCHET_METRICS,
    RUN_COST_FILE,
)
from coordinator_core.usage.agent_usage import agent_meta, scan_run, started_agents as _started_agents
from coordinator_core.usage.price_table import TOKEN_FIELDS
from coordinator_core.workflow_watch.stall import _tail_text, _transcript_path

EXECUTOR_WALL_CAP_SECONDS = 30 * 60
EXECUTOR_LABEL_PREFIX = "work:"
HEAD_BYTES = 256 * 1024
TAIL_BYTES = 64 * 1024
UNKNOWN = "unknown"
RATCHET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_cost_ratchet.json")
_EXECUTOR_METRIC = {
    "executor_peak_context": "peak_context",
    "executor_tool_calls": "tool_calls",
    "executor_max_same_file_reads": "max_same_file_reads",
}


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
    phases = _journal_phases(journal_path)
    split: dict[str, dict[str, Any]] = {}
    executors = []
    for row in rows:
        phase = phases.get(row["agent_id"], UNKNOWN)
        bucket = split.setdefault(phase, {"total_tokens": 0, "share": 0.0})
        bucket["total_tokens"] += row.get("total_tokens") or 0
        scanned = by_id.get(row["agent_id"])
        if scanned is not None and row["label"].startswith(EXECUTOR_LABEL_PREFIX):
            full_peak = scanned.get("peak_context") or 0
            entry = {"label": row["label"], "agent_id": row["agent_id"],
                     "tool_calls": scanned.get("tool_calls", 0),
                     "peak_context": full_peak if full_peak else row["peak_context"],
                     "max_same_file_reads": scanned.get("max_same_file_reads", 0),
                     "partial": row["partial"] and not full_peak}
            executors.append(entry)
    grand = sum(b["total_tokens"] for b in split.values())
    for bucket in split.values():
        bucket["share"] = round(bucket["total_tokens"] / grand, 4) if grand else 0.0
    return {"wall_cap_seconds": EXECUTOR_WALL_CAP_SECONDS, "rows": rows, "over_cap": over,
            "by_model": usage["by_model"], "run_total": usage["run_total"],
            "phase_split": split, "executors": executors}


def _journal_phases(journal_path: str) -> dict[str, str]:
    """agentId -> lower-cased `phase` of its first `started` event; absent phase is "unknown"."""
    out: dict[str, str] = {}
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
                if not isinstance(agent_id, str) or agent_id in out:
                    continue
                phase = event.get("phase")
                out[agent_id] = phase.strip().lower() if isinstance(phase, str) and phase.strip() else UNKNOWN
    except OSError:
        return {}
    return out


def load_ratchet(path: Optional[str] = None) -> dict[str, int]:
    """Tracked ceilings; a missing or unreadable file falls back to MEASURED_BASELINE."""
    try:
        with open(path or RATCHET_FILE, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return dict(MEASURED_BASELINE)
    return {m: data[m] if isinstance(data.get(m), int) else MEASURED_BASELINE[m]
            for m in RATCHET_METRICS}


def ratchet_block(report: dict, ceilings: dict[str, int]) -> dict[str, dict[str, Any]]:
    """{metric: {ceiling, observed, verdict}}; observed is the max over the run's executors."""
    block = {}
    for metric in RATCHET_METRICS:
        key = _EXECUTOR_METRIC[metric]
        observed = max((e[key] or 0 for e in report["executors"]), default=0)
        block[metric] = {"ceiling": ceilings[metric], "observed": observed,
                         "verdict": "breach" if observed > ceilings[metric] else "ok"}
    return block


def tighten(block: dict, path: Optional[str] = None) -> dict[str, int]:
    """Lower each ceiling to a better observed value (only when the run had executors); never raise."""
    target = path or RATCHET_FILE
    ceilings = load_ratchet(target)
    changed = False
    for metric, cell in block.items():
        observed = cell["observed"]
        if observed > 0 and observed < ceilings[metric]:
            ceilings[metric] = observed
            changed = True
    if changed:
        _atomic_write(target, json.dumps(ceilings, indent=2, sort_keys=True) + "\n")
    return ceilings


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_record(journal_path: str, ratchet_path: Optional[str] = None) -> str:
    """Write run-cost.json beside the journal; returns its path. Contains no wall-clock field."""
    report = build_report(journal_path)
    record = {"phase_split": report["phase_split"], "executors": report["executors"],
              "run_total": report["run_total"],
              "ratchet": ratchet_block(report, load_ratchet(ratchet_path))}
    out = os.path.join(os.path.dirname(journal_path), RUN_COST_FILE)
    _atomic_write(out, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return out


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
    parser.add_argument("--record", action="store_true", help="write run-cost.json beside the journal")
    parser.add_argument("--tighten", action="store_true",
                        help="lower any ratchet ceiling this run beat (never raises); implies --record")
    args = parser.parse_args(argv)
    if not os.path.isfile(args.journal):
        print(f"journal not found: {args.journal}", file=sys.stderr)
        return 2
    if args.record or args.tighten:
        path = write_record(args.journal)
        if args.tighten:
            with open(path, encoding="utf-8") as handle:
                tighten(json.load(handle)["ratchet"])
        print(path)
        return 0
    report = build_report(args.journal)
    print(json.dumps(report, sort_keys=True) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
