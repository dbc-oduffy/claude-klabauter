#!/usr/bin/env python3
"""workflow-journal — read a run's journal.jsonl without a guard-blocked ad-hoc script.

WHY THIS EXISTS. `journal.jsonl` (the emitted `.workflow.mjs` script's own event
log, see `coordinator_core/workflow_watch/`) is the fastest way to see what a run
actually did, but a one-off `python3 <inline-script>` to parse it is exactly the
shape `block-subagent-raw-python-invoke`-class guards refuse, so the only route
left was `grep`ing raw JSON lines by hand -- unreadable, and blind to which agent
a truncated line belongs to. This is a tiny, permanent, naked-Python reader: one
line per agent label, its last-seen status, and the first line of its result (or
failure), reading the SAME event shape `coordinator_core/workflow_watch/render.py`
already renders incrementally for the live-follow case.

NOT a JournalRenderer reuse: this is a ONE-SHOT read over a (usually finished)
journal, printed as a per-agent summary table rather than a chronological event
stream -- a different question ("what did each agent end up doing") than
render.py's ("what happened, in order, as it happens"). Reading the whole file
once is fine here; there is no poll loop to make repeated re-parses expensive.

Journal event shape (one JSON object per line): `{"agentId": ..., "type":
"started"|"result"|"failed", "label": ..., "result": ...}`. A line that fails to
parse as JSON, or carries no `type`, is skipped -- never a crash.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREADABLE = 3


def _first_line(text: str) -> str:
    if not isinstance(text, str):
        return ""
    return text.splitlines()[0] if text.splitlines() else ""


def summarize(journal_path: str) -> "dict[str, dict]":
    """Fold `journal_path`'s events into one summary per agentId, in
    first-seen order. Returns ``{agentId: {"label": str, "status": str,
    "result": str}}``. A malformed line is skipped, not fatal -- a
    partially-written journal (a run still in flight) still summarizes
    whatever landed so far."""
    agents: "dict[str, dict]" = {}
    with open(journal_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            agent_id = event.get("agentId")
            if not isinstance(event_type, str):
                continue
            if not isinstance(agent_id, str) or not agent_id:
                # The single terminal-stamp line carries no agentId; it is a
                # run-level event, not a per-agent one -- out of scope for
                # this per-agent summary.
                continue
            entry = agents.setdefault(
                agent_id, {"label": agent_id, "status": "unknown", "result": ""}
            )
            label = event.get("label")
            if isinstance(label, str) and label:
                entry["label"] = label
            if event_type == "started":
                entry["status"] = "started"
            elif event_type == "result":
                entry["status"] = "result"
                result = event.get("result")
                entry["result"] = _first_line(result) if isinstance(result, str) else ""
            elif event_type == "failed":
                entry["status"] = "FAILED"
                result = event.get("result") or event.get("error")
                entry["result"] = _first_line(result) if isinstance(result, str) else ""
    return agents


def _int_field(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _transcript_usage(path: Path) -> "tuple[int, int]":
    """(fresh+output, cache_read) summed over assistant records, one count per
    `message.id`: a message split across content blocks repeats its usage."""
    seen: "set[str]" = set()
    fresh = cache_read = 0
    with open(path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            message_id = message.get("id")
            if isinstance(message_id, str) and message_id:
                if message_id in seen:
                    continue
                seen.add(message_id)

            fresh += (_int_field(usage, "input_tokens") + _int_field(usage, "cache_creation_input_tokens")
                      + _int_field(usage, "output_tokens"))
            cache_read += _int_field(usage, "cache_read_input_tokens")
    return fresh, cache_read


def phase_tokens(journal_path: str) -> "dict[str, dict]":
    """Per-phase token totals from the `agent-<id>.meta.json` / `.jsonl` pairs
    beside `journal_path`. Returns ``{phase: {"agents": int, "models": [str],
    "fresh": int, "cache_read": int}}``. A meta without a readable transcript,
    or an unreadable meta, is skipped."""
    phases: "dict[str, dict]" = {}
    for meta_path in sorted(Path(journal_path).resolve().parent.glob("agent-*.meta.json")):
        transcript = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            fresh, cache_read = _transcript_usage(transcript)
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        phase = meta.get("workflowPhase")
        entry = phases.setdefault(
            phase if isinstance(phase, str) and phase else "(no phase)",
            {"agents": 0, "models": [], "fresh": 0, "cache_read": 0},
        )
        entry["agents"] += 1
        entry["fresh"] += fresh
        entry["cache_read"] += cache_read
        model = meta.get("model")
        if isinstance(model, str) and model and model not in entry["models"]:
            entry["models"].append(model)
    return phases


def _print_tokens(phases: "dict[str, dict]") -> None:
    print(f"{'phase':<28} {'agents':>6}  {'model':<16} {'fresh+output':>13} {'cache_read':>12}")
    for name, e in phases.items():
        print(f"{name:<28} {e['agents']:>6}  {','.join(e['models']):<16} "
              f"{e['fresh']:>13} {e['cache_read']:>12}")
    print(f"{'TOTAL':<28} {sum(e['agents'] for e in phases.values()):>6}  {'':<16} "
          f"{sum(e['fresh'] for e in phases.values()):>13} "
          f"{sum(e['cache_read'] for e in phases.values()):>12}")


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(
        prog="workflow-journal",
        description="Print each agent label's status and the first line of its result "
        "from a run's journal.jsonl.",
    )
    parser.add_argument("journal", help="path to the run's journal.jsonl")
    parser.add_argument(
        "--label", default=None, help="print only the agent whose label matches exactly"
    )
    parser.add_argument(
        "--tokens", action="store_true",
        help="print a per-phase token table from the agent transcripts beside the journal",
    )
    args = parser.parse_args(argv[1:])

    if args.tokens:
        phases = phase_tokens(args.journal)
        if not phases:
            print("workflow-journal: no agent transcripts beside this journal", file=sys.stderr)
            return EXIT_OK
        _print_tokens(phases)
        return EXIT_OK

    try:
        agents = summarize(args.journal)
    except OSError as exc:
        print(f"workflow-journal: cannot read {args.journal!r}: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE

    if args.label is not None:
        agents = {aid: entry for aid, entry in agents.items() if entry["label"] == args.label}
        if not agents:
            print(f"workflow-journal: no agent labeled {args.label!r} in this journal",
                  file=sys.stderr)
            return EXIT_USAGE

    if not agents:
        print("workflow-journal: no per-agent events in this journal", file=sys.stderr)
        return EXIT_OK

    for entry in agents.values():
        line = f"{entry['label']}  {entry['status']}"
        if entry["result"]:
            line += f": {entry['result']}"
        print(line)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
