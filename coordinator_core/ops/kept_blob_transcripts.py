"""Which Write, Edit and MultiEdit calls touched these repo paths, across every session transcript?"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

_EDIT_TOOLS = ("Write", "Edit", "MultiEdit")


@dataclass(frozen=True)
class ToolEdit:
    """One successful file-edit tool call; `ts` is UTC-aware, `src` is "<parent>/<file>"."""

    ts: datetime
    tool_use_id: str
    name: str  # Write | Edit | MultiEdit
    input: dict
    src: str


def default_transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


def _prefixes(repo_root: Path) -> List[str]:
    """Lower-cased repo-root prefixes (either drive-letter case, and the git-bash `/<drive>/...` form)."""
    r = str(repo_root).replace("\\", "/").rstrip("/")
    out = [r.lower() + "/"]
    if len(r) > 2 and r[1] == ":":
        out.append("/" + r[0].lower() + r[2:].lower() + "/")
    return out


def _resolve(file_path: str, prefixes: List[str], rels: frozenset) -> Optional[str]:
    p = file_path.replace("\\", "/")
    low = p.lower()
    for pre in prefixes:
        if low.startswith(pre):
            rest = p[len(pre):]
            return rest if rest in rels else None
    return None


def _parse_ts(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _jsonl_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
    elif root.is_dir():
        yield from root.rglob("*.jsonl")


def _needle_variants(needles: Iterable[str]) -> List[bytes]:
    out: List[bytes] = []
    for n in needles:
        out.append(n.encode("utf-8"))
        escaped = json.dumps(n)[1:-1].encode("ascii")
        if escaped not in out:
            out.append(escaped)
    return out


def _scan_file(
    path: Path, needles: List[bytes], prefixes: List[str], rels: frozenset
) -> List[Tuple[str, datetime, str, str, dict]]:
    """Successful (rel, ts, tool_use_id, name, input) calls of one transcript, first timestamped id wins."""
    try:
        data = path.read_bytes()
    except OSError:
        return []
    if not any(n in data for n in needles):
        return []
    calls: Dict[str, Tuple[str, Optional[datetime], str, dict]] = {}
    errored = set()
    for line in data.split(b"\n"):
        if b'"tool_use"' not in line and b'"tool_result"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        ts = _parse_ts(rec.get("timestamp"))
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_result":
                if block.get("is_error") is True and isinstance(block.get("tool_use_id"), str):
                    errored.add(block["tool_use_id"])
            elif kind == "tool_use" and block.get("name") in _EDIT_TOOLS:
                tid = block.get("id")
                inp = block.get("input")
                if not isinstance(tid, str) or not isinstance(inp, dict):
                    continue
                fp = inp.get("file_path")
                rel = _resolve(fp, prefixes, rels) if isinstance(fp, str) else None
                if rel is None:
                    continue
                prior = calls.get(tid)
                if prior is not None and (prior[1] is not None or ts is None):
                    continue
                calls[tid] = (rel, ts, block["name"], inp)
    return [
        (rel, ts, tid, name, inp)
        for tid, (rel, ts, name, inp) in calls.items()
        if tid not in errored and ts is not None
    ]


def collect_edits(repo_root: Path, rels: Iterable[str], roots: Iterable[Path]) -> Dict[str, List[ToolEdit]]:
    """Per rel path, its edits ordered by ts, errored results and duplicate tool_use ids dropped."""
    rel_set = frozenset(r.replace("\\", "/") for r in rels)
    found: Dict[str, List[ToolEdit]] = {r: [] for r in rel_set}
    if not rel_set:
        return found
    needles = _needle_variants({r.rsplit("/", 1)[-1] for r in rel_set})
    prefixes = _prefixes(Path(repo_root))
    for root in roots:
        for f in _jsonl_files(Path(root)):
            src = f"{f.parent.name}/{f.name}"
            for rel, ts, tid, name, inp in _scan_file(f, needles, prefixes, rel_set):
                found[rel].append(ToolEdit(ts=ts, tool_use_id=tid, name=name, input=inp, src=src))
    for rel, edits in found.items():
        edits.sort(key=lambda e: (e.ts, e.src, e.tool_use_id))
        seen: set = set()
        found[rel] = [e for e in edits if not (e.tool_use_id in seen or seen.add(e.tool_use_id))]
    return found
