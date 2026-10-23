"""A SIGTERMed publish round leaves no publish-staging-* directory beside its destination.

Run: python -m pytest coordinator/bin/tests/test_publish_staging_removed_on_sigterm.py -x -q
"""

from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.cadence

_BIN_DIR = Path(__file__).resolve().parent.parent

needs_posix_signals = pytest.mark.skipif(sys.platform == "win32", reason="os.kill SIGTERM is POSIX-only")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_staging_sigterm_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


@needs_posix_signals
def test_sigterm_during_main_removes_minted_staging_dirs(tmp_path, monkeypatch):
    dest = tmp_path / "mirror"
    dest.mkdir()
    minted = []

    def fake_main(argv=None):
        minted.append(publish._create_publish_staging_dir(dest))
        minted.append(publish._create_publish_staging_dir(dest))
        assert all(p.is_dir() for p in minted)
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    monkeypatch.setattr(publish, "_main", fake_main)
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as exc:
        publish.main([])
    assert exc.value.code == 128 + signal.SIGTERM
    assert len(minted) == 2
    assert not any(p.exists() for p in minted)
    assert list(tmp_path.glob(".mirror.publish-staging-*")) == []
    assert signal.getsignal(signal.SIGTERM) == before


@needs_posix_signals
def test_unrelated_dirs_survive_sigterm(tmp_path, monkeypatch):
    dest = tmp_path / "mirror"
    dest.mkdir()
    other = tmp_path / ".other.publish-staging-keep"
    other.mkdir()

    def fake_main(argv=None):
        publish._create_publish_staging_dir(dest)
        os.kill(os.getpid(), signal.SIGTERM)
        return 0

    monkeypatch.setattr(publish, "_main", fake_main)
    with pytest.raises(SystemExit):
        publish.main([])
    assert other.is_dir()
    assert list(tmp_path.glob(".mirror.publish-staging-*")) == []


def test_normal_exit_restores_handler_and_keeps_return(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_main", lambda argv=None: 7)
    before = signal.getsignal(signal.SIGTERM)
    assert publish.main([]) == 7
    assert signal.getsignal(signal.SIGTERM) == before
