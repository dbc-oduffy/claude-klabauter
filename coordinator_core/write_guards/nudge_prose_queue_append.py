"""coordinator_core.write_guards.nudge_prose_queue_append — advisory guard.

Spec: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
§ PM direction (A) ("Append-silence becomes append-advisory — the PM
sentence wins"). Part 3 of the original ruling specified this half as
silent; the PM direction that arrived afterward overrode that specific
choice ("we should not continue to produce into a legacy format") while
leaving the creation-deny half (``nudge_prose_queue_creation.py``) intact.
Read both sections together — this module implements the AMENDED shape,
not part 3's original one.

Background: eight repos still carry legacy single-markdown-file queues
(``improvement-queue.md``, ``bug-backlog.md``, ``debt-backlog.md``) that
predate the per-entry-YAML shape the query surface (``records_query``) and
every ceremony downstream of it actually reads. DR-115's creation-deny
(sibling module) stops NEW prose queues from being born; this module is the
other half — it does not block an existing repo's queue from being
maintained (six-plus repos still do, and a deny there earns a bypass inside
a day per the ruling's rejected-alternatives section), but it also must not
stay SILENT while a genuinely new entry is produced into the format DR-115's
PM direction calls "a legacy format we should not continue to produce
into." Non-blocking, not silent, is the amendment.

CLASS is "advisory" — this must NEVER deny. ``MATCHERS = ["Write", "Edit",
"MultiEdit"]``, same path gate as the creation-deny sibling (imported from
it directly rather than re-derived, so the two guards' notion of "a legacy
queue path" cannot drift apart), but this module's DISCRIMINANT is the
opposite of the sibling's: it fires only when the target file ALREADY
EXISTS, and only when the write actually INCREASES the dated-pipe-row
count.

THE DELTA GATE IS THE WHOLE DESIGN, reused rather than reinvented — DR-115
names ``nudge_improvement_queue_write``'s existing entry-count-delta logic
(``_ENTRY_LINE_RE``, counted before/after and compared) as the discriminator
to reuse for exactly this purpose, and this module imports that same regex
constant so the one definition of "what counts as a dated queue row" cannot
drift into two. Unlike that sibling guard (which only counts entries on the
``Edit`` tool's own ``old_string``/``new_string`` fragments, and skips the
gate entirely for ``Write``/``MultiEdit``), this guard needs the delta to
hold across ALL THREE matchers, so it reconstructs the POST-write whole file
the same way ``nudge_baton_body_bar`` does for its own Edit/MultiEdit
handling: ``Write`` already carries the whole new file; ``Edit``/
``MultiEdit`` read the on-disk PRE-write content (best-effort, capped) and
re-apply the same old_string -> new_string substitution(s) to get the
POST-write content. The entry count is then taken over the on-disk PRE
content vs the reconstructed POST content. Any reconstruction failure
(oversized file, ``old_string`` not found, read error) skips the advisory
silently — never falls back to a weaker fragment-scoped count, since a
fragment cannot tell whether the count actually went up across the whole
file.

This delta gate is what keeps this an offer rather than a nag, and DR-115
is explicit that it is load-bearing: a prune (row count decreases), a pure
reformat (count unchanged), a closure-line deletion, and the `/update-docs`
Phase 11i sweep must all pass in COMPLETE silence. The advisory speaks ONLY
when a new entry is being produced into the legacy format — matched
condition-for-condition against the PM's sentence. A guard that fired on
every touch of a 192-line backlog would be bypassed inside a day and would
then protect nothing (DR-115 § PM direction (A)).

Text leads with the transformer, per design-as-offers
(``docs/wiki/hook-best-practices.md`` § nag->action / design-as-offers):
the fleet migration op is named as FORTHCOMING (verified absent from disk
at authoring time — ``coordinator_core/ops/fleet/migrate_prose_queue.py``
does not exist yet; DR-115 § PM direction (C) authorizes it but it has not
landed), then ``coordinator-queue-append`` for a single new entry, then the
rule, never first.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope
    carries only ``additionalContext``, never ``permissionDecision``.
  - Does NOT fire when the target does not exist on disk — that is the
    creation-deny sibling's job, never this module's (disjoint
    discriminants by construction: sibling fires target-ABSENT, this one
    fires target-PRESENT).
  - Does NOT fire when the dated-pipe-row count does not increase — a
    prune, reformat, or closure deletion (count decreases or stays the
    same) is always silent, regardless of how much text changed.
  - Does NOT fire on any ``*.yaml`` write under a structured queue
    directory — the path gate (shared with the creation sibling) only
    matches the legacy single-file prose shape.
  - Does NOT fall back to a weaker per-fragment count when whole-file
    reconstruction fails — skips the advisory entirely in that case.
  - Never raises: any unexpected input shape, read failure, or
    reconstruction failure returns ``None`` (ALLOW/no-op), never an
    advisory.

Spec backlink: example-doctrine-repo docs/decisions/DR-115-queue-shape-is-a-scope-collision-not-a-staleness.md
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    is_trivial_reason as _is_trivial_reason,
    operator_override_note,
)
from coordinator_core.write_guards.nudge_improvement_queue_write import _ENTRY_LINE_RE
from coordinator_core.write_guards.nudge_prose_queue_creation import (
    _extract_file_path,
    _is_queue_shaped_basename,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 170  # deny-offer band; next slot after 160 (see nudge_new_sh_file_naked_python.py)

#: Read cap for Edit/MultiEdit whole-file reconstruction, matching the
#: sibling nudge_baton_body_bar's cap -- these are small text queue files.
_MAX_WHOLE_FILE_BYTES = 1024 * 1024

#: Reason-shaped punt, following the COORDINATOR_QUEUE_PUNT /
#: COORDINATOR_BATON_BODY_PUNT convention for advisory/deny-offer guards in
#: this package -- a non-trivial reason (>= 12 chars) set BEFORE launch
#: suppresses this advisory on future writes; this guard never blocks
#: regardless of what this var holds.
_ESCAPE_HATCH_ENV_VAR = "COORDINATOR_PROSE_QUEUE_APPEND_PUNT"

#: Forthcoming fleet migration op named by DR-115 § PM direction (C) -- not
#: yet landed as of this module's authoring; verified absent from disk.
#: Do not invent an invocation syntax for a script that does not exist.
_TRANSFORMER_PATH = "coordinator_core/ops/fleet/migrate_prose_queue.py"


def _extract_str(tool_input: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = tool_input.get(key)
        if value:
            return str(value)
    return ""


def _read_file_safely(file_path: str) -> Optional[str]:
    try:
        if os.path.getsize(file_path) > _MAX_WHOLE_FILE_BYTES:
            return None
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _apply_one_edit(
    content: str, old_string: Any, new_string: Any, replace_all: Any
) -> Optional[str]:
    if not isinstance(old_string, str) or not old_string:
        return None
    if old_string not in content:
        return None
    new_string = new_string if isinstance(new_string, str) else ""
    if replace_all:
        return content.replace(old_string, new_string)
    return content.replace(old_string, new_string, 1)


def _reconstruct_pre_and_post(
    tool_name: str, tool_input: Dict[str, Any], resolved_path: str
) -> Optional[tuple]:
    """Return (pre_content, post_content) for the entry-count delta, or
    ``None`` on any reconstruction failure (skip the advisory)."""
    if tool_name == "Write":
        pre = _read_file_safely(resolved_path)
        if pre is None:
            return None
        post = _extract_str(tool_input, "content")
        return (pre, post)

    pre = _read_file_safely(resolved_path)
    if pre is None:
        return None

    if tool_name == "Edit":
        post = _apply_one_edit(
            pre,
            tool_input.get("old_string"),
            tool_input.get("new_string"),
            tool_input.get("replace_all"),
        )
        if post is None:
            return None
        return (pre, post)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list) or not edits:
            return None
        post = pre
        for edit in edits:
            if not isinstance(edit, dict):
                return None
            post = _apply_one_edit(
                post,
                edit.get("old_string"),
                edit.get("new_string"),
                edit.get("replace_all"),
            )
            if post is None:
                return None
        return (pre, post)

    return None


_REASON_TEMPLATE = """Legacy queue. Append instead: `coordinator-queue-append --schema {family}`. Read instead: `records_query`. Migrate instead: `{transformer}`.

{override_note}"""

_TRIVIAL_HINT = """
[hook] {env_var} was set but the value was trivial ("1", "ok", "yes",
[hook] under 12 chars, or degenerate filler). A human operator, before
[hook] launch, would need to re-set it with an actual reason, e.g.:
[hook]   {env_var}="repo not migrating yet, tracked separately"
"""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None

        normalized = file_path.replace("\\", "/")
        while "//" in normalized:
            normalized = normalized.replace("//", "/")

        segments = [seg for seg in normalized.split("/") if seg]
        if not segments:
            return None
        if any(seg.lower() == "archive" for seg in segments):
            return None
        if not any(seg.lower() == "state" for seg in segments):
            return None

        basename = normalized.rsplit("/", 1)[-1]
        if not _is_queue_shaped_basename(basename):
            return None

        # --- existence gate: only an ALREADY-EXISTING legacy queue is an
        #     append target; a nonexistent target is the creation-deny
        #     sibling's concern, never this one's ---
        resolved = file_path
        if not os.path.isabs(resolved):
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                resolved = os.path.join(cwd, file_path)
        try:
            if not os.path.exists(resolved):
                return None
        except Exception:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        reconstructed = _reconstruct_pre_and_post(tool_name, tool_input, resolved)
        if reconstructed is None:
            return None
        pre_content, post_content = reconstructed

        pre_entries = len(_ENTRY_LINE_RE.findall(pre_content))
        post_entries = len(_ENTRY_LINE_RE.findall(post_content))
        if post_entries <= pre_entries:
            return None

        punt_reason = os.environ.get(_ESCAPE_HATCH_ENV_VAR, "") or ""
        if punt_reason and not _is_trivial_reason(punt_reason):
            return None

        family = basename[:-3] if basename.lower().endswith(".md") else basename
        hint = ""
        if punt_reason:
            hint = _TRIVIAL_HINT.format(env_var=_ESCAPE_HATCH_ENV_VAR)

        reason = _REASON_TEMPLATE.format(
            transformer=_TRANSFORMER_PATH,
            family=family,
            override_note=operator_override_note(
                _ESCAPE_HATCH_ENV_VAR,
                reason_placeholder="repo not migrating yet, tracked separately",
            ),
        )
        if hint:
            reason = reason + "\n" + hint

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard advises only on a
        # positive append match, never on an error.
        return None
