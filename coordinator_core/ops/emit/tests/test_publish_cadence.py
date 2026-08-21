"""Tests for `coordinator_core.ops.emit.publish_cadence` -- the C5 cadence wiring
(workday-close / workweek-close scheduled call sites for `emission.publish`).

No network, no real disk artifact -- `_emission_publish` itself is monkeypatched at its
call site, mirroring `test_emission_publish_op.py`'s own no-I/O posture. The AC10
negative-spec tests are AST scans, not runtime behavior, matching this repo's existing
AC5/AC9/AC11 negative-spec style in that same test module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.ops.emit import publish_cadence as pc

_OPS_DIR = Path(pc.__file__).resolve().parents[1]  # coordinator_core/ops
_ARTIFACT_EMIT_PATH = _OPS_DIR / "artifact_emit.py"
_ENVELOPE_PATH = _OPS_DIR / "emit" / "envelope.py"


# ---------------------------------------------------------------------------
# run_publish_cadence: thin, in-process delegation to _emission_publish
# ---------------------------------------------------------------------------

def test_delegates_to_emission_publish_handler(monkeypatch):
    calls = []

    def _fake(params, repo_root=None):
        calls.append((params, repo_root))
        return {"ok": True, "repo_slug": "owner/repo"}

    monkeypatch.setattr(pc, "_emission_publish", _fake)

    result = pc.run_publish_cadence("/some/repo/root")

    assert result == {"ok": True, "repo_slug": "owner/repo"}
    assert len(calls) == 1
    params, repo_root = calls[0]
    assert params == {}
    assert repo_root == "/some/repo/root"


def test_does_not_reemit_before_publishing():
    """AST-based: `run_publish_cadence` calls no `artifact.emit`/`envelope.emit`
    entry point -- it delegates to `_emission_publish` and nothing else.
    """
    src = Path(pc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_publish_cadence"
    )
    call_names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                call_names.add(node.func.attr)
    assert call_names == {"_emission_publish"}, (
        "run_publish_cadence must call exactly _emission_publish -- any other call "
        f"({call_names}) risks re-emitting or duplicating the publish"
    )


# ---------------------------------------------------------------------------
# Fail-loud: a failure from the underlying op propagates unchanged, uncaught.
# ---------------------------------------------------------------------------

def test_publish_failure_propagates_uncaught(monkeypatch):
    def _boom(params, repo_root=None):
        raise RuntimeError("transport unreachable")

    monkeypatch.setattr(pc, "_emission_publish", _boom)

    with pytest.raises(RuntimeError, match="transport unreachable"):
        pc.run_publish_cadence("/some/repo/root")


def test_module_never_catches_exceptions():
    """AST-based: no try/except anywhere in publish_cadence.py -- the non-fatal
    decision belongs to the calling close-ceremony script, not this module (see
    module docstring's fail-loud note).
    """
    src = Path(pc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    assert not any(isinstance(node, ast.Try) for node in ast.walk(tree)), (
        "publish_cadence.py must not swallow exceptions -- fail-loud is the "
        "calling ceremony's decision, not this module's"
    )


# ---------------------------------------------------------------------------
# AC10 -- the only scheduled publish call sites are workday-close and
# workweek-close; assert no publish call site exists on the emit path.
# ---------------------------------------------------------------------------

def _module_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports_any(tree: ast.Module, needle: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(needle in alias.name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if needle in module:
                return True
            if any(needle in alias.name for alias in node.names):
                return True
    return False


def test_ac10_artifact_emit_never_imports_publish_cadence():
    tree = _module_ast(_ARTIFACT_EMIT_PATH)
    assert not _imports_any(tree, "publish_cadence")
    assert not _imports_any(tree, "emission_publish")


def test_ac10_envelope_never_imports_publish_cadence():
    tree = _module_ast(_ENVELOPE_PATH)
    assert not _imports_any(tree, "publish_cadence")
    assert not _imports_any(tree, "emission_publish")


def test_ac10_artifact_emit_never_calls_run_publish_cadence():
    tree = _module_ast(_ARTIFACT_EMIT_PATH)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "run_publish_cadence")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "run_publish_cadence")
        )
    ]
    assert calls == []


def test_ac10_envelope_never_calls_run_publish_cadence():
    tree = _module_ast(_ENVELOPE_PATH)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "run_publish_cadence")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "run_publish_cadence")
        )
    ]
    assert calls == []


def test_ac10_publish_cadence_itself_never_imports_the_emit_path():
    """Symmetric check: publish_cadence.py never imports artifact_emit or
    envelope's emit() writer either -- the refused posture holds in both
    directions.
    """
    tree = _module_ast(Path(pc.__file__))
    assert not _imports_any(tree, "artifact_emit")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("emit.envelope"):
            raise AssertionError(
                "publish_cadence.py imports emit.envelope -- risks re-emitting "
                "before publishing (this chunk's own hard constraint)"
            )
