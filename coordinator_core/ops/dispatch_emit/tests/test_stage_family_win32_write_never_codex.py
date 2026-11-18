"""On win32 a write family stage never selects codex; read-only codex stages and other platforms keep it."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import stage_family


def test_win32_write_stage_naming_codex_is_rerouted_with_a_reason(monkeypatch):
    monkeypatch.setattr(stage_family.sys, "platform", "win32")
    family, reason = stage_family.resolve_for_stage("codex", "write")
    assert family != "codex" and family in stage_family.FAMILIES
    assert reason == stage_family.WIN32_CODEX_WRITE_REASON and "win32" in reason


@pytest.mark.parametrize("stage", ["scout", "research", "review"])
def test_win32_read_only_codex_stage_still_resolves_to_codex(monkeypatch, stage):
    monkeypatch.setattr(stage_family.sys, "platform", "win32")
    assert stage_family.resolve_for_stage("codex", stage) == ("codex", None)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_non_win32_write_stage_keeps_codex(monkeypatch, platform):
    monkeypatch.setattr(stage_family.sys, "platform", platform)
    assert stage_family.resolve_for_stage("codex", "write") == ("codex", None)


@pytest.mark.parametrize("family", ["agy", "grok"])
def test_win32_write_stage_of_another_family_is_untouched(monkeypatch, family):
    monkeypatch.setattr(stage_family.sys, "platform", "win32")
    assert stage_family.resolve_for_stage(family, "write") == (family, None)


def test_unknown_family_still_raises(monkeypatch):
    monkeypatch.setattr(stage_family.sys, "platform", "win32")
    with pytest.raises(ValueError):
        stage_family.resolve_for_stage("gemini", "write")
