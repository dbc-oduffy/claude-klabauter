"""test_cli_timeout_ceiling_clamp.py — the ceiling guard for the two
`coordinator/bin/` doors that expose a `--timeout` flag over a locked mutating
op: `priority-set.py` (priority.set) and `set-goal-kr-status.py`
(goal.set_kr_status).

Purpose: the CLI flag is the front door an EM actually types at. The op-side
clamp (coordinator_core/ops/tests/test_lock_timeout_ceiling_clamp.py) is the
authority and holds regardless of what these doors send, but a door that
forwards an over-ask verbatim tells its caller nothing — these cases pin that
each door clamps at the point of entry AND says so on stderr, so the over-ask is
answered where it was made rather than silently downstream.

Both doors are loaded by file path (hyphenated filenames are not importable),
mirroring test_priority_set_no_cwd_gate.py's loader.

Negative-spec:
  - Does NOT assert the ceiling's VALUE. The number is a judgement call that may
    be re-ruled; what must never regress is that a caller cannot exceed whatever
    it currently is.
  - Does NOT spawn the op — `cc_invoke` is never reached; these cases exercise
    argument handling only.
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
from contextlib import redirect_stderr

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)


def _load(filename: str, module_name: str):
    path = os.path.join(_BIN_DIR, filename)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(
    scope="module",
    params=[("priority-set.py", "ceiling_priority_set"), ("set-goal-kr-status.py", "ceiling_set_kr")],
    ids=["priority-set", "set-goal-kr-status"],
)
def door(request):
    return _load(*request.param)


def test_over_ceiling_timeout_is_clamped(door):
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        clamped = door._clamp_timeout(str(door.MAX_TIMEOUT_SECS * 1000))

    assert clamped == door.MAX_TIMEOUT_SECS
    assert "ceiling" in stderr.getvalue(), (
        "a clamped over-ask must be visible on stderr — a silent clamp at the "
        "door leaves the caller believing the dial worked"
    )


def test_under_ceiling_timeout_passes_through_quietly(door):
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        clamped = door._clamp_timeout("0.5")

    assert clamped == 0.5
    assert stderr.getvalue() == ""


def test_unparseable_timeout_exits_one(door):
    with redirect_stderr(io.StringIO()):
        with pytest.raises(SystemExit) as exc:
            door._clamp_timeout("soon")

    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# The clamp must sit on the path that actually builds the op params — a helper
# nothing calls would pass every case above and ship an unbounded door.
# ---------------------------------------------------------------------------


def _sent_params(module, argv: list[str]) -> dict:
    """Drive module.main(argv) with cc_invoke and repo resolution stubbed, and
    return the params dict the door tried to send."""
    captured: dict = {}

    def _fake_cc_invoke(op_key, params, cwd_repo_root):
        captured.update(params)
        return {}

    real_invoke = module.cc_invoke
    real_resolve = module.resolve_checked_repo_root
    module.cc_invoke = _fake_cc_invoke
    module.resolve_checked_repo_root = lambda explicit_root=None: (
        "/repo",
        {"verdict": "MATCH", "resolved_root": "/repo", "message": "stub"},
    )
    try:
        with redirect_stderr(io.StringIO()):
            module.main(argv)
    finally:
        module.cc_invoke = real_invoke
        module.resolve_checked_repo_root = real_resolve
    return captured


def test_priority_set_door_sends_the_clamped_timeout():
    module = _load("priority-set.py", "ceiling_priority_set_main")
    params = _sent_params(
        module,
        [
            "--target-id", "t1", "--target-kind", "handoff", "--priority", "high",
            "--timeout", str(module.MAX_TIMEOUT_SECS * 1000),
        ],
    )

    assert params["timeout"] == module.MAX_TIMEOUT_SECS


def test_set_goal_kr_status_door_sends_the_clamped_timeout():
    module = _load("set-goal-kr-status.py", "ceiling_set_kr_main")
    params = _sent_params(
        module,
        [
            "--goal-file", "g.yaml", "--kr-id", "kr-1", "--status", "done",
            "--timeout", str(module.MAX_TIMEOUT_SECS * 1000),
        ],
    )

    assert params["timeout"] == module.MAX_TIMEOUT_SECS
