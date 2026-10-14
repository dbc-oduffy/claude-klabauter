"""`baseline-export ... -- <runner>` classifies and is decided exactly as the bare runner."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard

_REPO = str(__import__("pathlib").Path(__file__).resolve().parents[3])
_TEST_FILE = "coordinator_core/bash_guards/tests/test_check_test_suite_invocation.py"

_RUNNERS = [
    "pytest",
    "python3 -m pytest",
    "python -m pytest -m 'not cadence'",
    f"pytest {_TEST_FILE}",
    "pnpm run test",
]

_WRAPPERS = [
    ("baseline-export --sha abc --", ""),
    ("baseline-export --sha abc --link node_modules --link a/b --", ""),
    ("baseline-export.cmd --sha abc --", ""),
    ("baseline-export.ps1 --sha abc --", ""),
    ("/abs/path/baseline-export --sha abc --", ""),
    ("with-suite-mutex -- baseline-export --sha abc --", "with-suite-mutex -- "),
]


def _payload(cmd: str) -> dict:
    return {
        "tool_name": "Bash",
        "session_id": "e641c238-68e3-480a-9e44-3ed73e8c5c94",
        "cwd": _REPO,
        "tool_input": {"command": cmd},
    }


def _decision(cmd: str):
    res = guard.check(_payload(cmd))
    return None if res is None else bool(res)


@pytest.mark.parametrize("runner", _RUNNERS)
@pytest.mark.parametrize("wrapper,baseline", _WRAPPERS)
def test_wrapped_classifies_and_decides_as_bare(wrapper, baseline, runner):
    wrapped = f"{wrapper} {runner}"
    bare = f"{baseline}{runner}"
    got = guard.classify_command(wrapped, cwd=_REPO)
    want = guard.classify_command(bare, cwd=_REPO)
    assert [(m.tier, m.detected) for m in got] == [(m.tier, m.detected) for m in want]
    assert _decision(wrapped) == _decision(bare)


def test_a_broad_runner_is_actually_detected_through_the_wrapper():
    assert guard.classify_command("baseline-export --sha abc -- pytest", cwd=_REPO)


def test_without_double_dash_nothing_is_peeled():
    assert guard.classify_command("baseline-export --sha abc pytest", cwd=_REPO) == []


def test_option_values_never_surface_as_a_runner_token():
    got = guard.classify_command(
        "baseline-export --sha pytest --link pytest -- echo hi", cwd=_REPO
    )
    assert got == []
