"""A subagent's `baseline-export ... -- pytest <file::node ids>` rerun (review's failing_at_base
measurement) is admitted untouched; any other positional keeps the existing subagent rung.

Spec backlink: coordinator_core/bash_guards/check_test_suite_invocation.py
(_is_node_id_baseline_rerun)
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard

_REPO = str(__import__("pathlib").Path(__file__).resolve().parents[3])
_ID_A = "coordinator_core/bash_guards/tests/test_check_test_suite_invocation.py::test_a"
_ID_B = "coordinator/bin/tests/test_x.py::TestC::test_b[p q]"
_WRAP = "baseline-export --sha abc123 -- "


def _verdict(cmd: str, subagent: bool = True):
    payload = {"tool_name": "Bash", "session_id": "s", "cwd": _REPO, "tool_input": {"command": cmd}}
    if subagent:
        payload["agent_id"] = "agent-1"
    res = guard.check(payload)
    return None if res is None else res.verdict_kind


@pytest.mark.parametrize(
    "cmd",
    [
        _WRAP + f"python -m pytest -rfE {_ID_A} '{_ID_B}'",
        _WRAP + f"pytest -rfE {_ID_A}",
        _WRAP + f"python3 -m pytest -rfE {_ID_A}",
        _WRAP + "python -m pytest -rfE " + " ".join(f"{_ID_A}{i}" for i in range(28)),
    ],
)
def test_node_id_only_rerun_is_admitted(cmd):
    assert _verdict(cmd) is None


@pytest.mark.parametrize(
    "cmd",
    [
        _WRAP + f"python -m pytest -rfE {_ID_A} coordinator_core/bash_guards/tests/",
        _WRAP + "python -m pytest -rfE coordinator_core/bash_guards/tests/test_check_test_suite_invocation.py",
        _WRAP + "python -m pytest -rfE",
        _WRAP + "python -m pytest coordinator_core",
        f"python -m pytest -rfE {_ID_A}",
        f"echo hi && python -m pytest {_ID_A}",
    ],
)
def test_other_shapes_are_not_admitted_by_this_leg(cmd):
    assert _verdict(cmd) is not None


def test_a_directory_positional_beside_ids_is_not_admitted():
    assert not guard._is_node_id_baseline_rerun(
        _WRAP + f"python -m pytest {_ID_A} coordinator_core/bash_guards/tests/", None
    )


def test_a_file_positional_without_node_is_not_admitted():
    assert not guard._is_node_id_baseline_rerun(
        _WRAP + "python -m pytest coordinator_core/bash_guards/tests/test_a.py", None
    )


def test_unwrapped_pytest_on_ids_is_not_admitted_by_this_leg():
    assert not guard._is_node_id_baseline_rerun(f"python -m pytest {_ID_A}", None)
    assert _verdict(f"python -m pytest {_ID_A}") == "rewrite"


def test_a_decoy_leg_does_not_admit_an_unwrapped_runner():
    assert not guard._is_node_id_baseline_rerun(
        _WRAP + f"python -m pytest {_ID_A} && python -m pytest coordinator_core", None
    )


def test_a_broad_wrapped_run_stays_denied():
    assert _verdict(_WRAP + "pytest") == "deny"
