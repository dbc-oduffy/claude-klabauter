"""Per-run scratch ledger and the repo keep index.

The ledger (`scratch-ledger.json` in the run's record dir) lists the repo-relative scratch paths a
run created plus the paths it keeps with a purpose and TTL; the keep index
(`scratch/.kept-index.json`) mirrors the keeps repo-wide so a reaper can honour a peer's keep.
Invariant: every stored path is "/"-normalised, repo-relative and under scratch/ (or a share-dir review-prep sidecar); nothing here
lists a directory or spawns a process.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.ops.dispatch_emit import ask_contract

LEDGER_FILE = "scratch-ledger.json"
KEEP_INDEX_REL = "scratch/.kept-index.json"
#: Longest TTL a keep may declare (30 days); a longer hold is a purpose for a durable home, not scratch.
MAX_KEEP_TTL_HOURS = 24 * 30

_SCRATCH = "scratch"
_TOMBSTONE = ".deleted"
_HOLD = "scratch-hold"
_STAGE_READERS = "stage-readers"
_LOCAL = ".coordinator-local"
_SHARE = "subagent-share"
_REVIEW_PREP_SUFFIXES = (".review-prep.md", ".review-prep.lock")


def _is_review_prep_sidecar(parts: list[str]) -> bool:
    """True for exactly `.coordinator-local/subagent-share/<segment>/<name>.review-prep.{md,lock}`."""
    if len(parts) != 4 or parts[0] != _LOCAL or parts[1] != _SHARE:
        return False
    return any(len(parts[3]) > len(s) and parts[3].endswith(s) for s in _REVIEW_PREP_SUFFIXES)


def normalise_rel(rel: Any) -> str:
    """The "/"-normalised repo-relative form of `rel`; raises ValueError naming the refusal
    (absolute, `..`, scratch/ itself, tombstones, scratch-hold, stage-readers, outside scratch/).
    Besides scratch/, only a review-prep sidecar or lock in a share dir is admitted."""
    if not isinstance(rel, str) or not rel.strip():
        raise ValueError("path is not a non-empty string")
    norm = rel.replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        raise ValueError(f"path {rel!r} is absolute")
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"path {rel!r} has a '..' segment")
    if _is_review_prep_sidecar(parts):
        return "/".join(parts)
    if not parts or parts[0] != _SCRATCH:
        raise ValueError(f"path {rel!r} is outside scratch/")
    if len(parts) == 1:
        raise ValueError("path is scratch/ itself")
    if parts[1] == _TOMBSTONE:
        raise ValueError(f"path {rel!r} is under scratch/{_TOMBSTONE}/")
    if parts[1] == _HOLD:
        raise ValueError(f"path {rel!r} is under {_HOLD}")
    if parts[1] == _STAGE_READERS:
        raise ValueError(f"path {rel!r} is under scratch/{_STAGE_READERS}/")
    return "/".join(parts)


def ledger_path(root: Path, run_id: str) -> Path:
    """`<record_dir>/scratch-ledger.json`; raises ValueError on an unsafe run_id."""
    return ask_contract.record_dir(Path(root), run_id) / LEDGER_FILE


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def read_ledger(root: Path, run_id: str) -> dict:
    """`{"paths": [rel...], "keeps": [{path, purpose, ttl_hours, kept_at}...]}`; empty when absent."""
    data = _read_json(ledger_path(root, run_id))
    paths = [p for p in data.get("paths", []) if isinstance(p, str)]
    keeps = [k for k in data.get("keeps", []) if isinstance(k, dict)]
    return {"paths": paths, "keeps": keeps}


def record_paths(root: Path, run_id: str, rels: Iterable[str]) -> list[str]:
    """Append `rels` to the run's ledger (deduped, order kept); returns the normalised paths added
    or already present. Raises ValueError, writing nothing, if any path is refused."""
    norm = [normalise_rel(r) for r in rels]
    ledger = read_ledger(root, run_id)
    have = set(ledger["paths"])
    for p in norm:
        if p not in have:
            ledger["paths"].append(p)
            have.add(p)
    _write_json(ledger_path(root, run_id), ledger)
    return norm


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> Optional[datetime]:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def declare_keep(
    root: Path, run_id: str, rel: str, purpose: str, ttl_hours: float, now: Optional[datetime] = None
) -> dict:
    """Keep `rel` past run end for `ttl_hours`: records it in the ledger and upserts the keep index.
    Raises ValueError on an empty purpose, a ttl_hours <= 0 or above MAX_KEEP_TTL_HOURS, or a refused path."""
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("a keep needs a non-empty purpose")
    if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)) or ttl_hours <= 0:
        raise ValueError("a keep needs ttl_hours > 0")
    if ttl_hours > MAX_KEEP_TTL_HOURS:
        raise ValueError(f"ttl_hours {ttl_hours} exceeds the {MAX_KEEP_TTL_HOURS}h cap")
    norm = normalise_rel(rel)
    kept = (now or datetime.now(timezone.utc))
    entry = {"path": norm, "purpose": purpose.strip(), "ttl_hours": ttl_hours, "kept_at": _iso(kept)}
    ledger = read_ledger(root, run_id)
    ledger["keeps"] = [k for k in ledger["keeps"] if k.get("path") != norm] + [entry]
    if norm not in ledger["paths"]:
        ledger["paths"].append(norm)
    _write_json(ledger_path(root, run_id), ledger)
    index_file = Path(root) / KEEP_INDEX_REL
    index = _read_json(index_file)
    index[norm] = {
        "run_id": run_id,
        "purpose": entry["purpose"],
        "expires_at": _iso(kept + timedelta(hours=ttl_hours)),
    }
    _write_json(index_file, index)
    return entry


def read_keep_index(root: Path) -> dict:
    """The repo keep index `{path: {run_id, purpose, expires_at}}`; empty when absent."""
    return _read_json(Path(root) / KEEP_INDEX_REL)


def keep_index_expired(root: Path, now: Optional[datetime] = None) -> list[str]:
    """Paths whose keep has passed `expires_at` (an unparseable expiry counts as expired)."""
    moment = now or datetime.now(timezone.utc)
    out = []
    for path, meta in read_keep_index(root).items():
        stamp = _parse(meta.get("expires_at")) if isinstance(meta, dict) else None
        if stamp is None or stamp <= moment:
            out.append(path)
    return sorted(out)
