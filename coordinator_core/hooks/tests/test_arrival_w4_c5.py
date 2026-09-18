"""coordinator_core/hooks/tests/test_arrival_w4_c5.py — the W4-C5 arrival
gate for "Doctrine-surface guards and their helpers".

Subject: the ten modules `docs/plans/2026-09-18-doe-holds-no-scripts.md`'s
W4-C5 row lands under `coordinator_core/hooks/` --
`claude_md_ledger.py`, `doctrine_changelog_prose.py`,
`doctrine_surface_netting.py`, `guard_doctrine_surface_bash_write.py`,
`guard_doctrine_surface_ratio.py`, `guard_doctrine_surface_ratio_precommit.py`,
`guard_doctrine_changelog_prose.py`, `check_claude_md_size.py`,
`derive_global_doctrine_live_copy.py`, `derive_setup_copies.py`.

Three of these (`guard_doctrine_surface_ratio_precommit.py` and its W4-C7
sibling shape, plus every native git pre-commit hook) carry NO `register_op`
-- see each module's own `# guard-not-a-hook-entrypoint` marker. The other
five (`guard_doctrine_surface_bash_write`, `guard_doctrine_surface_ratio`,
`guard_doctrine_changelog_prose`, `check_claude_md_size`) register a
`hooks.<name>` op; `claude_md_ledger`/`doctrine_changelog_prose`/
`doctrine_surface_netting` are plain support libraries a sibling guard
imports; `derive_global_doctrine_live_copy`/`derive_setup_copies` register
their own PostToolUse-shaped ops.

This file focuses on the row's OWN five writes not already covered by an
earlier arrival gate (`claude_md_ledger`, `doctrine_changelog_prose`,
`doctrine_surface_netting`, `guard_doctrine_surface_ratio`,
`guard_doctrine_changelog_prose` each shipped with the working tree already
carrying substantial coverage from a prior attempt at this chunk) --
`check_claude_md_size`, `derive_global_doctrine_live_copy`,
`derive_setup_copies`, `guard_doctrine_surface_ratio_precommit`, and
`guard_doctrine_surface_bash_write` (a byte-faithful port of a 1748-line
DoE-claude source; this file spot-checks the security-relevant carve-outs
named in its own module docstring rather than reproducing DoE's full
regression suite).
"""

from __future__ import annotations

import sys
import time

import pytest

from coordinator_core.hooks import check_claude_md_size as ccms
from coordinator_core.hooks import derive_global_doctrine_live_copy as dgdlc
from coordinator_core.hooks import derive_setup_copies as dsc
from coordinator_core.hooks import guard_doctrine_surface_bash_write as gdsbw


# ---------------------------------------------------------------------------
# Cold-import budget -- all five modules, per-module, well under the plan's
# 500ms brightline for a single op fire.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mod_name",
    [
        "coordinator_core.hooks.check_claude_md_size",
        "coordinator_core.hooks.derive_global_doctrine_live_copy",
        "coordinator_core.hooks.derive_setup_copies",
        "coordinator_core.hooks.guard_doctrine_surface_ratio_precommit",
        "coordinator_core.hooks.guard_doctrine_surface_bash_write",
    ],
)
def test_cold_import_is_fast(mod_name):
    sys.modules.pop(mod_name, None)
    start = time.perf_counter()
    __import__(mod_name)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{mod_name} import took {elapsed:.3f}s, over the 500ms brightline"


# ---------------------------------------------------------------------------
# op-registration shape
# ---------------------------------------------------------------------------


def test_check_claude_md_size_registers_its_op():
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.check_claude_md_size" in _REGISTRY


def test_derive_global_doctrine_live_copy_registers_its_op():
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.derive_global_doctrine_live_copy" in _REGISTRY


def test_derive_setup_copies_registers_its_op():
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.derive_setup_copies" in _REGISTRY


def test_guard_doctrine_surface_bash_write_registers_its_op():
    from coordinator_core.ipc import _REGISTRY

    assert "hooks.guard_doctrine_surface_bash_write" in _REGISTRY


def test_guard_doctrine_surface_ratio_precommit_is_not_a_hook_op():
    """Native git pre-commit hook -- no `register_op`, same shape as its
    W4-C7 sibling `guard_phantom_staged_deletion_precommit.py`."""
    from coordinator_core.hooks import guard_doctrine_surface_ratio_precommit as gdsrp

    assert not hasattr(gdsrp, "register_op")
    assert hasattr(gdsrp, "main")


# ---------------------------------------------------------------------------
# check_claude_md_size — evaluate() core
# ---------------------------------------------------------------------------


def test_check_claude_md_size_allows_ungoverned_write():
    result = ccms.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/some/unrelated/file.md", "content": "hello"},
        }
    )
    assert result is None


def test_check_claude_md_size_non_write_tool_is_noop():
    assert ccms.evaluate({"tool_name": "Read", "tool_input": {}}) is None


def test_check_claude_md_size_bootstrap_admission_denies_growth(tmp_path):
    target = tmp_path / "CLAUDE.md"
    result = ccms.evaluate(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(target), "content": "x" * 50},
        }
    )
    assert result is not None
    message, channel = result
    assert channel == ccms._CHANNEL_DENY
    assert "bootstrap disposition" in message.prose


# ---------------------------------------------------------------------------
# derive_global_doctrine_live_copy — evaluate() core
# ---------------------------------------------------------------------------


def test_derive_global_doctrine_live_copy_noop_without_dev_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(dgdlc, "_resolve_doctrine_repo_root", lambda: tmp_path)
    result = dgdlc.evaluate({"hook_event_name": "SessionStart"})
    assert result is None


def test_derive_global_doctrine_live_copy_session_start_derives(tmp_path, monkeypatch):
    (tmp_path / ".coordinator-dev-repo").write_text("", encoding="utf-8")
    (tmp_path / "global-doctrine").mkdir()
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text("tracked content", encoding="utf-8")
    monkeypatch.setattr(dgdlc, "_resolve_doctrine_repo_root", lambda: tmp_path)
    monkeypatch.setattr(dgdlc.Path, "home", staticmethod(lambda: tmp_path / "home"))

    result = dgdlc.evaluate({"hook_event_name": "SessionStart"})
    assert result is not None
    assert "re-derived" in result.prose
    assert (tmp_path / "home" / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == (
        "tracked content"
    )


def test_derive_global_doctrine_live_copy_already_synced_is_silent(tmp_path, monkeypatch):
    (tmp_path / ".coordinator-dev-repo").write_text("", encoding="utf-8")
    (tmp_path / "global-doctrine").mkdir()
    (tmp_path / "global-doctrine" / "CLAUDE.md").write_text("same", encoding="utf-8")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("same", encoding="utf-8")
    published = tmp_path / "coordinator" / "templates" / "global-doctrine" / "CLAUDE.md"
    published.parent.mkdir(parents=True)
    published.write_text("same", encoding="utf-8")
    monkeypatch.setattr(dgdlc, "_resolve_doctrine_repo_root", lambda: tmp_path)
    monkeypatch.setattr(dgdlc.Path, "home", staticmethod(lambda: home))

    result = dgdlc.evaluate({"hook_event_name": "SessionStart"})
    assert result is None


# ---------------------------------------------------------------------------
# derive_setup_copies — evaluate() core
# ---------------------------------------------------------------------------


def test_derive_setup_copies_noop_without_repo_root(monkeypatch):
    monkeypatch.setattr(dsc, "_resolve_doctrine_repo_root", lambda: None)
    result = dsc.evaluate(
        {"tool_input": {"file_path": "/some/setup/percolate-hooks/percolate-store.yaml"}}
    )
    assert result is None


def test_derive_setup_copies_derived_write_is_advised_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setattr(dsc, "_resolve_doctrine_repo_root", lambda: tmp_path)
    derived = tmp_path / "coordinator" / "templates" / "setup" / "publish_sync.py"
    derived.parent.mkdir(parents=True)
    derived.write_text("hand-maintained", encoding="utf-8")

    result = dsc.evaluate({"tool_input": {"file_path": str(derived)}})
    assert result is not None
    assert "hand-maintained DERIVED copy" in result.prose
    assert derived.read_text(encoding="utf-8") == "hand-maintained"


def test_derive_setup_copies_byte_copy_row_re_derives(tmp_path, monkeypatch):
    monkeypatch.setattr(dsc, "_resolve_doctrine_repo_root", lambda: tmp_path)
    canonical = tmp_path / "setup" / "percolate-hooks" / "percolate-store.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("canonical content", encoding="utf-8")

    result = dsc.evaluate({"tool_input": {"file_path": str(canonical)}})
    assert result is not None
    assert "re-derived" in result.prose
    derived = tmp_path / "coordinator" / "templates" / "setup" / "percolate-hooks" / "percolate-store.yaml"
    assert derived.read_text(encoding="utf-8") == "canonical content"


# ---------------------------------------------------------------------------
# guard_doctrine_surface_bash_write — is_denied_bash_write() spot checks,
# one per module-docstring carve-out point.
# ---------------------------------------------------------------------------


def test_bash_write_allows_unrelated_command():
    assert not gdsbw.is_denied_bash_write("ls -la")


def test_bash_write_allows_plain_read():
    assert not gdsbw.is_denied_bash_write("cat CLAUDE.md")


def test_bash_write_denies_redirect_onto_governed_file():
    assert gdsbw.is_denied_bash_write("echo pwned > CLAUDE.md")


def test_bash_write_denies_tee_onto_governed_file():
    assert gdsbw.is_denied_bash_write("echo pwned | tee CLAUDE.md")


def test_bash_write_denies_sed_inplace_on_governed_file():
    assert gdsbw.is_denied_bash_write("sed -i 's/a/b/' CLAUDE.md")


def test_bash_write_allows_unrelated_stage_in_joined_command():
    """point 2: an unrelated tool two `;` away from a governed mention must
    not leak a marker into that mention's verdict."""
    assert not gdsbw.is_denied_bash_write("wc -c CLAUDE.md; python3 -m pytest -q")


def test_bash_write_cross_segment_variable_assignment_denies():
    """point 4: a governed path bound to a variable, dereferenced by a
    write two segments later."""
    cmd = 'SURFACE="CLAUDE.md"; echo "pwned" > "$SURFACE"'
    assert gdsbw.is_denied_bash_write(cmd)


def test_bash_write_git_commit_message_prose_is_allowed():
    """point 7: a commit message merely naming a governed file is not a
    write to it."""
    assert not gdsbw.is_denied_bash_write('git commit -m "docs: update CLAUDE.md guidance"')


def test_bash_write_git_checkout_of_governed_file_denies():
    """point 8: a content-mutating git subcommand is itself a write
    marker."""
    assert gdsbw.is_denied_bash_write("git checkout HEAD~5 -- CLAUDE.md")


def test_bash_write_grant_cli_invocation_is_allowed():
    """point 9: the claude_md_grant CLI's write target is its own grant
    record, never the governed file."""
    cmd = 'python3 -m coordinator_core.session.claude_md_grant grant pm "authorizes CLAUDE.md edit"'
    assert not gdsbw.is_denied_bash_write(cmd)


def test_bash_write_grant_cli_with_real_redirect_still_denies():
    cmd = (
        'python3 -m coordinator_core.session.claude_md_grant grant pm "note" > CLAUDE.md'
    )
    assert gdsbw.is_denied_bash_write(cmd)


def test_bash_write_xargs_pipe_indirection_denies():
    """point 10: xargs substitutes the piped identifier into a later
    segment's write at runtime."""
    assert gdsbw.is_denied_bash_write('echo CLAUDE.md | xargs tee')


def test_bash_write_interpreter_quoted_mention_is_allowed():
    """point 11: a governed name appearing only inside a quoted Python
    string literal, with no write marker anywhere in the segment."""
    assert not gdsbw.is_denied_bash_write("python3 -c \"print('reads CLAUDE.md')\"")


def test_bash_write_interpreter_write_mode_open_denies():
    assert gdsbw.is_denied_bash_write("python3 -c \"open('CLAUDE.md','w').write('x')\"")


def test_bash_write_non_command_tool_is_noop():
    assert gdsbw.evaluate({"tool_name": "Read", "tool_input": {}}) is None


def test_bash_write_missing_command_fails_open():
    assert gdsbw.evaluate({"tool_name": "Bash", "tool_input": {}}) is None
