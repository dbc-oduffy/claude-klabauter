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

Spec backlink: cross-repo/inbox/2026-08-07-example-store-repo-em-memo-tool-rejects-
the-shape-it-teaches.md (the originating memo — this landed straight off a
memo pickup, with no intervening plan artifact to cite).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
# advisory/deny-offer band; next slot after nudge_private_git_fact_resolver
# (200, live-but-not-yet-manifest-registered as of this module's authoring —
# see docs/wiki/write-guard-priority-bands.md for the band convention).
PRIORITY = 210

#: state/memo-outbox/<topic>.md — exactly one path segment after
#: memo-outbox/, so state/memo-outbox/sent/<topic>.md (two segments) never
#: matches. Case-insensitive: Windows/macOS default filesystems are
#: case-insensitive, so a differently-cased candidate is the same on-disk
#: file (mirrors the sibling memo guards' case-fold rationale).
#: BOTH outbox roots. This pinned only `state/memo-outbox/` until
#: 2026-09-03; writers moved to `.coordinator-local/memo-outbox/` that
#: day, so a hand-authored draft at the new root got no shape nudge at
#: all while this guard's own suite stayed green -- a guard policing an
#: address the engine had stopped writing to. Same fail-open shape as
#: `block_home_dir_memo_delivery` / `block_oss_mirror_memo_delivery`
#: carried until C6 of the memo-channel plan widened them.
_OUTBOX_DRAFT_RE = re.compile(
    r"(^|/)(state|\.coordinator-local)/memo-outbox/[^/]+\.md$", re.IGNORECASE
)

#: Read cap for Edit/MultiEdit pre-image reconstruction — outbox drafts are
#: small hand-authored markdown files, matching sibling guards' caps.
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
    """Reconstruct the POST-write content for `cand`, or `None` on any
    reconstruction failure (skip the advisory) — mirrors the whole-file
    reconstruction discipline used by sibling advisory guards (e.g.
    `nudge_prose_queue_append`)."""
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
        # MultiEdit's edits[] apply to the ONE file named by tool_input's
        # top-level file_path — edit entries do not carry their own
        # file_path (mirrors nudge_prose_queue_append._reconstruct_pre_and_post
        # and nudge_windows_subprocess_popup's MultiEdit reconstruction).
        post = pre
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            post = _apply_one_edit(post, edit.get("old_string"), edit.get("new_string"))
            if post is None:
                return None
        return post

    return None


_ADVISORY_TEMPLATE = """Outbox draft frontmatter shape: this draft won't pass `cross-repo-memo send`'s validation as written.
{problems}
Fix with `cross-repo-memo compose <topic>` (or hand-edit the fields directly), then `cross-repo-memo send <topic>`. Correct shape: `status: draft` (send promotes it to `open` — never author `open` yourself, even though that's the shape every *received* memo in cross-repo/inbox/ carries)."""


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

        for cand in candidates:
            cn = _collapse_slashes(cand)
            if not _OUTBOX_DRAFT_RE.search(cn):
                continue

            resolved = cand
            if not os.path.isabs(resolved) and isinstance(cwd, str) and cwd:
                resolved = os.path.join(cwd, cand)

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
                # No frontmatter block yet (mid-authoring) — nothing to advise on.
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
        # Fail-OPEN on any unexpected error — this guard advises only on a
        # positive shape-violation match, never on an error.
        return None
