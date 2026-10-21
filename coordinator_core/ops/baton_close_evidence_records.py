"""Sizing and warp run-record evidence for the retroactive baton close backstop.

Read-only: stats and small reads, no spawn, no write. Each reader returns evidence dicts in the
pinned shape (kind, reads, strength, proposes, source, detail) keyed by the baton's repo-relative
posix path; an absent key means no evidence. Imports nothing from the predicate module, so the
kind strings are literals that the backstop test cross-checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from coordinator_core.ops.deliverable_cascade import _SIZING_TERMINAL_STATUS, _read_sizing_meta
from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton
from coordinator_core.ops.fleet.archive_sizings import _derive_yyyy_mm
from coordinator_core.session import record_homes

_SIZING_TERMINAL = "SIZING_TERMINAL"
_SIZING_DECLINED = "SIZING_DECLINED"
_SIZING_MISSING = "SIZING_MISSING"
_RUN_COMMITTED = "RUN_COMMITTED"
_RUN_HALTED = "RUN_HALTED"


def _evidence(kind: str, reads: str, strength: str, proposes: str | None, source: str, detail: str) -> dict:
    return {
        "kind": kind,
        "reads": reads,
        "strength": strength,
        "proposes": proposes,
        "source": source,
        "detail": detail,
    }


def _norm_rel(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    posix = value.replace("\\", "/").strip()
    return posix or None


def _resolve_sizing(root: Path, sizing_rel: str) -> tuple[str, str | None] | None:
    """(repo-relative source, status) for a cited sizing, live then archived; None when neither
    resolves. A present-but-unreadable record yields status None."""
    name = sizing_rel.rsplit("/", 1)[-1]
    candidates = [(Path(record_homes.home_dir(str(root), "sizings")) / name)]
    yyyy_mm = _derive_yyyy_mm(name)
    if yyyy_mm:
        candidates.append(root / "archive" / "sizings" / yyyy_mm / name)
    for path in candidates:
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        try:
            status = _read_sizing_meta(str(path)).get("status")
        except Exception:
            return rel, None
        return rel, status if isinstance(status, str) else None
    return None


def sizing_evidence(worktree_root: Path, batons: dict[str, dict]) -> dict[str, list[dict]]:
    """Evidence from each baton's cited `sizing_object`, one read per distinct sizing."""
    root = Path(worktree_root)
    cache: dict[str, tuple[str, str | None] | None] = {}
    out: dict[str, list[dict]] = {}
    for baton_rel, fm in batons.items():
        cited = _norm_rel((fm or {}).get("sizing_object"))
        if cited is None:
            continue
        if cited not in cache:
            cache[cited] = _resolve_sizing(root, cited)
        found = cache[cited]
        if found is None:
            out[baton_rel] = [_evidence(_SIZING_MISSING, "live", "weak", None, cited, "sizing resolves nowhere")]
            continue
        source, status = found
        if status in _SIZING_TERMINAL_STATUS:
            if status == "shipped":
                ev = _evidence(_SIZING_TERMINAL, "terminal", "strong", "shipped", source, "sizing status shipped")
            else:
                ev = _evidence(_SIZING_DECLINED, "terminal", "weak", "abandoned", source, "sizing status declined")
            out[baton_rel] = [ev]
    return out


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run_record_evidence(worktree_root: Path, batons: dict[str, dict]) -> dict[str, list[dict]]:
    """Evidence from warp run records that name a baton by `run-baton.json` or by `run.json`
    `sizing_rel` equal to the baton's `sizing_object`. A halted run is live evidence; a run with
    an outcome and no halt is committed; a run with neither is in flight and contributes nothing."""
    root = Path(worktree_root)
    warp = Path(record_homes.home_dir(str(root), "warp"))
    if not warp.is_dir():
        return {}
    by_sizing: dict[str, list[str]] = {}
    for baton_rel, fm in batons.items():
        cited = _norm_rel((fm or {}).get("sizing_object"))
        if cited:
            by_sizing.setdefault(cited, []).append(baton_rel)
    out: dict[str, list[dict]] = {}
    for entry in sorted(os.scandir(warp), key=lambda e: e.name):
        if not entry.is_dir():
            continue
        run_dir = Path(entry.path)
        run_id = entry.name
        named: set[str] = set()
        direct = read_run_baton(root, run_id)
        if direct is not None and direct in batons:
            named.add(direct)
        run = _read_json(run_dir / "run.json")
        sizing_rel = _norm_rel(run.get("sizing_rel")) if run else None
        if sizing_rel:
            named.update(by_sizing.get(sizing_rel, ()))
        if not named:
            continue
        source = (run_dir / "run.json").relative_to(root).as_posix()
        if (run_dir / "halted.json").is_file():
            ev = _evidence(_RUN_HALTED, "live", "strong", None, source, f"run {run_id} halted")
        elif (run_dir / "run-outcome.json").is_file():
            ev = _evidence(_RUN_COMMITTED, "terminal", "strong", "shipped", source, f"run {run_id} committed")
        else:
            continue
        for baton_rel in named:
            out.setdefault(baton_rel, []).append(dict(ev))
    return out
