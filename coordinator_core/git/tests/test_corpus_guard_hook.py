"""Tests for the self-resolving corpus-guard hook body and the forwarder-stub predicate."""
from __future__ import annotations

import pytest

from coordinator_core.git import corpus_guard_hook as cgh
from pathlib import Path

DOE_STUB = (
    "#!/usr/bin/env python3\n"
    "# engine-owned; forwards to the resolved engine root. Edit in claude-klabauter.\n"
    "import sys, os\n"
    "sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))\n"
    "from _engine_forward import forward\n"
)


def test_stub_predicate_both_sides() -> None:
    assert cgh.is_forwarder_stub(DOE_STUB)
    assert not cgh.is_forwarder_stub("#!/bin/sh\nexit 0\n")
    assert not cgh.is_forwarder_stub("from _engine_forward import forward\n")
    assert not cgh.is_forwarder_stub("Edit in claude-klabauter\n")


def test_hook_body_shape() -> None:
    body = cgh.hook_body(["/a/claude-klabauter", "/b/mirror"])
    assert body.startswith("#!/bin/sh\n")
    assert cgh.STAMP in body
    assert not cgh.is_forwarder_stub(body)
    assert "sys.path" not in body
    assert "_engine_forward" not in body
    assert "pre_commit_corpus_artifact_guard.py" in body


def test_env_root_tried_before_baked_roots() -> None:
    body = cgh.hook_body(["/a/claude-klabauter", "/b/mirror"])
    env_at = body.index("COORDINATOR_ENGINE_ROOT:-")
    assert env_at < body.index("'/a/claude-klabauter'") < body.index("'/b/mirror'")


def test_no_root_fails_loud() -> None:
    body = cgh.hook_body([])
    assert "COORDINATOR_ENGINE_ROOT" in body
    assert "pre_commit_corpus_artifact_guard --install-hook" in body
    assert body.rstrip().endswith("exit 1")


def test_roots_use_forward_slashes_and_quote() -> None:
    body = cgh.hook_body(["x\\y\\it's"])
    assert "'x/y/it'\\''s'" in body


def test_hook_body_refuses_stub_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cgh, "is_forwarder_stub", lambda text: True)
    with pytest.raises(ValueError):
        cgh.hook_body([])


def test_candidate_engine_roots_filters_dedupes_and_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine_tree = tmp_path / "claude-klabauter"
    mirror = tmp_path / "mirror"
    engine_tree.mkdir()
    mirror.mkdir()
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.merged_flat_registry",
        lambda: {"repos.claude_klabauter": str(engine_tree)},
    )
    monkeypatch.setattr(
        "coordinator_core.engine_root.published_engine_mirror_path", lambda: str(mirror)
    )
    assert cgh.candidate_engine_roots() == [
        str(engine_tree).replace("\\", "/"),
        str(mirror).replace("\\", "/"),
    ]
    monkeypatch.setattr(
        "coordinator_core.engine_root.published_engine_mirror_path", lambda: str(engine_tree)
    )
    assert cgh.candidate_engine_roots() == [str(engine_tree).replace("\\", "/")]
    monkeypatch.setattr(
        "coordinator_core.engine_root.published_engine_mirror_path",
        lambda: str(tmp_path / "missing"),
    )
    monkeypatch.setattr(
        "coordinator_core.machine_resolver.merged_flat_registry", lambda: {}
    )
    assert cgh.candidate_engine_roots() == []
