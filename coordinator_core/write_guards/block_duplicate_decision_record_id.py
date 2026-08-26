"""coordinator_core.write_guards.block_duplicate_decision_record_id — hard-deny guard.

Purpose: catch a SECOND `docs/decisions/*.md` record claiming an `id:` a
sibling record already carries, at write time — the cheap backstop named
alongside the mint-and-reserve verb in `coordinator_core.ops.
decision_record_mint` (see that module's docstring for the fuller incident
history: `docs/plans/2026-08-22-a-commit-is-one-spawn-not-eleven.md` was
authored against `DR-351`; a peer committed a different record under the
same number first; the EXECUTOR caught it and refused, but only after a
dispatch, a stopped workflow run, and a 17-site renumber had already been
spent). `decision_record.mint_id` closes the common case (a plan minted its
number up front); this guard closes the remainder — a hand-authored record
that never went through the mint op at all, which the mint verb cannot see.

Scope: fires ONLY on a path matching `docs/decisions/*.md` (the same
`applies_to` glob `coordinator_core/frontmatter/schemas/decision.schema.json`
declares) — this is a duplicate-ID check for the decision-record family
specifically, not a general frontmatter-uniqueness guard.

Algorithm: extract the PROSPECTIVE `id:` value the write would leave on
disk (see `_prospective_id`, which mirrors `validate_frontmatter_schema_deny.
_compute_content_to_probe`'s per-tool content-reconstruction rather than
importing it — that helper is module-private there and this guard's need is
narrow enough not to justify a shared/public extraction). If the prospective
id is present, scan every OTHER `.md` file in the same directory for the
same `id:` value in its own frontmatter. A hit denies; anything else (no id
on the prospective write, no sibling collision, an unreadable sibling) is a
silent ALLOW.

Negative-spec:
    - Does NOT deny a write that merely RE-STATES the same file's own
      existing id (an Edit that reformats a record without touching `id:`,
      or that writes back byte-identical frontmatter) — comparison always
      excludes the file being written from the sibling scan.
    - Does NOT validate the id's SHAPE (e.g. that it matches `DR-\\d+`) — the
      schema's `id` field is already a plain string
      (`decision.schema.json`), and shape enforcement is
      `validate_frontmatter_schema_advisory`'s concern (schema-shape is
      always-advisory per the 2026-08-06 warn-not-block ruling), not this
      guard's. This guard checks exactly one property: uniqueness across the
      directory.
    - Does NOT consult `state/decision-record-reservations/` — a reservation
      is a NUMBER held by the mint op before any file exists; this guard
      only ever compares REAL files already on disk. A number reserved but
      never yet written is invisible to this guard by design (nothing to
      collide with on disk yet); the mint op's own atomic reservation create
      is what prevents two mints from handing out that same number in the
      first place.
    - Does NOT fail closed on a read/parse error for a SIBLING file — an
      unreadable or malformed neighbour degrades that one comparison to "no
      match", never to a deny; this guard's job is to catch a positively
      observed duplicate, not to demand every sibling be perfectly formed.
    - Does NOT scan subdirectories — `docs/decisions/*.md` is a flat glob;
      matching it with `Path.iterdir()` rather than `rglob` keeps the scan
      bounded to exactly the corpus the schema itself claims.

Spec backlink: state/improvement-queue/2026-08-23-nothing-allocates-dr-
numbers-so-a-plan-s-7aa417a58bce.yaml
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 137  # hard-deny band; next free slot after guard_memory_store_cap (136)

_DECISIONS_PATH_RE = re.compile(r"(?:^|[/\\])docs[/\\]decisions[/\\][^/\\]+\.md$", re.IGNORECASE)
_ID_LINE_RE = re.compile(r"^id:\s*(.+?)\s*$")


def _extract_frontmatter_id(content: str) -> Optional[str]:
    """Return the `id:` value from `content`'s leading `---` frontmatter block, or None."""
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = _ID_LINE_RE.match(line)
        if m:
            return re.sub(r'^["\']|["\']$', "", m.group(1)).strip()
    return None


def _apply_edit(content: str, old_string: str, new_string: str) -> Tuple[str, bool]:
    idx = content.find(old_string)
    if idx == -1:
        return content, False
    return content[:idx] + new_string + content[idx + len(old_string):], True


def _prospective_id(tool_name: str, tool_input: dict, abs_file_path: str) -> Optional[str]:
    """Return the `id:` the write would leave on disk, or None if undeterminable.

    Mirrors `validate_frontmatter_schema_deny._compute_content_to_probe`'s
    per-tool reconstruction (Write: raw content; Edit/MultiEdit: apply the
    edit(s) onto the file's current on-disk text) — see module docstring for
    why this is a narrow local copy rather than a shared import.
    """
    if tool_name == "Write":
        return _extract_frontmatter_id(tool_input.get("content") or "")

    try:
        with open(abs_file_path, "r", encoding="utf-8") as fh:
            probe = fh.read()
    except OSError:
        return None

    if tool_name == "Edit":
        result, matched = _apply_edit(
            probe, tool_input.get("old_string") or "", tool_input.get("new_string") or ""
        )
        if not matched:
            return None
        return _extract_frontmatter_id(result)

    if tool_name == "MultiEdit":
        for edit in tool_input.get("edits") or []:
            result, matched = _apply_edit(
                probe, edit.get("old_string") or "", edit.get("new_string") or ""
            )
            if not matched:
                return None
            probe = result
        return _extract_frontmatter_id(probe)

    return None


def _sibling_holding_id(directory: Path, target_id: str, exclude: Path) -> Optional[Path]:
    """Return the path of a sibling `.md` file already claiming `target_id`, or None.

    `exclude` is never scanned (the file this write itself targets) — an
    Edit that leaves its own id untouched must never collide with itself.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_file() or entry.suffix.lower() != ".md":
                continue
            if entry.resolve() == exclude.resolve():
                continue
        except OSError:
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _extract_frontmatter_id(text) == target_id:
            return entry
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in MATCHERS:
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        file_path = tool_input.get("file_path") or ""
        if not file_path or not _DECISIONS_PATH_RE.search(file_path):
            return None

        abs_file_path = str(Path(file_path))
        prospective_id = _prospective_id(tool_name, tool_input, abs_file_path)
        if not prospective_id:
            return None

        directory = Path(abs_file_path).parent
        collision = _sibling_holding_id(directory, prospective_id, Path(abs_file_path))
        if collision is None:
            return None

        reason = (
            f"`id: {prospective_id}` is already claimed by {collision.name} in this "
            f"directory — two decision records cannot share one DR number. Mint a "
            f"fresh number via the `decision_record.mint_id` op instead of guessing "
            f"`max + 1`, which is exactly what produced this collision."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    except Exception:
        # Unexpected processing error fails open — this guard denies only on a
        # POSITIVELY OBSERVED sibling collision, never on its own inability to
        # read/parse something (see module docstring's negative-spec).
        return None
