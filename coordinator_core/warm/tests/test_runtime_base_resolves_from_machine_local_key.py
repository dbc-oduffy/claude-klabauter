"""The warm runtime base resolves from the settings-home machine-local key.

Order, shared with door.c and door_posix.c: `COORDINATOR_WARM_RUNTIME_BASE`
> settings-home `machine-local/.coordinator-warm-runtime-base` > LOCALAPPDATA
> ~/.cache.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb

_DOOR_DIR = Path(breadcrumb.__file__).resolve().parent / "door"


@pytest.fixture
def settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.delenv(breadcrumb.RUNTIME_BASE_ENV, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))
    return home


def _write_key(home: Path, text: str) -> None:
    (home / "machine-local" / breadcrumb.RUNTIME_BASE_KEY_FILE).write_text(text, encoding="utf-8")


def test_key_file_moves_the_runtime_dir(settings_home: Path, tmp_path: Path) -> None:
    target = tmp_path / "d-drive" / "warm-runtime"
    _write_key(settings_home, f"{target}\n")
    assert breadcrumb.runtime_base() == target
    svc = breadcrumb.svc_dir(tmp_path / "clone")
    assert target in svc.parents
    assert (tmp_path / "local-app-data") not in svc.parents


def test_unset_key_falls_back_to_localappdata(settings_home: Path, tmp_path: Path) -> None:
    assert breadcrumb.runtime_base() == tmp_path / "local-app-data"


@pytest.mark.parametrize("text", ["", "   \n", "relative/dir"])
def test_blank_or_relative_key_reads_as_unset(settings_home: Path, tmp_path: Path, text: str) -> None:
    _write_key(settings_home, text)
    assert breadcrumb.runtime_base() == tmp_path / "local-app-data"


def test_env_seam_outranks_the_key(settings_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_key(settings_home, str(tmp_path / "keyed"))
    seam = tmp_path / "seam"
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(seam))
    assert breadcrumb.runtime_base() == seam


def test_both_door_sources_read_the_same_key_file() -> None:
    for name in ("door.c", "door_posix.c"):
        text = (_DOOR_DIR / name).read_text(encoding="utf-8")
        code = [ln for ln in text.splitlines() if not ln.lstrip().startswith(("/*", "*", "//"))]
        assert any(breadcrumb.RUNTIME_BASE_KEY_FILE in ln for ln in code), name
