"""
coordinator_core.ops.handoff_repoint_origin — "handoff.repoint_origin" op.

Purpose: the SANCTIONED writer for repointing or nulling the ``origin_handoff``
provenance field on an existing ``state/handoffs/*.md`` stub. The write-time
lineage-reachability hook (Claude Edit/Write PreToolUse surface; see
``coordinator_core.dag.check_lineage_reachability``) hard-rejects any raw Edit
that would set ``origin_handoff`` to an unresolvable value — but there is no
sanctioned path for CORRECTING an already-wrong or now-stale ``origin_handoff``
on a stub that already exists (e.g. a phantom origin left over from a botched
fork, or a lineage that needs to be cut with ``null``). This op is that path.

Structurally hook-exempt, not override-exempt: the guard fires only on the
Claude Edit/Write tool surface. This op mutates via plain Python ``open()`` /
``Path.write_text`` — a different surface entirely — so it is exempt by
construction, the same way every other ``coordinator_core.ops`` frontmatter
writer (``handoff_normalize``, ``handoff_transition``, ``handoff_stamp``) is.
It does NOT weaken, bypass flags for, or special-case around the hook itself;
it re-applies the hook's own resolution rule (``dag.resolve_target``, the 3-tier
live ∪ archive-on-disk ∪ git-history resolver) as its OWN validation gate before
persisting a non-null value, so it can never legitimately write a new phantom.

Self-registration: importing this module fires
``register_op("handoff.repoint_origin")`` as a side-effect. Add this module to
``coordinator_core/ops/__init__.py`` to trigger registration at start_server()
time.

Params:
    targets    (str | list[str], required) — one or more handoff paths
               (absolute, or repo-relative under state/handoffs/) whose
               ``origin_handoff`` field is to be repointed/nulled.
    new_origin (str | None, required key) — the new origin_handoff value.
               Either a resolvable ``state/handoffs/...`` (or archive)
               reference, or the literal null/None (accepted spellings:
               JSON null, Python None, or the strings "null"/"none",
               case-insensitive) to CUT the provenance edge entirely.

Behavior per target:
  1. Read the file, ``split_frontmatter``.
  2. Compare the CURRENT ``origin_handoff:`` value against the requested new
     value — if already equal, this target is a no-op success (idempotent).
  3. If the new value is non-null: validate it resolves via
     ``coordinator_core.dag.resolve_target`` (live ∪ archive-on-disk ∪
     git-history, the SAME 3-tier resolver the write-time hook itself uses).
     Unresolvable → FAIL LOUD for this target (no write) — the sanctioned
     writer must never persist a new phantom, mirroring the guard it is
     exempt from.
  4. If the new value is null: write ``origin_handoff: null`` unconditionally
     (no resolution needed — cutting the edge can never create a phantom).
  5. ``replace_fm_field`` in place — every other key, key order, and the body
     are preserved verbatim.

Does NOT git-commit — pure file mutation, same contract as ``handoff_normalize``.

Reuse (no reimplementation of tested internals):
  - ``coordinator_core.frontmatter.primitives`` (split_frontmatter,
    read_fm_field, replace_fm_field, rebuild) — same primitives
    ``handoff_normalize`` uses.
  - ``coordinator_core.dag.resolve_target`` — the SAME 3-tier resolver
    ``check_lineage_reachability`` (the write-time guard) uses for
    ``origin_handoff``. Not reimplemented, not modified.
  - ``coordinator_core.ops.fleet._common.main_worktree_root`` — P9 worktree
    derivation from the "common_dir"-scoped repo_root arg.
  - ``coordinator_core.ops._path_guard.contained_path`` — caller-supplied
    path containment, same pattern as ``handoff_archive_transition`` /
    ``handoff_lineage_ancestry``.

Spec backlink: cross-repo memo — sanctioned hook-exempt writer for the
origin_handoff provenance edge (2026-07-19).
Sibling / structural template: coordinator_core/ops/handoff_normalize.py
Edge-kind SSOT: coordinator_core/dag.py (EDGE_KIND_META, resolve_target,
check_lineage_reachability).

Negative-spec:
  - Does NOT git-commit. Pure in-place frontmatter file writes only.
  - Does NOT modify or weaken coordinator_core/dag.py or the write-time
    lineage-reachability hook — reuses resolve_target as-is.
  - Does NOT touch any field other than origin_handoff — key order and every
    other frontmatter key, plus the body, are preserved byte-for-byte.
  - Does NOT operate on archive/handoffs/ targets — active stubs only
    (mirrors handoff_normalize's "archive is immutable history" invariant);
    a target path outside state/handoffs/ is rejected by the containment
    check before any read is attempted.
  - Does NOT accept a caller-supplied edge_kinds override — origin_handoff is
    the only edge this op ever validates against; there is no lineage-walk
    here, only a single-field point resolution.
"""

from __future__ import annotations
import sys

import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

from coordinator_core.dag import resolve_target
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    rebuild,
    replace_fm_field,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
# Aliased: `rel_id` is also a local variable name in _handler below, and an
# unaliased import would be shadowed by that binding (UnboundLocalError).
from coordinator_core.wire_paths import rel_id as _wire_rel_id

_LOG = logging.getLogger(__name__)

#: Sentinel distinguishing "new_origin key absent from params" (usage error)
#: from "new_origin explicitly set to null" (a valid null-repoint request).
_MISSING = object()

#: Spellings that mean "cut the provenance edge" — mirrors the sentinel
#: strings resolve_target/check_lineage_reachability already treat as absent
#: ('none', 'null'), plus the empty string.
_NULL_SPELLINGS = ("", "none", "null")


def _normalize_new_origin(raw: object) -> Optional[str]:
    """Normalize a caller-supplied new_origin value to either a non-empty
    stripped string (a real target reference) or None (a null-repoint).

    Accepts Python None, JSON null (also arrives as None), or the strings
    "null"/"none" (any case) as null-repoint spellings — mirrors the sentinel
    handling in dag.resolve_target / check_lineage_reachability so this op's
    null convention matches the guard's own.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text.lower() in _NULL_SPELLINGS:
        return None
    return text


def _normalize_current_origin(raw: Optional[str]) -> Optional[str]:
    """Normalize an on-disk origin_handoff value the same way, for the
    idempotency compare (step 2 in the module docstring's per-target list).
    """
    if raw is None:
        return None
    # Strip a single layer of YAML quoting so a quoted on-disk value compares
    # equal to an equivalent unquoted caller-supplied value. Delegated to the
    # shared inverse of serialize_yaml_scalar, which additionally unescapes the
    # doubled '' inner-quote form the previous local slice did not.
    text = unquote_yaml_scalar(raw.strip()) or ""
    if text.lower() in _NULL_SPELLINGS:
        return None
    return text


@register_op("handoff.repoint_origin")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.repoint_origin" handler.

    The sanctioned writer for repointing/nulling ``origin_handoff`` on one or
    more existing ``state/handoffs/*.md`` stubs. See module docstring for the
    full contract.

    Params:
        targets    (str | list[str], required)
        new_origin (str | None, required key)

    Returns:
        exit_code (int)   — 0 ok (all targets succeeded, incl. no-op idempotent
                            hits); 1 one or more targets failed (unresolvable
                            new_origin, missing file, no frontmatter, escapes
                            containment); 2 usage error (bad/missing params).
        applied   (bool)  — True if any target was actually written to disk.
        results   (list)  — [{file, old_origin, new_origin, changed}] one
                            entry per successfully processed target (changed
                            is False for the idempotent no-op case).
        errors    (list)  — [{file, error}] one entry per target that failed.

    Negative-spec:
      - Does NOT commit. Pure in-place frontmatter file writes only.
      - Does NOT accept params.root / a scan-root override — repo_root
        (socket-authoritative common_dir) is required, mirroring
        handoff_normalize's op-family path-containment contract.
    """
    targets_raw = params.get("targets")
    if targets_raw is None:
        targets_raw = params.get("target")
    if isinstance(targets_raw, str):
        targets: List[str] = [targets_raw]
    elif isinstance(targets_raw, list):
        targets = [str(t) for t in targets_raw]
    else:
        targets = []

    new_origin_raw = params.get("new_origin", _MISSING)

    if repo_root is None:
        return {
            "exit_code": 2,
            "applied": False,
            "results": [],
            "errors": [
                {
                    "file": None,
                    "error": (
                        "handoff.repoint_origin: repo_root is required "
                        "(no founding root available — handler called without "
                        "socket-authoritative common_dir)"
                    ),
                }
            ],
        }

    if not targets:
        return {
            "exit_code": 2,
            "applied": False,
            "results": [],
            "errors": [
                {"file": None, "error": "'targets' is required (str or list[str])"}
            ],
        }

    if new_origin_raw is _MISSING:
        return {
            "exit_code": 2,
            "applied": False,
            "results": [],
            "errors": [{"file": None, "error": "'new_origin' key is required (str or null)"}],
        }

    new_origin = _normalize_new_origin(new_origin_raw)

    worktree = main_worktree_root(repo_root)
    handoffs_dir = worktree / "state" / "handoffs"

    # Non-null new_origin must resolve BEFORE any target is written — this
    # validation is target-independent (the referenced value is the same for
    # every target in the batch), so it happens once, up front.
    if new_origin is not None:
        resolved = resolve_target(new_origin, str(handoffs_dir), str(worktree))
        if resolved is None:
            return {
                "exit_code": 1,
                "applied": False,
                "results": [],
                "errors": [
                    {
                        "file": None,
                        "error": (
                            f"new_origin {new_origin!r} is unresolvable in live ∪ "
                            "archive-on-disk ∪ git-history (provably never-existed) "
                            "— refusing to persist a new phantom origin_handoff"
                        ),
                    }
                ],
            }

    results: List[Dict[str, Union[str, bool, None]]] = []
    errors: List[Dict[str, Optional[str]]] = []
    applied = False

    for target_raw in targets:
        p = Path(target_raw)
        if not p.is_absolute():
            p = worktree / p
        contained = contained_path(p, [handoffs_dir])
        if contained is None:
            errors.append(
                {"file": target_raw, "error": f"target escapes state/handoffs/: {target_raw!r}"}
            )
            continue

        if not contained.is_file():
            errors.append({"file": target_raw, "error": f"target not found on disk: {target_raw}"})
            continue

        rel_id = _wire_rel_id(contained, worktree)

        try:
            content = contained.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"skip: _handler: content = contained.read_text(encoding=\"utf-8\") failed: {exc}", file=sys.stderr)
            errors.append({"file": rel_id, "error": f"I/O error reading: {exc}"})
            continue

        split = split_frontmatter(content)
        if split is None:
            errors.append(
                {"file": rel_id, "error": "no valid YAML frontmatter block — skipped (immutable)"}
            )
            continue

        current_raw = read_fm_field(split.fm_text, "origin_handoff")
        current_normalized = _normalize_current_origin(current_raw)

        if current_normalized == new_origin:
            # Idempotent no-op: already at the requested value.
            results.append(
                {
                    "file": rel_id,
                    "old_origin": current_normalized,
                    "new_origin": new_origin,
                    "changed": False,
                }
            )
            continue

        try:
            if current_raw is None:
                # Field absent entirely — insert rather than replace (mirrors
                # handoff_normalize's absent-field handling; replace_fm_field
                # deliberately does NOT insert a new line for a missing key).
                new_fm_text = insert_fm_field(split.fm_text, "origin_handoff", new_origin)
            else:
                new_fm_text = replace_fm_field(split.fm_text, "origin_handoff", new_origin)
        except ValueError as exc:
            print(f"skip: _handler: if current_raw is None: failed: {exc}", file=sys.stderr)
            errors.append({"file": rel_id, "error": str(exc)})
            continue

        rebuilt = rebuild(split, new_fm_text)

        try:
            contained.write_text(rebuilt, encoding="utf-8")
        except OSError as exc:
            print(f"skip: _handler: contained.write_text(rebuilt, encoding=\"utf-8\") failed: {exc}", file=sys.stderr)
            errors.append({"file": rel_id, "error": f"I/O error writing: {exc}"})
            continue

        applied = True
        results.append(
            {
                "file": rel_id,
                "old_origin": current_normalized,
                "new_origin": new_origin,
                "changed": True,
            }
        )

    exit_code = 1 if errors else 0

    return {
        "exit_code": exit_code,
        "applied": applied,
        "results": results,
        "errors": errors,
    }
