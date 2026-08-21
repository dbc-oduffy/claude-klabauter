"""Tests for `coordinator_core.ops.emission_publish` -- the C4 handler wiring identity
(C1), envelope-splice (C2) and transport (C3) together, plus its CLI trampoline
(`coordinator/bin/publish-emission.py`) and its `authz/classification.py` entry (AC14).

No network, no real disk artifact of any meaningful size -- every collaborator
(`resolve_repo_name`, `splice_publish_envelope`, `publish_document`, `publish_doc_id`,
`repo_slug`) is monkeypatched at its call site inside `emission_publish`, mirroring the
C1/C2/C3 test suites' own no-network, no-parse posture.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import emission_publish as ep

_BIN_DIR = Path(__file__).resolve().parents[4] / "coordinator" / "bin"
_TRAMPOLINE_PATH = _BIN_DIR / "publish-emission.py"

_LIB_DIR = str(_BIN_DIR / "lib")


@pytest.fixture(autouse=True)
def _clear_inflight():
    ep._INFLIGHT_REPOS.clear()
    yield
    ep._INFLIGHT_REPOS.clear()


@pytest.fixture
def _wired(monkeypatch, tmp_path):
    """Wire every collaborator to a deterministic, no-I/O stand-in and return the
    per-test call-count spies plus the derived-root path used.
    """
    derived_root = tmp_path / "repo"
    state_dir = derived_root / "state"
    state_dir.mkdir(parents=True)
    artifact = state_dir / "cockpit-emission.json"
    artifact.write_bytes(b'{"schema_version": "3.13.0"}')

    monkeypatch.setattr(ep, "main_worktree_root", lambda repo_root: derived_root)
    monkeypatch.setattr(ep._context, "resolve_repo_name", lambda root: "owner/repo")

    splice_calls = []

    def _fake_splice(raw, *, owner, repo, **kw):
        splice_calls.append((raw, owner, repo))
        return raw + b"-spliced"

    monkeypatch.setattr(ep._publish_envelope, "splice_publish_envelope", _fake_splice)

    publish_calls = []

    def _fake_publish(base_url, document, **kw):
        publish_calls.append((base_url, document))

    monkeypatch.setattr(ep._publish_transport, "publish_document", _fake_publish)
    monkeypatch.setenv(ep.BASE_URL_ENV_VAR, "https://cockpit.example")

    return {
        "derived_root": derived_root,
        "artifact": artifact,
        "splice_calls": splice_calls,
        "publish_calls": publish_calls,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_op_registered():
    assert get_op_handler("emission.publish") is ep._emission_publish


# ---------------------------------------------------------------------------
# Fail-loud repo_root guard (mirrors artifact.emit's own AC5 posture)
# ---------------------------------------------------------------------------

def test_none_repo_root_raises():
    with pytest.raises(ValueError, match="repo_root is None"):
        ep._emission_publish({}, repo_root=None)


# ---------------------------------------------------------------------------
# Happy path: exactly one read, one splice, one POST
# ---------------------------------------------------------------------------

def test_publishes_exactly_once(_wired):
    result = ep._emission_publish({}, repo_root=str(_wired["derived_root"]))

    assert result["ok"] is True
    assert result["repo_slug"] == "owner/repo"
    assert len(_wired["splice_calls"]) == 1
    assert _wired["splice_calls"][0][1:] == ("owner", "repo")
    assert len(_wired["publish_calls"]) == 1
    assert _wired["publish_calls"][0][0] == "https://cockpit.example"
    assert _wired["publish_calls"][0][1] == _wired["artifact"].read_bytes() + b"-spliced"


def test_emission_path_override(_wired, tmp_path):
    override = tmp_path / "custom.json"
    override.write_bytes(b'{"schema_version": "3.13.0", "x": 1}')

    result = ep._emission_publish(
        {"emission_path": str(override)}, repo_root=str(_wired["derived_root"])
    )

    assert result["emission_path"] == str(override)
    assert _wired["splice_calls"][0][0] == override.read_bytes()


# ---------------------------------------------------------------------------
# Single-flight concurrency guard
# ---------------------------------------------------------------------------

def test_single_flight_refuses_concurrent_same_repo(_wired):
    lock_key = str(_wired["derived_root"])
    ep._INFLIGHT_REPOS.add(lock_key)
    try:
        with pytest.raises(ep.PublishInFlightError):
            ep._emission_publish({}, repo_root=str(_wired["derived_root"]))
    finally:
        ep._INFLIGHT_REPOS.discard(lock_key)


def test_single_flight_releases_after_success(_wired):
    ep._emission_publish({}, repo_root=str(_wired["derived_root"]))
    assert str(_wired["derived_root"]) not in ep._INFLIGHT_REPOS


def test_single_flight_releases_after_failure(_wired, monkeypatch):
    def _boom(base_url, document, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ep._publish_transport, "publish_document", _boom)
    with pytest.raises(RuntimeError, match="boom"):
        ep._emission_publish({}, repo_root=str(_wired["derived_root"]))
    assert str(_wired["derived_root"]) not in ep._INFLIGHT_REPOS


# ---------------------------------------------------------------------------
# Base-URL resolution failure is loud, not silent (mirrors C3's own token posture)
# ---------------------------------------------------------------------------

def test_missing_base_url_raises_loud(_wired, monkeypatch):
    monkeypatch.delenv(ep.BASE_URL_ENV_VAR, raising=False)
    monkeypatch.setattr(ep, "registry_get", lambda key: None)
    with pytest.raises(RuntimeError, match="no cockpit publish endpoint configured"):
        ep._emission_publish({}, repo_root=str(_wired["derived_root"]))
    # single-flight guard still released on this failure path too
    assert str(_wired["derived_root"]) not in ep._INFLIGHT_REPOS


# ---------------------------------------------------------------------------
# AC5 (second clause) -- no fan-out/multi-repo/consolidated-write code path exists
# anywhere in emission_publish.py, not merely "a single call happens to write one doc".
# ---------------------------------------------------------------------------

_OP_SOURCE = Path(ep.__file__).read_text(encoding="utf-8")
_OP_AST = ast.parse(_OP_SOURCE)


def _handler_node() -> ast.FunctionDef:
    for node in ast.walk(_OP_AST):
        if isinstance(node, ast.FunctionDef) and node.name == "_emission_publish":
            return node
    raise AssertionError("_emission_publish not found in emission_publish.py")


def test_ac5_no_loop_constructs_in_handler():
    handler = _handler_node()
    for node in ast.walk(handler):
        assert not isinstance(
            node,
            (ast.For, ast.While, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp),
        ), (
            "emission_publish handler contains a loop/comprehension construct "
            f"({type(node).__name__}) -- AC5 requires no fan-out/multi-repo code path"
        )


def test_ac5_publish_document_called_at_most_once_textually():
    calls = [
        node
        for node in ast.walk(_OP_AST)
        if isinstance(node, ast.Attribute) and node.attr == "publish_document"
    ]
    assert len(calls) == 1, (
        "exactly one publish_document call site expected in emission_publish.py; "
        f"found {len(calls)}"
    )


# ---------------------------------------------------------------------------
# AC9 -- a pull never re-emits: emission_publish.py never imports or calls
# artifact_emit's entry point.
# ---------------------------------------------------------------------------

def test_ac9_never_imports_artifact_emit():
    for node in ast.walk(_OP_AST):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "artifact_emit" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert "artifact_emit" not in module
            for alias in node.names:
                assert "artifact_emit" not in alias.name
    # The AST walk above is the real gate (import statements only); the module's own
    # docstring names "artifact_emit" in prose to document the negative-spec, which is
    # expected and does not defeat this test.


def test_ac9_never_calls_envelope_emit():
    """`_envelope.emit` (the writer artifact.emit delegates to) is imported for its
    DEFAULT_OUTPUT_NAME constant only, never invoked.
    """
    calls = [
        node
        for node in ast.walk(_OP_AST)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "emit"
    ]
    assert calls == []


# ---------------------------------------------------------------------------
# AC14 -- OP_CLASSIFICATION carries the required inline rationale comment
# ---------------------------------------------------------------------------

def test_ac14_classified_mutating():
    assert OP_CLASSIFICATION["emission.publish"] is OpClass.MUTATING


def test_ac14_classification_comment_present():
    src = Path(
        sys.modules["coordinator_core.authz.classification"].__file__
    ).read_text(encoding="utf-8")
    idx = src.index('"emission.publish": OpClass.MUTATING')
    preceding = src[max(0, idx - 2000):idx]
    assert "ambiguity default" in preceding.lower() or "AMBIGUITY DEFAULT" in preceding
    assert "enforces nothing on the in-process path" in preceding.lower()


# ---------------------------------------------------------------------------
# Trampoline: IMPORT DISCIPLINE (§ Performance plan) -- MUST NOT import
# coordinator_core.ops (directly or transitively at module scope).
# ---------------------------------------------------------------------------

def _trampoline_ast() -> ast.Module:
    return ast.parse(_TRAMPOLINE_PATH.read_text(encoding="utf-8"))


def test_trampoline_does_not_import_coordinator_core_ops():
    tree = _trampoline_ast()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "coordinator_core.ops", (
                    "publish-emission.py imports coordinator_core.ops at module scope "
                    "-- breaches DR-344's 50ms warm-engine bar (measured 343.8ms)"
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module != "coordinator_core.ops" and not module.startswith(
                "coordinator_core.ops."
            ), (
                f"publish-emission.py imports from {module!r} at module scope -- "
                "breaches DR-344's 50ms warm-engine bar"
            )


def test_trampoline_calls_route_mutation_not_route():
    """AST-based, not substring-based: a docstring mentioning `cc_invoke.route()` in
    prose (to explain why route_mutation is used instead) must not false-positive this
    check the way a bare substring scan would.
    """
    tree = _trampoline_ast()
    call_attrs = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "route_mutation" in call_attrs
    assert "route" not in call_attrs


# ---------------------------------------------------------------------------
# AC11 -- trampoline's argument parser accepts no path/destination/body parameter.
# ---------------------------------------------------------------------------

def test_ac11_trampoline_parser_has_no_arguments(monkeypatch):
    monkeypatch.syspath_prepend(str(_BIN_DIR))
    monkeypatch.syspath_prepend(_LIB_DIR)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "publish_emission_trampoline", _TRAMPOLINE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    parser = module._build_parser()
    # Only the auto-added -h/--help action should exist -- no positional or optional
    # arguments for a caller to steer a path, destination, or body through.
    non_help_actions = [
        a for a in parser._actions if not isinstance(a, argparse_help_action_type(parser))
    ]
    assert non_help_actions == []

    # A future `--emission-path` (or any) flag turns this red, not green.
    with pytest.raises(SystemExit):
        module.main(["--emission-path", "/tmp/whatever"])


def argparse_help_action_type(parser):
    import argparse as _argparse

    return _argparse._HelpAction
