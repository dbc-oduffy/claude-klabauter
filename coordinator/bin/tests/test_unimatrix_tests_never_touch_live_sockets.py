"""The conftest net refuses any test unlink under the live peer-socket dir."""

from __future__ import annotations

import os

import pytest


def test_unlink_under_live_socket_dir_is_refused():
    with pytest.raises(AssertionError, match="live peer socket"):
        os.unlink("/tmp/cc-socks/cc-msg-never-a-real-one.sock")
    with pytest.raises(AssertionError, match="live peer socket"):
        os.remove("/private/tmp/cc-socks/x.sock")


def test_unlink_elsewhere_still_works(tmp_path):
    f = tmp_path / "x.sock"
    f.write_text("")
    os.unlink(f)
    assert not f.exists()
