"""
coordinator_core.ops.goal_kr_status — locked KR-status writer for goal YAML artifacts
(goal.set_kr_status op).

Purpose: record a single key_result's `status` field into a `state/goals/*.yaml`
whole-document goal artifact (schema: goal, C1 2026-07-13 shape), performing the
read-modify-write under `coordinator_core.locked_write.locked_rmw()` so that
concurrent writers targeting the SAME goal file (multiple plan-arc executors
fanned out from one roadmap-seed plan, each reporting a different KR's progress)
cannot clobber one another's update via a last-writer-wins race.

Motivating gap: `coordinator_core.goals.reassess_krs` reads existing signal and
proposes KR statuses but explicitly does NOT overwrite `key_results[].status`
(see its module docstring negative-spec). No prior op wrote the live status field
at all — every caller that needed to record a KR's actual progress had to hand-edit
the YAML in-place, unguarded by any lock. This op is that missing write path.

Parsing approach: line-oriented, mirroring `coordinator_core.goals.reassess_krs`'s
`extract_key_results` / `parse_kr_block` — NOT a round-trip YAML parser. A full
YAML load+dump would normalize quoting/comments/key ordering across the whole
document; this op instead locates the target KR entry's existing `status:` line
by text and rewrites ONLY that line's value, leaving every other byte (comments,
formatting, the free-form `body:` block) untouched.

Registered as ``goal.set_kr_status`` in ops/__init__.py's eager-import table,
classified ``OpClass.MUTATING`` in authz/classification.py, and scoped ``"none"``
in op_scopes.py (same class as `goals.reassess_krs` — the goal file path is an
explicit caller-supplied param, not repo_root-derived).

Spec backlink: state/handoffs/2026-07-25_001110_slate-shared-substrate-extractions.md
(BINLIB/HITRAFFIC/HOOKS roadmap-seed plan arcs — parallel KR-progress writers to
one shared goal YAML)

Write provenance (2026-07-25, actioning example-market-data-repo-em's cross-repo ask —
cross-repo/inbox/2026-07-25-example-market-data-repo-em-goal-engine-seams-and-kr-status-
provenance.md): alongside the rewritten `status:` line, this op ALSO writes two
sibling fields into the SAME `key_results[]` entry — `status_source` (the
constant "goal.set_kr_status", identifying this op as the writer) and
`status_set_at` (UTC ISO-8601, seconds precision, "Z" suffix). Before this,
a hand-edited `status:` and a properly-applied one were byte-identical on
disk, so field-ownership was unenforceable — no consumer could tell them
apart. The two provenance fields turn that into a mechanically checkable
predicate: status changed but provenance absent or stale => out-of-band edit.
Idempotent: a second invocation rewrites both sibling lines in place rather
than appending duplicates.

Negative-spec:
  - Does NOT touch `coordinator_core.ops.goal_append`'s per-machine JSONL event
    log (`goals-log.<machine>.jsonl`) — that is a distinct append-only writer for
    goal-DECLARATION events, not KR-progress updates to an existing goal artifact.
  - Is NOT a general YAML editor — it locates and rewrites exactly one
    `status:` scalar inside one matched `key_results[]` entry, plus creating-or-
    updating that SAME entry's `status_source`/`status_set_at` provenance
    siblings (see "Write provenance" above), and nothing else. It does not add,
    remove, or reorder KR entries, and does not touch any other field (id, text,
    kind, weekly_perceptible, or any top-level field such as title, objective,
    body, etc.). The provenance write is a narrowly-scoped, explicitly-documented
    exception to "does not add fields that aren't already there" — exactly two
    named siblings of the one field this op owns, never a general license to add
    arbitrary fields.
  - Does NOT create a KR entry that does not already exist on disk — an unknown
    `kr_id` is a hard ValueError, never a silent no-op or a newly-minted entry.
  - Does NOT validate `status` against a fixed enum — the KR-status contract
    (`GoalKeyResultStatus.status` in contract/cockpit_schema/entities/goal.py) is
    typed as a bare `str`, so this op only rejects empty/whitespace-only values.
  - Does NOT project `status_source`/`status_set_at` onto the cockpit wire —
    `GoalKeyResultStatus` (contract/cockpit_schema/entities/goal.py) and the
    frozen `cockpit-contract` schema JSON are untouched by this change; that
    surface is gated by a bilateral version-bump with DoE and is a separate
    decision. The two provenance fields exist only in the artifact-side YAML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, NamedTuple, Optional

from coordinator_core.ipc import register_op
from coordinator_core.locked_write import locked_rmw

# Value written into a written/rewritten key_results[<id>].status_source line —
# identifies THIS op as the writer (see module docstring "Write provenance").
STATUS_SOURCE = "goal.set_kr_status"


def _utc_now_stamp() -> str:
    """UTC ISO-8601 timestamp, seconds precision, 'Z' suffix (status_set_at shape)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------------------------------------------------------------------
# Line-shape regexes — same shapes as coordinator_core.goals.reassess_krs, kept
# independent (not imported) so this module's parsing cannot silently drift if
# reassess_krs's regexes are ever tightened/loosened for its own purposes.
# ---------------------------------------------------------------------------

# New-KR-list-item line: "  - " (any leading whitespace, dash, required whitespace).
_KR_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+")
# Indented "field: value" line inside an active KR entry.
_KR_FIELD_RE = re.compile(r"^([ \t]+)([a-z_]+):[ \t]*(.*)$")
# Inline "id: value" on the dash line itself, after stripping through the
# first "- " occurrence (mirrors reassess_krs's `${line#*- }` shortest-prefix strip).
_INLINE_ID_RE = re.compile(r"^id:[ \t]*(.*)$")


def _line_is_top_level_key(line: str) -> bool:
    """True for a line starting a new top-level YAML key (no leading whitespace)."""
    return bool(re.match(r"^[a-zA-Z_]", line)) and not re.match(r"^[ \t]", line)


def _find_key_results_start(lines: List[str]) -> Optional[int]:
    """Return the index of the FIRST line after ``key_results:`` header, or None.

    Port of reassess_krs.extract_key_results's opening condition: any line
    starting with "key_results:" (awk-style prefix match, not exact-line) opens
    the block; the header line itself is not part of the block.
    """
    for i, line in enumerate(lines):
        if line == "key_results:" or line.startswith("key_results:"):
            return i + 1
    return None


class _KREntry(NamedTuple):
    """One indexed ``key_results[]`` list item.

    ``status_idx`` is None when the entry has no ``status:`` field — callers
    must fail loud on that case (see module negative-spec: this op never adds
    a *new* ``status:`` field to an entry that lacks one). ``status_source_idx``
    / ``status_set_at_idx`` are None when the corresponding provenance sibling
    is absent — that is NOT a fail-loud case; the write path creates it.
    """

    kr_id: str
    status_idx: Optional[int]
    status_source_idx: Optional[int]
    status_set_at_idx: Optional[int]


def _index_kr_entries(lines: List[str], start: int) -> List[_KREntry]:
    """Walk the key_results[] block starting at *start*, returning one
    ``_KREntry`` per "- " list item.
    """
    entries: List[_KREntry] = []
    in_entry = False
    cur_id = ""
    cur_status_idx: Optional[int] = None
    cur_status_source_idx: Optional[int] = None
    cur_status_set_at_idx: Optional[int] = None

    def _flush() -> None:
        if in_entry:
            entries.append(
                _KREntry(cur_id, cur_status_idx, cur_status_source_idx, cur_status_set_at_idx)
            )

    i = start
    n = len(lines)
    while i < n:
        line = lines[i]
        if _line_is_top_level_key(line):
            break

        if _KR_ITEM_RE.match(line):
            _flush()
            in_entry = True
            cur_id = ""
            cur_status_idx = None
            cur_status_source_idx = None
            cur_status_set_at_idx = None

            dash_idx = line.find("- ")
            rest = line[dash_idx + 2 :] if dash_idx != -1 else line
            m_id = _INLINE_ID_RE.match(rest)
            if m_id:
                cur_id = m_id.group(1).strip()
        elif in_entry:
            m = _KR_FIELD_RE.match(line)
            if m:
                field_name, field_val = m.group(2), m.group(3)
                if field_name == "id":
                    cur_id = field_val.strip()
                elif field_name == "status":
                    cur_status_idx = i
                elif field_name == "status_source":
                    cur_status_source_idx = i
                elif field_name == "status_set_at":
                    cur_status_set_at_idx = i

        i += 1

    _flush()
    return entries


def _apply_kr_status(old_text: str, kr_id: str, new_status: str) -> str:
    """Pure mutate step: rewrite ``key_results[<kr_id>].status`` in *old_text*,
    stamping write provenance (``status_source``/``status_set_at``) alongside it.

    Raises ValueError (never a silent no-op) when:
      - the document has no ``key_results:`` block at all,
      - no entry in that block matches ``kr_id``,
      - the matched entry has no existing ``status:`` line to rewrite.

    Preserves every other byte of the document — only the matched status
    line's value is replaced (indentation kept exactly as found), and the
    matched entry's ``status_source``/``status_set_at`` sibling lines are
    created (immediately after the ``status:`` line, same indentation) or
    updated in place if already present. Idempotent: a second call rewrites
    the existing provenance lines rather than appending duplicates.
    """
    had_trailing_newline = old_text.endswith("\n")
    lines = old_text.splitlines()

    start = _find_key_results_start(lines)
    if start is None:
        raise ValueError("goal file has no key_results: block")

    entries = _index_kr_entries(lines, start)
    known_ids = [e.kr_id for e in entries if e.kr_id]
    match = next((e for e in entries if e.kr_id == kr_id), None)
    if match is None:
        raise ValueError(
            f"unknown kr_id {kr_id!r} — no key_results[] entry with this id "
            f"(known ids: {known_ids})"
        )

    if match.status_idx is None:
        raise ValueError(
            f"key_results[] entry {kr_id!r} has no existing status: field to rewrite — "
            "refusing to add a new field to an entry that does not already carry one"
        )

    m = _KR_FIELD_RE.match(lines[match.status_idx])
    assert m is not None  # status_idx was only ever set from a line that matched
    indent = m.group(1)
    now = _utc_now_stamp()

    # Update in-place first (original indices are still valid — no insertion
    # has happened yet), then insert any missing sibling(s) immediately after
    # the status: line, source before set_at, preserving stable field order.
    lines[match.status_idx] = f"{indent}status: {new_status}"
    if match.status_source_idx is not None:
        lines[match.status_source_idx] = f"{indent}status_source: {STATUS_SOURCE}"
    if match.status_set_at_idx is not None:
        lines[match.status_set_at_idx] = f"{indent}status_set_at: {now}"

    insertions: List[str] = []
    if match.status_source_idx is None:
        insertions.append(f"{indent}status_source: {STATUS_SOURCE}")
    if match.status_set_at_idx is None:
        insertions.append(f"{indent}status_set_at: {now}")
    for offset, new_line in enumerate(insertions):
        lines.insert(match.status_idx + 1 + offset, new_line)

    new_text = "\n".join(lines)
    if had_trailing_newline:
        new_text += "\n"
    return new_text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_kr_status(
    goal_file: Path,
    kr_id: str,
    status: str,
    *,
    repo_root: Optional[Path] = None,
    timeout: float = 10.0,
) -> dict:
    """Locked read-modify-write of one KR's status field in *goal_file*.

    Parameters:
        goal_file  — absolute path to the target ``state/goals/*.yaml`` artifact.
                     Must already exist (missing_ok=False on locked_rmw — a caller
                     naming a nonexistent goal file gets FileNotFoundError, never
                     a silently-created one).
        kr_id      — the key_results[] entry's ``id`` value, matched verbatim.
        status     — the new status value. Non-empty (post-strip) required; no
                     fixed enum (see module negative-spec).
        repo_root  — any path inside goal_file's git repository, used by
                     locked_rmw to locate the lock sidecar (see locked_write.py).
                     Defaults to goal_file's parent directory, which is always
                     inside the repo that owns state/goals/.
        timeout    — max seconds to wait for the cross-process lock (locked_rmw's
                     LOCK_TIMEOUT_SECS default is 10s; overridable for callers
                     with tighter/looser budgets).

    Returns:
        {goal_file, kr_id, status} — the effective values written (or already
        present, on a byte-identical no-op).

    Raises:
        ValueError  — kr_id/status missing or blank, unknown kr_id, or a matched
                      entry with no existing status: field.
        FileNotFoundError — goal_file does not exist.
        LockTimeout — the cross-process lock could not be acquired within timeout.
    """
    kr_id = (kr_id or "").strip()
    if not kr_id:
        raise ValueError("kr_id is required")
    status = (status or "").strip()
    if not status:
        raise ValueError("status is required")

    goal_file = Path(goal_file)
    lock_repo_root = Path(repo_root) if repo_root is not None else goal_file.parent

    locked_rmw(
        goal_file,
        lambda old_text: _apply_kr_status(old_text, kr_id, status),
        repo_root=lock_repo_root,
        timeout=timeout,
        missing_ok=False,
    )

    return {"goal_file": str(goal_file), "kr_id": kr_id, "status": status}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("goal.set_kr_status")
def _goal_set_kr_status(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'goal.set_kr_status' handler — locked KR-status write.

    MUTATING (rewrites one status: scalar in a state/goals/*.yaml artifact under
    a cross-process flock). Delegates to ``set_kr_status()``.

    "none"-scoped (op_scopes.py): goal_file is an explicit caller-supplied
    absolute path, same class as goals.reassess_krs's goals_dir — the injected
    ``repo_root`` argument (always None for a "none"-scoped op; see
    ipc.py::_OP_KEY_SCOPE) is NOT used. Lock placement instead derives from the
    optional ``repo_root`` param below, or goal_file's parent when omitted.

    Required params:
        goal_file (str) — absolute path to the target goal YAML.
        kr_id     (str) — the key_results[] entry's id.
        status    (str) — the new status value.

    Optional params:
        repo_root (str) — absolute path inside goal_file's git repo, used to
                          locate the lock sidecar. Default: goal_file's parent.
        timeout   (float) — max seconds to wait for the lock. Default: 10.0.

    Returns: {goal_file, kr_id, status}.

    Raises:
        ValueError (propagated as JSON-RPC error) for missing/blank required
        params, an unknown kr_id, or a matched entry with no status: field.
    """
    goal_file_raw = params.get("goal_file") or ""
    if not goal_file_raw:
        raise ValueError("goal.set_kr_status requires goal_file (absolute path to the goal YAML)")

    repo_root_raw = params.get("repo_root") or ""
    timeout = float(params.get("timeout", 10.0))

    return set_kr_status(
        Path(goal_file_raw),
        params.get("kr_id", ""),
        params.get("status", ""),
        repo_root=Path(repo_root_raw) if repo_root_raw else None,
        timeout=timeout,
    )
