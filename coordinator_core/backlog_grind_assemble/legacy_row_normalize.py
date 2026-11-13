"""Engine-side normalization of legacy rows before schema validation.

Shared by lessons admission (`queue_select`) and `grind-row close`, so both gates agree.
Operates on a validation copy only: the row on disk and the DoE-owned schema are never touched.
Derives `scope`/`from_repo` from the row's `repo`/`path` and treats an out-of-enum legacy
`system.provenance_completeness` as advisory (it routes nothing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Mapping

_DERIVED_SCOPE = "project"


@dataclass(frozen=True)
class NormalizedRow:
    """`row` is the validation copy; `advisories` name each tolerated or derived field;
    `handback` is a named reason when a required field cannot be derived, else empty."""

    row: dict
    advisories: tuple[str, ...] = field(default_factory=tuple)
    handback: str = ""


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _repo_from_path(path: Any) -> str:
    """Leading segment of a repo-qualified `<repo>/...` path; empty for anything else.

    Trap: nothing checks the segment names a repo, so an in-repo relative path (`docs/x.md`) yields
    `docs`; only the row's own `repo` field is authoritative."""
    if not isinstance(path, str) or not path.strip():
        return ""
    text = path.strip().replace("\\", "/")
    if PurePosixPath(text).is_absolute() or PureWindowsPath(path.strip()).drive:
        return ""
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".", "..")]
    return parts[0] if len(parts) > 1 else ""


def _derive_from_repo(row: Mapping[str, Any]) -> str:
    repo = row.get("repo")
    if isinstance(repo, str) and repo.strip():
        return repo.strip()
    return _repo_from_path(row.get("path"))


def _enum_of(schema: Mapping[str, Any], *keys: str) -> list:
    node: Any = schema
    for key in keys:
        node = (node.get("properties") or {}).get(key) if isinstance(node, Mapping) else None
    return list(node.get("enum") or []) if isinstance(node, Mapping) else []


def owning_repo_default(repo_root: Any, row_rel_path: Any) -> str:
    """The repo a row with no `repo`/`path` provenance belongs to: `repo_root`'s own name for a row
    in the repo's own queue; empty for a row under a `cross-repo` outbox, whose origin is unknown."""
    parts = PurePosixPath(str(row_rel_path).replace("\\", "/")).parts
    if "cross-repo" in parts:
        return ""
    return PurePosixPath(str(repo_root).replace("\\", "/")).name


def normalize_legacy_row(
    row: Mapping[str, Any], schema: Mapping[str, Any], default_from_repo: str = ""
) -> NormalizedRow:
    """Return a validation copy of `row` with derivable required fields filled in.

    Only fields the schema declares are touched. A present-but-invalid `scope` is left alone
    (a genuine failure). `default_from_repo` fills `from_repo` when the row names no repo itself.
    `handback` names the fields that could not be derived."""
    out = dict(row)
    advisories: list[str] = []
    props = schema.get("properties") or {}
    missing: list[str] = []

    if "from_repo" in props and _blank(out.get("from_repo")):
        derived = _derive_from_repo(out)
        if derived:
            out["from_repo"] = derived
            advisories.append(f"from_repo derived from repo/path: {derived}")
        elif default_from_repo:
            out["from_repo"] = default_from_repo
            advisories.append(f"from_repo defaulted to the owning repo: {default_from_repo}")
        else:
            missing.append("from_repo")

    if "scope" in props and _blank(out.get("scope")):
        if "from_repo" in missing:
            missing.append("scope")
        else:
            out["scope"] = _DERIVED_SCOPE
            advisories.append(f"scope derived: {_DERIVED_SCOPE}")

    system = out.get("system")
    allowed = _enum_of(schema, "system", "provenance_completeness")
    if allowed and isinstance(system, Mapping) and "provenance_completeness" in system:
        value = system["provenance_completeness"]
        if value not in allowed:
            out["system"] = {k: v for k, v in system.items() if k != "provenance_completeness"}
            advisories.append(f"system.provenance_completeness {value!r} is not in {allowed}; advisory only")

    handback = ""
    if missing:
        handback = (
            f"legacy row hand-back: {'/'.join(missing)} not derivable "
            "(row carries no repo or repo-qualified path)"
        )
    return NormalizedRow(row=out, advisories=tuple(advisories), handback=handback)
