"""artifact.adopt: convert a hand-written plan to canonical frontmatter shape.

Fills what `coordinator-doc-new --type plan` would have stamped (fill-if-absent, never
overwrite), reports the required fields it cannot derive, and keeps every byte after the
closing `---` identical. Dry run by default; the op never commits and never spawns.

Params: path (str, repo-relative, required); write (bool, optional - a non-bool is refused,
not coerced).

Negative-spec:
  - Does NOT stamp an adopt marker; the producer's own `plan_id` is the provenance, so a
    second run finds nothing absent and returns `changed: false`.
  - Does NOT treat unparseable or unterminated frontmatter as absent; it refuses.
  - Does NOT derive `status`; an absent one is reported in `unfilled_required`.
  - Does NOT repair a body mismatch; it refuses the write.
"""

from __future__ import annotations

import difflib
import functools
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.frontmatter.schema_validate import (
    load_schemas,
    match_schema_for_path,
    validate_frontmatter_obj,
)
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.locked_write import MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.artifact_adopt_contract import (
    OP_NAME,
    AdoptResult,
    adopt_command,
    is_plan_path,
)
from coordinator_core.ops.artifact_adopt_plan import PlanDerived, port_plan_frontmatter
from coordinator_core.ops.mint_deliverable_id import (
    mint as mint_deliverable,
    mint_artifact_id,
    slug_from_title,
)
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import harness_registry

_SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "frontmatter" / "schemas"
_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_H1 = re.compile(r"^# +(.+?)[ \t]*$", re.MULTILINE)
_OPEN_FENCE = re.compile(r"^---[ \t]*\r?\n")


@functools.lru_cache(maxsize=1)
def _schemas() -> dict:
    return load_schemas(str(_SCHEMAS_DIR))


def _tail_raw(raw: str, norm_tail: str) -> str:
    """The suffix of `raw` that `split_frontmatter` saw as `norm_tail` (it folds CRLF to LF)."""
    n = norm_tail.count("\n")
    if n == 0:
        return norm_tail
    idx = len(raw)
    for _ in range(n):
        idx = raw.rfind("\n", 0, idx)
    return raw[idx - 1 :] if idx > 0 and raw[idx - 1] == "\r" else raw[idx:]


def _prefix_raw(raw: str, norm_prefix: str) -> str:
    n = norm_prefix.count("\n")
    idx = -1
    for _ in range(n):
        idx = raw.find("\n", idx + 1)
    return raw[: idx + 1]


def _session_author(worktree_root: Path) -> Optional[str]:
    sid = resolve_current_session_id(worktree_root)
    if not sid:
        return None
    record = harness_registry.lookup(sid)
    name = getattr(record, "name", None)
    return f"{name} ({sid})" if name else None


def _derive(
    rel: str, abs_path: Path, fm_text: str, body_norm: str, worktree_root: Path
) -> PlanDerived:
    """Values for the keys `_scaffold_plan` stamps; minted only when the key is absent."""
    stem = Path(rel).stem
    m = _DATE_PREFIX.match(Path(rel).name)
    created = m.group(1) if m else datetime.fromtimestamp(abs_path.stat().st_mtime).strftime("%Y-%m-%d")
    derived: PlanDerived = {
        "created": created,
        "author": _session_author(worktree_root),
        "branch": head_branch(worktree_root),
    }
    need_plan_id = read_fm_field(fm_text, "plan_id") is None
    need_dlv = read_fm_field(fm_text, "deliverable_id") is None
    if need_plan_id or need_dlv:
        title = None
        try:
            loaded = yaml.safe_load(fm_text) or {}
            raw_title = loaded.get("title") or loaded.get("name") if isinstance(loaded, dict) else None
            title = str(raw_title) if raw_title else None
        except yaml.YAMLError:
            pass
        if not title:
            h1 = _H1.search(body_norm)
            title = h1.group(1) if h1 else stem
        slug = slug_from_title(title) or slug_from_title(stem) or "plan"
        if need_plan_id:
            derived["plan_id"] = mint_artifact_id("pln", slug)
        if need_dlv:
            derived["deliverable_id"] = os.environ.get("DELIVERABLE_ID") or mint_deliverable(slug=slug)[0]
    return derived


def _unfilled(fm_text: str, schema: dict) -> list[str]:
    fm_dict: Any = yaml.safe_load(fm_text) or {}
    result = validate_frontmatter_obj(fm_dict, schema)
    if result.get("ok"):
        return []
    return [f"{e.get('field')}: {e.get('error')}" for e in result.get("errors", [])]


def _compute(text: str, rel: str, abs_path: Path, worktree_root: Path, schema: dict):
    """Pure over `text`: returns (new_text, old_fm, new_fm, changes)."""
    norm = text.replace("\r\n", "\n")
    first_nl = text.find("\n")
    eol = "\r\n" if first_nl > 0 and text[first_nl - 1] == "\r" else "\n"
    split = split_frontmatter(text)
    status_enum = schema.get("properties", {}).get("status", {}).get("enum", [])
    if split is None:
        if _OPEN_FENCE.match(text):
            raise ValueError(f"{rel}: frontmatter block is not terminated; refusing to treat it as absent")
        old_fm, body_norm, body_raw, prefix_raw = "", norm, eol + text, ""
        derived = _derive(rel, abs_path, "", norm, worktree_root)
        new_fm, changes = port_plan_frontmatter(None, norm, derived, status_enum)
    else:
        try:
            parsed = yaml.safe_load(split.fm_text)
        except yaml.YAMLError as exc:
            raise ValueError(f"{rel}: frontmatter does not parse as YAML: {exc}") from exc
        if parsed is not None and not isinstance(parsed, dict):
            raise ValueError(f"{rel}: frontmatter is not a mapping")
        old_fm, body_norm = split.fm_text, split.body_with_leading_newline
        body_raw, prefix_raw = _tail_raw(text, body_norm), _prefix_raw(text, split.preamble)
        derived = _derive(rel, abs_path, old_fm, body_norm, worktree_root)
        new_fm, changes = port_plan_frontmatter(old_fm, body_norm, derived, status_enum)
    if not changes:
        return text, old_fm, old_fm, []
    fm_out = (new_fm if new_fm.endswith("\n") else new_fm + "\n").replace("\n", eol)
    new_text = prefix_raw + "---" + eol + fm_out + "---" + body_raw
    check = split_frontmatter(new_text)
    expected_body = body_norm if split is not None else eol + norm
    if check is None or not new_text.endswith(body_raw) or check.body_with_leading_newline.replace("\r\n", "\n") != expected_body.replace("\r\n", "\n"):
        raise ValueError(f"{rel}: body bytes would change; refusing to write")
    return new_text, old_fm, new_fm, changes


def _diff(rel: str, old_fm: str, new_fm: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_fm.splitlines(keepends=True),
            new_fm.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )


@register_op(OP_NAME)
def _handler(params: dict, repo_root: Optional[Path] = None) -> AdoptResult:
    """JSON-RPC "artifact.adopt" handler. See module docstring."""
    if repo_root is None:
        raise ValueError(f"{OP_NAME} requires a resolved repo_root")
    raw_path = params.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("path must be a non-empty repo-relative string")
    write = params.get("write", False)
    if not isinstance(write, bool):
        raise ValueError(f"write must be a bool, got {type(write).__name__}")

    worktree_root = Path(main_worktree_root(repo_root))
    candidate = Path(raw_path.strip().replace("\\", "/"))
    abs_path = candidate if candidate.is_absolute() else worktree_root / candidate
    if contained_path(abs_path, [worktree_root]) is None:
        raise ValueError(f"path escapes the resolved worktree: {raw_path!r}")
    try:
        rel = abs_path.relative_to(worktree_root).as_posix()
    except ValueError:
        rel = abs_path.resolve().relative_to(worktree_root.resolve()).as_posix()

    matched = match_schema_for_path(rel, _schemas()) if is_plan_path(rel) else None
    if not matched or matched.get("schemaName") != "plan":
        raise ValueError(f"unsupported type: {rel} is not a docs/plans/<name>.md plan")
    schema = matched["schema"]
    if not abs_path.is_file():
        raise ValueError(f"no such file: {rel}")

    with open(abs_path, "r", encoding="utf-8", newline="") as fh:
        text = fh.read()
    new_text, old_fm, new_fm, changes = _compute(text, rel, abs_path, worktree_root, schema)
    changed = bool(changes)
    applied = False

    if write and changed:
        box: list = [changes, old_fm, new_fm]

        def mutate(_translated: str) -> str:
            # locked_rmw reads with universal newlines, which would drop CRLF; re-read raw.
            with open(abs_path, "r", encoding="utf-8", newline="") as fh:
                current = fh.read()
            if current == text:
                return new_text
            # The file moved since the read; recompute against the bytes now on disk.
            fresh, fo, fn, ch = _compute(current, rel, abs_path, worktree_root, schema)
            box[:] = [ch, fo, fn]
            if not ch:
                raise MutateAbort("already adopted")
            return fresh

        try:
            locked_rmw(abs_path, mutate, repo_root=repo_root)
            applied = True
        except MutateAbort:
            changed = False
        changes, old_fm, new_fm = box

    return AdoptResult(
        path=rel,
        type="plan",
        dry_run=not write,
        applied=applied,
        changed=changed,
        diff=_diff(rel, old_fm, new_fm) if changed else "",
        changes=list(changes) if changed else [],
        unfilled_required=_unfilled(new_fm if changed else old_fm, schema),
        command=adopt_command(rel),
    )
