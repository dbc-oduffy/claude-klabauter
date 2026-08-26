"""coordinator_core.write_guards.block_home_dir_memo_delivery — hard-deny guard.

Python engine-ification of DoE's
``coordinator/hooks/scripts/block-home-dir-memo-delivery.py`` PreToolUse
(Write|Edit|MultiEdit|NotebookEdit) hook, per the write-guard fan-in
(write_guards/INTERFACE.md).

Purpose (ported verbatim from the reference hook): ``~/.claude`` is a
CONFIG-ONLY tree — harness settings, ``docs/decisions/``, and one coordinator
bootstrap pointer. It is not a working repo and it is not a valid memo
receiver. The canonical central receiver is ``doe-claude-em`` (registry key
``repos.doe_claude``), inbox ``<doe_claude>/cross-repo/inbox/``.

The `cross-repo-memo` CLI is NOT the hole: it already code-pins the
``.claude-em``, ``claude-home``, and ``coordinator-claude`` aliases onto
``claude-central-em``. The hole is a HAND-WRITTEN delivery by absolute path —
an agent that skips the CLI and Writes straight at
``C:\\Users\\<me>\\.claude\\cross-repo\\inbox\\<file>.md``.

Block predicate: DENY when the tool is a file-mutating tool AND its resolved
absolute target path is equal to, or a descendant of,
``<home>/.claude/cross-repo`` for ANY plausible home root. Home resolution is
a UNION of ``USERPROFILE``, ``HOME``, and ``pathlib.Path.home()`` (plus
``CLAUDE_HOME`` treated as a direct ``.claude`` root when set), rather than a
first-wins chain — a Git-Bash ``HOME``/``USERPROFILE`` divergence on Windows
must never open a hole.

Scope is deliberately ``<home>/.claude/cross-repo/**`` and nothing else:
ordinary config edits under ``~/.claude`` (``settings.json``,
``docs/decisions/``, CLAUDE.md, machine-local) MUST pass through silently.

Maintainer override — DELIBERATELY UNADVERTISED: ``COORDINATOR_OVERRIDE_HOME_
MEMO_GUARD=1`` disables this guard entirely, exactly as in the reference hook.
It exists for maintenance and test fixtures and is documented HERE only — it
is deliberately NOT named in the deny message (every other guard in this tree
advertises its override in the deny text; this one is the exception, because
this guard's scope has no legitimate use, and an advertised env var would
turn a hard block into a speed bump for precisely the caller it exists to
stop).

Ported from the DoE hook
  ``coordinator/hooks/scripts/block-home-dir-memo-delivery.py``.

Negative-spec:
  - Does NOT filter on ``tool_name`` beyond the reference's own explicit
    membership check — MATCHERS-based filtering in the engine already
    restricts invocation to the same four tools, but the in-body check is
    kept for fidelity with the reference's own defense-in-depth.
  - Does NOT hand-roll the symlink/``..``-resolution — containment delegates
    directly to ``coordinator_core.ops._path_guard.contained_path`` (fidelity
    rule 8: reuse shared helpers, don't reinvent): symlink-safe, non-strict
    for not-yet-created targets, matching the reference's use of
    ``os.path.realpath`` on paths that usually don't exist yet. A prior
    revision of this guard carried a local ``_contained`` duplicate because
    the shared helper printed a ``skip: contained_path: ...`` diagnostic on
    every non-matching root — on this guard's call frequency (every
    ``Write``/``Edit``/``MultiEdit``/``NotebookEdit`` in the fleet) that was
    stderr noise on nearly every ordinary write. The shared helper no longer
    logs the ordinary "not under this root" ``ValueError`` case (only a
    genuine ``OSError`` is logged), so the duplicate is gone and this guard
    calls ``contained_path`` directly. The reference's ``os.path.normcase``
    case-fold is preserved on the roots for symmetry with the reference
    hook, but ``os.path.normcase`` is a no-op on POSIX (macOS included) —
    the actual case-insensitive-but-case-preserving APFS gap is closed by
    ``_casefold_path`` (Unicode casefold, applied to both candidate and
    roots before ``contained_path``'s ``relative_to`` comparison, which is
    itself case-sensitive), mirroring
    ``block_derived_global_doctrine_write._normalize`` and
    ``block_oss_mirror_memo_delivery._casefold_path``. (Review:
    code-reviewer — case bypass in this guard, 2026-07-31.)
  - Never raises: any unexpected input shape or internal error is treated as
    ALLOW (fail-open), matching every one of the reference hook's own
    documented fail-open branches (unreadable payload, unknown tool_name, no
    target path, no home root resolvable, any path-normalization exception).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._case_fold_path import casefold_path

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 125

#: Deliberately unadvertised maintainer/test escape hatch.
OVERRIDE_ENV = "COORDINATOR_OVERRIDE_HOME_MEMO_GUARD"

#: Tools whose tool_input names a file this guard must vet.
_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")

#: tool_input keys that can carry the target path, in probe order.
#: NotebookEdit uses notebook_path; the rest use file_path.
_PATH_KEYS = ("file_path", "notebook_path", "path")

_CLAUDE_DIRNAME = ".claude"
_CROSS_REPO_DIRNAME = "cross-repo"


def _guarded_roots() -> "list[Path]":
    """``<home>/.claude/cross-repo`` roots to guard, union of USERPROFILE /
    HOME / Path.home(), plus CLAUDE_HOME treated as a direct ``.claude`` root.
    """
    homes: "list[str]" = []
    for env_key in ("USERPROFILE", "HOME"):
        val = os.environ.get(env_key, "")
        if val and val.strip():
            homes.append(val.strip())
    try:
        homes.append(str(Path.home()))
    except Exception:
        pass

    roots: "list[Path]" = []
    for home in homes:
        try:
            roots.append(
                Path(casefold_path(str(Path(home) / _CLAUDE_DIRNAME / _CROSS_REPO_DIRNAME)))
            )
        except Exception:
            continue

    # CLAUDE_HOME (when set) points AT the .claude root itself, not at $HOME.
    claude_home = os.environ.get("CLAUDE_HOME", "")
    if claude_home and claude_home.strip():
        try:
            roots.append(
                Path(casefold_path(str(Path(claude_home.strip()) / _CROSS_REPO_DIRNAME)))
            )
        except Exception:
            pass

    # De-dup, preserving order (USERPROFILE/HOME/Path.home() commonly coincide).
    seen: "set[Path]" = set()
    out: "list[Path]" = []
    for r in roots:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def _deny_reason(target: str) -> str:
    return (
        f"[home-dir memo guard] DENY {target}: not a memo receiver, goes "
        "unseen by /pickup. Use instead:\n"
        "  cross-repo-memo draft <slug> --to doe-claude-em "
        '--title "<one line>"\n'
        "  cross-repo-memo send <slug>"
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if os.environ.get(OVERRIDE_ENV, "") == "1":
        return None

    try:
        if payload.get("tool_name", "") not in _GUARDED_TOOLS:
            return None

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None

        target_raw = ""
        for key in _PATH_KEYS:
            val = tool_input.get(key, "")
            if isinstance(val, str) and val.strip():
                target_raw = val.strip()
                break
        if not target_raw:
            return None

        roots = _guarded_roots()
        if not roots:
            return None

        candidate = Path(casefold_path(target_raw))
        if contained_path(candidate, roots) is None:
            return None

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _deny_reason(target_raw),
            }
        }
    except Exception:
        # Fail-open on any unexpected error, matching every documented
        # fail-open branch in the reference hook.
        return None
