"""Every halt kind is classified, and no mechanical kind can still reach the EM outside `remedy_or_halt`."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit.ask_contract import REGISTER_ROWS_UNCLAIMED_REFUSAL
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import (
    REVERIFY_INDETERMINATE,
    REVIEW_REVERIFY_FAILED,
    REVIEW_REVERIFY_INDETERMINATE,
    TARGETED_TESTS_FAILED,
    ZERO_FAIL_UNATTRIBUTED,
)

CORE = Path(__file__).resolve().parents[3]
DISPATCH_EMIT = CORE / "ops" / "dispatch_emit"
SCAN_ROOTS = (CORE / "ops", CORE / "bash_guards")
_NOT_A_KIND_SUFFIXES = ("_FIELD", "_FIELDS", "_TRAILER", "_UNCLOSED_REASON", "_RULE")
REMEDY_CALL = "remedy_or_halt"

# Pseudo-kinds have no constant: a marker is a substring of the message a site emits for that condition.
PSEUDO_MESSAGE_MARKERS = {
    "arm-no-replan-leg": ("no re-plan leg",),
    "live-briefed-revise-refused": ("no row brief reads this revision",),
}


def _py_files(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(CORE).as_posix()
        if "/tests/" in rel or p.name == "conftest.py" or p.name == "halt_remedy_class.py":
            continue
        yield p


def _module_str_constants(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    yield t.id, node.value.value


def _halt_constants() -> dict[str, str]:
    """Module-level HALT_* kind constants of dispatch_emit, name -> value."""
    found: dict[str, str] = {}
    for path in _py_files(DISPATCH_EMIT):
        if "HALT_" not in path.read_text(encoding="utf-8"):
            continue
        for name, value in _module_str_constants(path):
            if name.startswith("HALT_") and not name.endswith(_NOT_A_KIND_SUFFIXES):
                found[f"{path.name}:{name}"] = value
    return found


def test_every_halt_constant_and_landing_refusal_code_is_classified():
    kinds = dict(_halt_constants())
    kinds.update({
        "REGISTER_ROWS_UNCLAIMED_REFUSAL": REGISTER_ROWS_UNCLAIMED_REFUSAL,
        "TARGETED_TESTS_FAILED": TARGETED_TESTS_FAILED,
        "REVIEW_REVERIFY_FAILED": REVIEW_REVERIFY_FAILED,
        "REVERIFY_INDETERMINATE": REVERIFY_INDETERMINATE,
        "REVIEW_REVERIFY_INDETERMINATE": REVIEW_REVERIFY_INDETERMINATE,
        "ZERO_FAIL_UNATTRIBUTED": ZERO_FAIL_UNATTRIBUTED,
    })
    assert len(kinds) > 15, "enumeration found too few halt constants; the scan is broken"
    missing = {n: v for n, v in kinds.items() if v not in hrc.HALT_REMEDY}
    assert not missing, f"halt kinds missing from halt_remedy_class.HALT_REMEDY: {missing}"


def test_registry_shape_and_remedy_or_halt_bound():
    for kind, (cls, remedy) in hrc.HALT_REMEDY.items():
        assert cls in (hrc.DECISION, hrc.MECHANICAL), kind
        assert (remedy is not None) == (cls == hrc.MECHANICAL), kind
        assert hrc.is_mechanical(kind) == (cls == hrc.MECHANICAL)
    assert not hrc.is_mechanical("never-registered")

    calls: list[int] = []

    def flaky(n: int):
        calls.append(n)
        if n < 2:
            raise RuntimeError("not yet")
        return "done"

    assert hrc.remedy_or_halt("targeted-tests-failed", flaky, lambda f: "halted") == "done"
    assert calls == [1, 2]

    def always(n: int):
        raise RuntimeError(f"no {n}")

    halted = hrc.remedy_or_halt("targeted-tests-failed", always, lambda f: list(f))
    assert len(halted) == hrc.MAX_IN_RUN_REMEDY_ATTEMPTS and "attempt 2" in halted[1]
    with pytest.raises(ValueError):
        hrc.remedy_or_halt("usage_limit", always, lambda f: f)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        f = node.func
        return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
    return None


def _is_emitting_site(node: ast.AST, parents: dict[ast.AST, ast.AST], message: bool = False) -> bool:
    """True when `node` sits in a raise/return value, outside a comparison, a dict, and remedy_or_halt.

    A `message` hit (a pseudo-kind's text) counts anywhere inside a function that is not a docstring.
    """
    cur, in_emit = node, message
    if message and isinstance(parents.get(node), ast.Expr):
        return False
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.Compare) or (not message and isinstance(cur, (ast.Dict, ast.DictComp))):
            return False
        if _call_name(cur) == REMEDY_CALL:
            return False
        if isinstance(cur, (ast.Raise, ast.Return)):
            in_emit = True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            break
    return in_emit


def _mechanical_kinds() -> dict[str, list[str]]:
    """Mechanical kind -> the literal values and constant names that denote it."""
    values = {k for k, (c, _) in hrc.HALT_REMEDY.items() if c == hrc.MECHANICAL}
    names: dict[str, set[str]] = {k: set() for k in values}
    for root in SCAN_ROOTS:
        for path in _py_files(root):
            text = path.read_text(encoding="utf-8")
            if not any(v in text for v in values):
                continue
            for name, value in _module_str_constants(path):
                if value in values:
                    names[value].add(name)
    return {k: sorted(v) for k, v in names.items()}


def _offending_sites() -> dict[str, list[str]]:
    mech = _mechanical_kinds()
    out: dict[str, list[str]] = {k: [] for k in mech}
    for root in SCAN_ROOTS:
        for path in _py_files(root):
            text = path.read_text(encoding="utf-8")
            needles = {n for ns in mech.values() for n in ns} | set(mech)
            needles |= {m for ms in PSEUDO_MESSAGE_MARKERS.values() for m in ms}
            if not any(n in text for n in needles):
                continue
            tree = ast.parse(text)
            parents = _parents(tree)
            rel = path.relative_to(CORE).as_posix()
            for node in ast.walk(tree):
                for kind, names in mech.items():
                    is_msg = isinstance(node, (ast.Constant, ast.JoinedStr)) and any(
                        m in (node.value if isinstance(node, ast.Constant) and isinstance(node.value, str)
                              else ast.unparse(node) if isinstance(node, ast.JoinedStr) else "")
                        for m in PSEUDO_MESSAGE_MARKERS.get(kind, ())
                    )
                    hit = (
                        (isinstance(node, ast.Name) and node.id in names)
                        or (isinstance(node, ast.Attribute) and node.attr in names)
                        or (isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == kind)
                        or is_msg
                    )
                    if hit and _is_emitting_site(node, parents, message=is_msg):
                        out[kind].append(f"{rel}:{node.lineno}")
    return out


def _designed_red_unhonoured() -> list[str]:
    """review_blocked on a designed_red node: the Python test-blocker mirror must consult the marker."""
    src = (CORE / "ops" / "review_test_blocker.py").read_text(encoding="utf-8")
    return [] if "if not is_designed_red(" in src else ["ops/review_test_blocker.py: from_reverify never consults designed_red"]


def test_no_mechanical_halt_kind_reaches_the_em():
    sites = _offending_sites()
    sites["review_blocked-designed_red"] = _designed_red_unhonoured()
    bad = {k: v for k, v in sites.items() if v}
    assert not bad, "mechanical halt kinds still reaching the EM outside remedy_or_halt: " + "; ".join(
        f"{k} at {', '.join(v[:6])}" for k, v in sorted(bad.items())
    )
