"""
coordinator_core.workstream_complete.tests.test_every_decisions_key_read_is_declared

A `decisions[...]` key that a `directives_*` submodule READS must appear in
some submodule's ``FREE_VALUE_KEYS``, because that union is what
``build_decisions_template`` publishes and therefore the only surface from
which a caller can discover the key exists.

MOTIVATING INCIDENT (2026-08-30, this repo). C2 of
``docs/plans/2026-08-30-the-close-ships-the-baton-it-closed.md`` shipped a
ship-stamp leg that reads ``decisions["handoff_dispositions"]`` and never
declared it. Every conjunct downstream was correct -- the writers were
reached, the claim ledger was consulted instead of the corpus, the commit
folded -- and the feature was inert in production regardless, because the
gating key was undiscoverable and therefore never supplied.
``resolve_ship_stamp_candidates`` short-circuited on the absent key and
reported its clean "ran, found nothing" outcome, which is exactly the
green-stamp-on-an-empty-set failure that plan's own Anti-scope named as the
thing it existed to close. Three reviewers, a full test suite and a passing
falsifier all read green over it; a criterion-only reader denied the plan
found it in one pass.

``directives_commit_tail.py`` already stated the rule -- "a key read here but
absent from this tuple is a key no caller can discover from the template,
which is the whole defect ``decisions_template`` exists to close" -- and a
rule with no artifact behind it is what the north star exists to replace.
This module is that artifact.

NEGATIVE SPEC. This does NOT assert that every DECLARED key is read: a key
published for a caller to supply, which no submodule consumes on the default
path, is legitimate. The implication runs one way only, read -> declared.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PKG_DIR = pathlib.Path(__file__).resolve().parents[1]

#: Keys read out of a `decisions`-shaped mapping that are NOT caller-supplied
#: free values. Each needs a reason, not just an entry.
#:
#: Both entries below surfaced when Finding 1 (coordinator:code-reviewer,
#: 2026-08-30) widened this guard's scan past `directives_*.py` to also cover
#: `apply.py`/`__init__.py`.
_NOT_FREE_VALUES: dict[str, str] = {
    # apply.py:699 -- `decision.get("disposition")`, where `decision` is a
    # single judgment point's own already-extracted value
    # (`decisions[jp_id]`), not the top-level `decisions` mapping itself.
    # The AST heuristic matches on "decision" as a substring of the local
    # name and cannot tell the two apart; this is not a second top-level
    # free-value key.
    "disposition": (
        "false positive: `decision.get(...)` reads a per-judgment-point "
        "value already extracted from decisions[jp_id], not a top-level "
        "decisions[...] key (apply.py:699)"
    ),
    # __init__.py's own module comment beside `_JP_SESSION_SHAPE_ID` already
    # states this: the key is discoverable via `judgment_points[].id` in
    # `preflight.decisions_template` whenever the point is raised, the same
    # "answer a judgment point" contract `ceremony_common/apply_halt.py`
    # already reads under -- it is not a FREE_VALUE_KEYS candidate.
    "jp-session-shape": (
        "not a free value: discoverable via judgment_points[].id when the "
        "point fires, per __init__.py's own comment beside "
        "_JP_SESSION_SHAPE_ID"
    ),
}


def _directive_modules() -> list[pathlib.Path]:
    return sorted(PKG_DIR.glob("directives_*.py"))


# Review: coordinator:code-reviewer (Finding 1, 2026-08-30) -- the guard only
# ever scanned `directives_*.py`, so a `decisions[...]` read landing directly
# in `apply.py` (which holds the close-commit tail this incident actually
# shipped from) or `__init__.py`'s `brief()` half was invisible to it -- the
# exact blind spot the ship-stamp incident would reproduce if it recurred one
# module over. Scanned separately from `_directive_modules()` (not folded in)
# because these two are not `directives_*`-shaped submodules with their own
# `FREE_VALUE_KEYS`; they are the callers this guard needs to also cover.
def _non_directive_scanned_modules() -> list[pathlib.Path]:
    return [PKG_DIR / "apply.py", PKG_DIR / "__init__.py"]


def _declared_keys() -> set[str]:
    """The union `build_decisions_template` publishes, read off the modules
    themselves rather than re-listed here -- a second copy of this set is the
    same fork this test exists to prevent."""
    import coordinator_core.workstream_complete as wsc

    declared: set[str] = set(wsc.FREE_VALUE_KEYS)
    for path in _directive_modules():
        mod = __import__(
            f"coordinator_core.workstream_complete.{path.stem}", fromlist=["FREE_VALUE_KEYS"]
        )
        declared.update(getattr(mod, "FREE_VALUE_KEYS", ()))
    return declared


def _module_string_constants(tree: ast.AST) -> dict[str, str]:
    """Module-level `NAME = "literal"` bindings, so a key read through its own
    `_KEY_*` constant resolves to the string it holds.

    NOT optional sugar: the incident this guard exists for reads
    `decisions.get(_KEY_HANDOFF_DISPOSITIONS)`, never the bare literal. A
    literals-only matcher passes clean over it -- which this test's own first
    draft did, and which is why the resolution below exists."""
    consts: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        consts[tgt.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.target.id] = node.value.value
    return consts


def _keys_read_from_decisions(tree: ast.AST) -> set[str]:
    """Subscripts and `.get(...)` calls on any name containing "decisions",
    resolving both bare string literals and module-level `_KEY_*` constants."""
    found: set[str] = set()
    consts = _module_string_constants(tree)

    def as_key(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    def names_a_decisions_mapping(node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return "decision" in node.id.lower()
        if isinstance(node, ast.Attribute):
            return "decision" in node.attr.lower()
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and names_a_decisions_mapping(node.value):
            key = as_key(node.slice)
            if key is not None:
                found.add(key)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and names_a_decisions_mapping(node.func.value)
            and node.args
        ):
            key = as_key(node.args[0])
            if key is not None:
                found.add(key)
    return found


@pytest.mark.parametrize("module_path", _directive_modules(), ids=lambda p: p.name)
def test_every_decisions_key_read_by_a_directive_module_is_declared(module_path):
    declared = _declared_keys()
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    read = _keys_read_from_decisions(tree)
    undeclared = sorted(k for k in read - declared if k not in _NOT_FREE_VALUES)
    assert not undeclared, (
        f"{module_path.name} reads decisions key(s) {undeclared} that no module's "
        "FREE_VALUE_KEYS declares, so build_decisions_template never publishes them "
        "and no caller can discover them. The code will run, find the key absent, and "
        "report a clean no-op -- the exact shape that shipped the ship-stamp inert on "
        "2026-08-30. Declare the key in this module's FREE_VALUE_KEYS, or add it to "
        "_NOT_FREE_VALUES here with the reason it is not caller-supplied."
    )


@pytest.mark.parametrize(
    "module_path", _non_directive_scanned_modules(), ids=lambda p: p.name
)
def test_every_decisions_key_read_by_apply_or_init_is_declared(module_path):
    """Review: coordinator:code-reviewer (Finding 1, 2026-08-30) -- the
    directive-module-only guard above covers `directives_*.py` but never saw
    `apply.py`/`__init__.py`, which is exactly where the close-commit tail
    lives. This closes the same class of blind spot for those two modules."""
    declared = _declared_keys()
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    read = _keys_read_from_decisions(tree)
    undeclared = sorted(k for k in read - declared if k not in _NOT_FREE_VALUES)
    assert not undeclared, (
        f"{module_path.name} reads decisions key(s) {undeclared} that no module's "
        "FREE_VALUE_KEYS declares, so build_decisions_template never publishes them "
        "and no caller can discover them. Declare the key in the appropriate "
        "directives_*.py module's FREE_VALUE_KEYS, or add it to _NOT_FREE_VALUES "
        "here with the reason it is not caller-supplied."
    )
