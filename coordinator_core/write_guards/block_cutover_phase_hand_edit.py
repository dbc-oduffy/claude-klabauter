"""coordinator_core.write_guards.block_cutover_phase_hand_edit — advisory guard.

Closes the unsanctioned-hand-edit half of the cutover state machine's D1
split (D1/D4, `docs/plans/2026-07-25-cutover-state-machine.md`): the
sanctioned advance is a registered op (`cutover.advance`, gated by
`cutover.gate`'s engine-derived consumer-coverage check); this guard is the
complementary advisory on the OTHER path — an agent directly `Write`/`Edit`/
`MultiEdit`-ing a cutover record's `phase:` frontmatter field, which skips
the gate entirely. `phase: dual-write` -> `phase: retiring` is a two-character
Edit, and per plan decision D3 the frontmatter schema validator does not
catch it (warn-by-default, deny branch gated on
`COORDINATOR_SCHEMA_STRICT=1`, and even in strict mode a lone `phase` flip on
a record whose `confirmed_consumers` is already non-empty violates no
`allOf` coupling). This guard is the actual discharge.

Modelled on the sibling `block_consumed_handoff_edit.py` — same package,
same interface (`write_guards/INTERFACE.md`): path-candidate extraction
(`file_path`, or `edits[].file_path` for `MultiEdit`), backslash/slash-run
normalization, `..`-traversal rejection, git-root-relative containment
against the cutover records tree, and a first-frontmatter-block field
extractor. Unlike that guard there is no scaffold to pre-create — the
sanctioned route already exists (`cutover-cli advance`) — so the advisory
leads directly with that route (design-as-offers) rather than writing a
file.

Residual, stated plainly rather than papered over (plan D4): write guards
intercept the AGENT's own tool calls (`Write`/`Edit`/`MultiEdit` issued
through this harness). An external editor, or a shell redirect / `sed`
against the file from outside the agent's tool surface, still gets through
— this guard has no hook into either. That residual is accepted because the
guard covers the dominant agent-mediated path and keeps the diff reviewable;
"cannot be routed around" would overstate what it does. The correct claim is
narrower: the agent-mediated hand-edit path is closed, and a residual
out-of-band edit path remains and is accepted.

Negative-spec:
  - Does NOT flag edits to any OTHER field of a cutover record (e.g.
    `confirmed_consumers`, `gate_source`, `derivation_history`) — only a
    touch to the `phase:` line is in scope. Those other fields are the
    op's own write surface (`cutover.advance` writes them alongside
    `phase`) and a guard that blanket-flagged the whole file would also
    surface noise on the op's own writes if it ever went through this same
    tool surface, and would over-reach past what D4 asks for.
  - Does NOT flag Write/Edit/MultiEdit that CREATES a new cutover record
    (file does not yet exist on disk) — authoring a fresh record (C3,
    C11-C17) is the normal authoring path, not a hand-edit of an existing
    phase; the guard only fires once a record already exists on disk.
  - Does NOT fail closed on any error — every subprocess/file-read failure
    degrades to ALLOW (fewer candidates matched), matching the sibling
    guards' fail-open discipline; a guard crash never fabricates an
    advisory (`write_guards/engine.py`'s own contract: "the engine never
    fabricates a deny on a guard crash", which applies equally to the
    advisory phase never fabricating a firing).
  - Does NOT re-implement `cutover.gate`'s coverage logic — this guard has
    no opinion on whether an advance IS correct, only on whether it went
    through the sanctioned op.

Spec backlink: DoE-claude:pln-cutover-state-machine-a-phase--96db57 (chunk C4d, D1, D3, D4)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 112

_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_CUTOVER_PHASE_HAND_EDIT"

_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")

_CUTOVER_RECORD_RE = re.compile(r"(^|/)state/roadmap/.+/cutovers/[^/]+\.md$")

_PHASE_LINE_RE = re.compile(r"^phase:[ \t]*(.*)$")


def _collapse_slashes(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    result = resolve_repo_root(cwd)
    if result is None:
        print(
            f"block_cutover_phase_hand_edit: no git root resolved for cwd="
            f"{cwd!r}, treating as no git root (decision unaffected)",
            file=sys.stderr,
        )
    return result


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


def _normalize_and_gate(cand: str, git_root: Optional[str]) -> Optional[str]:
    cn = _collapse_slashes(cand)

    if _TRAVERSAL_RE.search(cn):
        return None

    if git_root:
        if cn.startswith("/") or re.match(r"^[A-Za-z]:", cn):
            abs_cn = cn
        else:
            # `rstrip("/\\")`, never `rstrip("/")` -- a trailing BACKSLASH
            abs_cn = git_root.rstrip("/\\") + "/" + cn
        try:
            abs_cn_canon = casefold_path(str(Path(abs_cn).resolve(strict=False)))
        except OSError as exc:
            print(f"block_cutover_phase_hand_edit: path resolve failed for "
                  f"{abs_cn!r}, gating on unresolved form: {exc}", file=sys.stderr)
            abs_cn_canon = abs_cn
        expected_prefix = casefold_path(git_root.rstrip("/\\") + "/state/roadmap/")
        if not abs_cn_canon.startswith(expected_prefix):
            return None

    if not _CUTOVER_RECORD_RE.search(cn):
        return None

    return cn


def _touches_phase_line(fragment: Optional[str]) -> bool:
    if not fragment:
        return False
    for line in fragment.splitlines():
        if _PHASE_LINE_RE.match(line):
            return True
    return False


def _edit_touches_phase(
    tool_name: str, tool_input: Dict[str, Any], cand: str, pre_image: Optional[str] = None
) -> bool:
    if tool_name == "Write":
        content = tool_input.get("content")
        content_str = content if isinstance(content, str) else None
        if _touches_phase_line(content_str):
            return True
        if pre_image is not None and _touches_phase_line(pre_image) and not _touches_phase_line(
            content_str
        ):
            return True
        return False

    if tool_name == "Edit":
        return (
            _touches_phase_line(tool_input.get("old_string"))
            or _touches_phase_line(tool_input.get("new_string"))
        )

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return False
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            if edit.get("file_path") != cand:
                continue
            if _touches_phase_line(edit.get("old_string")) or _touches_phase_line(
                edit.get("new_string")
            ):
                return True
        return False

    return False


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        if os.environ.get(_OVERRIDE_ENV, "0") == "1":
            return None

        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        cwd = payload.get("cwd") or None
        git_root = _resolve_git_root(cwd)
        base_dir = Path(cwd) if cwd else Path.cwd()

        file_path = ""
        disk_path: Optional[Path] = None
        for cand in candidates:
            cn = _normalize_and_gate(cand, git_root)
            if cn is None:
                continue
            candidate_disk = Path(cn) if Path(cn).is_absolute() else (base_dir / cn)
            # A record that does not exist yet is being AUTHORED, not
            if not candidate_disk.is_file():
                continue
            # a phase-field DELETION (new content omits phase: entirely), not
            pre_image: Optional[str] = None
            if tool_name == "Write":
                try:
                    pre_image = candidate_disk.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pre_image = None
            if not _edit_touches_phase(tool_name, tool_input, cand, pre_image):
                continue
            file_path = cand
            disk_path = candidate_disk
            break

        if not file_path or disk_path is None:
            return None

        _note = operator_override_note(_OVERRIDE_ENV, payload=payload, git_root=git_root)
        reason = (
            f"Cutover phase edit on {file_path}: advance via the sanctioned "
            "op instead of a hand-edit, which skips the consumer-coverage "
            "check. Use instead: `cutover-cli advance <record>`."
            + ("\n\n" + _note if _note else "")
        )

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        return None
