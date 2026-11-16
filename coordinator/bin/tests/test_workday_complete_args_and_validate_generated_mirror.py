"""check-cross-machine refuses a repo carrying a .generated-mirror marker."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "workday-complete-args-and-validate.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wcav_under_test", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_refuses_when_marker_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / ".generated-mirror").write_text('{"schema":1}')
    monkeypatch.chdir(tmp_path)
    for args in ("", "--for-date 2026-10-21", "--for-date 2026-10-21 --machine x"):
        assert _load().main(["check-cross-machine", args]) == 1
        assert "REFUSED: generated-mirror" in capsys.readouterr().err


def test_unchanged_when_marker_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    mod = _load()
    assert mod.main(["check-cross-machine", ""]) == 0
    assert mod.main(["check-cross-machine", "--for-date 2026-10-21"]) == 0
    assert "REFUSED" not in capsys.readouterr().err
