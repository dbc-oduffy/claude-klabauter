"""scripts/unstick_claude.py returns the bare `claude` to the real binary on a
box a coordinator shim bricked, touching nothing the generator did not write."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent / "unstick_claude.py"


@pytest.fixture
def unstick(monkeypatch, tmp_path):
    for key in ("CLAUDE_HOME", "USERPROFILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path))
    spec = importlib.util.spec_from_file_location("unstick_claude", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _block(mod) -> str:
    return f"{mod.SENTINEL_BEGIN}\n[ -f x ] && source x\n{mod.SENTINEL_END}\n"


def test_removes_the_shim_and_the_rc_block_and_keeps_the_operators_lines(unstick, tmp_path, capsys):
    shim = tmp_path / ".claude" / "shell" / "claude-doe-shim.sh"
    shim.parent.mkdir(parents=True)
    shim.write_text("claude() { :; }\n")
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text("export A=1\n\n" + _block(unstick) + "alias ll='ls -l'\n")

    assert unstick.main() == 0

    assert not shim.exists()
    assert zshrc.read_text() == "export A=1\n\nalias ll='ls -l'\n"
    assert "Fixed." in capsys.readouterr().out


def test_an_unterminated_block_is_left_alone(unstick, tmp_path):
    bashrc = tmp_path / ".bashrc"
    text = f"{unstick.SENTINEL_BEGIN}\nmine\n"
    bashrc.write_text(text)

    assert unstick.main() == 0

    assert bashrc.read_text() == text


def test_a_clean_box_is_a_no_op(unstick, capsys):
    assert unstick.main() == 0
    assert "Nothing to fix" in capsys.readouterr().out


def test_imports_nothing_from_the_engine():
    assert "coordinator_core" not in "".join(
        line for line in _SCRIPT.read_text().splitlines() if line.startswith(("import", "from"))
    )
