"""test_coordinator_compute_layer_scaffold.py — unit test for
coordinator/bin/coordinator-compute-layer-scaffold.py's `--repo` DR-279
refusal (finding 6, coordinator:code-reviewer review of
docs/plans/2026-08-13-compute-layer-scaffolder.md chunk C4).

Loaded by file path (`importlib.util.spec_from_file_location`) since the
veneer lives under `coordinator/bin/` and is not an importable package
member — same load idiom as sibling bin/ unit tests (e.g.
test_session_claim_cli.py's `_load_cli_module`). `cc_invoke_bare` is
monkeypatched on the loaded module object so these tests never spawn the
real `coordinator_core.invoke` subprocess or require CLAUDE_KLABAUTER_ROOT to
resolve.

Spec backlink: pln-the-compute-layer-scaffolder-e-90d036 § C4.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_veneer_module():
    module_path = _BIN_DIR / "coordinator-compute-layer-scaffold.py"
    spec = importlib.util.spec_from_file_location(
        "coordinator_compute_layer_scaffold", str(module_path)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["coordinator_compute_layer_scaffold"] = module
    spec.loader.exec_module(module)
    return module


_veneer = _load_veneer_module()


@pytest.fixture()
def stub_cc_invoke_bare():
    """Stub the veneer's own `cc_invoke_bare` seam for the test body, then
    restore the original — never spawns the real subprocess transport."""
    orig = _veneer.cc_invoke_bare

    def _apply(fn):
        _veneer.cc_invoke_bare = fn

    yield _apply
    _veneer.cc_invoke_bare = orig


def test_repo_flag_refused_exits_1_names_op_scope_and_dr(
    stub_cc_invoke_bare, capsys
):
    def _fail_if_called(op, params, repo_root):
        raise AssertionError("cc_invoke_bare must not be called after --repo refusal")

    stub_cc_invoke_bare(_fail_if_called)

    rc = _veneer.main(
        ["--emit", "--skill-name", "example_assemble", "--verb", "do_thing", "--repo", "/some/repo"]
    )

    assert rc == 1
    err = capsys.readouterr().err
    assert "compute_layer.scaffold" in err
    assert '"none"' in err
    assert "DR-279" in err


def test_repo_flag_omitted_does_not_refuse(stub_cc_invoke_bare, capsys):
    seen = {}

    def _stub(op, params, repo_root):
        seen["op"] = op
        seen["params"] = params
        return {"module_text": "# generated text"}

    stub_cc_invoke_bare(_stub)

    rc = _veneer.main(
        ["--emit", "--skill-name", "example_assemble", "--verb", "do_thing"]
    )

    assert rc == 0
    assert seen["op"] == "compute_layer.scaffold"
    out = capsys.readouterr().out
    assert "# generated text" in out
