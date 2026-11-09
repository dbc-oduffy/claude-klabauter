"""Reviewed carve-out registry for the `abs-path-ok:` marker.

A marker discharges an absolute-path literal only when it names a registry key
(`abs-path-ok: key=<k>`), names a carve-out whose `paths` include the file
(`abs-path-ok: carve-out=<id>`), or is a bare-reason marker on a line listed
verbatim in a carve-out's `sites`. The registry is `<root>/.abs-path-carve-outs.toml`.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, Tuple

from coordinator_core.ops.session.guard_concrete_path_citations import _MARKER_RE

CARVE_OUTS_FILENAME = ".abs-path-carve-outs.toml"

CODE_SURFACE_EXTS: Tuple[str, ...] = (
    ".py", ".pyw", ".ps1", ".psm1", ".sh", ".bash", ".cmd", ".bat", ".js", ".mjs", ".ts",
)

_GLOB_CHARS = frozenset("*?[]{}")
_KEY_RE = re.compile(r"key=(\S+)")
_CARVE_RE = re.compile(r"carve-out=(\S+)")


@dataclass(frozen=True)
class CarveOuts:
    """Loaded registry. `malformed` is non-empty when the file failed to parse
    (the caller reports it as one finding); `rejected` lists dropped entries."""

    keys: FrozenSet[str] = frozenset()
    by_id: Dict[str, FrozenSet[str]] = field(default_factory=dict)
    sites: FrozenSet[Tuple[str, str]] = frozenset()
    malformed: str = ""
    rejected: Tuple[str, ...] = ()


def _posix(p: str) -> str:
    return p.replace("\\", "/")


def load_carve_outs(root: Path) -> CarveOuts:
    """Read `<root>/.abs-path-carve-outs.toml`; never raises. Missing file -> empty."""
    path = Path(root) / CARVE_OUTS_FILENAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return CarveOuts()
    except OSError as exc:
        return CarveOuts(malformed=f"{CARVE_OUTS_FILENAME}: unreadable: {exc}")
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return CarveOuts(malformed=f"{CARVE_OUTS_FILENAME}: malformed: {exc}")

    rejected = []
    keys: set = set()
    reg = data.get("registry_keys")
    if isinstance(reg, dict) and isinstance(reg.get("keys"), list):
        keys = {k for k in reg["keys"] if isinstance(k, str) and k.strip()}

    by_id: Dict[str, FrozenSet[str]] = {}
    sites: set = set()
    entries = data.get("carve_out")
    if not isinstance(entries, list):
        entries = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            rejected.append(f"carve_out[{i}]: not a table")
            continue
        cid = entry.get("id")
        reason = entry.get("reason")
        if not isinstance(cid, str) or not cid.strip():
            rejected.append(f"carve_out[{i}]: missing id")
            continue
        if not isinstance(reason, str) or not reason.strip():
            rejected.append(f"carve_out[{cid}]: missing reason")
            continue
        paths = set()
        for p in entry.get("paths") or []:
            if not isinstance(p, str) or not p.strip():
                rejected.append(f"carve_out[{cid}]: bad path entry {p!r}")
            elif _GLOB_CHARS & set(p):
                rejected.append(f"carve_out[{cid}]: glob path {p!r}")
            else:
                paths.add(_posix(p))
        by_id[cid] = frozenset(paths) | by_id.get(cid, frozenset())
        for s in entry.get("sites") or []:
            sp, sl = (s.get("path"), s.get("line")) if isinstance(s, dict) else (None, None)
            if isinstance(sp, str) and isinstance(sl, str) and sp.strip() and sl.strip():
                sites.add((_posix(sp), sl.strip()))
            else:
                rejected.append(f"carve_out[{cid}]: bad site entry {s!r}")
    return CarveOuts(
        keys=frozenset(keys), by_id=by_id, sites=frozenset(sites), rejected=tuple(rejected)
    )


def marker_is_discharged(line: str, rel_path: str, carve_outs: CarveOuts) -> bool:
    """True iff `line` carries an abs-path-ok marker backed by the registry.

    key=<k> needs k in `keys`; carve-out=<id> needs `rel_path` in that id's
    paths; a bare-reason marker needs (rel_path, stripped line) in `sites`.
    """
    rel = _posix(rel_path)
    for m in _MARKER_RE.finditer(line):
        rest = line[m.end():].strip()
        if not rest:
            continue
        km = _KEY_RE.match(rest)
        if km:
            if km.group(1) in carve_outs.keys:
                return True
            continue
        cm = _CARVE_RE.match(rest)
        if cm:
            if rel in carve_outs.by_id.get(cm.group(1), frozenset()):
                return True
            continue
        if (rel, line.strip()) in carve_outs.sites:
            return True
    return False
