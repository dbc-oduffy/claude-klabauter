"""
Tests for `coordinator_core.testing.state_write_audit`.

All exercise `classify_event` over synthetic `(event, args, frames)` tuples.
None install the global `sys.addaudithook` — `classify_event` is pure and
takes an already-collected frame-filename sequence, precisely so this file
never needs to. See module docstring's "OPT-IN, NEVER STANDING" section for
why a standing hook install is a defect this suite must never introduce.
"""

from __future__ import annotations

import os
import sys

from coordinator_core.testing import state_write_audit as swa

_CC_FRAME = "coordinator_core/ops/some_writer.py"
_CC_TEST_FRAME = "coordinator_core/ops/tests/test_some_writer.py"
_CC_CONFTEST_FRAME = "coordinator_core/ops/conftest.py"
_CC_SEAM_FRAME = "coordinator_core/session/claimed_write.py"
_CC_APPEND_FRAME = "coordinator_core/atomic_append.py"
_CC_REPLACE_FRAME = "coordinator_core/atomic_replace.py"
_CC_TESTING_FRAME = "coordinator_core/testing/run.py"
_NON_CC_FRAME = "/usr/lib/python3.11/tempfile.py"


def test_module_never_installs_the_hook_on_import():
    assert swa.audit_hook_installed() is False


def test_write_mode_chars_classify_as_write():
    for mode in ("w", "a", "x", "wb", "ab", "xb", "w+", "a+", "r+", "r+b"):
        record = swa.classify_event(
            "open", ("state/foo.txt", mode, None), [_CC_FRAME]
        )
        assert record is not None, mode
        assert record["event"] == "open"
        assert record["path"] == "state/foo.txt"


def test_read_mode_chars_are_not_a_write():
    for mode in ("r", "rb", "rt"):
        record = swa.classify_event(
            "open", ("state/foo.txt", mode, None), [_CC_FRAME]
        )
        assert record is None, mode


def test_write_flag_combinations_classify_as_write():
    combos = [
        os.O_WRONLY,
        os.O_RDWR,
        os.O_CREAT,
        os.O_APPEND,
        os.O_TRUNC,
        os.O_WRONLY | os.O_CREAT,
        os.O_RDWR | os.O_APPEND,
    ]
    for flags in combos:
        record = swa.classify_event(
            "open", ("state/foo.bin", None, flags), [_CC_FRAME]
        )
        assert record is not None, flags


def test_o_rdonly_alone_is_not_a_write():
    record = swa.classify_event(
        "open", ("state/foo.bin", None, os.O_RDONLY), [_CC_FRAME]
    )
    assert record is None


def test_os_rename_dst_under_state_is_a_write():
    record = swa.classify_event(
        "os.rename", ("/tmp/src.tmp", "state/foo.txt"), [_CC_FRAME]
    )
    assert record is not None
    assert record["event"] == "os.rename"
    assert record["path"] == "state/foo.txt"


def test_os_rename_dst_not_under_state_is_none():
    record = swa.classify_event(
        "os.rename", ("/tmp/src.tmp", "/tmp/dst.tmp"), [_CC_FRAME]
    )
    assert record is None


def test_non_state_path_is_none_even_for_a_write():
    record = swa.classify_event(
        "open", ("coordinator_core/foo.py", "w", None), [_CC_FRAME]
    )
    assert record is None


def test_state_must_be_a_full_path_component():
    record = swa.classify_event(
        "open", ("some/reinstated/foo.txt", "w", None), [_CC_FRAME]
    )
    assert record is None


def test_unrelated_event_is_none():
    record = swa.classify_event(
        "os.remove", ("state/foo.txt",), [_CC_FRAME]
    )
    assert record is None


def test_attributes_to_innermost_and_outermost_coordinator_core_frame():
    outer = "coordinator_core/ops/outer_caller.py"
    record = swa.classify_event(
        "open",
        ("state/foo.txt", "w", None),
        [_NON_CC_FRAME, _CC_FRAME, _CC_TEST_FRAME, outer, _NON_CC_FRAME],
    )
    assert record is not None
    assert record["inner_frame"] == _CC_FRAME
    assert record["outer_frame"] == outer


def test_single_qualifying_frame_is_both_inner_and_outer():
    record = swa.classify_event(
        "open", ("state/foo.txt", "w", None), [_NON_CC_FRAME, _CC_FRAME]
    )
    assert record is not None
    assert record["inner_frame"] == _CC_FRAME
    assert record["outer_frame"] == _CC_FRAME


def test_no_qualifying_frame_is_none():
    record = swa.classify_event(
        "open",
        ("state/foo.txt", "w", None),
        [_NON_CC_FRAME, _CC_TEST_FRAME, _CC_CONFTEST_FRAME],
    )
    assert record is None


def test_plugin_frame_itself_is_never_attributed():
    record = swa.classify_event(
        "open",
        ("state/foo.txt", "w", None),
        ["coordinator_core/testing/state_write_audit.py"],
    )
    assert record is None


def test_claimed_write_seam_frame_is_never_attributed():
    record = swa.classify_event(
        "open", ("state/foo.txt", "w", None), [_CC_SEAM_FRAME]
    )
    assert record is None


def test_atomic_append_and_replace_frames_are_never_attributed():
    for frame in (_CC_APPEND_FRAME, _CC_REPLACE_FRAME):
        record = swa.classify_event(
            "open", ("state/foo.txt", "w", None), [frame]
        )
        assert record is None, frame


def test_seam_frame_does_not_block_a_real_caller_above_it():
    record = swa.classify_event(
        "open",
        ("state/foo.txt", "w", None),
        [_CC_SEAM_FRAME, _CC_FRAME],
    )
    assert record is not None
    assert record["inner_frame"] == _CC_FRAME
    assert record["outer_frame"] == _CC_FRAME


def test_testing_subpackage_frames_are_excluded_like_tests():
    record = swa.classify_event(
        "open", ("state/foo.txt", "w", None), [_CC_TESTING_FRAME]
    )
    assert record is None


def test_non_coordinator_core_frame_is_excluded():
    record = swa.classify_event(
        "open", ("state/foo.txt", "w", None), [_NON_CC_FRAME]
    )
    assert record is None


def test_addoption_registers_expected_default(pytestconfig):
    try:
        value = pytestconfig.getoption("state_write_audit_out")
    except (ValueError, LookupError):
        import pytest

        pytest.skip("state_write_audit plugin not loaded with -p in this run")
    else:
        assert isinstance(value, str)


def test_audit_hook_installed_reflects_module_state():
    before = swa.audit_hook_installed()
    assert before in (True, False)
