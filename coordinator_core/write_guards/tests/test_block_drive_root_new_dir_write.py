"""Behavioral tests for the drive-root / home new-top-level Write/Edit guard, driven through ``check()``."""

from __future__ import annotations

import pytest

from coordinator_core.write_guards import block_drive_root_new_dir_write as guard
from coordinator_core.write_guards import block_home_root_write


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (root / "existing").mkdir()
    monkeypatch.setenv("COORDINATOR_DRIVE_ROOTS", str(root))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))
    return root, home


def _payload(path, tool="Write", key="file_path", **extra):
    return {"tool_name": tool, "tool_input": {key: str(path)}, **extra}


def _reason(out):
    body = out["hookSpecificOutput"]
    assert body["permissionDecision"] == "deny"
    return body["permissionDecisionReason"]


def test_write_to_new_top_level_is_denied_naming_scratch(world):
    root, _ = world
    reason = _reason(guard.check(_payload(root / "fresh" / "f.txt")))
    assert "<repo>/scratch/" in reason
    assert "fresh" in reason
    assert "COORDINATOR_" not in reason


@pytest.mark.parametrize("tool,key", [("Edit", "file_path"), ("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_other_tools_are_judged(world, tool, key):
    root, _ = world
    assert guard.check(_payload(root / "fresh" / "n.ipynb", tool, key)) is not None


def test_existing_top_level_and_nested_allowed(world):
    root, _ = world
    assert guard.check(_payload(root / "existing" / "f.txt")) is None
    assert guard.check(_payload(root / "existing" / "a" / "b" / "f.txt")) is None


def test_placed_checkout_allowed(world):
    root, _ = world
    (root / "placed" / ".git").mkdir(parents=True)
    assert guard.check(_payload(root / "placed" / "src" / "f.py")) is None


def test_payload_env_override_allows(world):
    root, _ = world
    payload = _payload(root / "fresh" / "f.txt", env={"COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR": "1"})
    assert guard.check(payload) is None


def test_home_new_first_component_denied_but_depth_one_left_to_home_pair(world):
    _, home = world
    assert guard.check(_payload(home / "newdir" / "f.txt")) is not None
    target = home / "fo.txt"
    assert guard.check(_payload(target)) is None
    assert block_home_root_write.check(_payload(target)) is not None


def test_malformed_payload_allows(world):
    assert guard.check({"tool_input": "nope"}) is None
    assert guard.check({}) is None
    assert guard.check({"tool_input": {"file_path": ""}}) is None
