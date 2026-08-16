"""Tests for coordinator_core.install.shell_rc_guard.

Covers rc-path resolution (zsh/bash/default, plus Git-Bash/MSYS `bash.exe`
and `MSYSTEM` precedence), sentinel-guarded append,
check-only (no mutation), double-invocation idempotency (AC7), the
native-Windows no-op branch, and the `install.write_shell_rc_guard_block`
JSON-RPC handler's params/response contract.

Spec backlink: pln-coordinator-ops-buildout-from--903224
"""
from __future__ import annotations

import asyncio
import os
import re

import pytest

from coordinator_core.install import shell_rc_guard as mod
from coordinator_core.install.shell_rc_guard import (
    SENTINEL_BEGIN,
    SENTINEL_END,
    WRITE_SURFACE,
    _build_path_entry_body,
    _handler,
    _resolve_rc_path,
    _sentinel_markers,
    _shell_dquote_escape,
    _strip_block_text,
    applicable_rc_files,
    write_path_entry_guard_blocks,
    write_shell_rc_guard_block,
)
from coordinator_core.install.write_surface import (
    ABSENT_ON_LEGACY_INSTALLS,
    ShapedClause,
    validate,
)


class _OSNameProxy:
    """Delegates every attribute to the real ``os`` module except ``name``,
    which this module's own tests need to pin independently of the host
    interpreter's actual platform.

    Rebinding ``mod.os`` to this proxy (rather than flipping the real
    ``os.name`` global) is deliberate: `pathlib.Path` itself branches on
    `os.name` (via its own, unproxied import of the real `os` module) to
    decide `WindowsPath` vs `PosixPath` -- flipping the real global breaks
    every `Path` construction for the rest of the process, including the
    `tmp_path` fixture's own `WindowsPath` instances mid-test. Proxying only
    `shell_rc_guard.py`'s local `os` reference exercises its `os.name ==
    "nt"` branch without touching pathlib's platform selection.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


@pytest.fixture(autouse=True)
def _fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SHELL", raising=False)
    # A real Git-Bash/MSYS pytest invocation exports MSYSTEM; left in place it
    # forces every rc-path resolution in this module to `.bashrc` and the
    # zsh/default expectations below become host-dependent.
    monkeypatch.delenv("MSYSTEM", raising=False)
    monkeypatch.setattr(
        mod, "coordinator_claude_klabauter_root_with_class", lambda: ("/resolved/claude-klabauter/clone", "live-working-tree")
    )
    # This module's mutation-path tests exercise the cross-platform rc-file
    # write/append/idempotency/escaping logic itself, not the native-Windows
    # no-op branch (that branch has its own dedicated tests, which
    # `monkeypatch.setattr(mod.os, "name", "nt")` on top of this default --
    # see `_OSNameProxy` for why a real `os.name` flip isn't used here).
    monkeypatch.setattr(mod, "os", _OSNameProxy("posix"))
    # The suite-wide `COORDINATOR_DISABLE_MACHINE_MUTATION=1` belt-and-braces
    # opt-out (coordinator_core/conftest.py::_quarantine_real_home) would
    # otherwise refuse every real write this module's tests exercise inside
    # a fully sandboxed tmp_path HOME — safe to lift for this module's tests
    # by default; the guard-coverage tests below re-`setenv` it explicitly.
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return tmp_path


def test_resolve_rc_path_zsh(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _resolve_rc_path() == tmp_path / ".zshrc"


def test_resolve_rc_path_bash(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/bash")
    assert _resolve_rc_path() == tmp_path / ".bashrc"


def test_resolve_rc_path_default_unset(tmp_path):
    assert _resolve_rc_path() == tmp_path / ".zshrc"


def test_resolve_rc_path_git_bash_exe_suffix(monkeypatch, tmp_path):
    """Git-Bash/MSYS `$SHELL` carries a `.exe` suffix — the pre-fix
    exact-match against `"bash"` missed it and returned `.zshrc`, a file
    that shell never sources."""
    monkeypatch.setenv("SHELL", "C:/Program Files/Git/usr/bin/bash.exe")  # abs-path-ok: verbatim Git-for-Windows $SHELL value under test, not a path this repo resolves
    assert _resolve_rc_path() == tmp_path / ".bashrc"


def test_resolve_rc_path_msystem_wins_over_unset_shell(monkeypatch, tmp_path):
    """Under MSYS `$SHELL` is frequently unset entirely; `MSYSTEM` is the
    reliable signal and takes precedence."""
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    assert _resolve_rc_path() == tmp_path / ".bashrc"


def test_resolve_rc_path_msystem_wins_over_zsh_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _resolve_rc_path() == tmp_path / ".bashrc"


def test_resolve_rc_path_zsh_exe_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "C:/tools/zsh.exe")  # abs-path-ok: synthetic $SHELL value under test, not a path this repo resolves
    assert _resolve_rc_path() == tmp_path / ".zshrc"


def test_first_invocation_appends_block(tmp_path):
    result = write_shell_rc_guard_block(check_only=False)
    assert result["already_present"] is False
    assert result["modified"] is True
    rc_path = tmp_path / ".zshrc"
    assert str(rc_path) == result["rc_path"]
    text = rc_path.read_text(encoding="utf-8")
    assert SENTINEL_BEGIN in text
    assert SENTINEL_END in text
    assert 'export CLAUDE_KLABAUTER_CLONE="/resolved/claude-klabauter/clone"' in text


def test_double_invocation_is_safe_noop(tmp_path):
    first = write_shell_rc_guard_block(check_only=False)
    assert first["modified"] is True
    text_after_first = (tmp_path / ".zshrc").read_text(encoding="utf-8")

    second = write_shell_rc_guard_block(check_only=False)
    assert second["already_present"] is True
    assert second["modified"] is False
    text_after_second = (tmp_path / ".zshrc").read_text(encoding="utf-8")

    assert text_after_first == text_after_second
    assert text_after_second.count(SENTINEL_BEGIN) == 1


def test_check_only_never_mutates(tmp_path):
    result = write_shell_rc_guard_block(check_only=True)
    assert result["modified"] is False
    assert result["already_present"] is False
    rc_path = tmp_path / ".zshrc"
    assert not rc_path.exists()


def test_check_only_reports_already_present_without_mutating(tmp_path):
    write_shell_rc_guard_block(check_only=False)
    result = write_shell_rc_guard_block(check_only=True)
    assert result["already_present"] is True
    assert result["modified"] is False


def test_append_preserves_existing_content_and_adds_newline(tmp_path):
    rc_path = tmp_path / ".zshrc"
    rc_path.write_text("existing line, no trailing newline", encoding="utf-8")

    result = write_shell_rc_guard_block(check_only=False)
    assert result["modified"] is True
    text = rc_path.read_text(encoding="utf-8")
    assert text.startswith("existing line, no trailing newline\n")
    assert SENTINEL_BEGIN in text


def test_claude_klabauter_root_resolution_failure_skips_write_without_raising(tmp_path, monkeypatch):
    def _raise():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable")

    monkeypatch.setattr(mod, "coordinator_claude_klabauter_root_with_class", _raise)
    result = write_shell_rc_guard_block(check_only=False)
    assert result["modified"] is False
    assert result["already_present"] is False
    assert not (tmp_path / ".zshrc").exists()


def test_windows_branch_is_documented_noop(monkeypatch):
    monkeypatch.setattr(mod.os, "name", "nt")
    result = write_shell_rc_guard_block(check_only=False)
    assert result == {"rc_path": "", "already_present": False, "modified": False}


def test_handler_check_only_true(tmp_path):
    result = asyncio.run(_handler({"check_only": True}))
    assert result["modified"] is False
    assert not (tmp_path / ".zshrc").exists()


def test_handler_check_only_false_default(tmp_path):
    result = asyncio.run(_handler({}))
    assert result["modified"] is True
    assert (tmp_path / ".zshrc").exists()


def test_handler_registered_under_contractual_op_key():
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("install.write_shell_rc_guard_block") is not None


# --- Finding 2 (P1): COORDINATOR_DISABLE_MACHINE_MUTATION must cover every
# writer entry point, including the IPC handler, not just substrate.py's two
# call sites into write_path_entry_guard_blocks. ---


def test_write_shell_rc_guard_block_refuses_when_mutation_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    result = write_shell_rc_guard_block(check_only=False)
    assert result["modified"] is False
    assert not (tmp_path / ".zshrc").exists()


def test_write_shell_rc_guard_block_proceeds_when_mutation_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    result = write_shell_rc_guard_block(check_only=False)
    assert result["modified"] is True
    assert (tmp_path / ".zshrc").exists()


def test_handler_refuses_when_mutation_disabled(tmp_path, monkeypatch):
    """The IPC entry point (`install.write_shell_rc_guard_block`) — the third,
    independently-reachable call path — must inherit the same guard as
    substrate.py's call sites, since it's reachable outside the
    install-substrate flow entirely."""
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    result = asyncio.run(_handler({}))
    assert result["modified"] is False
    assert not (tmp_path / ".zshrc").exists()


def test_handler_check_only_true_still_works_with_mutation_disabled(tmp_path, monkeypatch):
    """`ops/install_health_run.py` calls this path with `check_only=True` as
    a read-only health probe — it must keep working even with the kill
    switch set, since check-only mutates nothing regardless."""
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    result = asyncio.run(_handler({"check_only": True}))
    assert result["modified"] is False
    assert not (tmp_path / ".zshrc").exists()


def test_write_path_entry_guard_blocks_check_only_works_with_mutation_disabled(tmp_path, monkeypatch):
    """`write_path_entry_guard_blocks(check_only=True)` — the read-only
    health-probe shape used by `ops/install_health_run.py` — must not be
    blocked by the kill switch, since it performs no write."""
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    result = write_path_entry_guard_blocks(
        path_entry="/some/bin/dir", sentinel_id="SETTINGS_HOME_BIN", position="append", check_only=True,
    )
    assert result["modified"] is False


def test_write_path_entry_guard_blocks_refuses_when_mutation_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")
    result = write_path_entry_guard_blocks(
        path_entry="/some/bin/dir", sentinel_id="SETTINGS_HOME_BIN", position="append", check_only=False,
    )
    assert result["modified"] is False
    assert not (tmp_path / ".zshrc").exists()


# --- Finding 1 (P1, data loss): unterminated BEGIN block must never
# silently truncate the rest of the file. ---


def test_strip_block_text_well_formed_still_strips():
    """Sanity/regression: the happy path (BEGIN...END both present) must
    still strip correctly — the malformed-input fix must not break this."""
    text = "before\nBEGIN\nbody line\nEND\nafter"
    assert _strip_block_text(text, "BEGIN", "END") == "before\nafter"


def test_strip_block_text_unterminated_begin_is_true_noop():
    """An orphaned BEGIN marker with no matching END anywhere after it
    (truncated write, crash mid-install, hand-edit that removed the END
    line) must leave `text` completely unchanged — never silently drop the
    operator's own content below the orphaned marker."""
    text = "operator line 1\nBEGIN\nstale body\noperator line 2\noperator line 3"
    assert _strip_block_text(text, "BEGIN", "END") == text


def test_strip_block_text_absent_begin_is_noop():
    text = "just some operator content\nno markers here at all"
    assert _strip_block_text(text, "BEGIN", "END") == text


def test_relocation_self_heal_with_unterminated_stale_block_does_not_eat_rc_file(tmp_path):
    """Integration-level regression for Finding 1's exact failure mode: a
    stale (content-mismatched) block whose END marker is missing must not
    cause `_write_block_to_file`'s relocation self-heal to drop the
    operator's other content when it rewrites the file. The malformed
    block is left in place and a fresh, correct block is appended after
    it — a working install, not data loss."""
    rc_path = tmp_path / ".zshrc"
    begin, end = mod._sentinel_markers("CLAUDE_KLABAUTER_CLONE")
    operator_line = "export SOME_OPERATOR_VAR=1"
    # Orphaned BEGIN with no END, followed by operator content.
    rc_path.write_text(f"{begin}\nstale body, never terminated\n{operator_line}\n", encoding="utf-8")

    result = mod._write_block_to_file(
        rc_path, begin, end, 'export CLAUDE_KLABAUTER_CLONE="/resolved/claude-klabauter/clone"', check_only=False, label="CLAUDE_KLABAUTER_CLONE",
    )

    assert result["modified"] is True
    text = rc_path.read_text(encoding="utf-8")
    # Operator's unrelated content survives.
    assert operator_line in text
    # The orphaned/malformed block is left in place rather than eaten.
    assert "stale body, never terminated" in text
    # A fresh, correct block is present so the install still works.
    assert 'export CLAUDE_KLABAUTER_CLONE="/resolved/claude-klabauter/clone"' in text
    assert text.count(begin) == 2  # orphaned + fresh


# --- Finding 2 (P1, shell injection): path_entry / claude_klabauter_clone must be
# escaped before interpolation into a double-quoted shell string. ---


def test_shell_dquote_escape_metacharacters():
    assert _shell_dquote_escape('$(rm -rf ~)') == '\\$(rm -rf ~)'
    assert _shell_dquote_escape('`whoami`') == '\\`whoami\\`'
    assert _shell_dquote_escape('has "quotes"') == 'has \\"quotes\\"'
    assert _shell_dquote_escape('back\\slash') == 'back\\\\slash'


def test_shell_dquote_escape_path_with_spaces_is_unmodified():
    assert _shell_dquote_escape("/Users/op erator/settings home/bin") == "/Users/op erator/settings home/bin"


def test_build_path_entry_body_escapes_command_substitution():
    body = _build_path_entry_body('/tmp/$(touch /tmp/pwned)', "append")
    # The `$` is backslash-escaped, so the shell never sees an unescaped
    # `$(` — it sees a literal `\$(`, not the start of command
    # substitution.
    assert "\\$(touch /tmp/pwned)" in body
    assert re.search(r"(?<!\\)\$\(", body) is None, f"unescaped command substitution survived in: {body!r}"


def test_build_path_entry_body_escapes_backticks():
    body = _build_path_entry_body("/tmp/`touch /tmp/pwned`", "prepend")
    assert "`touch /tmp/pwned`" not in body
    assert "\\`touch /tmp/pwned\\`" in body


def test_write_path_entry_guard_blocks_with_metacharacter_path_is_inert_when_sourced(tmp_path):
    """End-to-end: a malicious path_entry containing command substitution,
    written via the real multi-file production path, must render inert
    shell text — not an executable payload — in the rc file actually
    written to disk."""
    home = tmp_path / "home"
    home.mkdir()
    malicious = '/tmp/$(touch /tmp/PWNED)/`touch /tmp/PWNED2`'

    result = write_path_entry_guard_blocks(
        path_entry=malicious,
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
    )
    assert result["modified"] is True

    written = (home / ".zshrc").read_text(encoding="utf-8")
    assert "\\$(touch /tmp/PWNED)" in written
    assert "\\`touch /tmp/PWNED2\\`" in written
    assert re.search(r"(?<!\\)\$\(", written) is None, f"unescaped command substitution in: {written!r}"
    assert re.search(r"(?<!\\)`", written) is None, f"unescaped backtick in: {written!r}"


def test_write_path_entry_guard_blocks_with_spaces_path_roundtrips(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    spacey = "/Users/op erator/settings home/bin"

    result = write_path_entry_guard_blocks(
        path_entry=spacey,
        sentinel_id="SETTINGS_HOME_BIN",
        position="append",
        home=home,
    )
    assert result["modified"] is True

    written = (home / ".zshrc").read_text(encoding="utf-8")
    assert spacey in written


def test_claude_klabauter_clone_body_escapes_metacharacters(tmp_path, monkeypatch):
    monkeypatch.setattr(
        mod, "coordinator_claude_klabauter_root_with_class", lambda: ('/tmp/$(touch /tmp/PWNED)', "live-working-tree")
    )
    result = write_shell_rc_guard_block(check_only=False)
    assert result["modified"] is True
    text = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert "\\$(touch /tmp/PWNED)" in text


def test_write_surface_identity():
    assert WRITE_SURFACE.writer_id == "shell-rc-guard"
    assert WRITE_SURFACE.source_module == "coordinator_core.install.shell_rc_guard"


def test_write_surface_is_clean():
    assert validate(WRITE_SURFACE) == ()


def test_write_surface_has_one_shaped_rc_block_clause():
    assert len(WRITE_SURFACE.clauses) == 1
    clause = WRITE_SURFACE.clauses[0]
    assert isinstance(clause, ShapedClause)
    assert clause.entry_template.kind == "rc-block"


def test_write_surface_marker_template_matches_sentinel_markers():
    """The declared begin/end marker template must be recomputed from
    `_sentinel_markers` at test time, not hand-copied — a future edit to
    `_sentinel_markers`'s format alone (with no edit to WRITE_SURFACE) must
    make this test go red, per the module's own convention."""
    expected_begin, expected_end = _sentinel_markers("<sentinel_id>")
    entry = WRITE_SURFACE.clauses[0].entry_template
    assert entry.begin_marker == expected_begin
    assert entry.end_marker == expected_end


def test_write_surface_end_marker_is_not_absent_on_legacy():
    """This writer always renders BEGIN and END together in one write
    (`_desired_block`) — it never produces a begin-only block itself, so the
    tri-state end_marker must be a literal string, never the
    ABSENT_ON_LEGACY_INSTALLS sentinel."""
    entry = WRITE_SURFACE.clauses[0].entry_template
    assert entry.end_marker != ABSENT_ON_LEGACY_INSTALLS
    assert entry.end_marker is not None


def test_write_surface_target_file_set_is_computed_not_hardcoded():
    """The declaration's `discovered_by` names applicable_rc_files as the
    file-set mechanism rather than enumerating files — cross-check that the
    live function still returns the five-file set this declaration's prose
    describes, so a change to applicable_rc_files's tuple is caught here
    even though WRITE_SURFACE itself carries no hardcoded file list."""
    names = {p.name for p in applicable_rc_files(home=None)}
    assert names == {".zprofile", ".bash_profile", ".profile", ".zshrc", ".bashrc"}


# ---------------------------------------------------------------------------
# resolution-journal wiring (C7 of docs/research/2026-08-06-install-receipt-
# persistence-design.md) — this writer's sole ShapedClause (index 0).
# ---------------------------------------------------------------------------


@pytest.fixture
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as journal_mod

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(journal_mod.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    return journal_mod


def _resolved(journal_mod):
    journal = journal_mod.read_journal()
    return journal.get("shell-rc-guard", {}).get(mod._RC_BLOCK_CLAUSE_INDEX)


def test_journal_records_written_rc_block(tmp_path, _journal_env):
    write_shell_rc_guard_block(check_only=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    paths = [e.path for e in resolution.entries]
    assert str(tmp_path / ".zshrc") in paths
    entry = resolution.entries[0]
    assert entry.kind == "rc-block"
    assert entry.begin_marker == SENTINEL_BEGIN
    assert entry.end_marker == SENTINEL_END


def test_journal_records_already_present_block_on_second_run(tmp_path, _journal_env):
    write_shell_rc_guard_block(check_only=False)
    write_shell_rc_guard_block(check_only=False)

    resolution = _resolved(_journal_env)
    paths = [e.path for e in resolution.entries]
    assert str(tmp_path / ".zshrc") in paths


def test_journal_not_written_under_check_only(tmp_path, _journal_env):
    write_shell_rc_guard_block(check_only=True)

    assert _journal_env.read_journal() == {}


def test_journal_unreported_on_windows_noop(monkeypatch, _journal_env):
    # The native-Windows platform branch is deliberately left UNREPORTED
    # (no journal row at all), not journaled empty — see the call site's
    # own comment.
    monkeypatch.setattr(mod.os, "name", "nt")
    write_shell_rc_guard_block(check_only=False)

    assert _resolved(_journal_env) is None


def test_journal_records_empty_tuple_on_claude_klabauter_root_resolution_failure(monkeypatch, _journal_env):
    def _raise():
        raise RuntimeError("CLAUDE_KLABAUTER_ROOT unresolvable")

    monkeypatch.setattr(mod, "coordinator_claude_klabauter_root_with_class", _raise)
    write_shell_rc_guard_block(check_only=False)

    resolution = _resolved(_journal_env)
    assert resolution is not None
    assert resolution.entries == ()


def test_journal_omits_entry_for_refused_write(tmp_path, _journal_env, monkeypatch):
    # COORDINATOR_DISABLE_MACHINE_MUTATION refuses BOTH the rc-block write
    # AND the journal append itself (resolution_journal honours the same
    # kill switch) — so this clause is left UNREPORTED, not journaled
    # empty, for this run.
    monkeypatch.setenv("COORDINATOR_DISABLE_MACHINE_MUTATION", "1")

    write_shell_rc_guard_block(check_only=False)

    assert _resolved(_journal_env) is None
    assert not (tmp_path / ".zshrc").exists()
