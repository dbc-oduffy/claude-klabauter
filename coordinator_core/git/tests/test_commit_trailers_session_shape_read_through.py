"""session-shape.json is opened once per call scope by both trailer readers; pinned by open counts."""

from __future__ import annotations

import builtins
import json
import os

import pytest

from coordinator_core.session_hub_path import session_dir_for

from coordinator_core.git import call_io_scope as cis
from coordinator_core.git import commit_trailers as ct
from pathlib import Path

SID = "sess-1"


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch):
    real = builtins.open
    seen = []

    def counting(file, *a, **kw):
        seen.append(os.fspath(file) if isinstance(file, (str, os.PathLike)) else file)
        return real(file, *a, **kw)

    monkeypatch.setattr(builtins, "open", counting)
    return seen


def _write(tmp_path, text) -> tuple[str, str]:
    d = session_dir_for(tmp_path, SID)
    d.mkdir(parents=True)
    p = d / "session-shape.json"
    p.write_text(text, encoding="utf-8")
    return str(tmp_path), str(p)


def _calls(git_dir, n: int = 4) -> None:
    for _ in range(n):
        ct._resolve_deliverable_id_at(git_dir, SID)
        ct._held_pickup_deliverable_ids(git_dir, SID)


def test_inside_scope_opens_once(tmp_path: Path, opens) -> None:
    git_dir, shape = _write(tmp_path, json.dumps({"pickup": {"deliverable_id": "D1"}}))
    with cis.call_io_scope():
        _calls(git_dir)
    assert opens.count(shape) == 1


def test_outside_scope_opens_each_call(tmp_path: Path, opens) -> None:
    git_dir, shape = _write(tmp_path, json.dumps({"pickup": {"deliverable_id": "D1"}}))
    _calls(git_dir, 3)
    assert opens.count(shape) == 6


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_malformed_fallback_same_inside_and_outside(tmp_path: Path, text) -> None:
    git_dir, _ = _write(tmp_path, text)
    outside = (ct._resolve_deliverable_id_at(git_dir, SID), ct._held_pickup_deliverable_ids(git_dir, SID))
    with cis.call_io_scope():
        inside = (ct._resolve_deliverable_id_at(git_dir, SID), ct._held_pickup_deliverable_ids(git_dir, SID))
    assert outside == inside == ("", [])


def test_missing_file_fallback(tmp_path: Path) -> None:
    with cis.call_io_scope():
        assert ct._resolve_deliverable_id_at(str(tmp_path), SID) == ""
        assert ct._held_pickup_deliverable_ids(str(tmp_path), SID) == []


def test_valid_value_resolves_inside_scope(tmp_path: Path) -> None:
    git_dir, _ = _write(tmp_path, json.dumps({"pickup": {"deliverable_id": " D1 "}}))
    with cis.call_io_scope():
        assert ct._resolve_deliverable_id_at(git_dir, SID) == "D1"
