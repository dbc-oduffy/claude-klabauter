"""test_coordinator_compute_layer_scaffold.py — unit test for
coordinator/bin/coordinator-compute-layer-scaffold.py's `--repo` DR-279
refusal (finding 6, coordinator:code-reviewer review of
docs/plans/2026-08-13-compute-layer-scaffolder.md chunk C4), plus the four
manifest-vs-directive conformance tests and the spec-backlink assertion the
2026-08-20 doe-claude-em scaffolder review named as unemitted (item 1 and
item 4 of that memo's four review comments;
state/handoffs/2026-08-30-cross-repo-contract-surfaces-and-agent-f.md
member 5).

Loaded by file path (`importlib.util.spec_from_file_location`) since the
veneer lives under `coordinator/bin/` and is not an importable package
member — same load idiom as sibling bin/ unit tests (e.g.
test_session_claim_cli.py's `_load_cli_module`). `cc_invoke_bare` is
monkeypatched on the loaded module object so these tests never spawn the
real `coordinator_core.invoke` subprocess or require the engine root to
resolve.

The manifest-vs-directive tests stub `cc_invoke_bare` to call the REAL
`compose_producer_module` in-process (never the actual op/IPC transport)
so the emitted module text under test is the genuine emitter output, then
parse that text with `ast` to check the manifest (`CONSUMES_MANIFEST`)
against the directive dispatch table (`_CLI_DISPATCH`) — never a
hand-typed fixture standing in for either.

Spec backlink: pln-the-compute-layer-scaffolder-e-90d036 § C4.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.ops.compute_layer_scaffold.emit import (  # noqa: E402
    compose_producer_module,
)

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


def _emit_via_veneer(stub_cc_invoke_bare, capsys, skill_name: str, verbs: list[str]) -> str:
    """Drives the veneer's `--emit` path with `cc_invoke_bare` stubbed to
    call the real `compose_producer_module` in-process — the emitted
    module text under test is genuine emitter output, never a hand-typed
    fixture, but no op/IPC transport is spawned."""

    def _stub(op, params, repo_root):
        return {"module_text": compose_producer_module(params["skill_name"], params["verbs"])}

    stub_cc_invoke_bare(_stub)

    rc = _veneer.main(
        ["--emit", "--skill-name", skill_name] + [x for v in verbs for x in ("--verb", v)]
    )
    assert rc == 0
    return capsys.readouterr().out


def _manifest_and_dispatch_keys(module_text: str) -> tuple[list[str], dict[str, str]]:
    """Parses the emitted module's `CONSUMES_MANIFEST` tuple and
    `_CLI_DISPATCH` dict via `ast` — never a string search — returning the
    manifest verb order and a `{verb: dispatch_fn_name}` map."""
    tree = ast.parse(module_text)
    manifest: list[str] = []
    dispatch: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "CONSUMES_MANIFEST" and isinstance(node.value, ast.Tuple):
                manifest = [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant)]
            if node.target.id == "_CLI_DISPATCH" and isinstance(node.value, ast.Dict):
                for key, value in zip(node.value.keys, node.value.values):
                    if isinstance(key, ast.Constant) and isinstance(value, ast.Name):
                        dispatch[key.value] = value.id
    return manifest, dispatch


def test_manifest_and_dispatch_table_are_a_bijection(stub_cc_invoke_bare, capsys):
    """Every manifest verb has exactly one dispatch entry and vice versa —
    no unmapped verb, no orphan dispatch handler."""
    verbs = ["handoff.supersede_predecessor", "handoff.retire"]
    module_text = _emit_via_veneer(stub_cc_invoke_bare, capsys, "example_assemble", verbs)
    manifest, dispatch = _manifest_and_dispatch_keys(module_text)
    assert set(manifest) == set(dispatch.keys()) == set(verbs)


def test_manifest_and_dispatch_table_preserve_declared_order(stub_cc_invoke_bare, capsys):
    """The manifest and the dispatch table are emitted in the same caller-
    declared verb order — never resorted or reversed."""
    verbs = ["c_verb", "a_verb", "b_verb"]
    module_text = _emit_via_veneer(stub_cc_invoke_bare, capsys, "example_assemble", verbs)
    manifest, dispatch = _manifest_and_dispatch_keys(module_text)
    assert manifest == verbs
    assert list(dispatch.keys()) == verbs


def test_dispatch_function_names_derive_from_manifest_verbs(stub_cc_invoke_bare, capsys):
    """Each `_CLI_DISPATCH` handler name is `_dispatch_<sanitized-verb>` —
    the directive dispatch side names itself FROM the manifest, never an
    independently hand-picked name."""
    verbs = ["handoff.supersede_predecessor", "do-thing"]
    module_text = _emit_via_veneer(stub_cc_invoke_bare, capsys, "example_assemble", verbs)
    _, dispatch = _manifest_and_dispatch_keys(module_text)
    assert dispatch["handoff.supersede_predecessor"] == "_dispatch_handoff_supersede_predecessor"
    assert dispatch["do-thing"] == "_dispatch_do_thing"


def test_every_manifest_verb_has_a_defined_dispatch_stub_function(stub_cc_invoke_bare, capsys):
    """Every dispatch-table target names an actually-defined `def` in the
    emitted module — the manifest never points the directive table at a
    function that was not also emitted."""
    verbs = ["one_verb", "two_verb"]
    module_text = _emit_via_veneer(stub_cc_invoke_bare, capsys, "example_assemble", verbs)
    _, dispatch = _manifest_and_dispatch_keys(module_text)
    tree = ast.parse(module_text)
    defined_fns = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert set(dispatch.values()).issubset(defined_fns)


def test_emitted_module_carries_the_spec_backlink(stub_cc_invoke_bare, capsys):
    """The emitted module's own docstring names the generator's spec
    backlink — findable later, not just present in `emit.py` itself."""
    module_text = _emit_via_veneer(stub_cc_invoke_bare, capsys, "example_assemble", ["do_thing"])
    assert "Spec backlink: pln-the-compute-layer-scaffolder-e-90d036" in module_text
