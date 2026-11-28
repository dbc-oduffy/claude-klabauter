"""A verify's pytest child imports `coordinator_core` from the mirror even when the engine's own environment names
another checkout (the editable install) as the engine root."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import pyresolve
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _isolated_system_temp, _repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_PROBE_TEST = "t/test_mirror_symbol.py"

# The probe replays what a `coordinator/bin` script does on import (front-insert the env-named engine root) after
# an editable install's `_EditableFinder`-style finder is on `sys.meta_path`.
_PROBE_SRC = '''\
import importlib.machinery
import os
import sys

FOREIGN = os.environ["FOREIGN_CHECKOUT"]


class _EditableFinder:
    @classmethod
    def find_spec(cls, fullname, path=None, target=None):
        if fullname == "coordinator_core":
            return importlib.machinery.PathFinder.find_spec(fullname, path=[FOREIGN])
        return None


sys.meta_path.append(_EditableFinder)
sys.path.append(FOREIGN)
sys.path.insert(0, os.environ["COORDINATOR_ENGINE_ROOT"])

import coordinator_core


def test_the_mirror_only_symbol_is_seen():
    assert coordinator_core.MIRROR_ONLY == "mirror"
'''


def _foreign_checkout(tmp_path: Path) -> Path:
    foreign = tmp_path / "foreign"
    _write(foreign, "coordinator_core/__init__.py", "FOREIGN_ONLY = True\n")
    return foreign


def test_a_mirror_run_sees_a_symbol_present_only_in_the_mirror(tmp_path, monkeypatch):
    root, _head = _repo(tmp_path)
    _write(root, "coordinator_core/__init__.py", "MIRROR_ONLY = 'mirror'\n")
    _write(root, _PROBE_TEST, _PROBE_SRC)
    _git(root, "add", "coordinator_core/__init__.py", _PROBE_TEST)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "probe")
    head = _git(root, "rev-parse", "HEAD").strip()
    foreign = _foreign_checkout(tmp_path)
    monkeypatch.setenv("FOREIGN_CHECKOUT", str(foreign))
    monkeypatch.setenv(pyresolve.ENGINE_ROOT_ENV, str(foreign))

    out = cov.verify(root, "run1", head, [_PROBE_TEST])

    assert out.verdict == "green", out


def test_the_mirror_env_names_the_mirror_as_the_engine_root_whatever_the_caller_inherited(tmp_path):
    dest = tmp_path / "mirror"
    _write(dest, "coordinator_core/__init__.py", "")
    env = cov._mirror_env({pyresolve.ENGINE_ROOT_ENV: str(_foreign_checkout(tmp_path))}, dest)

    assert env[pyresolve.ENGINE_ROOT_ENV] == str(dest)
    assert pyresolve.engine_root(env) == str(dest)
