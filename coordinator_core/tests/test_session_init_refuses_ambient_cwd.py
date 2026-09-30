"""``session.core.init`` never resolves the session hub from the process cwd.

Under pytest the process cwd is the real repo root, so an ``init(sid)`` with no
``cwd`` would mint ``<repo>/.git/coordinator-sessions/<sid>`` in the live hub
whatever tmp repo the test had built. The guard is a raise, not a ``False``
return: ``False`` already means "not in a git repo" and callers treat it as a
quiet no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.session import core


def test_init_without_cwd_or_sessions_base_raises_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError):
        core.init("ambient-cwd-fixture")
    with pytest.raises(ValueError):
        core.init("ambient-cwd-fixture", cwd="")
    assert not (tmp_path / ".git" / "coordinator-sessions").exists()


def test_init_with_explicit_sessions_base_needs_no_cwd(tmp_path):
    base = tmp_path / "hub"
    base.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    core.init("explicit-base-fixture", sessions_base=str(base), root=str(root))
    assert (Path(base) / "explicit-base-fixture").is_dir()
