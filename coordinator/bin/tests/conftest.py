from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_stamp_probe import (  # noqa: E402  (import after path setup)
    _ENGINE_ROOT_VAR,
    _stamped_dispatch_root,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent


@pytest.fixture
def stamped_engine_env(monkeypatch) -> str:
    root = _stamped_dispatch_root()
    if root is None:
        pytest.skip(
            f"no stamped engine on this box ({_ENGINE_ROOT_VAR} unresolvable or "
            "the resolved root carries no build stamp) — a real dispatch cannot "
            "be exercised here"
        )
    monkeypatch.setenv(_ENGINE_ROOT_VAR, root)
    return root


@pytest.fixture(autouse=True)
def _stub_source_preflight(request, monkeypatch):
    """The round's source-published gate runs real git and a real push; the
    round tests fake the source dir. A module sets REAL_SOURCE_PREFLIGHT = True
    to exercise the real gate."""
    if getattr(request.module, "REAL_SOURCE_PREFLIGHT", False):
        return
    try:
        from coordinator_core.percolate import source_preflight
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("coordinator_core"):
            raise
        return

    monkeypatch.setattr(source_preflight, "check_source_published", lambda _source_dir, *_a, **_k: None)
