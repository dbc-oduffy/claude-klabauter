"""Every ask-returning guard, driven through the agy adapter, is a registry-backed deny or a model-visible advisory.

agy renders `ask` as an interactive prompt and auto-approves it under `-p` (headless), so an `ask` left on the
wire is a silent pass. Source of that behaviour: observed on agy 1b Machine-b (handoff
state/handoffs/2026-11/2026-11-16-20e08e-1-agy-headless-launch-and-nag-channel.md, item 3). The model-visible
channel is the PreInvocation `injectSteps` of the agy hooks contract (hooks.md section 4): PreToolUse
documents no `injectSteps`, so the PreToolUse answer is an allow and the advisory is stashed for the next
PreInvocation (`unimatrix-agy-hook-pretool.py --event PreInvocation` injects it).
"""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import _firing_shape
from coordinator_core.host_hooks import shim
from coordinator_core.host_hooks.agy_adapter import AgyAdapter, advisory_dir

_CORE = Path(__file__).resolve().parents[2]
#: Packages whose modules are guards: an `ask` verdict can only originate from a module under one of these.
_GUARD_PACKAGES = ("bash_guards", "write_guards", "hooks")
_ASK_LITERALS = frozenset({"ask", "force_ask"})
_PAYLOAD = {
    "conversationId": "c-1",
    "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}},
}


def _docstring_nodes(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _comparison_operands(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in [node.left, *node.comparators]:
                found.add(id(operand))
                if isinstance(operand, (ast.Tuple, ast.List, ast.Set)):
                    found.update(id(e) for e in operand.elts)
    return found


def ask_emitting_guard_modules() -> list[str]:
    """Guard module stems that carry an `ask` or `force_ask` literal outside a docstring or a comparison."""
    stems = []
    for package in _GUARD_PACKAGES:
        for path in sorted((_CORE / package).rglob("*.py")):
            if "tests" in path.parts or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            skip = _docstring_nodes(tree) | _comparison_operands(tree)
            if any(isinstance(n, ast.Constant) and n.value in _ASK_LITERALS and id(n) not in skip
                   for n in ast.walk(tree)):
                stems.append(path.stem)
    return stems


def _guard_names() -> list[str]:
    names = set(deny_registry.GUARD_DENY_CLASS) | set(deny_registry.SUBAGENT_DENY_CLASS)
    names |= {name.replace("_", "-") for name, spec in _firing_shape.REGISTRY.items()
              if spec.cls is _firing_shape.FiringClass.ASK}
    names |= {stem.replace("_", "-") for stem in ask_emitting_guard_modules()}
    return sorted(names)


@pytest.fixture(autouse=True)
def _settings_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    return tmp_path


def _stashed() -> list:
    folder = advisory_dir() / _PAYLOAD["conversationId"]
    return [p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.txt"))] if folder.is_dir() else []


def _answer(guard: str, decision: str) -> dict:
    adapter = AgyAdapter()
    return adapter.render(adapter.adapt_event(_PAYLOAD),
                          {"decision": decision, "reason": "why " + guard, "guard": guard})


def _is_model_visible(answer: dict) -> bool:
    """An allow whose advisory text sits in the stash, where the PreInvocation hook delivers it."""
    return (answer.get("decision") == "allow" and "injectSteps" not in answer
            and bool(answer.get("reason")) and answer["reason"] in _stashed())


@pytest.mark.parametrize("decision", ["ask", "force_ask"])
@pytest.mark.parametrize("guard", _guard_names())
def test_every_ask_guard_renders_a_registry_deny_or_a_model_visible_advisory_never_ask(guard, decision) -> None:
    answer = _answer(guard, decision)
    assert answer["decision"] != "ask", "an ask is auto-approved headless: a silent pass"
    if deny_registry.guard_deny_class(guard) is not None:
        assert answer == {"decision": "deny", "reason": "why " + guard}
    else:
        assert _is_model_visible(answer), answer
        assert _stashed() == ["why " + guard]


def _pretool_hook():
    path = _CORE.parent / "coordinator" / "bin" / "unimatrix-agy-hook-pretool.py"
    spec = importlib.util.spec_from_file_location("unimatrix_agy_hook_pretool_guard_map", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("decision", ["ask", "force_ask"])
@pytest.mark.parametrize("guard", [g for g in _guard_names() if deny_registry.guard_deny_class(g) is None])
def test_a_non_registry_ask_guard_reaches_the_model_as_inject_steps_at_the_next_preinvocation(
        guard, decision, capsys, monkeypatch) -> None:
    _answer(guard, decision)
    hook = _pretool_hook()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversationId": _PAYLOAD["conversationId"]})))
    assert hook.main(["--event", "PreInvocation"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"injectSteps": [{"systemMessage": {"systemMessage": "why " + guard}}]}
    assert _stashed() == []


@pytest.mark.parametrize("guard", [g for g in _guard_names() if deny_registry.guard_deny_class(g) is not None])
def test_a_registry_classed_ask_guard_denies_and_leaves_nothing_for_preinvocation(guard) -> None:
    assert _answer(guard, "ask") == {"decision": "deny", "reason": "why " + guard}
    assert _stashed() == []


def test_the_enumerated_guard_names_cover_both_sides_of_the_decision() -> None:
    names = _guard_names()
    assert any(deny_registry.guard_deny_class(n) for n in names)
    assert any(deny_registry.guard_deny_class(n) is None for n in names)


@pytest.mark.parametrize("stem", ask_emitting_guard_modules() or [pytest.param(None, marks=pytest.mark.skip(
    reason="no guard module emits an ask literal today; the parametrization grows when one appears"))])
def test_a_registry_classed_guard_never_emits_ask_it_denies_at_the_engine_seam(stem) -> None:
    assert deny_registry.guard_deny_class(stem) is None, (
        "%s emits ask but has a deny_registry class: convert it to deny where its name is known" % stem)


def test_ask_emitting_guard_modules_are_each_in_the_driven_name_set() -> None:
    assert set(s.replace("_", "-") for s in ask_emitting_guard_modules()) <= set(_guard_names())


def test_the_ask_scan_finds_an_ask_literal_in_a_synthetic_guard(tmp_path: Path, monkeypatch) -> None:
    pkg = tmp_path / "bash_guards"
    pkg.mkdir()
    (pkg / "nag_guard.py").write_text('def f():\n    return {"permissionDecision": "ask"}\n', encoding="utf-8")
    (pkg / "cmp_only.py").write_text('def f(d):\n    return d == "ask"\n', encoding="utf-8")
    me = sys.modules[__name__]
    monkeypatch.setattr(me, "_CORE", tmp_path)
    monkeypatch.setattr(me, "_GUARD_PACKAGES", ("bash_guards",))
    assert ask_emitting_guard_modules() == ["nag_guard"]


@pytest.mark.parametrize("verdict,expect_deny", [
    ({"decision": "ask", "reason": "confirm"}, False),
    ({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                             "permissionDecisionReason": "confirm"}}, False),
    ({"decision": "ask", "reason": "irreversible", "guard": "block-held-push"}, True),
])
def test_an_engine_ask_reaches_agy_through_the_shim_as_deny_or_injected_advisory(
        monkeypatch: pytest.MonkeyPatch, verdict, expect_deny) -> None:
    monkeypatch.setattr(shim, "_dial", lambda request: {"result": verdict})
    out = shim.decide("agy", _PAYLOAD)
    assert out["decision"] != "ask", out
    if expect_deny:
        assert out == {"decision": "deny", "reason": "irreversible"}
    else:
        assert _is_model_visible(out), out
        assert _stashed() == ["confirm"]
