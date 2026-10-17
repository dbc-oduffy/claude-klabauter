"""Whole-session estimated USD from an incremental per-file offset + running-sum cache.

Covers the main transcript, `<session>/subagents/agent-*.jsonl` and
`<session>/subagents/workflows/wf_*/agent-*.jsonl`. A render parses only bytes
appended past each file's cached offset, lists the tree at most once per
_RESCAN_AFTER_SEC, and absorbs at most a byte budget of backlog. Stdlib plus
price_table only; no subprocess; never raises to the caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.usage.price_table import TOKEN_FIELDS, estimate_usd

# Provisional; C0 of statusline-session-cost-field measures and confirms or replaces them.
_DEDUPE_WINDOW = 32
BYTE_BUDGET = 4 * 1024 * 1024
_RESCAN_AFTER_SEC = 10.0

_CACHE_VERSION = 1


def usage_line(raw: str, n: int) -> Optional[tuple]:
    """(key, model, usage, speed) of one transcript line, or None when it carries no usage dict.

    key is (message.id, requestId), else ("line", n); n must be unique within the file.
    """
    if '"usage"' not in raw:
        return None
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    msg = event.get("message") if isinstance(event, dict) else None
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    mid, rid = msg.get("id"), event.get("requestId")
    key = (mid, rid) if mid is not None or rid is not None else ("line", n)
    speed = usage.get("speed")
    return key, msg.get("model"), usage, speed if isinstance(speed, str) else None


def session_files(session_dir: Path) -> list[Path]:
    """Agent transcripts under session_dir: subagents/ and subagents/workflows/wf_*/; never journal.jsonl."""
    out: list[Path] = []

    def agents_in(directory: str) -> list[str]:
        try:
            with os.scandir(directory) as it:
                return sorted(e.path for e in it
                              if e.name.startswith("agent-") and e.name.endswith(".jsonl"))
        except OSError:
            return []

    sub = os.path.join(str(session_dir), "subagents")
    out.extend(Path(p) for p in agents_in(sub))
    try:
        with os.scandir(os.path.join(sub, "workflows")) as it:
            wf_dirs = sorted(e.path for e in it if e.name.startswith("wf_"))
    except OSError:
        wf_dirs = []
    for d in wf_dirs:
        out.extend(Path(p) for p in agents_in(d))
    return out


def _empty_cache(session_id: str) -> dict[str, Any]:
    return {"version": _CACHE_VERSION, "session_id": session_id, "scanned_at": 0.0,
            "usd_total": 0.0, "unpriced_tokens": 0, "complete": False, "files": {}}


def _load_cache(cache_path: Path, session_id: str) -> dict[str, Any]:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_cache(session_id)
    if (not isinstance(data, dict) or data.get("version") != _CACHE_VERSION
            or data.get("session_id") != session_id or not isinstance(data.get("files"), dict)):
        return _empty_cache(session_id)
    return data


def _write_cache(cache_path: Path, record: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, cache_path)


def _price(model: Any, usage: dict, speed: Optional[str]) -> tuple[float, int]:
    """(usd, unpriced_tokens) for one message; unknown model adds tokens, no USD."""
    tokens = 0
    for f in TOKEN_FIELDS:
        v = usage.get(f)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            tokens += int(v)
    usd = estimate_usd(model, usage, speed) if isinstance(model, str) and model else None
    return (usd, 0) if usd is not None else (0.0, tokens)


def _apply(cache: dict[str, Any], entry: dict[str, Any], chunk: bytes, base: int) -> None:
    """Fold the complete lines of chunk (starting at absolute offset base) into entry and cache."""
    window: dict[str, list] = entry["window"]
    pos = 0
    for line in chunk.split(b"\n"):
        start = base + pos
        pos += len(line) + 1
        if not line:
            continue
        parsed = usage_line(line.decode("utf-8", errors="replace"), start)
        if parsed is None:
            continue
        key, model, usage, speed = parsed
        usd, unp = _price(model, usage, speed)
        if key[0] == "line":
            entry["usd"] += usd
            cache["usd_total"] += usd
            entry["unpriced"] += unp
            cache["unpriced_tokens"] += unp
            continue
        skey = json.dumps(list(key))
        prior = window.pop(skey, None)
        if prior is not None:
            usd_delta, unp_delta = usd - prior[0], unp - prior[1]
        else:
            usd_delta, unp_delta = usd, unp
        entry["usd"] += usd_delta
        cache["usd_total"] += usd_delta
        entry["unpriced"] += unp_delta
        cache["unpriced_tokens"] += unp_delta
        window[skey] = [usd, unp]
        while len(window) > _DEDUPE_WINDOW:
            del window[next(iter(window))]


def _reset(cache: dict[str, Any], entry: dict[str, Any]) -> None:
    cache["usd_total"] -= entry["usd"]
    cache["unpriced_tokens"] -= entry["unpriced"]
    entry.update(offset=0, usd=0.0, unpriced=0, window={})


def advance(transcript_path: str, cache_path: Path, *, now: float, byte_budget: int) -> dict:
    """Fold bytes appended since the last read into the cache; {"usd", "complete", "unpriced_tokens", "files"}.

    Lists the tree only when the cache is older than _RESCAN_AFTER_SEC; while a
    backlog remains it keeps reading the already-known files without relisting.
    files is the count of transcripts known (0 = nothing readable).
    """
    result = {"usd": 0.0, "complete": False, "unpriced_tokens": 0, "files": 0}
    if not transcript_path:
        return result
    try:
        main = Path(transcript_path)
        cache = _load_cache(cache_path, main.stem)
        base = main.parent
        files: dict[str, dict] = cache["files"]
        rescan = now - cache["scanned_at"] >= _RESCAN_AFTER_SEC
        dirty = False
        if rescan:
            paths = [main, *session_files(main.with_suffix(""))]
            for p in paths:
                if not p.is_file():
                    continue
                rel = p.relative_to(base).as_posix() if p.is_relative_to(base) else p.name
                files.setdefault(rel, {"offset": 0, "usd": 0.0, "unpriced": 0, "window": {}, "size": 0})
            cache["scanned_at"] = now
            # Read by statusline's no-engine fast path: before this instant a complete
            # cache needs no advance(), so the render skips importing the engine.
            cache["fresh_until"] = now + _RESCAN_AFTER_SEC
            dirty = True
        elif cache["complete"] and _main_unchanged(cache, main):
            return _summary(cache)
        remaining = max(0, byte_budget)
        complete = True
        for rel, entry in files.items():
            try:
                size = (os.stat(base / rel).st_size
                        if rescan or rel == main.name or entry["size"] > entry["offset"] else entry["size"])
            except OSError:
                continue
            entry["size"] = size
            if size < entry["offset"]:
                _reset(cache, entry)
                dirty = True
            if size <= entry["offset"]:
                continue
            want = size - entry["offset"]
            if remaining < want:
                complete = False
            if remaining <= 0:
                continue
            with open(base / rel, "rb") as handle:
                handle.seek(entry["offset"])
                chunk = handle.read(min(want, remaining))
            cut = chunk.rfind(b"\n")
            if cut < 0:
                # A line longer than the budget is never absorbed; the "+" marker stays.
                continue
            chunk = chunk[:cut + 1]
            _apply(cache, entry, chunk, entry["offset"])
            entry["offset"] += len(chunk)
            remaining -= len(chunk)
            dirty = True
        main_entry = files.get(main.name)
        if main_entry is not None and cache.get("main_size") != main_entry["size"]:
            cache["main_size"] = main_entry["size"]
            dirty = True
        if cache["complete"] != complete:
            dirty = True
        cache["complete"] = complete
        if dirty:
            _write_cache(cache_path, cache)
        return _summary(cache)
    except (OSError, ValueError, KeyError, TypeError):
        return result


def _main_unchanged(cache: dict[str, Any], main: Path) -> bool:
    """One stat: the main transcript is still the size the cache's `main_size` recorded."""
    try:
        return os.stat(main).st_size == cache.get("main_size")
    except OSError:
        return True


def _summary(cache: dict[str, Any]) -> dict:
    return {"usd": max(0.0, cache["usd_total"]), "complete": bool(cache["complete"]),
            "unpriced_tokens": max(0, cache["unpriced_tokens"]), "files": len(cache["files"])}


def render(result: Optional[dict]) -> Optional[str]:
    """`~$N.NN`, `~$N.NN+` while backlog remains, None when nothing was readable."""
    if not result or not result.get("files"):
        return None
    return f"~${result['usd']:.2f}" + ("" if result["complete"] else "+")
