"""Pure prod-migration checker: three rules over a change set, no spawn, no IO beyond the readers.

A path is a prod migration iff it matches ``<dir>/migrations/<digits>_<name>.sql`` and
``<dir>/migrations/applied-in-production.json`` exists (landed tree or HEAD). Rules:
  D -- an added migration needs ``<dir>/migrations-down/<stem>.down.sql`` in the landed tree.
  I -- an applied migration (a key of the HEAD manifest) may not be deleted, renamed away or
       changed from its recorded sha256.
  S -- an added migration's number must be in the set ``started()`` returns (called lazily, once).
Reports, never raises: a failing ``started()`` becomes an S refusal carrying its message.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Callable, Dict, FrozenSet, List, NamedTuple, Optional, Sequence

MANIFEST_NAME = "applied-in-production.json"
_MIGRATION = re.compile(r"^(?:(?P<dir>.+)/)?migrations/(?P<num>\d+)_(?P<name>[^/]+)\.sql$")


class Change(NamedTuple):
    """One changed path: ``status`` in A/M/D/R; ``old_path`` is set for R only."""

    status: str
    path: str
    old_path: Optional[str] = None


class Refusal(NamedTuple):
    """One refused path: ``rule`` in D/I/S."""

    rule: str
    path: str
    message: str


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    return p[2:] if p.startswith("./") else p


def _num(digits: str) -> str:
    return digits.lstrip("0") or "0"


def _manifest(raw: Optional[bytes]) -> Optional[Dict[str, object]]:
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def check_prod_migrations(
    changes: Sequence[Change],
    read_landed: Callable[[str], Optional[bytes]],
    read_head: Callable[[str], Optional[bytes]],
    started: Callable[[], FrozenSet[str]],
) -> List[Refusal]:
    """Return one ``Refusal`` per broken rule per migration path; empty means the change passes."""
    manifests: Dict[str, Optional[Dict[str, object]]] = {}
    refusals: List[Refusal] = []
    added: List[tuple] = []

    def head_manifest(prefix: str) -> Optional[Dict[str, object]]:
        key = f"{prefix}/migrations/{MANIFEST_NAME}" if prefix else f"migrations/{MANIFEST_NAME}"
        if key not in manifests:
            head = _manifest(read_head(key))
            if head is None and read_landed(key) is not None:
                head = {}
            manifests[key] = head
        return manifests[key]

    def judge(path: str, status: str) -> None:
        m = _MIGRATION.match(path)
        if m is None:
            return
        prefix = m.group("dir") or ""
        applied = head_manifest(prefix)
        if applied is None:
            return
        base = path.rsplit("/", 1)[-1]
        if base in applied:
            if status == "D":
                refusals.append(Refusal("I", path, f"{base} is applied in production and was deleted. Add a new migration instead."))
                return
            recorded = applied[base]
            landed = read_landed(path)
            digest = hashlib.sha256(landed).hexdigest() if landed is not None else None
            if digest != recorded:
                refusals.append(Refusal("I", path, f"{base} is applied in production and its bytes changed. Add a new migration instead."))
            return
        if status == "A":
            added.append((path, prefix, m.group("num"), m.group("name")))

    for ch in changes:
        status = ch.status.upper()
        path = _norm(ch.path)
        if status == "R":
            if ch.old_path:
                judge(_norm(ch.old_path), "D")
            judge(path, "A")
        elif status in ("A", "M", "D"):
            judge(path, status)

    for path, prefix, num, name in added:
        down = f"{prefix}/migrations-down/{num}_{name}.down.sql" if prefix else f"migrations-down/{num}_{name}.down.sql"
        if read_landed(down) is None:
            refusals.append(Refusal("D", path, f"{path} has no down file. Add {down}."))

    if added:
        try:
            named = frozenset(_num(n) for n in started())
        except Exception as exc:  # the gate reports, never raises
            refusals.extend(Refusal("S", p, f"START record unreadable: {exc}") for p, *_ in added)
        else:
            for path, _prefix, num, _name in added:
                if _num(num) not in named:
                    refusals.append(Refusal("S", path, f"Migration {num} is not named in a G-EM START. Request the START, then commit."))
    return refusals
