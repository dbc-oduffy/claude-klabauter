"""Tests for the published-engine-mirror write advisory."""

from __future__ import annotations

import pytest

from coordinator_core import engine_root
from coordinator_core.write_guards import block_published_engine_mirror_write as guard

MIRROR = "/fake/claude-klabauter"
SOURCE = "/fake/claude-klabauter"
REL = "coordinator_core/ops/dispatch_emit/plan_blitz_args.py"


@pytest.fixture
def roots(monkeypatch):
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: MIRROR)
    monkeypatch.setattr(engine_root, "engine_source_root", lambda: SOURCE)


def _payload(path, tool="Edit"):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "tool_input": {key: path}}


def _context(envelope):
    return envelope["hookSpecificOutput"]["additionalContext"]


def test_class_is_advisory():
    assert guard.CLASS == "advisory"


def test_mirror_write_names_source_twin_and_does_not_deny(roots):
    out = guard.check(_payload(f"{MIRROR}/{REL}"))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert f"{SOURCE}/{REL}" in hso["additionalContext"]


def test_backslash_and_case_variants_still_match(roots):
    out = guard.check(_payload("\\FAKE\\claude-klabauter\\" + REL.replace("/", "\\"), tool="Write"))
    assert out is not None
    assert f"{SOURCE}/{REL}" in _context(out)


def test_notebook_path_is_read(roots):
    assert guard.check(_payload(f"{MIRROR}/x.ipynb", tool="NotebookEdit")) is not None


def test_path_outside_mirror_is_silent(roots):
    assert guard.check(_payload(f"{SOURCE}/{REL}")) is None


def test_sibling_prefix_is_not_the_mirror(roots):
    assert guard.check(_payload(f"{MIRROR}-extra/{REL}")) is None


def test_no_mirror_registered_is_silent(monkeypatch):
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: None)
    assert guard.check(_payload(f"{MIRROR}/{REL}")) is None


def test_no_source_root_names_relative_path(monkeypatch):
    monkeypatch.setattr(engine_root, "published_engine_mirror_path", lambda: MIRROR)
    monkeypatch.setattr(engine_root, "engine_source_root", lambda: None)
    out = guard.check(_payload(f"{MIRROR}/{REL}"))
    assert out is not None
    assert f"{REL} in the source repo checkout" in _context(out)


def test_malformed_payload_is_silent(roots):
    assert guard.check({}) is None
    assert guard.check({"tool_input": "nope"}) is None
    assert guard.check({"tool_input": {"file_path": 7}}) is None


def test_registry_error_is_silent(monkeypatch):
    def boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(engine_root, "published_engine_mirror_path", boom)
    assert guard.check(_payload(f"{MIRROR}/{REL}")) is None


def test_engine_dispatch_surfaces_advisory_for_mirror_write(roots, tmp_path):
    from coordinator_core.write_guards import engine

    out = engine.evaluate(_payload(f"{MIRROR}/{REL}"), cwd=str(tmp_path))
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert f"{SOURCE}/{REL}" in hso["additionalContext"]
