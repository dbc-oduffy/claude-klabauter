"""A fresh cloud container's missing install-currency cache reads as expected, not absent."""
from __future__ import annotations

from coordinator_core.hooks import project_orientation as po
from pathlib import Path
import pytest


def _banner(monkeypatch, tmp_path, env) -> str:
    monkeypatch.setattr(po, "home_dir", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    out: list[str] = []
    po._install_currency_banner(out, env)
    return "".join(out)


def test_cloud_container_reads_not_yet_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text = _banner(monkeypatch, tmp_path, {"CLAUDE_CODE_REMOTE": "true"})
    assert "not yet recorded on this cloud container" in text and "absent" not in text


def test_desk_still_reads_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert "Install currency: absent" in _banner(monkeypatch, tmp_path, {})
