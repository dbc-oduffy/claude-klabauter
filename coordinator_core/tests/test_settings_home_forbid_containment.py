"""The forbidden-real-home guard refuses the home and anything beneath it."""

import pytest

from coordinator_core._settings_home import (
    FORBID_REAL_SETTINGS_HOME_ENV,
    RealSettingsHomeLeakError,
    _check_not_the_forbidden_real_home,
)


def test_child_path_raises(tmp_path, monkeypatch):
    real = tmp_path / "real" / ".coordinator-claude-settings"
    monkeypatch.setenv(FORBID_REAL_SETTINGS_HOME_ENV, str(real))
    with pytest.raises(RealSettingsHomeLeakError):
        _check_not_the_forbidden_real_home(real / "machine-local" / "engine-records")
    with pytest.raises(RealSettingsHomeLeakError):
        _check_not_the_forbidden_real_home(real)


def test_sibling_and_unset_pass(tmp_path, monkeypatch):
    real = tmp_path / "real" / ".coordinator-claude-settings"
    monkeypatch.setenv(FORBID_REAL_SETTINGS_HOME_ENV, str(real))
    _check_not_the_forbidden_real_home(tmp_path / "other" / ".coordinator-claude-settings")
    _check_not_the_forbidden_real_home(tmp_path / "real" / ".coordinator-claude-settings-x")
    monkeypatch.delenv(FORBID_REAL_SETTINGS_HOME_ENV)
    _check_not_the_forbidden_real_home(real / "child")
