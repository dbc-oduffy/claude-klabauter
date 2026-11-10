"""Status-line field contract: Unimatrix name resolver and the one line formatter.

An unobserved field prints `unknown`, never 0 and never a full gauge.
"""
from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Iterable
from pathlib import Path

from coordinator_core.agent_pipe.contract import peers_path

UNKNOWN = "unknown"
PARTIAL_MARK = "+ [coverage incomplete]"  # appended to the cost with no separator: "~$1.00+ [coverage incomplete]"


def resolve_name(vendor: str, ids: Iterable[str]) -> str | None:
    """Unimatrix name of the single `vendor` peer row whose target is in `ids`; None on zero or several matches.

    A registered session (address_version 1) whose name is among the matches wins.
    """
    wanted = {i for i in ids if isinstance(i, str) and i}
    if not wanted:
        return None
    try:
        value = json.loads(peers_path().read_text(encoding="utf-8"))
        rows = value.get("peers", []) if isinstance(value, dict) else value
        matches = sorted({row["name"] for row in rows if isinstance(row, dict)
                          and row.get("vendor") == vendor and row.get("target") in wanted
                          and isinstance(row.get("name"), str)})
    except (OSError, ValueError, TypeError, KeyError):
        matches = []
    if len(matches) > 1:
        try:
            from coordinator_core.session import external_registry
            for sid in sorted(wanted):
                owned = external_registry.lookup(sid)
                if owned and owned.get("address_version") == 1 and owned.get("name") in matches:
                    return owned["name"]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None
    if matches:
        return matches[0]
    return _session_source_name(vendor, wanted)


_BIN_SCRIPTS: dict[str, object | None] = {}


def _bin_script(filename: str, modname: str):
    """Load a `coordinator/bin` script by path, once per process; None when absent or failing to import.

    Cold load measured 25ms (grok) and 4ms (router). A failed import is cached as None.
    """
    if filename in _BIN_SCRIPTS:
        return _BIN_SCRIPTS[filename]
    _BIN_SCRIPTS[filename] = _load_bin_script(filename, modname)
    return _BIN_SCRIPTS[filename]


def _load_bin_script(filename: str, modname: str):
    path = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / filename
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:
        return None


def _session_source_name(vendor: str, wanted: set[str]) -> str | None:
    """Roster name for a grok or agy session absent from peers.json, derived as the peer router derives it.

    Grok: the `active_sessions.json` row for the session id. Agy: the `agy-legs/<conversation>.json`
    record beside peers.json. Reads only; no liveness probe.
    """
    try:
        if vendor == "grok":
            grok = _bin_script("unimatrix-peer-grok.py", "unimatrix_peer_grok")
            if grok is None:
                return None
            rows = json.loads((grok.grok_home() / "active_sessions.json").read_text(encoding="utf-8"))
            found = sorted({grok.peer_name(r["session_id"], r["cwd"]) for r in rows if isinstance(r, dict)
                            and r.get("session_id") in wanted and isinstance(r.get("cwd"), str) and r["cwd"]})
        elif vendor == "agy":
            router = _bin_script("unimatrix-peer-router.py", "unimatrix_peer_router")
            if router is None:
                return None
            found = []
            for conv in sorted(wanted):
                if not re.fullmatch(r"[A-Za-z0-9_-]+", conv):
                    continue
                try:
                    rec = json.loads((peers_path().parent / "agy-legs" / f"{conv}.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if isinstance(rec, dict) and rec.get("conversation") == conv:
                    found.append(router.readable_peer_name("agy", conv, rec.get("cwd")))
            found = sorted(set(found))
        else:
            return None
        return found[0] if len(found) == 1 else None
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _gauge(pct: float) -> str:
    filled = max(0, min(5, round(pct / 20)))
    return "▰" * filled + "▱" * (5 - filled) + f" {pct:.0f}% ctx"


def format_line(name: str | None, before_compact_pct: float | None, usd: float | None,
                partial: bool = False, middle: tuple[str, ...] = ()) -> str:
    """`name · *middle · gauge · ~$usd`, each missing slot `unknown`; `partial` marks incomplete spend coverage."""
    cost = UNKNOWN if usd is None else f"~${usd:.2f}" + (PARTIAL_MARK if partial else "")
    context = UNKNOWN if before_compact_pct is None else _gauge(before_compact_pct)
    return " · ".join((name or UNKNOWN, *middle, context, cost))
