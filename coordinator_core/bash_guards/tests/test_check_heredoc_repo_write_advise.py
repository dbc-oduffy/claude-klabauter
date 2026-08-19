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
`tmp_path`, never a real repo checkout, mirroring the guard's own contract
that `git_root` is pure path arithmetic, not something it resolves itself.
"""
from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc


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


def _hso(result):
    assert result is not None
    return result["hookSpecificOutput"]


class TestFires:
    def test_pathlib_write_text_under_git_root(self, tmp_path):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        result = dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path))
        hso = _hso(result)
        assert hso["permissionDecision"] == "allow"
        assert "coordinator_core/x.py" in hso["additionalContext"]

    def test_open_write_mode_under_git_root(self, tmp_path):
        cmd = _heredoc('open("docs/foo.md", "w").write("hi")')
        result = dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path))
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
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_target_outside_git_root(self, tmp_path):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("../outside/x.py").write_text("hi")'
        )
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_read_only_heredoc(self, tmp_path):
        cmd = _heredoc(
            'import pathlib\ndata = pathlib.Path("coordinator_core/x.py").read_text()'
        )
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_open_read_mode_is_not_a_write(self, tmp_path):
        cmd = _heredoc('open("coordinator_core/x.py", "r").read()')
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_unresolvable_variable_path(self, tmp_path):
        cmd = _heredoc('p = "coordinator_core/x.py"\nopen(p, "w").write("hi")')
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_unresolvable_fstring_path(self, tmp_path):
        cmd = _heredoc(
            'name = "x"\nopen(f"coordinator_core/{name}.py", "w").write("hi")'
        )
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None

    def test_empty_git_root(self):
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, "") is None
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, None) is None

    def test_shell_heredoc_body_never_scanned(self, tmp_path):
        """A `bash <<'EOF' ... EOF` heredoc is a DIFFERENT class
        (`_classify_heredoc_intro` returns `"shell"`, not `"scriptable"`) --
        this guard only ever looks at scriptable-interpreter bodies, so a
        shell heredoc containing the identical write-shaped text must never
        fire, whatever it says."""
        cmd = "bash <<'EOF'\npathlib.Path(\"coordinator_core/x.py\").write_text(\"hi\")\nEOF"
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None


class TestAdvisoryNeverBlocking:
    def test_all_firing_cases_are_advisory_allow(self, tmp_path):
        cases = [
            _heredoc('import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'),
            _heredoc('open("docs/foo.md", "w").write("hi")'),
        ]
        for cmd in cases:
            hso = _hso(dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)))
            assert hso["permissionDecision"] == "allow"
            assert "updatedInput" not in hso


class TestOverride:
    def test_override_env_var_suppresses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_HEREDOC_REPO_WRITE", "1")
        cmd = _heredoc(
            'import pathlib\npathlib.Path("coordinator_core/x.py").write_text("hi")'
        )
        assert dc.check_heredoc_repo_write_advise(cmd, "sess", None, str(tmp_path)) is None
