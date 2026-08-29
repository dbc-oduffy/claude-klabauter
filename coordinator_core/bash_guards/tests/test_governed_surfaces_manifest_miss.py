"""Regression cover for `dispatch.resolve_governed_authoring_surfaces`'s
miss-handling (state/bug-backlog/2026-08-29-the-guard-rehome-is-not-yet-safe-
to-dele-9f7396118b81.yaml, gap 2).

WHY THIS FILE EXISTS. Cold imports `GOVERNED_AUTHORING_SURFACES` from
`_claude_md_ledger`, so it structurally cannot lose its identifier list. The
port resolves a per-call manifest from `<plugin_root>` instead, and a miss
(absent file, unreadable, bad JSON, wrong shape) used to return `None`
silently -- indistinguishable from `guard-doctrine-surface-bash-write`
correctly declining because it genuinely has nothing to deny. Measured with
a plugin_root holding no manifest: cold DENY, warm ALLOW. This file pins the
fix -- each miss kind is now loud (one stderr line) and counted (one
`record_advisory_fire` call) -- and that the ROOT miss (already loud via
`resolve_plugin_root_loud`) is never double-counted here.

NEGATIVE SPEC. The load-bearing property is that a miss NEVER DENIES and
NEVER RAISES, same as `resolve_plugin_root_loud`'s own contract -- a hard
deny on a manifest miss would brick Bash on any install with no manifest at
all. The `returns None`/`does not raise` assertions below are not restating
the type signature; they pin the choice against a future "harden this into a
deny" edit that would look defensible in review.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from coordinator_core.bash_guards import dispatch


@pytest.fixture
def recorded_fires(monkeypatch: pytest.MonkeyPatch) -> List[Tuple[Any, ...]]:
    """Capture `record_advisory_fire` calls without touching the real counter."""
    calls: List[Tuple[Any, ...]] = []

    def _fake(*args: Any, **kwargs: Any) -> None:
        calls.append(args)

    monkeypatch.setattr(dispatch, "_record_advisory_fire", _fake)
    return calls


class TestRootMissIsNotDoubleCounted:
    def test_plugin_root_none_returns_none_silently_here(
        self, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        """`resolve_plugin_root_loud` already spoke for a None plugin_root --
        this function must not repeat the complaint or the counter."""
        result = dispatch.resolve_governed_authoring_surfaces(None, "sess", "/cwd")
        assert result is None
        assert capsys.readouterr().err == ""
        assert recorded_fires == []


class TestMissKindsAreLoudAndCounted:
    def test_absent_file_is_loud_and_counted(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result is None
        err = capsys.readouterr().err
        assert "absent" in err
        assert len(recorded_fires) == 1
        assert recorded_fires[0][0] == dispatch._GOVERNED_MANIFEST_UNREADABLE_GUARD_NAME

    def test_bad_json_is_loud_and_counted(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text("{not valid json", encoding="utf-8")
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result is None
        err = capsys.readouterr().err
        assert "not valid JSON" in err
        assert len(recorded_fires) == 1

    def test_wrong_shape_not_a_list_is_loud_and_counted(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result is None
        err = capsys.readouterr().err
        assert "not a list" in err
        assert len(recorded_fires) == 1

    def test_wrong_shape_non_string_entries_is_loud_and_counted(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text(json.dumps(["ok.md", 7]), encoding="utf-8")
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result is None
        err = capsys.readouterr().err
        assert "non-string" in err
        assert len(recorded_fires) == 1

    def test_no_miss_kind_raises(self, tmp_path: Path) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        for content in ("{not json", json.dumps({"x": 1}), json.dumps([1, 2])):
            manifest.write_text(content, encoding="utf-8")
            dispatch.resolve_governed_authoring_surfaces(str(tmp_path), "sess", "/cwd")
        manifest.unlink()
        dispatch.resolve_governed_authoring_surfaces(str(tmp_path), "sess", "/cwd")


class TestHitIsSilent:
    def test_hit_returns_the_list_unchanged(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text(json.dumps(["CLAUDE.md", "docs/wiki/x.md"]), encoding="utf-8")
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result == ["CLAUDE.md", "docs/wiki/x.md"]

    def test_hit_is_silent_and_counts_nothing(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]], capsys
    ) -> None:
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text(json.dumps([]), encoding="utf-8")
        dispatch.resolve_governed_authoring_surfaces(str(tmp_path), "sess", "/cwd")
        assert capsys.readouterr().err == ""
        assert recorded_fires == []

    def test_hit_empty_list_is_a_real_answer_not_a_miss(
        self, tmp_path: Path, recorded_fires: List[Tuple[Any, ...]]
    ) -> None:
        """An explicit empty list means 'this install governs no surfaces' --
        a real answer, distinct from a read failure."""
        manifest = tmp_path / dispatch._GOVERNED_AUTHORING_SURFACES_MANIFEST_NAME
        manifest.write_text(json.dumps([]), encoding="utf-8")
        result = dispatch.resolve_governed_authoring_surfaces(
            str(tmp_path), "sess", "/cwd"
        )
        assert result == []
        assert recorded_fires == []
