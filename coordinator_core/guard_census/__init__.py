"""guard_census -- one fate per guard, hook and op refusal, reconciled against deny_registry.

Each plane module (``bash_guards``, ``write_guards``, ``hooks``, ``op_refusals``) exposes
``ROWS: list[Row]``. A plane module may be absent until its owner fills it; ``reconcile``
checks only the planes present. Not imported on the hook hot path.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from coordinator_core import deny_registry

BLOCK = "block"
GUIDE = "guide"
DELETED = "deleted"
FATES = frozenset({BLOCK, GUIDE, DELETED})

PLANES = ("bash_guards", "write_guards", "hooks")
_ROOT = Path(deny_registry.__file__).resolve().parent


@dataclass(frozen=True)
class Row:
    """``name`` is kebab-case. ``redirect`` is required for ``block`` and names the elegant path."""

    name: str
    fate: str
    redirect: str = ""
    note: str = ""


def enumerate_bash_guards() -> List[str]:
    """Every bash-dispatch guard id on the roster."""
    from coordinator_core.bash_guards.roster import guard_roster

    return sorted({e.id for e in guard_roster()})


def _stems(sub: str) -> List[str]:
    out = set()
    for path in (_ROOT / sub).rglob("*.py"):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        if path.stem.startswith("_") or path.stem in ("__main__", "conftest"):
            continue
        out.add(path.stem.replace("_", "-"))
    return sorted(out)


def enumerate_write_guards() -> List[str]:
    """Every public module under ``coordinator_core/write_guards``, kebab-cased."""
    return _stems("write_guards")


def enumerate_hooks() -> List[str]:
    """Every public module under ``coordinator_core/hooks``, kebab-cased."""
    return _stems("hooks")


def op_refusal_sites() -> Dict[str, int]:
    """Every ``raise *Refus*`` site under ``coordinator_core/ops`` (``deny_registry.op_refusal_sites``)."""
    return deny_registry.op_refusal_sites(_ROOT / "ops")


_ENUMERATORS = {
    "bash_guards": enumerate_bash_guards,
    "write_guards": enumerate_write_guards,
    "hooks": enumerate_hooks,
}


def plane_rows(plane: str) -> List[Row] | None:
    """The plane's ``ROWS``, or ``None`` while its module is absent."""
    try:
        mod = importlib.import_module(f"{__name__}.{plane}")
    except ModuleNotFoundError as exc:
        if exc.name == f"{__name__}.{plane}":
            return None
        raise
    return list(mod.ROWS)


def reconcile() -> List[str]:
    """Problems across every present plane; empty means the census and registry agree."""
    problems: List[str] = []
    blocks: set[str] = set()
    all_present = True
    for plane in PLANES:
        rows = plane_rows(plane)
        if rows is None:
            all_present = False
            continue
        existing = set(_ENUMERATORS[plane]())
        seen: set[str] = set()
        for row in rows:
            where = f"{plane}:{row.name}"
            if row.fate not in FATES:
                problems.append(f"{where}: fate {row.fate!r} is not block|guide|deleted")
                continue
            if row.name in seen:
                problems.append(f"{where}: row listed twice")
            seen.add(row.name)
            if row.fate == DELETED and row.name in existing:
                problems.append(f"{where}: deleted but the module is still present")
            if row.fate != DELETED and row.name not in existing:
                problems.append(f"{where}: row names no {plane} module")
            if row.fate == BLOCK:
                blocks.add(row.name)
                if not row.redirect.strip():
                    problems.append(f"{where}: block row has no redirect")
                if row.name not in deny_registry.GUARD_DENY_CLASS and row.name not in deny_registry.SUBAGENT_DENY_CLASS:
                    problems.append(f"{where}: block row is not in the deny registry")
        problems.extend(f"{plane}:{n}: unfated" for n in sorted(existing - seen))
    op_rows = plane_rows("op_refusals")
    if op_rows is not None:
        sites = op_refusal_sites()
        op_seen: set[str] = set()
        for row in op_rows:
            where = f"op_refusals:{row.name}"
            if row.name in op_seen:
                problems.append(f"{where}: row listed twice")
            op_seen.add(row.name)
            if row.name not in sites:
                problems.append(f"{where}: row names no op refusal site")
            if row.fate != BLOCK or not row.redirect.strip():
                problems.append(f"{where}: op refusal row must be a block with a redirect")
    if all_present:
        registered = set(deny_registry.GUARD_DENY_CLASS) | set(deny_registry.SUBAGENT_DENY_CLASS)
        problems.extend(f"registry:{n}: no block row" for n in sorted(registered - blocks))
    return problems
