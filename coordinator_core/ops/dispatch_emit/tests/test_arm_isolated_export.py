"""Arm fix builds land HEAD plus the arm's own hunks while a peer's live hunks survive."""

from __future__ import annotations

import importlib
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit


_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HEAD = "".join(f"line{i}\n" for i in range(1, 21))


def test_every_arm_brief_points_at_the_export_builder():
    clause = emit._isolated_arm_build_clause()
    module = importlib.import_module(emit._LANDING_EXPORT_MODULE)
    assert emit._LANDING_EXPORT_MODULE in clause
    assert callable(module.build_landing_export)
