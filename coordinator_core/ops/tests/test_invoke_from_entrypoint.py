
from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.ops.invoke_from_argv import _invoke_from_argv

_PROJECT_ROOT = str(Path(__file__).resolve().parents[3])
_BIN_DIR = Path(_PROJECT_ROOT) / "coordinator" / "bin"


def test_entrypoint_absent_is_unchanged_dispatch_argv_path():
    """Regression guard on the additive contract itself: with no `entrypoint`
    key at all, the response shape/exit_code is the ordinary `_dispatch_argv`
    JSON-RPC envelope -- proven fully by `test_invoke_from_argv.py`; this
    narrow check exists so a future edit that makes `entrypoint` load-bearing
    even when absent (e.g. defaulting it to something) fails HERE, close to
    the change that would cause it."""
    result = _invoke_from_argv({"argv": ["ping", "{}"], "cwd": _PROJECT_ROOT})
    assert result["exit_code"] == 0
    assert '"jsonrpc"' in result["stdout"]
    assert '"result"' in result["stdout"]


def test_entrypoint_present_runs_the_named_clis_own_parser():
    assert (_BIN_DIR / "cross-repo-memo.py").is_file(), (
        "setup error: coordinator/bin/cross-repo-memo.py must exist for "
        "this to be a meaningful test of the real entrypoint-loading path"
    )
    result = _invoke_from_argv({
        "argv": ["list", "--help"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] == 0
    assert "usage" in result["stdout"].lower()
    assert result["stderr"] == ""


def test_entrypoint_argv_is_relayed_verbatim_no_translation():
    result = _invoke_from_argv({
        "argv": ["definitely-not-a-real-verb"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] != 0
    assert "cross-repo-memo" in result["stderr"]


def test_entrypoint_naming_no_real_script_fails_closed():
    missing = "definitely-not-a-real-coordinator-bin-cli"
    assert not (_BIN_DIR / f"{missing}.py").is_file()

    with pytest.raises(ValueError) as excinfo:
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": missing})

    message = str(excinfo.value)
    assert missing in message
    assert "coordinator-invoke" not in message, (
        "a fail-closed refusal must name the ACTUAL missing script, never "
        "the default CLI -- silently falling back to it is the exact "
        "mis-dispatch this field exists to prevent"
    )


def test_entrypoint_must_be_a_non_empty_string_when_present():
    with pytest.raises(ValueError, match="params.entrypoint"):
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": ""})


def test_entrypoint_must_be_a_string_not_some_other_type():
    with pytest.raises(ValueError, match="params.entrypoint"):
        _invoke_from_argv({"argv": ["list"], "cwd": _PROJECT_ROOT, "entrypoint": 123})


def test_cwd_is_chdired_for_the_call_and_restored_after():
    before = os.getcwd()
    result = _invoke_from_argv({
        "argv": ["list", "--help"],
        "cwd": _PROJECT_ROOT,
        "entrypoint": "cross-repo-memo",
    })
    assert result["exit_code"] == 0
    assert os.getcwd() == before, (
        "invoke.from_argv must restore this (server) process's cwd after "
        "an entrypoint call -- a warm pool worker's cwd corrupting across "
        "requests would corrupt every subsequent call's relative-path "
        "resolution, not just this one's"
    )


def test_cwd_is_restored_even_when_the_entrypoint_fails_closed():
    before = os.getcwd()
    with pytest.raises(ValueError):
        _invoke_from_argv({
            "argv": ["list"],
            "cwd": _PROJECT_ROOT,
            "entrypoint": "definitely-not-a-real-coordinator-bin-cli",
        })
    assert os.getcwd() == before


@pytest.mark.parametrize(
    "entrypoint, argv",
    [
        ("workday-complete-assemble", ["apply", "--brief", "x.json"]),
        ("workday-complete-args-and-validate", ["run-step1"]),
    ],
)
def test_a_suite_running_verb_is_refused_undispatched(entrypoint, argv, monkeypatch):
    from coordinator_core import ipc
    from coordinator_core.ops import invoke_from_argv

    def _must_not_load(*_a, **_k):
        raise AssertionError("the entrypoint loaded before the refusal")

    monkeypatch.setattr(invoke_from_argv, "_load_entrypoint_main", _must_not_load)

    with pytest.raises(invoke_from_argv.EntrypointNotWarmLoadableError) as excinfo:
        _invoke_from_argv({"argv": argv, "cwd": _PROJECT_ROOT, "entrypoint": entrypoint})

    assert ipc._handler_exception_error(excinfo.value)["code"] == ipc.ENTRYPOINT_NOT_WARM_LOADABLE_ERROR


def test_the_same_entrypoints_other_verbs_still_serve_warm(monkeypatch):
    from coordinator_core.ops import invoke_from_argv

    loaded = []
    monkeypatch.setattr(
        invoke_from_argv,
        "_load_entrypoint_main",
        lambda _s, name: (loaded.append(name), lambda *_a: 0)[1],
    )

    result = _invoke_from_argv(
        {"argv": ["brief", "--json"], "cwd": _PROJECT_ROOT, "entrypoint": "workday-complete-assemble"}
    )

    assert result["exit_code"] == 0
    assert loaded == ["workday-complete-assemble"]
