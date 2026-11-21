"""percolate-round runs its code and publisher chain from a shared commit clone at the round's commit.

Driven through `main()` (default arguments) on a tmp_path repo whose publisher-chain file differs
from its HEAD; the clone cache lives under tmp_path.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest


pytestmark = pytest.mark.complete

_BIN_DIR = Path(__file__).resolve().parents[1]
_CHAIN_FILES = (
    "coordinator/bin/publish.py",
    "coordinator/bin/percolate-gate.py",
    "coordinator/bin/percolate-push.py",
    "coordinator/bin/percolate-parse-dryrun.py",
    "coordinator/bin/publish-allowlist-generate.py",
)
_ENTRY = "coordinator/bin/percolate-round.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def rnd():
    return _load(_BIN_DIR / "percolate-round.py", "percolate_round_commit_clone")


@pytest.fixture(autouse=True)
def _managed_temp_under_tmp_path(tmp_path, monkeypatch):
    """The clone cache lands under the managed temp dir; keep it in tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()


def test_the_retired_git_archive_chain_export_is_gone(rnd):
    for name in ("_pin_publisher_chain", "_clean_export_publish_cmd", "_PINNED_ROOT", "_bind_chain"):
        assert not hasattr(rnd, name), name
