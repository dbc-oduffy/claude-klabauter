
from __future__ import annotations

import pytest

from coordinator_core.warm import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(settings.ENV_VAR, raising=False)
    settings._reset_for_test()
    settings._reset_warm_disabled_announcement_for_test()
    yield
    settings._reset_for_test()
    settings._reset_warm_disabled_announcement_for_test()


def test_off_when_neither_env_nor_registry_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "registry_get", lambda key: None)
    assert settings.is_warm_enabled() is False


def test_registry_key_enables_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "registry_get", lambda key: "true" if key == settings.REGISTRY_KEY else None
    )
    assert settings.is_warm_enabled() is True


def test_env_zero_always_wins_over_registry_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "0")
    monkeypatch.setattr(
        settings, "registry_get", lambda key: "true" if key == settings.REGISTRY_KEY else None
    )
    assert settings.is_warm_enabled() is False


def test_env_false_token_also_wins_over_registry_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "false")
    monkeypatch.setattr(
        settings, "registry_get", lambda key: "true" if key == settings.REGISTRY_KEY else None
    )
    assert settings.is_warm_enabled() is False


def test_env_truthy_wins_over_registry_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "1")
    monkeypatch.setattr(settings, "registry_get", lambda key: None)
    assert settings.is_warm_enabled() is True


def test_unrecognized_env_value_falls_through_to_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "banana")
    monkeypatch.setattr(
        settings, "registry_get", lambda key: "true" if key == settings.REGISTRY_KEY else None
    )
    assert settings.is_warm_enabled() is True


def test_registry_value_is_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings, "registry_get", lambda key: "TRUE" if key == settings.REGISTRY_KEY else None
    )
    assert settings.is_warm_enabled() is True


def test_warm_disabled_via_env_announces_once(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "0")
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    assert settings.is_warm_enabled() is False
    assert settings.is_warm_enabled() is False
    assert settings.is_warm_enabled() is False

    err = capsys.readouterr().err
    assert err.count("warmth is disabled by configuration") == 1
    assert "every dispatch from this process runs cold" in err


def test_warm_disabled_via_registry_announces_once(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    assert settings.is_warm_enabled() is False
    assert settings.is_warm_enabled() is False

    err = capsys.readouterr().err
    assert err.count("warmth is disabled by configuration") == 1


def test_warm_enabled_never_announces(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "1")

    assert settings.is_warm_enabled() is True

    err = capsys.readouterr().err
    assert err == ""


def test_disabled_does_not_fail_only_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "off")
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    result = settings.is_warm_enabled()

    assert result is False
