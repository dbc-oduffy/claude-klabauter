"""Tests for guard_move_from_inside_cwd: advisory naming the shell cwd when a move takes it away."""

from __future__ import annotations

import time

import pytest

from coordinator_core.bash_guards.guard_move_from_inside_cwd import check_move_from_inside_cwd


def _ctx(out):
    return out["hookSpecificOutput"]["additionalContext"]


def _run(cmd, cwd, tool_name="Bash"):
    return check_move_from_inside_cwd(
        cmd, "s", payload={"tool_name": tool_name, "cwd": str(cwd), "tool_input": {"command": cmd}}
    )


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "sib").mkdir()
    return tmp_path


def test_cwd_inside_source_fires_and_names_cwd(tree):
    cwd = tree / "a" / "b"
    out = _run(f"mv {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}", cwd)
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "MOVE-FROM-INSIDE-CWD" in _ctx(out)
    assert "a" in _ctx(out) and "b" in _ctx(out)
    assert str(cwd) in _ctx(out)


def test_cwd_equals_source_fires(tree):
    assert _run(f"mv {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}", tree / "a") is not None


def test_cd_prefix_in_same_command_fires(tree):
    out = _run("cd a/b && mv .. ../../z", tree)
    assert out is not None and str(tree / "a" / "b") in _ctx(out)


def test_relative_source_resolves_against_cwd(tree):
    assert _run("mv ../b ../../z", tree / "a" / "b") is not None


def test_target_directory_flag_makes_all_operands_sources(tree):
    assert _run(f"mv -t {(tree / 'z').as_posix()} {(tree / 'a').as_posix()}", tree / "a" / "b") is not None


def test_powershell_move_item_named_params_fires(tree):
    out = _run(
        f"Move-Item -Path '{(tree / 'a').as_posix()}' -Destination '{(tree / 'z').as_posix()}'",
        tree / "a" / "b",
        tool_name="PowerShell",
    )
    assert out is not None and "MOVE-FROM-INSIDE-CWD" in _ctx(out)


def test_powershell_relative_path_from_cwd(tree):
    assert _run("Move-Item -Path .. -Destination z", tree / "a" / "b", tool_name="PowerShell") is not None


def test_powershell_positional_move_alias_fires(tree):
    assert _run(f"move {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}", tree / "a" / "b", "PowerShell") is not None


def test_control_cwd_outside_source(tree):
    assert _run(f"mv {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}", tree / "sib") is None


def test_control_sibling_move(tree):
    assert _run(f"mv {(tree / 'sib').as_posix()} {(tree / 'z').as_posix()}", tree / "a" / "b") is None


def test_control_destination_is_cwd_not_source(tree):
    assert _run(f"mv {(tree / 'sib').as_posix()} {(tree / 'a').as_posix()}", tree / "a") is None


def test_control_prefix_name_is_not_ancestor(tree):
    (tree / "ab").mkdir()
    assert _run(f"mv {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}", tree / "ab") is None


def test_control_cd_out_first(tree):
    assert _run(f"cd {tree.as_posix()} && mv a z", tree / "a" / "b") is None


@pytest.mark.parametrize("src", ["a*", "$SRC", "$(pwd)", "`pwd`"])
def test_silent_on_non_literal_operand(tree, src):
    assert _run(f"mv {src} z", tree / "a") is None


def test_silent_without_move_or_cwd(tree):
    assert _run("ls -la", tree / "a") is None
    assert check_move_from_inside_cwd("mv a z", "s", payload={"tool_name": "Bash"}) is None


def test_cost_under_five_ms_per_call(tree):
    cmd = f"mv {(tree / 'a').as_posix()} {(tree / 'z').as_posix()}"
    _run(cmd, tree / "a" / "b")
    n = 200
    t0 = time.perf_counter()
    for _ in range(n):
        _run(cmd, tree / "a" / "b")
    per_call_ms = (time.perf_counter() - t0) * 1000 / n
    assert per_call_ms < 5.0
