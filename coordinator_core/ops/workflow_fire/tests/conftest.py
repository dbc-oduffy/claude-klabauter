"""Every headless fire in this suite resolves a published engine root.

The worker refuses to start without a published stamped root (`headless._worker_engine_root`), and
a test box need not have one registered. This source tree stands in for it, so a spawned worker can
still import the engine. A test about root resolution sets its own resolver over this one.
"""
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _published_engine_root(monkeypatch):
    from coordinator_core.install.engine_root_for_install import InstallEngineRoot

    resolved = InstallEngineRoot('published', _SOURCE_ROOT, None)
    monkeypatch.setattr('coordinator_core.install.engine_root_for_install.resolve_engine_root_for_install',
                        lambda: resolved)
