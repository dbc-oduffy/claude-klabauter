"""coordinator_core.write_guards.guard_settings_json_write — hard-deny guard.

Ported from DoE-claude `coordinator/hooks/scripts/guard-settings-json-write.py`
(faithful port, per write_guards/INTERFACE.md).

PreToolUse hook (matcher: Write|Edit|MultiEdit|NotebookEdit): denies a
mid-session file-mutation that would poison the live `~/.claude/settings.json`
(or `settings.local.json`) with a foreign-platform hook path, or that would
introduce a `hooks` block while the kill-switch marker is present.

Why this exists
----------------
On 2026-07-28 `~/.claude/settings.json` was overwritten three times with hook
commands pointing at `X:/DoE-claude/...` -- a Windows drive path on a macOS
host. A hook whose `command` names a path that does not exist on the running
machine fails CLOSED (PreToolUse denies by default when the hook process
itself cannot run), so once the corrupted `hooks` block landed, EVERY
subsequent tool call -- Bash, Write, Edit, all of them -- died simultaneously:
a total tool lockout with no in-session repair path, because every tool that
could have fixed the file was itself gated by the now-broken hooks.

An existing SessionStart detector, `guard-foreign-platform-paths.py`, catches
this shape at session BOOT (its own docstring covers that leg and the
residual gaps it still has). It structurally cannot catch it MID-session --
by the time SessionStart next runs, the corrupted write has already
happened. This hook is that missing mid-session seam: it inspects the
CONTENT being written before it lands, not the file after the fact.

What this hook is not: it does not parse or validate `settings.json` as
JSON, does not reconstruct the resulting file for Edit/MultiEdit (only the
`new_string`/edit-list content proposed for introduction is visible to a
PreToolUse hook, and reconstructing the full post-edit file is unnecessary
for the two narrow predicates below), and does not gate ordinary settings.json
edits that keep paths native -- only the two failure shapes named below.

Two independent deny predicates
--------------------------------
Fires ONLY when the resolved write target is the live settings file
(`~/.claude/settings.json` or `~/.claude/settings.local.json`, resolved via
symlinks -- never a raw string/substring match, see `_is_settings_target`).
Everything else is allowed silently, including a path that merely CONTAINS
the substring "settings.json" but resolves elsewhere.

  (a) Foreign-platform path: the proposed content contains a Windows
      drive-letter absolute path (`X:/`, `X:\\`, any `[A-Za-z]:[/\\]`) while
      this hook is running on POSIX, or a POSIX `/Users/` or `/home/`
      absolute path while running on Windows. This is exactly the 2026-07-28
      incident shape -- a hook `command` string naming a path the running
      machine cannot resolve.
  (b) Hooks-block-while-disabled: the proposed content introduces a `hooks`
      key at all while the kill-switch marker
      `~/.claude/.coordinator-hooks-disabled` exists on disk. That marker
      means hooks are being deliberately suppressed (e.g. mid-recovery from
      exactly this class of corruption) -- a write that re-introduces a
      `hooks` block during that window is either racing the recovery or
      reintroducing the very thing being repaired.

Deny message discipline (design-as-offers)
--------------------------------------------
Every deny names the exact offending token/path it matched, states the
mechanism in one line (dead hook command -> PreToolUse fails closed -> total
tool lockout), and offers the real fix: hook commands must be
machine-portable -- `${CLAUDE_PLUGIN_ROOT}` in this plugin's own
`coordinator/hooks/hooks.json`, or the settings-home self-resolving form
(`coordinator_core.install._shared.PORTABLE_HOOK_ROOT_EXPR`, claude-klabauter)
for generated `settings.json` entries.

REMOVED (2026-08-13, C4a; plan
docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md;
docs/wiki/guard-messaging.md § Trichotomy) -- both deny renders used to
close with "Wedged? `!` bypasses to hand-repair," naming the input-box `!`
prefix's PreToolUse bypass. That clause operates on the GATE, not the
GOAL -- a textbook BYPASS -- and B6 bans naming a BYPASS to ANY audience,
not just a subagent one (`docs/wiki/guard-messaging.md` § Register: "an EM
message may carry the wiki pointer and nothing else -- no key, no path, no
command"; a bare `!`-bypass instruction is a command). Both deny functions
now take `payload` (the PreToolUse envelope) and thread it from `check()`,
mirroring `guard_doctrine_surface_edits._deny_reason` -- kept for shape
parity with that sibling site and any future audience-conditional content,
even though today's render is audience-invariant (the bypass clause is
gone for every reader, not merely gated by resolved audience).

Negative-spec
-------------
  - **No override env var, by design -- do not add one.** This guard is
    override-free because the override surface would itself be
    settings.json's own `env` block, i.e. exactly the surface this guard
    exists to protect. Every other ported guard's escape hatch is preserved
    verbatim; this one has none and stays that way.
  - Does not parse settings.json as JSON, and does not reconstruct the
    full post-edit file for Edit/MultiEdit -- only the proposed new content
    is inspected, matching the reference hook exactly.
  - Fail-open on every unresolvable shape AND on any internal exception
    (unreadable payload, tool_name not guarded, no resolvable target,
    target not the live settings file, or any exception while extracting
    content / evaluating the two predicates) -- this guard must never
    itself wedge the harness. This fail-open-on-error posture is
    deliberately ported as-is even though `CLASS` is hard-deny: the
    reference hook itself never emits a deny from its own error path.

Depends on `_sentinel_write_guard.extract_target_path` (already ported for
this tree, C3) for path extraction -- imported directly rather than
re-derived, exactly as the reference hook imports its own copy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._sentinel_write_guard import extract_target_path

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 131

_HOOKS_DISABLED_MARKER = ".coordinator-hooks-disabled"

# Windows drive-letter absolute path, e.g. X:/foo, X:\foo.
#
# The lookbehind is load-bearing, not defensive tidying. Without it the drive
# letter can be satisfied by the last letter of a URL scheme -- `https://`
# supplies `s` + `:` + `/` -- so every settings.json write introducing an
# ordinary https URL (an MCP server endpoint, say) matched as a foreign
# Windows path and was DENIED. This guard advertises no override by design,
# because the override surface would itself be settings.json's `env` block, so
# a false positive here wedges the session with no in-harness way out: exactly
# the unrecoverable shape the 2026-07-28 incident response exists to remove.
# A real drive letter is never preceded by a word character; `https:` always is.
_DRIVE_LETTER_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[/\\]")

# POSIX absolute paths under /Users/ or /home/.
_POSIX_HOME_RE = re.compile(r"(?:^|[\s\"'(])(/Users/|/home/)")

_HOOKS_BLOCK_RE = re.compile(r'"hooks"\s*:')


def _is_windows() -> bool:
    """Mockable seam -- tests monkeypatch this rather than os.name directly."""
    return os.name == "nt"


def _config_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return Path(raw)


def _settings_targets(config_dir: Path) -> "list[str]":
    """Resolved (symlink-following, non-strict) absolute paths for the two
    live settings surfaces this guard protects."""
    targets = []
    for name in ("settings.json", "settings.local.json"):
        try:
            targets.append(os.path.realpath(os.path.abspath(str(config_dir / name))))
        except Exception:
            continue
    return targets


def _is_settings_target(target_path: str, config_dir: Path) -> bool:
    if not target_path:
        return False
    try:
        resolved = os.path.realpath(os.path.abspath(target_path))
    except Exception:
        return False
    # Comparison-only fold: this function only decides allow/deny — it never
    # performs I/O on `resolved` itself (the actual tool call proceeds with
    # the caller's original-case `target_path`). Both sides must be folded
    # together, matching `_settings_targets`' own fold below.
    folded = casefold_path(resolved)
    return folded in {casefold_path(t) for t in _settings_targets(config_dir)}


def _extract_new_content(tool_name: str, tool_input: dict) -> str:
    """The proposed content being INTRODUCED -- never the reconstructed full
    file. Write -> `content`; Edit -> `new_string`; MultiEdit -> every edit's
    `new_string`, concatenated; NotebookEdit -> `new_source` (falls back to
    `new_string` if a future schema renames the field)."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string", "") or ""
    if tool_name == "MultiEdit":
        chunks = []
        for edit in tool_input.get("edits", []) or []:
            if isinstance(edit, dict):
                chunks.append(edit.get("new_string", "") or "")
        return "\n".join(chunks)
    if tool_name == "NotebookEdit":
        return tool_input.get("new_source", "") or tool_input.get("new_string", "") or ""
    return ""


def _foreign_path_match(content: str) -> "str | None":
    """Returns the exact offending matched token, or None."""
    if _is_windows():
        match = _POSIX_HOME_RE.search(content)
        if match:
            # Recover the full path-looking token starting at the match.
            start = match.start(1)
            tail = re.match(r"[^\s\"'()]*", content[start:])
            return tail.group(0) if tail else match.group(1)
        return None
    match = _DRIVE_LETTER_RE.search(content)
    if match:
        start = match.start()
        tail = re.match(r"[^\s\"'()]*", content[start:])
        return tail.group(0) if tail else match.group(0)
    return None


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _foreign_path_deny_reason(
    token: str, payload: Optional[Dict[str, Any]] = None
) -> str:
    """See module docstring "Deny message discipline" -- `payload` is
    accepted and threaded from `check()` for shape parity with
    `guard_doctrine_surface_edits._deny_reason`; today's render is
    audience-invariant (no BYPASS clause survives for any reader)."""
    del payload  # unused -- see module docstring's REMOVED note
    platform_name = "Windows" if _is_windows() else "POSIX"
    return (
        f"[settings.json guard] BLOCKED: {token!r} unresolvable on "
        f"{platform_name} -- a dead hook path locks out every tool.\n"
        "Instead: `${CLAUDE_PLUGIN_ROOT}` or PORTABLE_HOOK_ROOT_EXPR."
    )


def _hooks_disabled_deny_reason(payload: Optional[Dict[str, Any]] = None) -> str:
    """See module docstring "Deny message discipline" -- `payload` is
    accepted and threaded from `check()` for shape parity with
    `guard_doctrine_surface_edits._deny_reason`; today's render is
    audience-invariant (no BYPASS clause survives for any reader)."""
    del payload  # unused -- see module docstring's REMOVED note
    return (
        "[settings.json guard] BLOCKED: `hooks` block while "
        ".coordinator-hooks-disabled is present -- risks racing recovery or "
        "reintroducing the corruption being repaired.\n"
        "Instead: delete the marker first, then write the hooks block "
        "separately."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return None

        target = extract_target_path(tool_input)
        if not target:
            return None

        config_dir = _config_dir()
        if not _is_settings_target(target, config_dir):
            return None

        content = _extract_new_content(payload.get("tool_name", ""), tool_input)

        token = _foreign_path_match(content)
        if token:
            return _deny(_foreign_path_deny_reason(token, payload))

        if _HOOKS_BLOCK_RE.search(content):
            marker = config_dir / _HOOKS_DISABLED_MARKER
            if marker.is_file():
                return _deny(_hooks_disabled_deny_reason(payload))

        return None
    except Exception:
        # Fail-open on any unexpected error -- matches the reference hook's
        # own fail-open-on-error discipline (it never emits a deny from its
        # own error path, despite CLASS being hard-deny for its two named
        # predicates).
        return None
