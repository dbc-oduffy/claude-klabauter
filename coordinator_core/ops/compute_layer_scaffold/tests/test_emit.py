"""Tests for coordinator_core.ops.compute_layer_scaffold.emit.

Discharges AC1, AC2, AC4, AC5, AC12 for chunk C1 of
docs/plans/2026-08-13-compute-layer-scaffolder.md.

Regression oracle: the spike's hand-written probe emitted a generated test
that passed 4/4 over a module whose `main()` crashed with
`AttributeError: ... has no attribute 'OK'` because no generated test
invoked `main()` end-to-end (spike verdict, finding 3). `test_main_*` below
imports the emitted module and CALLS `main()` for exactly that reason.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.compute_layer_scaffold.emit import (
    InvalidVerb,
    compose_producer_module,
)


def _load_emitted_module(text: str, tmp_path: Path, module_name: str):
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text(text, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def test_compose_producer_module_requires_skill_name():
    with pytest.raises(ValueError):
        compose_producer_module("", ["do_thing"])


def test_compose_producer_module_requires_at_least_one_verb():
    with pytest.raises(ValueError):
        compose_producer_module("example_assemble", [])


def test_compose_producer_module_rejects_empty_verb():
    with pytest.raises(InvalidVerb):
        compose_producer_module("example_assemble", [""])


def test_ac1_imports_contract_symbols_and_reimplements_none(tmp_path):
    """AST-walk the emitted TEXT (not read-it-yourself) for the three
    required imports, and assert no local def named build_envelope,
    extend_exit_codes, or execute_directives shadows them."""
    text = compose_producer_module("example_assemble", ["do_thing", "undo_thing"])
    tree = ast.parse(text)

    imported_names: set[str] = set()
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)
                if alias.name in ("build_envelope", "extend_exit_codes"):
                    assert module.endswith("envelope")
                if alias.name == "apply_base":
                    imported_modules.add(alias.name)

    assert "build_envelope" in imported_names
    assert "extend_exit_codes" in imported_names
    assert "apply_base" in imported_names or "execute_directives" in imported_names

    # apply_base.execute_directives reached as a module-qualified attribute
    # access (the reach style the spike's real fleet uses — matching only
    # ast.ImportFrom would miss it, per the spike's own naive-scorer finding).
    attr_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "execute_directives"
    ]
    assert attr_calls, "expected apply_base.execute_directives to be called"

    reimplemented = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in ("build_envelope", "extend_exit_codes", "execute_directives")
    }
    assert not reimplemented


def test_ac2_brief_returns_exactly_the_8_envelope_keys(tmp_path):
    """Write the emitted text to a tmp path, import it, and CALL brief() —
    not a string-match over the template."""
    from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS

    text = compose_producer_module("example_assemble", ["do_thing"])
    module = _load_emitted_module(text, tmp_path, "emitted_ac2_example")

    envelope = module.brief()
    assert set(envelope.keys()) == set(ENVELOPE_KEYS)
    assert len(envelope) == 8


def test_ac4_ladder_is_always_explicit_never_bare(tmp_path):
    text = compose_producer_module("example_assemble", ["do_thing"])
    assert "BUSINESS_FAIL=1" in text
    assert "USAGE=2" in text
    assert "TRANSPORT_FAIL=3" in text

    tree = ast.parse(text)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "extend_exit_codes"
        ):
            assert node.keywords, "extend_exit_codes call must never be bare"
            keyword_names = {kw.arg for kw in node.keywords}
            assert keyword_names == {"BUSINESS_FAIL", "USAGE", "TRANSPORT_FAIL"}
            for kw in node.keywords:
                assert kw.arg != "SUCCESS"


def test_ac4_main_returns_exit_code_success_never_ok(tmp_path):
    """Regression oracle for the spike's ExitCode.OK/SUCCESS crash: import
    the emitted module and CALL main(['brief']), which must not raise."""
    text = compose_producer_module("example_assemble", ["do_thing"])
    module = _load_emitted_module(text, tmp_path, "emitted_ac4_example")

    result = module.main(["brief"])
    assert result == 0
    assert not hasattr(module.ExampleAssembleExitCode, "OK")
    assert hasattr(module.ExampleAssembleExitCode, "SUCCESS")


def test_main_end_to_end_brief_apply_and_unknown_verb(tmp_path):
    """Exercises main() across brief, apply, and the unknown-verb arm —
    the exact end-to-end path the spike's generated test skipped."""
    text = compose_producer_module("example_assemble", ["do_thing"])
    module = _load_emitted_module(text, tmp_path, "emitted_main_e2e_example")

    assert module.main(["brief"]) == int(module.ExampleAssembleExitCode.SUCCESS)
    assert module.main(["apply"]) == int(module.ExampleAssembleExitCode.SUCCESS)
    assert module.main(["nonsense-verb"]) == int(module.ExampleAssembleExitCode.USAGE)
    assert module.main([]) == int(module.ExampleAssembleExitCode.USAGE)


def test_ac5_cli_dispatch_is_a_closed_dict_literal_over_the_manifest(tmp_path):
    text = compose_producer_module("example_assemble", ["do_thing", "undo_thing"])
    tree = ast.parse(text)

    dispatch_assign = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "_CLI_DISPATCH":
                dispatch_assign = node
                break
    assert dispatch_assign is not None
    assert isinstance(dispatch_assign.value, ast.Dict)

    keys = {
        k.value for k in dispatch_assign.value.keys if isinstance(k, ast.Constant)
    }
    assert keys == {"do_thing", "undo_thing"}

    # No getattr/import_module dispatch anywhere in the emitted text.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("getattr", "import_module")

    module = _load_emitted_module(text, tmp_path, "emitted_ac5_example")
    assert set(module._CLI_DISPATCH.keys()) == set(module.CONSUMES_MANIFEST)


def test_emitter_source_has_no_getattr_or_import_module_dispatch_path():
    """The emitter itself (not just its output) must have no code path
    producing getattr/import_module dispatch (AC5, on the emitter)."""
    import coordinator_core.ops.compute_layer_scaffold.emit as emit_module

    source = Path(emit_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("getattr", "import_module")


def test_ac1_skill_name_with_triple_quote_does_not_break_out_of_docstring():
    """Regression for a code-reviewer finding: skill_name reached the
    module docstring unescaped while every sibling caller-supplied value
    (verbs, exit-code class name) was sanitized/escaped first. A skill_name
    containing `\"\"\"` used to break out of the docstring and leave the
    remainder as live module source, emitting text that failed to parse."""
    text = compose_producer_module('x"""\nimport os\nos.system("x")\n"""y', ["do_thing"])
    ast.parse(text)  # must not raise SyntaxError


def test_ac1_skill_name_with_newline_and_backslash_does_not_break_out_of_docstring():
    tricky = "a" + "\\" + "b" + "\n" + "c"
    text = compose_producer_module(tricky, ["do_thing"])
    ast.parse(text)  # must not raise SyntaxError


def test_ac12_compose_producer_module_performs_no_file_io(tmp_path, monkeypatch):
    """Emission writes module TEXT and nothing else — assert negatively
    that no file appears under a caller-named scratch dir during emission."""
    before = set(tmp_path.iterdir())
    compose_producer_module("example_assemble", ["do_thing"])
    after = set(tmp_path.iterdir())
    assert before == after
