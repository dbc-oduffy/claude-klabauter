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


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "fold_bar_real: exercise the real fold bar; the green-verdict stub skips this test"
    )


@pytest.fixture(autouse=True)
def _stub_fold_bar_verdict(request, monkeypatch):
    """Round and publish tests drive fake trees with no recorded suite verdict. A module sets
    REAL_FOLD_BAR = True, or a test carries the fold_bar_real marker, to exercise the real fold bar."""
    if getattr(request.module, "REAL_FOLD_BAR", False) or request.node.get_closest_marker("fold_bar_real"):
        return
    try:
        from coordinator_core.ops import fold_bar
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("coordinator_core"):
            raise
        return

    monkeypatch.setattr(fold_bar, "verdict", lambda sha, repo_root: fold_bar.FoldBarResult(True, sha))

    # Fixture source dirs carry no commit; the shipped-sha rev-parse in _resolve_shipped_sha would
    # refuse before the stubbed verdict runs. Only that one call shape falls back to a fixed sha
    # (every such rev-parse in these tests, not just the fold bar's).
    from coordinator_core.git import run as git_run

    real_git_out = git_run.git_out

    def _git_out_with_shipped_sha_fallback(args, **kwargs):
        argv = list(args)
        if argv[:3] == ["rev-parse", "--verify", "--quiet"] and argv[-1:] and argv[-1].endswith("^{commit}"):
            return "0" * 40
        return real_git_out(args, **kwargs)

    monkeypatch.setattr(git_run, "git_out", _git_out_with_shipped_sha_fallback)


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
