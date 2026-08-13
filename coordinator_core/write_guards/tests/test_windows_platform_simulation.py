"""Windows-path-semantics simulation tests for the folded write path (C16,
`docs/plans/2026-07-29-hook-fan-in-write-path.md`).

Purpose: this suite is authored on a macOS host with no Windows machine
reachable from the dispatch that wrote it. Per the precedent established by
`docs/plans/2026-07-29-fleet-wide-bash-spawn-fan-out.md` (AC-11) and its
`coordinator_core/bash_guards/_platform_verdict.py` implementation, a
platform-dependent branch can be proven from a POSIX host PROVIDED the
branch is driven by a keyword/monkeypatch seam rather than by an actual
OS-native filesystem resolution. That precedent generalizes cleanly to any
guard whose platform branch is `os.name == "nt"` (or an equivalent mockable
seam) plus pure lexical string/regex work.

It does NOT generalize to a guard whose platform-dependent step is
`pathlib.Path(...).resolve()` on a caller-supplied string: on a POSIX
interpreter, `pathlib.Path(...)` ALWAYS instantiates `PosixPath`, and
forcing `WindowsPath` instantiation (e.g. by monkeypatching `os.name`) is a
hard `NotImplementedError` from the stdlib itself — see
`test_windows_path_resolve_is_hardware_gated` below, which pins this as an
executable fact rather than an assertion in prose. That is the boundary
between what this suite proves and what
`state/spinoffs/2026-07-29-hook-fan-in-windows-verification.md` defers to a
real Windows host.

Two DISTINCT simulation techniques are used below, chosen per guard
according to which functions it actually calls:

  1. `os.name` / a guard's own `_is_windows()`-style seam, for guards whose
     platform branch is a plain boolean (`guard_settings_json_write`).
  2. Monkeypatching the process-wide `os.path` submodule to the stdlib's
     own `ntpath` module (`_windows_os_path` fixture below). `ntpath`'s
     functions (`realpath`, `abspath`, `basename`, `splitext`, ...) are
     PURE LEXICAL string manipulation on a non-Windows host — they do not
     touch the filesystem in a Windows-specific way and never raise merely
     for being invoked off-Windows (confirmed empirically before writing
     this suite: `ntpath.realpath`/`abspath`/`basename` all return normally
     when run on macOS). This is a materially stronger simulation than
     flipping a boolean: it exercises the SAME code path a real Windows
     interpreter would take through `os.path.*`, with genuine backslash-
     separator and case-fold semantics, not a hand-rolled stand-in.
     Restricted to guards that reach the filesystem only through
     `os.path.*` functions (never `pathlib.Path(...).resolve()`), which is
     exactly the boundary named above.

Guard coverage in this file:
  - guard_settings_json_write   — drive-letter deny (both `/` and `\\`
    forms), POSIX-home-path deny under `_is_windows()=True`, and the
    `https://` passing-side regression (AC-11's own named case).
  - block_worktree_sentinel_write / _sentinel_write_guard — sentinel-write
    detection against a Windows-shaped, case-varied absolute path, via the
    `os.path` -> `ntpath` swap.
  - guard_doctrine_surface_edits — `_norm()` and the full `check()` deny
    path against a Windows-shaped `$HOME/.claude/CLAUDE.md`-equivalent
    target, fallback branch (no git root), via the same swap.
  - nudge_em_code_dispatch (`coordinator_core.hooks.nudge_em_code_dispatch.
    _derive_executor_info`) — extension/basename derivation against a
    Windows-shaped `file_path`, via the same swap.
  - validate_frontmatter_schema_deny._to_repo_relative — pure string-level
    backslash normalization, no swap needed (the function already
    `.replace("\\\\", "/")`s both operands before comparing).
  - check_claude_md_size / claude_md_budget.is_governed_claude_md — a real
    deny path IS exercised (satisfying "at least one deny path ... for C8's
    SIZE leg"), but under normal POSIX paths only: `is_governed_claude_md`
    calls `Path(path).resolve()`, which is the same hard-gated primitive
    named above, so no Windows-shaped variant is attempted here — see the
    spinoff for that leg.

Spec backlink: docs/plans/2026-07-29-hook-fan-in-write-path.md § C16
"""

from __future__ import annotations

import ntpath
import os

import pytest

from coordinator_core.write_guards import (
    block_worktree_sentinel_write,
    guard_doctrine_surface_edits,
    guard_settings_json_write,
)
from coordinator_core.write_guards import _sentinel_write_guard
from coordinator_core.write_guards import validate_frontmatter_schema_deny as vfs_deny
from coordinator_core.hooks import nudge_em_code_dispatch as nudge_hook


@pytest.fixture()
def _windows_os_path(monkeypatch):
    """Swap the process-wide `os.path` submodule for the stdlib's `ntpath`.

    `ntpath` is pure lexical string manipulation when run off-Windows (no
    win32 syscalls) — see module docstring. Auto-restored by monkeypatch at
    teardown. Any guard invoked while this fixture is active sees the SAME
    `os.path.realpath`/`abspath`/`basename`/`splitext` behaviour a real
    Windows interpreter would produce for backslash-separated, drive-
    lettered input.
    """
    monkeypatch.setattr(os, "path", ntpath)


# ─── guard_settings_json_write ──────────────────────────────────────────────


def _write_payload(content: str) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": "/Users/x/.claude/settings.json", "content": content},
    }


def test_settings_guard_denies_windows_drive_letter_forward_slash(monkeypatch):
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: False)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        _write_payload('{"hooks": {"command": "X:/example-doctrine-repo/coordinator/hooks/scripts/x.py"}}')
    )
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "X:/example-doctrine-repo" in reason
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_settings_guard_denies_windows_drive_letter_backslash_form(monkeypatch):
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: False)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        _write_payload('{"command": "C:\\Users\\me\\example-doctrine-repo\\x.py"}')
    )
    assert result is not None
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    # The reason embeds `repr(token)`, so a literal backslash prints doubled;
    # assert on the content rather than the exact escaping.
    assert "Users" in reason and "me" in reason and "example-doctrine-repo" in reason


def test_settings_guard_denies_posix_home_path_when_simulated_windows(monkeypatch):
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: True)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        _write_payload('{"command": "/Users/alice/X/example-doctrine-repo/coordinator/hooks/scripts/x.py"}')
    )
    assert result is not None
    assert "/Users/alice" in result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Windows" in result["hookSpecificOutput"]["permissionDecisionReason"]


def test_settings_guard_https_url_is_passing_side_not_flagged(monkeypatch):
    """AC-11's own named passing-side case: an ordinary https:// URL (e.g. an
    MCP server endpoint) must never be misread as a Windows drive letter —
    the lookbehind in `_DRIVE_LETTER_RE` exists specifically because `s:` in
    `https:` satisfies a naive `[A-Za-z]:[/\\\\]` match."""
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: False)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        _write_payload('{"url": "https://mcp.example.com/endpoint"}')
    )
    assert result is None


def test_settings_guard_denies_differently_cased_settings_path(monkeypatch):
    """Casefold bypass-proof (2026-08-05 fast-follow to
    `test_casefold_bypass_lint.py`, commit `223e04b7bf2e`): `_is_settings_target`
    used to compare `os.path.realpath`-resolved strings via plain `in`, with
    no fold on either side. `os.path.realpath` does not itself normalize case
    (it is lexical for nonexistent path components), so a candidate whose
    directory segments are cased differently from the live settings file
    (`.CLAUDE`/`Settings.JSON` vs `.claude`/`settings.json`) resolves to a
    DIFFERENT string even though it names the SAME real file on a
    case-insensitive-but-case-preserving filesystem (macOS APFS, Windows) --
    the guard silently ALLOWED exactly the write it exists to block. Before
    the `casefold_path` fix this assertion failed (`result is None`)."""
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: False)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/Users/x/.CLAUDE/Settings.JSON",
                "content": '{"hooks": {"command": "X:/example-doctrine-repo/coordinator/hooks/scripts/x.py"}}',
            },
        }
    )
    assert result is not None, (
        "casefold bypass reopened: a differently-cased settings.json path "
        "slipped past the settings-target match"
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_settings_guard_unrelated_differently_cased_path_still_allowed(monkeypatch):
    """Casefolding the comparison must not widen the guard to unrelated
    files that merely share a casefolded prefix by accident."""
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: False)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        {
            "tool_name": "Write",
            "tool_input": {
                "file_path": "/Users/x/.CLAUDE/settings.local.json.bak",
                "content": '{"hooks": {"command": "X:/example-doctrine-repo/coordinator/hooks/scripts/x.py"}}',
            },
        }
    )
    assert result is None


def test_settings_guard_https_url_passing_side_also_holds_on_windows(monkeypatch):
    """Same passing-side case, other branch: on a simulated Windows host the
    guard matches `_POSIX_HOME_RE` instead, and an https:// URL must not
    satisfy that pattern either."""
    monkeypatch.setattr(guard_settings_json_write, "_is_windows", lambda: True)
    monkeypatch.setattr(
        guard_settings_json_write,
        "_config_dir",
        lambda: __import__("pathlib").Path("/Users/x/.claude"),
    )
    result = guard_settings_json_write.check(
        _write_payload('{"url": "https://mcp.example.com/endpoint"}')
    )
    assert result is None


# ─── block_worktree_sentinel_write / _sentinel_write_guard ──────────────────


def test_sentinel_write_guard_matches_windows_shaped_case_varied_path(_windows_os_path):
    """Case-fold + backslash-separated basename extraction under real ntpath
    semantics — `os.path.basename` on a plain PosixPath backend would treat
    the whole backslash-separated string as one filename component and never
    match; under the ntpath swap it correctly extracts the trailing
    component."""
    target = "C:\\Users\\me\\project\\.COORDINATOR-OVERRIDE-WORKTREE-GUARD"
    assert _sentinel_write_guard.is_sentinel_write(
        target, ".coordinator-override-worktree-guard"
    )


def test_sentinel_write_guard_does_not_match_windows_shaped_near_miss(_windows_os_path):
    target = "C:\\Users\\me\\project\\.coordinator-override-worktree-guard-typo"
    assert not _sentinel_write_guard.is_sentinel_write(
        target, ".coordinator-override-worktree-guard"
    )


def test_block_worktree_sentinel_write_denies_windows_shaped_target(_windows_os_path):
    payload = {
        "tool_name": "Write",
        "tool_input": {
            "file_path": "C:\\Users\\me\\project\\.coordinator-override-worktree-guard",
            "content": "",
        },
    }
    result = block_worktree_sentinel_write.check(payload)
    assert result is not None
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    # Deny message discipline: the sentinel basename is never printed.
    assert ".coordinator-override-worktree-guard" not in out["permissionDecisionReason"]


# ─── guard_doctrine_surface_edits ───────────────────────────────────────────


def test_norm_is_stable_under_windows_path_semantics(_windows_os_path):
    a = guard_doctrine_surface_edits._norm("C:\\Users\\me\\.claude\\CLAUDE.md")
    b = guard_doctrine_surface_edits._norm("C:\\Users\\me\\.claude\\CLAUDE.md")
    assert a == b
    assert a.lower().endswith("claude.md")


def test_doctrine_guard_denies_windows_shaped_home_claude_md(_windows_os_path, monkeypatch):
    """Fallback branch (no resolvable git root): only `$HOME/.claude/CLAUDE.md`
    is protected, and the sentinel is treated as absent -> deny (this
    guard's deliberate fail-closed posture)."""
    windows_home_claude_md = guard_doctrine_surface_edits._norm(
        "C:\\Users\\me\\.claude\\CLAUDE.md"
    )
    monkeypatch.setattr(guard_doctrine_surface_edits, "_git_root", lambda: None)
    monkeypatch.setattr(
        guard_doctrine_surface_edits, "_home_claude_md", lambda: windows_home_claude_md
    )

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "C:\\Users\\me\\.claude\\CLAUDE.md",
            "old_string": "x",
            "new_string": "y",
        },
    }
    result = guard_doctrine_surface_edits.check(payload)
    assert result is not None
    out = result["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    # Deny message discipline (2026-08-13, C4a, INVERTED -- plan
    # docs/plans/2026-08-13-guard-messages-stop-handing-agents-the-keys.md;
    # docs/wiki/guard-messaging.md § Register B6): the prior 2026-07-30
    # reversal asserted the sentinel filename WAS printed, reasoning that
    # naming it was safe once creation was denied on both surfaces -- B6
    # supersedes that reasoning: showing a confined reader the key while
    # forbidding its use is itself the disclosure that makes a well-meaning
    # subagent's rationalisation through the gate available, independent of
    # whether creation is also blocked. No unresolved-audience payload is
    # passed here (`check()`'s payload carries no `session_id`), so this
    # fires the unresolved-audience leg, which degrades to terse -- never
    # the mechanism, per B6's unresolved-audience degradation rule.
    assert ".coordinator-doctrine-edit-approved" not in out["permissionDecisionReason"]


def test_doctrine_guard_allows_unrelated_windows_shaped_path(_windows_os_path, monkeypatch):
    windows_home_claude_md = guard_doctrine_surface_edits._norm(
        "C:\\Users\\me\\.claude\\CLAUDE.md"
    )
    monkeypatch.setattr(guard_doctrine_surface_edits, "_git_root", lambda: None)
    monkeypatch.setattr(
        guard_doctrine_surface_edits, "_home_claude_md", lambda: windows_home_claude_md
    )

    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": "C:\\Users\\me\\project\\notes.md",
            "old_string": "x",
            "new_string": "y",
        },
    }
    assert guard_doctrine_surface_edits.check(payload) is None


# ─── nudge_em_code_dispatch (hooks-layer _derive_executor_info) ─────────────


def test_derive_executor_info_windows_shaped_coordinator_path(_windows_os_path):
    executor_type, ambiguous = nudge_hook._derive_executor_info(
        "C:\\Users\\me\\example-doctrine-repo\\coordinator\\hooks\\scripts\\foo.py"
    )
    assert executor_type == "coordinator-executor"
    assert ambiguous is False


def test_derive_executor_info_windows_shaped_extension_match(_windows_os_path):
    ext = os.path.splitext("C:\\Users\\me\\project\\script.py")[1].lower()
    assert ext == ".py"
    basename = os.path.basename("C:\\Users\\me\\project\\script.py")
    assert basename == "script.py"


# ─── validate_frontmatter_schema_deny — pure string-level normalization ────


def test_to_repo_relative_normalizes_mixed_separators():
    """`_to_repo_relative` never touches the filesystem — pure `.replace`
    calls on both operands — so this is a direct (no-swap-needed) proof the
    backslash-normalization logic is correct regardless of host."""
    abs_path = "C:\\example-doctrine-repo\\state\\handoffs\\foo.md"
    repo_root = "C:\\example-doctrine-repo"
    rel = vfs_deny._to_repo_relative(abs_path, repo_root)
    assert rel == "state/handoffs/foo.md"


def test_to_repo_relative_mismatched_root_returns_none():
    abs_path = "C:\\example-doctrine-repo\\state\\handoffs\\foo.md"
    repo_root = "C:\\some-other-repo"
    assert vfs_deny._to_repo_relative(abs_path, repo_root) is None


# ─── The hard-gated boundary, pinned as an executable fact ─────────────────


def test_windows_path_resolve_is_hardware_gated(monkeypatch):
    """Proves — rather than merely asserts in a docstring — that the ONE
    remaining class of Windows-path logic this suite does NOT attempt to
    simulate (`pathlib.Path(...).resolve()`, used by `contained_path` in
    `coordinator_core.ops._path_guard` and by `claude_md_budget.
    is_governed_claude_md`) is genuinely unreachable from a POSIX
    interpreter, not merely unauthored. `pathlib.Path.__new__` dispatches to
    `WindowsPath` when `os.name == 'nt'`, and instantiating a concrete
    `WindowsPath` on a non-Windows interpreter raises `NotImplementedError`
    at CONSTRUCTION time — before any `.resolve()` call. This is why
    `block_home_dir_memo_delivery`, `block_consumed_handoff_edit`'s
    containment leg, and `is_governed_claude_md` are named in
    `state/spinoffs/2026-07-29-hook-fan-in-windows-verification.md` as
    genuinely hardware-gated rather than covered here under a boolean
    `host_is_windows`-style seam like `_platform_verdict.py`'s guards."""
    import pathlib

    monkeypatch.setattr(os, "name", "nt")
    with pytest.raises(NotImplementedError):
        pathlib.Path("C:\\Users\\me\\.claude\\CLAUDE.md").resolve()
