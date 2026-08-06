"""
Tests for coordinator_core.ops.edit_live_hook.

Port of: edit-live-hook.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import edit_live_hook as elh


def _write_hook(path: Path, body: str = "#!/bin/bash\necho hi\n", mode: int = 0o755) -> None:
    path.write_text(body, encoding="utf-8")
    os.chmod(path, mode)


class TestUsage:
    def test_no_args_returns_usage_exit(self, capsys):
        rc = elh.main([])
        assert rc == elh.EXIT_USAGE
        assert "Usage:" in capsys.readouterr().err

    def test_unknown_subcommand_returns_usage_exit(self, capsys):
        rc = elh.main(["bogus"])
        assert rc == elh.EXIT_USAGE
        assert "unknown subcommand" in capsys.readouterr().err

    def test_help_returns_ok(self, capsys):
        rc = elh.main(["--help"])
        assert rc == elh.EXIT_OK
        assert "Usage:" in capsys.readouterr().out


class TestStage:
    def test_missing_hook_path_arg_is_usage_error(self):
        assert elh.cmd_stage([]) == elh.EXIT_USAGE

    def test_nonexistent_hook_is_usage_error(self, tmp_path):
        assert elh.cmd_stage([str(tmp_path / "nope.sh")]) == elh.EXIT_USAGE

    def test_stages_scratch_copy_same_dir(self, tmp_path, capsys):
        hook = tmp_path / "myhook.sh"
        _write_hook(hook)

        rc = elh.cmd_stage([str(hook)])
        assert rc == elh.EXIT_OK

        out = capsys.readouterr().out
        assert "Staged scratch copy:" in out
        scratch_path = out.split("Staged scratch copy:", 1)[1].strip()
        scratch = Path(scratch_path)
        assert scratch.parent == hook.resolve().parent
        assert scratch.read_text(encoding="utf-8") == hook.read_text(encoding="utf-8")

    def test_stage_preserves_exec_bit_on_scratch_copy(self, tmp_path, capsys):
        # Regression: shutil.copy2 must preserve mode bits (cp -p oracle
        # behavior) -- a scratch copy that lost the exec bit would silently
        # produce a non-executable hook on commit.
        #
        # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is
        # always 0o666 there regardless of the os.chmod() argument. Assert
        # the real invariant under test -- mode preserved across the copy
        # -- by comparing against whatever mode chmod actually produced on
        # this platform, rather than asserting the literal POSIX octal.
        hook = tmp_path / "myhook.sh"
        _write_hook(hook, mode=0o755)
        mode_before = stat.S_IMODE(os.stat(hook).st_mode)

        elh.cmd_stage([str(hook)])
        out = capsys.readouterr().out
        scratch = Path(out.split("Staged scratch copy:", 1)[1].strip())

        assert stat.S_IMODE(os.stat(scratch).st_mode) == mode_before

    def test_warns_when_bash_matcher_hook_detected(self, tmp_path, capsys, monkeypatch):
        hook = tmp_path / "guard.sh"
        _write_hook(hook)

        plugin_root = tmp_path / "plugin"
        (plugin_root / "hooks").mkdir(parents=True)
        hooks_json = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": f"bash {hook}"}],
                    }
                ]
            }
        }
        (plugin_root / "hooks" / "hooks.json").write_text(json.dumps(hooks_json), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("EDIT_LIVE_HOOK_SCRIPT_DIR", raising=False)

        elh.cmd_stage([str(hook)])
        err = capsys.readouterr().err
        assert "Bash-inclusive PreToolUse matcher" in err

    def test_no_warning_when_not_bash_matcher(self, tmp_path, capsys, monkeypatch):
        hook = tmp_path / "other.sh"
        _write_hook(hook)

        plugin_root = tmp_path / "plugin"
        (plugin_root / "hooks").mkdir(parents=True)
        hooks_json = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": []}]}}
        (plugin_root / "hooks" / "hooks.json").write_text(json.dumps(hooks_json), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("EDIT_LIVE_HOOK_SCRIPT_DIR", raising=False)

        elh.cmd_stage([str(hook)])
        assert "Bash-inclusive" not in capsys.readouterr().err


class TestIsLiveBashMatcherHook:
    def test_no_hooks_json_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("EDIT_LIVE_HOOK_SCRIPT_DIR", raising=False)
        assert elh.is_live_bash_matcher_hook(str(tmp_path / "x.sh")) is False

    def test_malformed_hooks_json_returns_false(self, tmp_path, monkeypatch):
        plugin_root = tmp_path / "plugin"
        (plugin_root / "hooks").mkdir(parents=True)
        (plugin_root / "hooks" / "hooks.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("EDIT_LIVE_HOOK_SCRIPT_DIR", raising=False)
        assert elh.is_live_bash_matcher_hook(str(tmp_path / "x.sh")) is False

    def test_substring_matcher_match_is_preserved(self, tmp_path, monkeypatch):
        # Negative-spec: "Bash" substring match, e.g. matcher "Bash|Read"
        # counts as a Bash-inclusive matcher -- faithful to the bash oracle's
        # substring test, not a stricter exact-match.
        plugin_root = tmp_path / "plugin"
        (plugin_root / "hooks").mkdir(parents=True)
        hooks_json = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Read",
                        "hooks": [{"type": "command", "command": "bash /x/guard.sh"}],
                    }
                ]
            }
        }
        (plugin_root / "hooks" / "hooks.json").write_text(json.dumps(hooks_json), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.delenv("EDIT_LIVE_HOOK_SCRIPT_DIR", raising=False)
        assert elh.is_live_bash_matcher_hook("/x/guard.sh") is True


class TestCommit:
    def test_missing_args_is_usage_error(self):
        assert elh.cmd_commit(["only-one-arg"]) == elh.EXIT_USAGE

    def test_missing_scratch_file_is_usage_error(self, tmp_path):
        hook = tmp_path / "hook.sh"
        _write_hook(hook)
        rc = elh.cmd_commit([str(hook), str(tmp_path / "nope.scratch")])
        assert rc == elh.EXIT_USAGE

    def test_valid_syntax_swaps_atomically(self, tmp_path, capsys):
        hook = tmp_path / "hook.sh"
        _write_hook(hook, body="#!/bin/bash\necho old\n")

        stage_rc = elh.cmd_stage([str(hook)])
        assert stage_rc == elh.EXIT_OK
        scratch = Path(capsys.readouterr().out.split("Staged scratch copy:", 1)[1].strip())
        scratch.write_text("#!/bin/bash\necho new\n", encoding="utf-8")

        rc = elh.cmd_commit([str(hook), str(scratch)])
        assert rc == elh.EXIT_OK
        assert hook.read_text(encoding="utf-8") == "#!/bin/bash\necho new\n"
        assert not scratch.exists()

    def test_syntax_error_refuses_swap_and_leaves_both_files(self, tmp_path, capsys):
        hook = tmp_path / "hook.sh"
        _write_hook(hook, body="#!/bin/bash\necho old\n")

        stage_rc = elh.cmd_stage([str(hook)])
        assert stage_rc == elh.EXIT_OK
        scratch = Path(capsys.readouterr().out.split("Staged scratch copy:", 1)[1].strip())
        scratch.write_text("#!/bin/bash\nif [ 1 -eq 1 ]; then\n  echo broken\n", encoding="utf-8")

        rc = elh.cmd_commit([str(hook), str(scratch)])
        assert rc == elh.EXIT_VALIDATION_FAILED
        assert hook.read_text(encoding="utf-8") == "#!/bin/bash\necho old\n"
        assert scratch.exists()

    def test_commit_preserves_live_exec_bit_across_swap(self, tmp_path, capsys):
        # Regression: os.replace does not carry permission bits from a
        # freshly-written scratch file -- commit must chmod the scratch file
        # to the live file's mode before the atomic swap (addendum rule 5).
        # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is
        # always 0o666 there regardless of the os.chmod() argument. Assert
        # the real invariant under test -- the LIVE file's mode survives
        # the swap, not the scratch file's overwritten mode -- by
        # comparing against whatever mode chmod actually produced on this
        # platform, rather than asserting the literal POSIX octal.
        hook = tmp_path / "hook.sh"
        _write_hook(hook, body="#!/bin/bash\necho old\n", mode=0o755)
        mode_before = stat.S_IMODE(os.stat(hook).st_mode)

        stage_rc = elh.cmd_stage([str(hook)])
        assert stage_rc == elh.EXIT_OK
        scratch = Path(capsys.readouterr().out.split("Staged scratch copy:", 1)[1].strip())

        # Simulate an editor that rewrote the scratch file with a non-exec
        # mode (e.g. a plain text-editor save) -- commit must still land it
        # executable because it preserves the LIVE file's mode, not the
        # scratch file's current mode.
        scratch.write_text("#!/bin/bash\necho new\n", encoding="utf-8")
        os.chmod(scratch, 0o644)

        rc = elh.cmd_commit([str(hook), str(scratch)])
        assert rc == elh.EXIT_OK
        assert stat.S_IMODE(os.stat(hook).st_mode) == mode_before

    def test_sh_unavailable_refuses_swap(self, tmp_path, monkeypatch):
        hook = tmp_path / "hook.sh"
        _write_hook(hook)
        scratch = tmp_path / ".hook.sh.edit-live-hook.1.scratch"
        shutil.copy2(hook, scratch)

        monkeypatch.setattr(elh.shutil, "which", lambda *_a, **_kw: None)

        rc = elh.cmd_commit([str(hook), str(scratch)])
        assert rc == elh.EXIT_VALIDATION_FAILED
        assert hook.read_text(encoding="utf-8") == scratch.read_text(encoding="utf-8")

    def test_different_dir_scratch_still_commits_with_warning(self, tmp_path, capsys):
        hook_dir = tmp_path / "live"
        hook_dir.mkdir()
        other_dir = tmp_path / "elsewhere"
        other_dir.mkdir()

        hook = hook_dir / "hook.sh"
        _write_hook(hook, body="#!/bin/bash\necho old\n")
        scratch = other_dir / "hook.sh.scratch"
        scratch.write_text("#!/bin/bash\necho new\n", encoding="utf-8")

        rc = elh.cmd_commit([str(hook), str(scratch)])
        assert rc == elh.EXIT_OK
        assert "different" in capsys.readouterr().err
        assert hook.read_text(encoding="utf-8") == "#!/bin/bash\necho new\n"


class TestStageCommitRoundTrip:
    def test_full_workflow_end_to_end(self, tmp_path, capsys):
        # Windows has no POSIX mode bits; os.stat().st_mode & 0o777 is
        # always 0o666 there regardless of the os.chmod() argument. Assert
        # the real invariant under test -- mode preserved end-to-end -- by
        # comparing against whatever mode chmod actually produced on this
        # platform, rather than asserting the literal POSIX octal.
        hook = tmp_path / "roundtrip.sh"
        _write_hook(hook, body="#!/bin/bash\necho v1\n", mode=0o755)
        mode_before = stat.S_IMODE(os.stat(hook).st_mode)

        stage_rc = elh.cmd_stage([str(hook)])
        assert stage_rc == elh.EXIT_OK
        scratch = Path(capsys.readouterr().out.split("Staged scratch copy:", 1)[1].strip())

        scratch.write_text("#!/bin/bash\necho v2\n", encoding="utf-8")

        commit_rc = elh.cmd_commit([str(hook), str(scratch)])
        assert commit_rc == elh.EXIT_OK
        assert hook.read_text(encoding="utf-8") == "#!/bin/bash\necho v2\n"
        assert stat.S_IMODE(os.stat(hook).st_mode) == mode_before
        assert not scratch.exists()
