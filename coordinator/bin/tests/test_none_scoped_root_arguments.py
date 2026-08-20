"""test_none_scoped_root_arguments.py — D2+D3+D4 (one rule, three
violations): no `coordinator/bin` caller may gate, require, or spend a git
spawn resolving a repo root for a `scope="none"` op.

For a `scope="none"` op, `cc_invoke.lib._should_pass_repo` discards the
caller's root before spawn (never forwarded on argv), and the underlying
`coordinator_core.invoke` child always runs `cwd=claude_klabauter_root` — a root the
caller computes is never transmitted, however it was obtained. Each CLI
below used to violate this in a different, incompatible way:

  D2  coordinator-workflow-scaffold.py REQUIRED `--repo` (exit 1) and
      isdir-validated it, for `workflow.scaffold` (scope "none").
  D3  cartography.py spawned `git` and `sys.exit(2)`'d outside a git tree
      to resolve a transport-only `--repo` default, for the entire
      `cartography.*` family (all scope "none").
  D4  schema-drift-gate.py did the same git-spawn-and-bail for
      `schema.drift_gate` (scope "none"), with no `--repo` flag to even
      justify the spend.

`coordinator-compute-layer-scaffold.py` (compute_layer.scaffold, scope
"none") is the pre-existing REFERENCE implementation this fix mirrors: a
loud DR-279-shaped refusal on `--repo`, no resolution attempt at all. It
already has its own test coverage (test_coordinator_compute_layer_scaffold.py)
and is not re-tested here.

Every test below stubs the transport seam (`cc_invoke_bare` /
`cc_invoke.route`) so no real `coordinator_core.invoke` subprocess is
spawned and no real git spawn can occur — the property under test is that
the CLI itself never attempts one, not the transport's own behaviour.

Spec backlink: docs/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C16.md
Spec backlink: docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md
"""
from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_by_path(module_name: str, filename: str):
    """Load a `coordinator/bin/*.py` script by file path — none of these
    are importable package members (hyphenated / extensionless filenames)."""
    path = _BIN_DIR / filename
    loader = SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_file_location(module_name, str(path), loader=loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# D2 — coordinator-workflow-scaffold.py (workflow.scaffold, scope "none")
# ---------------------------------------------------------------------------

_workflow_scaffold = _load_by_path(
    "test_none_scoped_workflow_scaffold_cli", "coordinator-workflow-scaffold.py"
)


@pytest.fixture()
def stub_workflow_cc_invoke_bare():
    orig = _workflow_scaffold.cc_invoke_bare

    def _apply(fn):
        _workflow_scaffold.cc_invoke_bare = fn

    yield _apply
    _workflow_scaffold.cc_invoke_bare = orig


def test_workflow_scaffold_succeeds_without_repo(stub_workflow_cc_invoke_bare, capsys):
    seen = {}

    def _stub(op, params, repo_root):
        seen["op"] = op
        seen["repo_root"] = repo_root
        return {"script": "# generated workflow"}

    stub_workflow_cc_invoke_bare(_stub)

    rc = _workflow_scaffold.main(["--name", "example", "--description", "one line"])

    assert rc == 0
    assert seen["op"] == "workflow.scaffold"
    assert seen["repo_root"] == ""
    assert "# generated workflow" in capsys.readouterr().out


def test_workflow_scaffold_refuses_repo_flag(stub_workflow_cc_invoke_bare, capsys):
    def _fail_if_called(op, params, repo_root):
        raise AssertionError("cc_invoke_bare must not be called after --repo refusal")

    stub_workflow_cc_invoke_bare(_fail_if_called)

    rc = _workflow_scaffold.main(
        ["--name", "example", "--description", "one line", "--repo", "/some/repo"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "workflow.scaffold" in err
    assert '"none"' in err
    assert "DR-279" in err


# ---------------------------------------------------------------------------
# D3 — cartography.py (cartography.*, all scope "none")
# ---------------------------------------------------------------------------

_cartography = _load_by_path("test_none_scoped_cartography_cli", "cartography.py")


@pytest.fixture()
def stub_cartography_cc_invoke_bare():
    orig = _cartography.cc_invoke_bare

    def _apply(fn):
        _cartography.cc_invoke_bare = fn

    yield _apply
    _cartography.cc_invoke_bare = orig


def test_cartography_succeeds_without_repo(stub_cartography_cc_invoke_bare, capsys, monkeypatch):
    seen = {}

    def _stub(op, params, repo_root):
        seen["op"] = op
        seen["repo_root"] = repo_root
        return {"ok": True}

    stub_cartography_cc_invoke_bare(_stub)
    monkeypatch.setattr(
        _cartography, "_cartography_ops", lambda: ["cartography.file_index"]
    )

    rc = _cartography.main(["file_index", "--target-root", "/some/target"])

    assert rc == 0
    assert seen["op"] == "cartography.file_index"
    assert seen["repo_root"] == ""


def test_cartography_refuses_repo_flag_before_any_git_or_resolution(
    stub_cartography_cc_invoke_bare, capsys, monkeypatch
):
    def _fail_if_called(op, params, repo_root):
        raise AssertionError("cc_invoke_bare must not be called after --repo refusal")

    stub_cartography_cc_invoke_bare(_fail_if_called)
    monkeypatch.setattr(
        _cartography, "_cartography_ops", lambda: ["cartography.file_index"]
    )

    rc = _cartography.main(
        ["file_index", "--target-root", "/some/target", "--repo", "/some/repo"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "cartography.*" in err
    assert '"none"' in err
    assert "DR-279" in err


def test_cartography_module_defines_no_git_spawning_resolver():
    """D3's own regression guard: `_resolve_repo_root` (the git-spawn-and-
    sys.exit(2) helper this chunk removed) must not reappear on the module."""
    assert not hasattr(_cartography, "_resolve_repo_root")


# ---------------------------------------------------------------------------
# D4 — schema-drift-gate.py (schema.drift_gate, scope "none")
# ---------------------------------------------------------------------------

_schema_drift_gate = _load_by_path(
    "test_none_scoped_schema_drift_gate_cli", "schema-drift-gate.py"
)


def test_schema_drift_gate_succeeds_without_resolving_a_repo_root(capsys, monkeypatch):
    seen = {}

    def _fake_route(op, params, repo_root, legacy_fn):
        seen["op"] = op
        seen["repo_root"] = repo_root
        return {"ok": True, "status": "MATCH", "drifted": [], "message": None}

    monkeypatch.setattr(_schema_drift_gate.cc_invoke, "route", _fake_route)

    rc = _schema_drift_gate.main([])

    assert rc == 0
    assert seen["op"] == "schema.drift_gate"
    assert seen["repo_root"] == ""


def test_schema_drift_gate_module_defines_no_git_spawning_resolver():
    """D4's own regression guard: `_resolve_repo_root` (the git-spawn-and-
    bail helper this chunk removed) must not reappear on the module."""
    assert not hasattr(_schema_drift_gate, "_resolve_repo_root")


# ---------------------------------------------------------------------------
# C28 — reap-integrated-review-findings.py::_reap_native no longer passes the
# reserved `_claude_klabauter_root=` kwarg into cc_invoke.cc_invoke(). cc_invoke()'s own
# docstring reserves that keyword for route()'s internal forwarding; callers
# outside route() should omit it and let cc_invoke() self-resolve.
#
# Spec backlink: docs/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C28.md
# ---------------------------------------------------------------------------

_reap_findings = _load_by_path(
    "test_none_scoped_reap_integrated_review_findings_cli",
    "reap-integrated-review-findings.py",
)


def test_reap_native_calls_cc_invoke_without_claude_klabauter_root_kwarg(monkeypatch):
    seen = {}

    def _fake_cc_invoke(op, params, repo_root, **kwargs):
        seen["op"] = op
        seen["params"] = params
        seen["repo_root"] = repo_root
        seen["kwargs"] = kwargs
        return {"exit_code": 0, "reaped": [], "candidates": []}

    monkeypatch.setattr(_reap_findings.cc_invoke, "cc_invoke", _fake_cc_invoke)

    rc = _reap_findings._reap_native(True, "")

    assert rc == 0
    assert seen["op"] == "fleet.reap_integrated_findings"
    assert "_claude_klabauter_root" not in seen["kwargs"]
