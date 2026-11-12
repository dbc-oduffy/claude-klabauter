"""Per-phase token and dispatch census over one Claude Code session transcript.

Reads a session JSONL and totals assistant `usage` (input, output, cache-read, cache-creation
tokens) and dispatches (tool_use named Agent, Task, Workflow or Skill) per ceremony phase.
A phase opens on a slash-command marker (`<command-name>/x</command-name>`), a Skill tool_use,
or a "Phase <n>" heading in assistant text; usage before the first marker lands in `preamble`.
Sidechain lines in the transcript, and the files of an optional subagents directory (attributed
to the phase whose time window holds their timestamp), are summed into the dispatching phase.
Assistant lines repeating a `message.id` count once.

Prints one JSON object: {ceremony, phases:[{phase, input_tokens, output_tokens,
cache_read_tokens, cache_creation_tokens, dispatches}], totals}.

    python -m coordinator_core.benchmarks.ceremony_token_census --transcript <session.jsonl>
        [--subagents-dir DIR] [--ceremony NAME]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable, Optional

DISPATCH_TOOLS = frozenset({"Agent", "Task", "Workflow", "Skill"})
PREAMBLE = "preamble"
_COMMAND_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
_PHASE_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?\**[ \t]*Phase[ \t]+(\d+[a-z]?)\b", re.MULTILINE | re.IGNORECASE)
_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens", "dispatches")


def _blank(phase: str) -> dict:
    row: dict = {"phase": phase}
    row.update({f: 0 for f in _FIELDS})
    return row


def _blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _opened_phase(entry: dict) -> Optional[str]:
    """Phase name this line opens, or None."""
    message = entry.get("message") or {}
    for block in _blocks(message):
        if entry.get("type") == "user" and block.get("type") == "text":
            found = _COMMAND_RE.search(block.get("text", ""))
            if found:
                return f"command:{found.group(1)}"
        if entry.get("type") == "assistant":
            if block.get("type") == "tool_use" and block.get("name") == "Skill":
                skill = (block.get("input") or {}).get("skill")
                if skill:
                    return f"skill:{skill}"
            if block.get("type") == "text":
                found = _PHASE_RE.search(block.get("text", ""))
                if found:
                    return f"Phase {found.group(1)}"
    return None


def _usage_row(entry: dict) -> dict:
    message = entry.get("message") or {}
    usage = message.get("usage") or {}
    row = {f: 0 for f in _FIELDS}
    row["input_tokens"] = int(usage.get("input_tokens") or 0)
    row["output_tokens"] = int(usage.get("output_tokens") or 0)
    row["cache_read_tokens"] = int(usage.get("cache_read_input_tokens") or 0)
    row["cache_creation_tokens"] = int(usage.get("cache_creation_input_tokens") or 0)
    row["dispatches"] = sum(
        1 for b in _blocks(message) if b.get("type") == "tool_use" and b.get("name") in DISPATCH_TOOLS
    )
    return row


def _read_lines(path: Path) -> list[dict]:
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
    return entries


def _add(target: dict, row: dict) -> None:
    for f in _FIELDS:
        target[f] += row[f]


def census(
    transcript: Path, subagents_dir: Optional[Path] = None, ceremony: Optional[str] = None
) -> dict:
    """Build the census object for `transcript` (plus every `*.jsonl` under `subagents_dir`)."""
    phases: dict[str, dict] = {PREAMBLE: _blank(PREAMBLE)}
    windows: list[tuple[str, str]] = []  # (start timestamp, phase), in transcript order
    current = PREAMBLE
    seen_ids: set[str] = set()
    first_command: Optional[str] = None

    def charge(entry: dict, phase: str) -> None:
        message = entry.get("message") or {}
        mid = message.get("id")
        if mid:
            if mid in seen_ids:
                return
            seen_ids.add(mid)
        phases.setdefault(phase, _blank(phase))
        _add(phases[phase], _usage_row(entry))

    for entry in _read_lines(transcript):
        opened = _opened_phase(entry)
        if opened is not None:
            current = opened
            phases.setdefault(current, _blank(current))
            windows.append((str(entry.get("timestamp") or ""), current))
            if first_command is None and opened.startswith("command:"):
                first_command = opened.split(":", 1)[1]
        if entry.get("type") == "assistant":
            charge(entry, current)

    if subagents_dir is not None:
        for path in sorted(subagents_dir.glob("*.jsonl")):
            for entry in _read_lines(path):
                if entry.get("type") != "assistant":
                    continue
                stamp = str(entry.get("timestamp") or "")
                phase = PREAMBLE
                for start, name in windows:
                    if start and start <= stamp:
                        phase = name
                charge(entry, phase)

    rows = [row for row in phases.values() if row["phase"] != PREAMBLE or any(row[f] for f in _FIELDS)]
    totals = {f: sum(r[f] for r in rows) for f in _FIELDS}
    return {"ceremony": ceremony or first_command or "unknown", "phases": rows, "totals": totals}


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--transcript", required=True, type=Path, help="session transcript JSONL")
    parser.add_argument("--subagents-dir", type=Path, default=None, help="directory of subagent transcript JSONL")
    parser.add_argument("--ceremony", default=None, help="label; defaults to the first slash command seen")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.transcript.is_file():
        print(f"transcript not found: {args.transcript}", file=sys.stderr)
        return 2
    print(json.dumps(census(args.transcript, args.subagents_dir, args.ceremony), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
