"""checkin_blob_sha adds the bytes it reads to the bound byte counter and nothing else changes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git import commit

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_nested_bind_shares_the_outer_box(tmp_path):
    outer = commit.bind_byte_counter()
    try:
        assert commit.bind_byte_counter() is None
    finally:
        commit.reset_byte_counter(outer)
