"""
coordinator_core.ops.baton_seed_split — "baton.seed_split" op.

Purpose: carve the gate-free part of a gated roadmap seed into its own
sizing-ready seed and narrow the original to the gated remainder, linked by
``split_into`` (original) and ``split_from`` (new seed).

Params: ``seed`` (live handoff id: stub_id, handoff_id or filename stem),
``gate_free`` (non-empty list of scope items; each must match at least one body
line of the seed, case-insensitively), optional ``title`` for the new seed
(default "<original title> (gate-free part)"), optional ``today`` (ISO date).

Effect: matching body lines move from the original to a new
``state/handoffs/<today>-<slug>.md`` with ``kind`` kept, a fresh ``handoff_id``,
``deployment_state: ready_to_fire``, every gate field and ``deliverable_id`` dropped,
and ``split_from: <original id>``. The original keeps ``awaiting_gate``, loses the
moved lines, gains ``split_into: [<new id>]`` and a ``## Split`` note.

Refuses: unknown seed, seed not ``awaiting_gate``, empty ``gate_free``, an item
matching no body line, a destination file that already exists. Both writes are
atomic; if narrowing the original fails the new seed is removed.

Negative-spec:
  - Does NOT git-commit and does NOT dispatch or size anything.
  - Does NOT edit archived batons.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.dag import _parse_frontmatter
from coordinator_core.frontmatter.primitives import (
    insert_fm_field_raw,
    rebuild,
    remove_fm_field,
    remove_fm_nested_field,
    replace_fm_field,
    replace_fm_field_raw,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.mint_deliverable_id import mint_artifact_id
from coordinator_core.roadmap.plan_gate import scan_batons

MUTATES = ["state/handoffs/*.md"]

_GATE_FIELDS = (
    "gate_dependency", "gate_notes", "blocking_notes", "blocked_by",
    "no_longer_blocked_by", "gate_evidence", "gate_cleared_by", "last_gate_recheck",
    "gate_holder", "deliverable_id", "split_into",
)


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": f"baton.seed_split: {message}"}


def _drop(fm: str, key: str) -> str:
    try:
        return remove_fm_field(fm, key)
    except ValueError:
        return remove_fm_nested_field(fm, key)


def _set(fm: str, key: str, value: str) -> str:
    raw = json.dumps(value)
    if re.search(rf"^{re.escape(key)}:", fm, re.MULTILINE):
        return replace_fm_field_raw(fm, key, raw)
    return insert_fm_field_raw(fm, key, raw)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50].strip("-") or "seed"


@register_op("baton.seed_split")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Split the gate-free scope items out of a gated seed.

    Returns ``{exit_code, applied, original, new_seed, new_handoff_id, moved}`` or
    ``{exit_code: 1, applied: False, error}``.
    """
    seed = str(params.get("seed") or "").strip()
    items = [str(i).strip() for i in (params.get("gate_free") or []) if str(i).strip()]
    if not seed:
        return _err("missing required param: seed")
    if not items:
        return _err("gate_free must name at least one scope item")
    if repo_root is None:
        return _err("repo_root is required")

    worktree = main_worktree_root(repo_root)
    by_id, _ = scan_batons(worktree, include_archived=False)
    source = by_id.get(seed)
    if source is None:
        return _err(f"seed not found among live batons: {seed!r}")
    if source["deployment_state"] != "awaiting_gate":
        return _err(f"{source['path']} is {source['deployment_state']!r}, not awaiting_gate")

    path = worktree / source["path"]
    today = date.fromisoformat(str(params["today"])) if params.get("today") else date.today()
    out: dict = {}

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no valid YAML frontmatter block in: {source['path']}")
        fm = _parse_frontmatter(old_text)
        if fm.get("deployment_state") != "awaiting_gate":
            raise MutateAbort(f"{source['path']} is no longer awaiting_gate")

        body_lines = split.body_with_leading_newline.splitlines(keepends=True)
        moved: list[str] = []
        kept: list[str] = []
        unmatched = []
        for item in items:
            if not any(item.lower() in ln.lower() for ln in body_lines if ln.strip()):
                unmatched.append(item)
        if unmatched:
            raise MutateAbort(f"scope items not found in the seed body: {unmatched}")
        for ln in body_lines:
            (moved if ln.strip() and any(i.lower() in ln.lower() for i in items) else kept).append(ln)

        orig_id = str(fm.get("handoff_id") or source["id"])
        title = str(params.get("title") or f"{fm.get('title') or orig_id} (gate-free part)")
        new_id = mint_artifact_id("hnd", _slug(title))
        dest = worktree / "state" / "handoffs" / f"{today.isoformat()}-{_slug(title)}.md"
        if dest.exists():
            raise MutateAbort(f"destination already exists: {dest.name}")

        new_fm = split.fm_text
        for key in _GATE_FIELDS:
            new_fm = _drop(new_fm, key)
        new_fm = replace_fm_field(new_fm, "deployment_state", "ready_to_fire")
        new_fm = replace_fm_field(new_fm, "created", today.isoformat())
        new_fm = _set(new_fm, "title", title)
        new_fm = _set(new_fm, "handoff_id", new_id)
        new_fm = _set(new_fm, "split_from", orig_id)
        new_body = (
            f"\n## What this covers\n\nGate-free part of `{orig_id}`, split out on {today.isoformat()}. "
            f"The gated remainder stays in `{source['path']}`.\n\n## Scope\n\n"
            + "".join(m if m.endswith("\n") else m + "\n" for m in moved)
        )
        atomic_write_bytes(dest, _assemble(split, new_fm, new_body).encode("utf-8"), preserve_mode=False)
        out.update(
            new_seed=dest.relative_to(worktree).as_posix(), new_handoff_id=new_id,
            moved=[m.strip() for m in moved], dest=dest,
        )

        prior = fm.get("split_into")
        ids = ([prior] if isinstance(prior, str) else list(prior or [])) + [new_id]
        narrowed = insert_fm_field_raw(split.fm_text, "split_into", json.dumps(ids)) if prior is None \
            else replace_fm_field_raw(split.fm_text, "split_into", json.dumps(ids))
        note = (
            f"\n## Split\n\n- {today.isoformat()}: gate-free scope moved to `{new_id}` "
            f"(`{out['new_seed']}`); this seed keeps only the gated remainder.\n"
        )
        return _assemble(split, narrowed, "".join(kept).rstrip("\n") + "\n" + note)

    try:
        locked_rmw(path, _mutate, repo_root=repo_root)
    except LockTimeout as exc:
        _cleanup(out)
        return _err(f"lock timeout: {exc}")
    except MutateAbort as exc:
        _cleanup(out)
        return _err(str(exc.args[0]) if exc.args else "mutate aborted")
    except OSError as exc:
        _cleanup(out)
        return _err(f"cannot read/write seed: {exc}")

    out.pop("dest", None)
    return {"exit_code": 0, "applied": True, "original": source["path"], **out}


def _assemble(split, fm_text: str, body: str) -> str:
    return rebuild(split, fm_text)[: -len(split.body_with_leading_newline) or None] + (
        body if body.startswith("\n") else "\n" + body
    )


def _cleanup(out: dict) -> None:
    dest = out.get("dest")
    if dest is not None:
        try:
            Path(dest).unlink()
        except OSError:
            pass
