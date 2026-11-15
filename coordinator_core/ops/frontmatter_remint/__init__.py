"""
coordinator_core.ops.frontmatter_remint — "frontmatter.remint" op.

Purpose: re-mint engine-owned frontmatter fields on existing records from
recorded provenance, refusing (never fabricating) where provenance is absent.

One op, four field families. Each family's resolver is a sibling module
imported lazily from `_RESOLVERS`; a family whose module is not built refuses
`resolver-unbuilt`. The contract (`resolve` / optional `write`, `Resolved` /
`Refused`, `REASONS`) lives in `_contract.py`.

A field whose resolver refused leaves the file byte-identical: value writes go
through a single `locked_rmw` per file that is only entered when at least one
family resolved, and `locked_rmw` skips an unchanged write.
"""

from __future__ import annotations

import importlib
import logging
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Optional

import yaml

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.frontmatter_remint._contract import (
    REASONS,
    Refused,
    Resolved,
)

_LOG = logging.getLogger(__name__)

_PKG = "coordinator_core.ops.frontmatter_remint"

#: family -> resolver module. Order is write order: review_stamp precedes its register.
_RESOLVERS: dict[str, str] = {
    "handoff_id": f"{_PKG}.handoff_id",
    "execution_authorized": f"{_PKG}.exec_auth",
    "review_stamp": f"{_PKG}.review_stamp_fields",
    "review_stamp.register": f"{_PKG}.review_stamp_fields",
}

#: record location -> vendored schema (relative to the frontmatter schemas dir).
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "frontmatter" / "schemas"
_SCHEMA_BY_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (("state", "handoffs"), "handoff.schema.json"),
    (("docs", "plans"), "plan.schema.json"),
)


def _schema_for(rel: PurePosixPath) -> Optional[Path]:
    for prefix, name in _SCHEMA_BY_PREFIX:
        if rel.parts[: len(prefix)] == prefix:
            return _SCHEMA_DIR / name
    return None


def _family_of(field: str) -> Optional[str]:
    """Map a validator error `field` to a remint family, or None for a non-engine field."""
    if field == "handoff_id":
        return "handoff_id"
    if field == "review_stamp":
        return "review_stamp"
    if field.startswith("review_stamp.register"):
        return "review_stamp.register"
    if "execution_authorized_" in field:
        return "execution_authorized"
    return None


def _failing_families(fm: Mapping[str, Any], schema: Path) -> list[str]:
    seen: set[str] = set()
    for err in validate_frontmatter(dict(fm), schema):
        fam = _family_of(str(err.get("field", "")))
        if fam:
            seen.add(fam)
    return [fam for fam in _RESOLVERS if fam in seen]


def _load_resolver(family: str):
    """Import the family module lazily; return (module, None) or (None, Refused)."""
    dotted = _RESOLVERS.get(family)
    if dotted is None:
        return None, Refused("resolver-unbuilt", f"no resolver family {family!r}")
    try:
        mod = importlib.import_module(dotted)
    except ImportError as exc:
        return None, Refused("resolver-unbuilt", f"{dotted}: {exc}")
    if not callable(getattr(mod, "resolve", None)):
        return None, Refused("resolver-unbuilt", f"{dotted} defines no resolve()")
    return mod, None


def _entry(field: str, outcome: str, reason: Optional[str] = None, detail: str = "") -> dict:
    if outcome == "refused":
        assert reason in REASONS, reason
    return {"field": field, "outcome": outcome, "reason": reason, "detail": detail}


def _read_fm(path: Path) -> Optional[dict]:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    if split is None:
        return None
    loaded = yaml.safe_load(split.fm_text)
    return loaded if isinstance(loaded, dict) else None


def _apply_values(path: Path, values: Mapping[str, Any], repo_root: Path) -> None:
    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {path.name}")
        fm = split.fm_text
        for key, val in values.items():
            if read_fm_field(fm, key) is None:
                fm = insert_fm_field(fm, key, val)
            else:
                fm = replace_fm_field(fm, key, val)
        return rebuild(split, fm)

    locked_rmw(path, mutate, repo_root=repo_root)


def _remint_one(rel_raw: str, worktree: Path, wanted: Optional[list[str]], repo_root: Path) -> dict:
    out: dict = {"path": rel_raw, "fields": [], "error": None}
    path = contained_path(worktree / rel_raw, [worktree])
    if path is None or not path.is_file():
        out["error"] = f"not a file under the repo root: {rel_raw}"
        return out
    rel = PurePosixPath(path.relative_to(worktree.resolve()).as_posix())
    schema = _schema_for(rel)
    if schema is None:
        out["error"] = "no schema for this path (state/handoffs/ and docs/plans/ only)"
        return out
    try:
        fm = _read_fm(path)
    except (OSError, yaml.YAMLError) as exc:
        out["error"] = f"unreadable frontmatter: {exc}"
        return out
    if fm is None:
        out["error"] = "no parseable YAML frontmatter"
        return out

    failing = _failing_families(fm, schema)
    families = list(wanted) if wanted is not None else failing
    families.sort(key=lambda f: list(_RESOLVERS).index(f) if f in _RESOLVERS else len(_RESOLVERS))

    values: dict[str, Any] = {}
    value_families: list[str] = []
    writer_families: list[tuple[str, Any]] = []
    for fam in families:
        if fam in _RESOLVERS and fam not in failing:
            out["fields"].append(_entry(fam, "already-valid"))
            continue
        mod, refused = _load_resolver(fam)
        if refused is not None:
            out["fields"].append(_entry(fam, "refused", refused.reason, refused.detail))
            continue
        if callable(getattr(mod, "write", None)):
            writer_families.append((fam, mod))
            continue
        res = mod.resolve(path, fm, worktree)
        if isinstance(res, Refused):
            out["fields"].append(_entry(fam, "refused", res.reason, res.detail))
        else:
            values.update(res.value)
            value_families.append(fam)

    if values:
        try:
            _apply_values(path, values, repo_root)
        except (LockTimeout, MutateAbort, OSError) as exc:
            out["error"] = f"write failed: {exc}"
            return out
    for fam in value_families:
        out["fields"].append(_entry(fam, "written"))

    for fam, mod in writer_families:
        try:
            fresh = _read_fm(path) or fm
        except (OSError, yaml.YAMLError):
            fresh = fm
        res = mod.resolve(path, fresh, worktree)
        if isinstance(res, Resolved):
            res = mod.write(path, worktree)
        if isinstance(res, Refused):
            out["fields"].append(_entry(fam, "refused", res.reason, res.detail))
        else:
            out["fields"].append(_entry(fam, "written"))
    return out


@register_op("frontmatter.remint")
def _old_handoff_ids(rels: list[str], worktree: Path) -> list[str]:
    ids = []
    for rel in rels:
        try:
            fm = _read_fm(worktree / rel)
        except (OSError, yaml.YAMLError):
            continue
        if fm and fm.get("handoff_id"):
            ids.append(str(fm["handoff_id"]).strip())
    return ids


def _remint_all(rels: list[str], worktree: Path, wanted: Optional[list[str]], repo_root: Path) -> list[dict]:
    # One citer scan for the whole call (handoff_id family); a failed scan falls back to per-path scans.
    if wanted is not None and "handoff_id" not in wanted:
        return [_remint_one(r, worktree, wanted, repo_root) for r in rels]
    from coordinator_core.ops.frontmatter_remint import handoff_id as _hid

    try:
        ctx = _hid.primed(_old_handoff_ids(rels, worktree), worktree)
        ctx.__enter__()
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return [_remint_one(r, worktree, wanted, repo_root) for r in rels]
    try:
        return [_remint_one(r, worktree, wanted, repo_root) for r in rels]
    finally:
        ctx.__exit__(None, None, None)


def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "frontmatter.remint" — re-mint engine-owned frontmatter fields.

    Params:
        paths (list[str], required) — repo-relative record paths.
        fields (list[str], optional) — subset of families (handoff_id,
            execution_authorized, review_stamp, review_stamp.register); default
            is every family the validator reports failing for each path. A named
            family that is not failing reports `already-valid`; an unknown one
            refuses `resolver-unbuilt`.

    Returns:
        exit_code (int) — 0 ok (refusals are per-field outcomes, not errors);
            2 usage error.
        results (list) — per path: `path`, `error` (path-level failure or null),
            `fields`: [{field, outcome: written|refused|already-valid, reason,
            detail}]; every `refused` reason is a `_contract.REASONS` member.
    """
    paths = params.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not paths or not all(isinstance(p, str) and p.strip() for p in paths):
        return {"exit_code": 2, "error": "'paths' is required: a non-empty list of repo-relative paths", "results": []}
    wanted = params.get("fields")
    if isinstance(wanted, str):
        wanted = [wanted]
    if wanted is not None and not all(isinstance(f, str) for f in wanted):
        return {"exit_code": 2, "error": "'fields' must be a list of family names", "results": []}
    if repo_root is None:
        return {"exit_code": 2, "error": "repo_root is required", "results": []}

    worktree = main_worktree_root(repo_root)
    results = _remint_all([p.strip() for p in paths], worktree, wanted, repo_root)
    return {"exit_code": 0, "error": None, "results": results}
