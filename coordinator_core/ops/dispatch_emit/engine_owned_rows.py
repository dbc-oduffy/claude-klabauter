"""Which rows of a run are delivered in the engine plane, and which root each was owed.

A run fired from a non-engine cwd judges its rows against the cwd repo, which cannot hold a
row delivered in the engine. `engine_owner_rows` names those rows in the owner-clause row shape
(`execute_review.owner_repo_clause`) with the resolved engine root and its resolution class;
`routed_roots` reads the same routing back from a stage manifest for the terminal ledger gate.

Ownership rule (cwd is not itself an engine checkout, i.e. no `coordinator_core/` beside a
`pyproject.toml` at the cwd root; in an engine cwd every row is cwd-owned). A row is engine-owned
when any of:

1. Plan row: an `external_gate[]` or `external_reads_ungated[]` entry, cleared or not, whose
   `owner_repo` casefolds into `ENGINE_OWNER_REPOS`.
2. Plan row: a `writes`/`writes_under`/`surface` path, or an evidence-sidecar entry for the row,
   naming `coordinator_core`.
3. Register row: `source_text` or `met_by` naming `coordinator_core`, `claude-klabauter` or
   `klabauter`.

An uncleared execution-blocking gated row stays on the gated path; the caller removes those ids
before calling. Classification reads text only: no git, no subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

import yaml

from coordinator_core import engine_root

ENGINE_OWNER_REPOS = ("claude-klabauter", "claude-klabauter")

UNRESOLVED_CLASS = "unresolved"


def resolve_engine_root() -> tuple[str, str]:
    """`(root, resolution_class)` of the engine from `engine_root.coordinator_engine_root_with_class`;
    `("", "unresolved")` when the engine cannot be resolved on this machine."""
    try:
        return engine_root.coordinator_engine_root_with_class()
    except RuntimeError:
        return "", UNRESOLVED_CLASS


def engine_owner_rows(
    plan_raw_rows: Sequence[Mapping],
    register_rows: Sequence[Mapping],
    evidence_text: str,
    cwd_root: object,
) -> list[dict]:
    """Rows delivered in the engine plane, per the module's ownership rule.

    Each is `{id, owner_repo: "claude-klabauter", owner_root: <posix root, "" when unresolved>,
    root_class, engine: True}`. Empty when `cwd_root` is itself an engine checkout or no row
    carries an engine signal. `owner_root` comes only from `resolve_engine_root`, called once
    and only when at least one row classifies."""
    if _is_engine_checkout(cwd_root):
        return []
    ids: list[str] = []
    for row in plan_raw_rows:
        if isinstance(row, Mapping) and _plan_row_is_engine(row):
            ids.append(str(row.get("id")))
    evidence = _evidence_by_row(evidence_text)
    for row_id, text in evidence.items():
        if _ENGINE_PATH in text.casefold():
            ids.append(row_id)
    for row in register_rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        text = f"{row.get('source_text') or ''}\n{row.get('met_by') or ''}".casefold()
        if any(token in text for token in _REGISTER_TOKENS):
            ids.append(row["id"])
    ids = list(dict.fromkeys(i for i in ids if i and i != "None"))
    if not ids:
        return []
    root, root_class = resolve_engine_root()
    root = root.replace("\\", "/")
    return [
        {
            "id": i,
            "owner_repo": "claude-klabauter",
            "owner_root": root,
            "root_class": root_class,
            "engine": True,
        }
        for i in ids
    ]


_ENGINE_PATH = "coordinator_core"
_REGISTER_TOKENS = (_ENGINE_PATH, "claude-klabauter", "klabauter")
_OWNER_LISTS = ("external_gate", "external_reads_ungated")
_PATH_FIELDS = ("writes", "writes_under", "surface")


def _is_engine_checkout(cwd_root: object) -> bool:
    root = Path(str(cwd_root))
    return (root / "coordinator_core").is_dir() and (root / "pyproject.toml").is_file()


def _as_list(value: object) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _plan_row_is_engine(row: Mapping) -> bool:
    for key in _OWNER_LISTS:
        for entry in _as_list(row.get(key)):
            owner = entry.get("owner_repo") if isinstance(entry, Mapping) else None
            if isinstance(owner, str) and owner.strip().casefold() in ENGINE_OWNER_REPOS:
                return True
    for key in _PATH_FIELDS:
        for path in _as_list(row.get(key)):
            if isinstance(path, str) and _ENGINE_PATH in path.casefold():
                return True
    return False


def _evidence_by_row(evidence_text: str) -> dict[str, str]:
    """`{row id: joined entry text}` from an evidence sidecar; `{}` when absent or unparseable."""
    if not evidence_text or not evidence_text.strip():
        return {}
    try:
        data = yaml.safe_load(evidence_text)
    except yaml.YAMLError:
        return {}
    rows = data.get("rows") if isinstance(data, Mapping) else None
    if not isinstance(rows, Mapping):
        return {}
    out: dict[str, str] = {}
    for row_id, entries in rows.items():
        parts = [
            str(e.get("text") or "") if isinstance(e, Mapping) else str(e)
            for e in _as_list(entries)
        ]
        out[str(row_id)] = "\n".join(parts)
    return out


def routed_roots(manifest: Mapping) -> dict[str, str]:
    """`{row id: engine root}` from a stage manifest's `engine_rows`; `{}` when it carries none."""
    rows = manifest.get("engine_rows") if isinstance(manifest, Mapping) else None
    if not isinstance(rows, list):
        return {}
    return {
        r["id"]: r.get("owner_root") or ""
        for r in rows
        if isinstance(r, Mapping) and isinstance(r.get("id"), str)
    }
