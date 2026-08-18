"""Tests for `coordinator_core.warm.settings` — the per-machine warm-engine
opt-in's resolution precedence.

Purpose: C23 of `docs/plans/2026-08-16-one-engine-for-the-whole-box.md`.
Exercises the three-rung precedence directly against `is_warm_enabled`,
stubbing `registry_get` rather than touching the real machine-local TOML
registry — this module's own contract is the precedence order, not the
registry's read mechanics (those are `coordinator_core.machine_resolver`'s
own tests).

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C23
"""

from __future__ import annotations

import pytest

from coordinator_core.warm import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(settings.ENV_VAR, raising=False)


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
