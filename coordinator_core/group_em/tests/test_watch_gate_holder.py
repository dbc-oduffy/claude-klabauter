"""The watch gate-holder pattern resolves the Group EM handle from the doctrine repo name."""

import inspect

import pytest

from coordinator_core import _fleet_names
from coordinator_core.group_em import watch
from coordinator_core.roadmap import prep_gate

_DOCTRINE = "-".join(["doe", "claude"])


@pytest.fixture(autouse=True)
def _fresh_cache():
    watch._gate_holder_re.cache_clear()
    yield
    watch._gate_holder_re.cache_clear()


def _gated(monkeypatch, name, phrase):
    monkeypatch.setattr(_fleet_names, "doctrine_repo_name", lambda: name)
    watch._gate_holder_re.cache_clear()
    return bool(watch._gate_holder_re().search(phrase))


def test_letter_prefixed_handle_is_gated(monkeypatch):
    assert _gated(monkeypatch, _DOCTRINE, f"Waiting on {_DOCTRINE}-e4 before I fire it.")


def test_numeric_handle_is_gated(monkeypatch):
    assert _gated(monkeypatch, _DOCTRINE, f"Waiting on {_DOCTRINE}-65 before I fire it.")


def test_handle_follows_the_resolved_name(monkeypatch):
    assert _gated(monkeypatch, "coordinator-claude", "blocked on coordinator-claude-12 today")
    assert not _gated(monkeypatch, "coordinator-claude", f"blocked on {_DOCTRINE}-12 today")


def test_other_repo_suffix_is_not_gated(monkeypatch):
    assert not _gated(monkeypatch, _DOCTRINE, "blocked on example-retrieval-repo-3 today")


def test_none_name_keeps_pm_and_drops_handle(monkeypatch):
    assert _gated(monkeypatch, None, "needs the PM to look")
    assert not _gated(monkeypatch, None, f"blocked on {_DOCTRINE}-e4 today")


def test_builder_source_carries_no_fleet_repo_name():
    source = inspect.getsource(watch._gate_holder_re).casefold()
    assert not [n for n in prep_gate.FLEET_REPOS if n.casefold() in source]
