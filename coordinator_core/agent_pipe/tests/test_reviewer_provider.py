import pytest

from coordinator_core.agent_pipe.reviewer_provider import (
    OPUS_EQUIVALENT_MODELS,
    PROVIDER_ENV,
    ProviderChoice,
    resolve_reviewer_provider,
)


def test_unset_is_none(monkeypatch):
    monkeypatch.delenv(PROVIDER_ENV, raising=False)
    assert resolve_reviewer_provider("coordinator:staff-eng") is None


def test_exact_entry_maps_to_opus_equivalent(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:staff-eng=codex")
    assert resolve_reviewer_provider("coordinator:staff-eng") == ProviderChoice(
        "codex", OPUS_EQUIVALENT_MODELS["codex"]
    )


def test_unlisted_agent_stays_claude(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "coordinator:staff-eng=codex")
    assert resolve_reviewer_provider("coordinator:other") is None


def test_wildcard_and_exact_precedence(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=grok, coordinator:staff-eng=agy")
    assert resolve_reviewer_provider("coordinator:staff-eng").provider == "agy"
    assert resolve_reviewer_provider("coordinator:other").provider == "grok"


def test_unknown_provider_raises(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "*=mistral")
    with pytest.raises(ValueError):
        resolve_reviewer_provider("coordinator:staff-eng")


def test_malformed_entries_ignored(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "garbage,=codex,x=")
    assert resolve_reviewer_provider("x") is None
