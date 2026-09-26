"""coordinator_core.write_guards.nudge_outbox_draft_frontmatter_shape —
advisory guard.

Closes the "the buffer is one line old, not two hundred" gap named by
cross-repo/inbox/2026-08-07-example-store-repo-em-memo-tool-rejects-the-shape-it-
teaches.md: a `state/memo-outbox/<topic>.md` draft hand-authored with
`status: open` — copied from the *received*-memo shape sitting right next to
it in `cross-repo/inbox/` — previously wasn't caught until `cross-repo-memo
send` refused at the very last step, by which point a PM-authorised memo had
sat undelivered for most of a session. This guard surfaces the same defect
the moment the draft file is written or edited, not two hundred lines and one
`send` invocation later.

CLASS is "advisory", not "hard-deny": the operator may be mid-authoring a
draft (a fresh `memo.draft` placeholder, a `--scoped-to-*` flag still being
filled in, an incomplete `kind:`) and a block here would be exactly the "asks
a model to refuse work it is eager to do" shape project CLAUDE.md's north
star calls advisory-in-practice. `cross-repo-memo send`'s own hard gate
(`_validate_outbox_frontmatter`) remains the actual enforcement; this guard
is the earlier-surfacing offer, not a second enforcement point. It is also
strictly less necessary than it once was for the specific `status: open`
case this incident named — `_cmd_send` now normalizes `open` -> `draft`
in-memory before validating (2026-08-07, same plan) — but a hand-authored
`open` is still worth naming AT AUTHORING TIME rather than leaving it for the
self-heal to paper over silently, and every OTHER shape defect
(`_validate_outbox_frontmatter` catches — missing fields, bad `kind`, an
incomplete `scoped_to` triple) still has no write-time surface without this
guard.

SINGLE SOURCE OF TRUTH: this guard does NOT reimplement
`_validate_outbox_frontmatter`'s rule set. Both this guard and
`cross-repo-memo`'s CLI call
`coordinator_core.ops.fleet._outbox_frontmatter_rules.validate_outbox_frontmatter`
— see that module's docstring for the extraction rationale. A rule change
lands once, in one place, and both write-time (this guard) and send-time
(the CLI) pick it up identically.

Scope: `state/memo-outbox/*.md` ONLY — one path segment between
`memo-outbox/` and the `.md` filename. `state/memo-outbox/sent/*.md` (the
archived, already-`status: sent` stamped copy `cross-repo-memo send` moves a
delivered draft to) is a DIFFERENT path shape (two segments:
`memo-outbox/sent/<name>.md`) and never matches this guard's path regex —
firing an "this doesn't look like a draft" advisory on an already-delivered,
frozen archival record would be pure noise, never actionable.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only `additionalContext`, never `permissionDecision`.
  - Does NOT fire on `state/memo-outbox/sent/*.md` (see Scope above).
  - Does NOT fire when the prospective frontmatter cannot be parsed at all
    (no `---` fences yet, mid-authoring) — silence, not a "your frontmatter
    is broken" advisory on an intentionally-incomplete in-progress buffer;
    `validate_outbox_frontmatter` is never even called in that case.
  - Does NOT fire when `validate_outbox_frontmatter` returns no errors — a
    genuinely valid draft (including one that legitimately carries
    `status: draft`) is always silent.
  - Never raises: any unexpected input shape, read failure, or
    reconstruction failure fails OPEN (returns `None`), matching every
    sibling advisory guard in this package.
  - Does NOT trust the path-shape regex alone as a containment check — a
    resolvable git root gates every regex-matched candidate through
    `coordinator_core.ops._path_guard.contained_path` (INTERFACE.md rule 8),
    mirroring `nudge_handoff_ac_shape.py`'s own gate, so an absolute path
    outside the repo that merely CONTAINS the matched shape (e.g.
    `/tmp/anywhere/state/memo-outbox/x.md`) is never treated as a real
    outbox draft. An unresolvable git root leaves this gate inert (`None`
    stays silent), same fail-open discipline as every other check here.

Spec backlink: cross-repo/inbox/2026-08-07-example-store-repo-em-memo-tool-rejects-
the-shape-it-teaches.md (the originating memo — this landed straight off a
memo pickup, with no intervening plan artifact to cite).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 210

_OUTBOX_DRAFT_RE = re.compile(
    r"(^|/)(state|\.coordinator-local)/memo-outbox/[^/]+\.md$", re.IGNORECASE
)

_MAX_WHOLE_FILE_BYTES = 1024 * 1024


def _collapse_slashes(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _extract_candidates(payload: Dict[str, Any]) -> List[str]:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    fp = tool_input.get("file_path")
    if fp:
        return [fp]
    out: List[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                efp = edit.get("file_path")
                if efp:
                    out.append(efp)
    return out


def _read_file_safely(file_path: str) -> Optional[str]:
    try:
        if os.path.getsize(file_path) > _MAX_WHOLE_FILE_BYTES:
            return None
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _apply_one_edit(content: str, old_string: Any, new_string: Any) -> Optional[str]:
    if not isinstance(old_string, str) or not old_string:
        return None
    idx = content.find(old_string)
    if idx == -1:
        return None
    new_string = new_string if isinstance(new_string, str) else ""
    return content[:idx] + new_string + content[idx + len(old_string):]


def _compute_post_content(
    tool_name: str, tool_input: Dict[str, Any], cand: str, resolved_path: str
) -> Optional[str]:
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    pre = _read_file_safely(resolved_path)
    if pre is None:
        return None

    if tool_name == "Edit":
        return _apply_one_edit(pre, tool_input.get("old_string"), tool_input.get("new_string"))

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        post = pre
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            post = _apply_one_edit(post, edit.get("old_string"), edit.get("new_string"))
            if post is None:
                return None
        return post

    return None


_ADVISORY_TEMPLATE = """Outbox draft won't pass `cross-repo-memo send` validation:
{problems}
Use instead: `cross-repo-memo compose <topic>` then `send <topic>`. `status: draft`, not `open` -- send promotes it."""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        cwd = payload.get("cwd") or None
        git_root = resolve_repo_root(cwd) if isinstance(cwd, str) and cwd else None
        allowed_roots = [Path(git_root)] if git_root else []

        for cand in candidates:
            cn = _collapse_slashes(cand)
            if not _OUTBOX_DRAFT_RE.search(cn):
                continue

            resolved = cand
            if not os.path.isabs(resolved) and isinstance(cwd, str) and cwd:
                resolved = os.path.join(cwd, cand)

            if allowed_roots:
                # INTERFACE.md rule 8: reuse contained_path rather than trust
                if contained_path(Path(resolved), allowed_roots) is None:
                    continue

            post_content = _compute_post_content(tool_name, tool_input, cand, resolved)
            if post_content is None:
                continue

            try:
                from coordinator_core.frontmatter.schema_validate import parse_frontmatter
            except Exception:  # noqa: BLE001 — fail-open, never block on infra
                return None
            try:
                fm = parse_frontmatter(post_content).get("frontmatter")
            except Exception:  # noqa: BLE001 — fail-open on an unparseable buffer
                return None
            if not isinstance(fm, dict):
                continue

            try:
                from coordinator_core.ops.fleet._outbox_frontmatter_rules import (
                    validate_outbox_frontmatter,
                )
                errors = validate_outbox_frontmatter(fm)
            except Exception:  # noqa: BLE001 — fail-open, never block on infra
                return None

            if not errors:
                continue

            problems = "\n".join(f"  - {e}" for e in errors)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _ADVISORY_TEMPLATE.format(problems=problems),
                }
            }

        return None
    except Exception:
        return None
