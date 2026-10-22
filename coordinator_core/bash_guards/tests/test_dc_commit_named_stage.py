"""Pure-module tests for the named-stage carrier parse and set difference."""

from __future__ import annotations

import shlex

from coordinator_core.bash_guards._dc_commit_named_stage import (
    NAMED_STAGE_CONFIG_KEY,
    named_set_refusal,
    named_stage_paths,
    unnamed_staged_paths,
)


def _t(cmd: str):
    return shlex.split(cmd)


def test_key_constant():
    assert NAMED_STAGE_CONFIG_KEY == "coordinator.staged"


def test_absent_returns_none():
    assert named_stage_paths(_t("git commit -m s")) is None


def test_non_git_returns_none():
    assert named_stage_paths(_t("echo -c coordinator.staged=a.py commit")) is None


def test_one_path():
    assert named_stage_paths(_t("git -c coordinator.staged=a.py commit -m s")) == ("a.py",)


def test_several_paths_and_wrapper_prefix():
    cmd = "FOO=1 git -C /r -c coordinator.staged=a.py -c coordinator.staged=d/b.py commit -m s"
    assert named_stage_paths(_t(cmd)) == ("a.py", "d/b.py")


def test_other_c_keys_ignored():
    assert named_stage_paths(_t("git -c user.name=x commit -m s")) is None
    cmd = "git -c user.name=x -c coordinator.staged=a.py commit"
    assert named_stage_paths(_t(cmd)) == ("a.py",)


def test_carrier_after_subcommand_not_read():
    assert named_stage_paths(_t("git commit -c coordinator.staged=a.py")) is None


def test_glued_and_config_env_forms_none():
    assert named_stage_paths(_t("git -ccoordinator.staged=a.py commit")) is None
    assert named_stage_paths(_t("git --config-env coordinator.staged=A commit")) is None
    assert named_stage_paths(_t("git --config-env=coordinator.staged=A commit")) is None


def test_dash_C_operand_not_mistaken_for_carrier():
    assert named_stage_paths(_t("git -C -c commit")) is None


def test_empty_value_is_present_and_refused():
    named = named_stage_paths(_t("git -c coordinator.staged= commit"))
    assert named == ("",)
    assert named_set_refusal(named)


def test_refusals():
    assert named_set_refusal(("/etc/x",))
    assert named_set_refusal(("C:\\x\\y",))  # abs-path-ok: fixture for absolute-path refusal
    assert named_set_refusal(("a/../b",))
    assert named_set_refusal(("..\\b",))
    assert named_set_refusal(("a.py", ""))


def test_valid_set_no_refusal():
    assert named_set_refusal(("a.py", "d/b..c.py", "./x")) is None


def test_set_difference_windows_separators():
    assert unnamed_staged_paths(["d\\a.py", "b.py"], ["./d/a.py"]) == ("b.py",)
    assert unnamed_staged_paths(["z", "a"], []) == ("a", "z")
    assert unnamed_staged_paths(["a"], ["a", "extra"]) == ()
