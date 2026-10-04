"""`publish.py :: assert_authored_parity` -- a `source_map` row refuses to publish a status line
that differs from the one authored in the row's own source tree.

Run: python -m pytest coordinator/bin/tests/test_publish_authored_parity.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load("publish_authored_parity_under_test", "publish.py")


def _trees(tmp_path: Path, authored: bytes, shipped: bytes | None):
    doe, engine = tmp_path / "doe", tmp_path / "engine"
    (doe / "bin").mkdir(parents=True)
    (engine / "bin").mkdir(parents=True)
    (doe / "bin" / "statusline.py").write_bytes(authored)
    if shipped is not None:
        (engine / "bin" / "statusline.py").write_bytes(shipped)
    return doe, engine


def _target(doe: Path, source_map: str):
    return publish.ResolvedTarget(name="coordinator-claude", mode="mirror", source_dir=doe,
                                  dest_dir=doe.parent / "dest", source_map=source_map)


def test_identical_copies_pass(tmp_path):
    doe, engine = _trees(tmp_path, b"rich\n", b"rich\n")
    publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))


def test_drifted_copy_refuses_the_row(tmp_path):
    doe, engine = _trees(tmp_path, b"rich\n", b"stripped\n")
    with pytest.raises(publish.EngineUnavailableError, match="bin/statusline.py"):
        publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))


def test_missing_shipped_copy_refuses_the_row(tmp_path):
    doe, engine = _trees(tmp_path, b"rich\n", None)
    with pytest.raises(publish.EngineUnavailableError, match="absent"):
        publish.assert_authored_parity(_target(doe, f"{engine}=bin,lib"))


def test_single_source_row_publishes_the_authored_file_itself(tmp_path):
    doe, _engine = _trees(tmp_path, b"rich\n", b"stripped\n")
    publish.assert_authored_parity(_target(doe, ""))
