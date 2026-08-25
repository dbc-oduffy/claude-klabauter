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
    # W12 (2026-08-20-a-refusal-cannot-exit-zero § C8): is_warm_enabled()
    # now memoises its registry-rung result process-wide. Without this
    # reset, whichever test in this module runs first would freeze
    # _cached_result for every test that runs after it in the same pytest
    # process, silently defeating each test's own registry_get monkeypatch.
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


# ---------------------------------------------------------------------------
# Warm-disabled announcement (state/handoffs/2026-08-21_103635_reaching-the-
# warm-engine.md) -- a deliberate off-switch must not FAIL a dispatch (that
# would make the switch useless), but it must not be SILENT either, mirroring
# `warm.client._log_live_tree_cold_once`'s own once-per-process shape.
# ---------------------------------------------------------------------------


def test_warm_disabled_via_env_announces_once(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "0")
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    assert settings.is_warm_enabled() is False
    assert settings.is_warm_enabled() is False  # second call: no second notice
    assert settings.is_warm_enabled() is False  # third call: still no second notice

    err = capsys.readouterr().err
    assert err.count("warmth is disabled by configuration") == 1
    assert "every dispatch from this process runs cold" in err


def test_warm_disabled_via_registry_announces_once(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    assert settings.is_warm_enabled() is False
    assert settings.is_warm_enabled() is False  # memoised rung: still no second notice

    err = capsys.readouterr().err
    assert err.count("warmth is disabled by configuration") == 1


def test_warm_enabled_never_announces(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv(settings.ENV_VAR, "1")

    assert settings.is_warm_enabled() is True

    err = capsys.readouterr().err
    assert err == ""


def test_disabled_does_not_fail_only_announces(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE POLICY ITSELF: a deliberate off-switch must return `False`
    cleanly, never raise -- refusing here would make the switch useless,
    per the PM's own framing."""
    monkeypatch.setenv(settings.ENV_VAR, "off")
    monkeypatch.setattr(settings, "registry_get", lambda key: None)

    result = settings.is_warm_enabled()  # must not raise

    assert result is False
