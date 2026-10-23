"""Behavioral coverage for `check_heredoc_repo_write_advise` --
`dispatch_checks.py`'s advisory guard for a scriptable-interpreter heredoc
(`python3 - <<'PY' ... PY`) that writes a tracked repo path. Its sibling
`check_cat_heredoc_write_advise` has no dedicated test file of its own
(coverage lives in `test_deny_message_accuracy.py` and
`test_alternative_liveness_gate.py` only) -- this check gets one because its
own detection logic (literal-path extraction, `git_root` containment,
scratch/temp exclusion) has materially more branches than that sibling's
single-regex `cat > FILE <<EOF` match, and the brief's 8-case test list
(2 fires, 5 silences, 1 advisory-not-blocking assertion) doesn't fit
naturally into either existing file's own per-guard convention.

No git spawn anywhere in this file's fixtures: `git_root` is always a plain
path string, never a real repo checkout, mirroring the guard's own contract
that `git_root` is pure path arithmetic, not something it resolves itself.
Most tests hand in `tmp_path` bare; `TestFires` and
`TestAdvisoryNeverBlocking` use `fires_git_root` instead (see its own
docstring) since a bare `tmp_path` would misclassify as scratch there.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))


@pytest.fixture(autouse=True)
def _isolate_temp_env(monkeypatch):
    """pytest's own `tmp_path` fixture (used below as a stand-in `git_root`)
    lives UNDER the real `$TEMP`/`$TMP` directory -- so without this, every
    fires-test's `git_root` would itself misclassify as a scratch root via
    the guard's own env-var scratch check (correct behavior for a REAL
    `git_root`, an artifact of this test's fixture choice). Cleared for
    every test in this file; `test_target_under_scratchpad_temp_root` below
    exercises the OTHER scratch signal (the `.../AppData/Local/Temp/claude/`
    path-marker check), which is independent of these two env vars."""
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)


def _heredoc(body: str) -> str:
    return "python3 - <<'PY'\n%s\nPY" % body


@pytest.fixture
def fires_git_root(tmp_path) -> str:
    """A stand-in `git_root` for the "should fire" tests below, deliberately
    NOT the bare `tmp_path` the other classes use: `_heredoc_write_target_is_scratch`
    treats any path under a literal `/tmp` as scratch by design (mirroring a
    Claude session's own `/tmp/claude-.../scratchpad` shape) -- and on
    Linux/macOS `tmp_path` itself lives under `/tmp`, so a fires-test using it
    bare would have its own `git_root` misclassify as scratch and silently
    return `None` regardless of what the heredoc body writes (the bug this
    fixture exists to route around: TF-20260923-bb-040). `tmp_path.name` is
    folded in only to keep each test's root textually distinct; nothing here
    ever touches disk, matching the guard's own pure-path-arithmetic contract."""
    return "/fires-git-root-fixture/" + tmp_path.name


def _hso(result):
    assert result is not None
    return result["hookSpecificOutput"]


class TestFires:
    def test_pathlib_write_text_under_git_root(self, fires_git_root):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        result = _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, fires_git_root)
        hso = _hso(result)
        assert hso["permissionDecision"] == "allow"
        assert "coordinator_core/x.py" in hso["additionalContext"]

    def test_open_write_mode_under_git_root(self, fires_git_root):
        cmd = _heredoc('open("docs/foo.md", "w").write("hi")')
        result = _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, fires_git_root)
        hso = _hso(result)
        assert hso["permissionDecision"] == "allow"
        assert "docs/foo.md" in hso["additionalContext"]


class TestSilent:
    def test_target_under_scratchpad_temp_root(self, tmp_path, monkeypatch):
        scratch = tmp_path / "AppData" / "Local" / "Temp" / "claude" / "proj"
        scratch.mkdir(parents=True)
        rel = os.path.relpath(str(scratch), str(tmp_path)).replace("\\", "/")
        cmd = _heredoc(
            'import pathlib\npathlib.Path(%r).write_text("hi")' % (rel + "/out.py")
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_target_outside_git_root(self, tmp_path):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("../outside/x.py").write_text("hi")'
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_read_only_heredoc(self, tmp_path):
        cmd = _heredoc(
            'import pathlib\ndata = pathlib.Path("coordinator_core/x.py").read_text()'
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_open_read_mode_is_not_a_write(self, tmp_path):
        cmd = _heredoc('open("coordinator_core/x.py", "r").read()')
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_unresolvable_variable_path(self, tmp_path):
        cmd = _heredoc('p = "coordinator_core/x.py"\nopen(p, "w").write("hi")')
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_unresolvable_fstring_path(self, tmp_path):
        cmd = _heredoc(
            'name = "x"\nopen(f"coordinator_core/{name}.py", "w").write("hi")'
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None

    def test_empty_git_root(self):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, "") is None
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, None) is None

    def test_shell_heredoc_body_never_scanned(self, tmp_path):
        """A `bash <<'EOF' ... EOF` heredoc is a DIFFERENT class
        (`_classify_heredoc_intro` returns `"shell"`, not `"scriptable"`) --
        this guard only ever looks at scriptable-interpreter bodies, so a
        shell heredoc containing the identical write-shaped text must never
        fire, whatever it says."""
        cmd = "bash <<'EOF'\npathlib.Path(\"coordinator_core/x.py\").write_text(\"hi\")\nEOF"
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None


class TestClaudeMemoryExempt:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        # A fake home outside TEMP/TMP: a real tmp_path home would be classed as
        # scratch first (TEMP-rooted on Windows, /tmp on Linux), making the
        # silent cases pass vacuously and the advisory cases fail.
        h = "/fires-git-root-fixture/" + tmp_path.name + "/home"
        monkeypatch.setenv("HOME", h)
        monkeypatch.setenv("USERPROFILE", h)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        return h

    def _write(self, target):
        return _heredoc('open(%r, "w").write("hi")' % target)

    def test_memory_file_silent_when_git_root_is_config_dir(self, home):
        cfg = home + "/.claude"
        target = cfg + "/projects/-slug/memory/note.md"
        assert _enveloped(dc.check_heredoc_repo_write_advise)(self._write(target), "s", None, cfg) is None

    def test_memory_file_silent_with_windows_separators(self, home):
        cfg = home + "/.claude"
        target = (cfg + "/Projects/-slug/MEMORY/note.md").replace("/", "\\")
        assert dc._heredoc_write_target_is_claude_memory(target)
        assert not dc._heredoc_write_target_is_claude_memory(cfg.replace("/", "\\") + "\\projects\\-slug\\memory")

    def test_memory_file_silent_under_claude_config_dir_env(self, tmp_path, monkeypatch):
        cfg = "/fires-git-root-fixture/" + tmp_path.name + "/cfg"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", cfg)
        target = cfg + "/projects/p/memory/a.md"
        assert _enveloped(dc.check_heredoc_repo_write_advise)(self._write(target), "s", None, cfg) is None

    def test_non_memory_file_in_config_dir_still_advises(self, home):
        cfg = home + "/.claude"
        target = cfg + "/projects/-slug/other/note.md"
        assert _enveloped(dc.check_heredoc_repo_write_advise)(self._write(target), "s", None, cfg) is not None

    def test_memory_shaped_path_in_ordinary_repo_still_advises(self, home):
        root = "/fires-git-root-fixture/repo"
        target = root + "/projects/x/memory/a.md"
        assert _enveloped(dc.check_heredoc_repo_write_advise)(self._write(target), "s", None, root) is not None


class TestAdvisoryNeverBlocking:
    def test_all_firing_cases_are_advisory_allow(self, fires_git_root):
        cases = [
            _heredoc('import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'),
            _heredoc('open("docs/foo.md", "w").write("hi")'),
        ]
        for cmd in cases:
            hso = _hso(_enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, fires_git_root))
            assert hso["permissionDecision"] == "allow"
            assert "updatedInput" not in hso


class TestOverride:
    def test_override_env_var_suppresses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_HEREDOC_REPO_WRITE", "1")
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        assert _enveloped(dc.check_heredoc_repo_write_advise)(cmd, "sess", None, str(tmp_path)) is None
