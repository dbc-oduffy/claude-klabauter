"""AST-structural test: `baton_class.py` is the ONLY module allowed to spell a
literal collection that pairs a retired D1 pre-rename `kind` value with its
D1 target successor (D2/C2/C3 of the baton-kind-vocabulary migration).

Why this exists: `coordinator_core.frontmatter.baton_class` centralises the
legacy<->target aliasing (`_PRE_RENAME_ALIASES`) so every other call site in
the tree makes AT MOST one `kind`-literal string comparison, never a
two-or-more-literal collection that silently re-implements the alias table.
Five sites already cite this file by name as the enforcement mechanism
(`baton_class.py` itself, `roadmap/audit.py`, `roadmap/number_stubs.py`) —
this test is what makes those citations true rather than aspirational.

Detection strategy (why AST, not grep): a regex/grep pattern only catches the
literal shapes it was tuned against (e.g. `{"a", "b"}` but not
`frozenset(("a", "b"))` or an `in (...)` compare or an or-chain of `==`
tests). This test parses every `.py` file's AST and walks `ast.Set`,
`ast.List`, `ast.Tuple`, `frozenset(...)`/`set(...)`/`tuple(...)` calls,
`ast.Compare` with `In`/`NotIn`, and `ast.BoolOp` or-chains of `==`
comparisons — collecting the string-constant members of each such literal
collection and checking whether any collection contains BOTH halves of one
of the three known retired/successor pairs.

Legal vs illegal, precisely (this is the part earlier drafts of this
criterion got wrong — a blanket "no two-or-more-string collection outside
baton_class.py" rule is over-broad; three same-class/PM-ruled membership
buckets legitimately exist elsewhere, e.g. `{"spinoff", "goal-seed"}` or
`{"roadmap-baton", "roadmap-seed"}`, and must NOT be flagged):

    LEGAL   — a collection of two-or-more `kind`/vocabulary strings that does
              NOT contain both a retired value and its own D1 successor.
    ILLEGAL — a collection containing BOTH halves of one of:
                  ("spinoff-roadmap", "roadmap-baton")
                  ("spinoff-roadmap-creator", "roadmap-seed")
                  ("spinoff-goal", "goal-seed")
              anywhere outside the exemption list below.

Exemptions (deliberately narrow, and self-checked below so the list cannot
silently grow):
  - `coordinator_core/frontmatter/baton_class.py` — the ONE canonical module
    that OWNS `_PRE_RENAME_ALIASES`. This test asserts the exemption list has
    exactly one module-path entry, so a future author cannot quietly add a
    second exempt module instead of routing through the canonical function.
  - `**/tests/**` and any `test_*.py` file — a test legitimately needs to
    spell both halves of a pair to prove dual-acceptance (e.g. asserting the
    aliasing function maps both spellings to the same class). Exempted by
    path pattern, not by module list, and named here explicitly per this
    test's own no-silent-exemption rule.

Both the enum universe and the alias pairs are SOURCED, never hardcoded here:
enum values come from `coordinator_core/frontmatter/schemas/handoff.schema.json`
(`properties.kind.enum` and `x-baton-class.mapping`), and the retired/target
pairs come from `baton_class._PRE_RENAME_ALIASES` itself. Hardcoding either
list in this test would make the test a fourth copy of the vocabulary — the
exact drift this whole migration exists to close.

Spec backlink: DoE-claude:pln-baton-kind-vocabulary-one-axis-d1ce8f
               § D1, D2, C2, C3, AC4.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Iterable, Optional

import pytest

from coordinator_core.frontmatter.baton_class import _PRE_RENAME_ALIASES
from coordinator_core.git.ls_files import tracked_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "frontmatter" / "schemas" / "handoff.schema.json"
)

#: Directories excluded from the walk outright — closed-out substrate that
#: `git ls-files` would otherwise still surface (it is tracked). None of
#: these hold live production call sites this test needs to police.
_EXCLUDED_DIR_NAMES = {"archive", "state"}

#: The one canonical module allowed to pair a retired value with its
#: successor — it OWNS `_PRE_RENAME_ALIASES`. Kept as a single-entry
#: container (not a bare constant) so the self-check below has something to
#: measure the length of.
_EXEMPT_MODULE_PATHS = (
    Path("coordinator_core") / "frontmatter" / "baton_class.py",
)


def _is_test_path(rel_path: Path) -> bool:
    """`**/tests/**` and any `test_*.py` file are exempt — a test legitimately
    needs to spell both halves of a retired/successor pair to prove
    dual-acceptance. Exempted by path shape, not by module list, and named
    explicitly in this test's own docstring per its no-silent-exemption rule.
    """
    if "tests" in rel_path.parts[:-1]:
        return True
    if rel_path.name.startswith("test_"):
        return True
    return False


def _iter_py_files() -> Iterable[Path]:
    """Enumerate via `git ls-files`, never a raw filesystem walk — a walk
    would also surface untracked scratch (`scratch/`, `scratchpad/`,
    `tasks/`, other sessions' working-file dumps under `state/subagent-
    share/`) that is never a live call site this test polices, and is
    gitignored/ephemeral rather than production surface by construction.

    Sourced from `coordinator_core.git.ls_files.tracked_files` (one cached
    `git ls-files -z` spawn for this (repo_root, "*.py") pair, never a
    per-call re-spawn) rather than a direct `subprocess.run` here.
    """
    for entry in tracked_files(_REPO_ROOT, "*.py"):
        rel = Path(entry)
        if any(part in _EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        yield _REPO_ROOT / rel


def _load_alias_pairs() -> list[tuple[str, str]]:
    """Sourced from `baton_class._PRE_RENAME_ALIASES` itself, never
    hardcoded here — see module docstring.
    """
    return list(_PRE_RENAME_ALIASES.items())


def _load_known_kind_universe() -> set[str]:
    """Sourced from the vendored schema's `properties.kind.enum` and
    `x-baton-class.mapping` keys, plus the retired pre-rename spellings from
    `_PRE_RENAME_ALIASES` (which are deliberately NOT in the schema enum —
    they are the still-live legacy spellings the schema's own C10 narrow
    retired). Used only to sanity-check that the sourced alias pairs are
    coherent with the schema; not used as the detection universe itself
    (detection below matches on the alias PAIRS directly, not the broader
    vocabulary, so a same-class collection like {"spinoff", "goal-seed"} is
    correctly left alone).
    """
    raw = _SCHEMA_PATH.read_text(encoding="utf-8")
    schema = json.loads(raw)
    enum_values = set(schema.get("properties", {}).get("kind", {}).get("enum", []))
    mapping_keys = set(schema.get("x-baton-class", {}).get("mapping", {}).keys())
    retired = set(_PRE_RENAME_ALIASES.keys())
    return enum_values | mapping_keys | retired


def _string_constants(node: ast.AST) -> Optional[set[str]]:
    """Return the set of string-constant *direct* elements of a literal
    collection node, or None if `node` is not a collection shape this test
    understands.

    Review: code-reviewer (P2, Finding 2) — `ast.Dict` was previously never
    recognised as a collection shape, so a dict literal pairing a retired
    value with its successor (e.g. `{"spinoff-roadmap": "legacy",
    "roadmap-baton": "current"}`) walked straight through undetected — a dict
    IS a literal collection, just one this function didn't understand.
    Collects from BOTH `node.keys` and `node.values`, since a dict can pair
    via either side.
    """
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        elts = node.elts
    elif isinstance(node, ast.Dict):
        values: set[str] = set()
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                values.add(key.value)
        for val in node.values:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                values.add(val.value)
        return values
    elif isinstance(node, ast.Call):
        func = node.func
        func_name = func.id if isinstance(func, ast.Name) else None
        if func_name not in ("frozenset", "set", "tuple"):
            return None
        if not node.args:
            return set()
        arg = node.args[0]
        if isinstance(arg, (ast.Set, ast.List, ast.Tuple)):
            elts = arg.elts
        else:
            return None
    else:
        return None

    values = set()
    for elt in elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            values.add(elt.value)
    return values


def _is_literal_name(node: ast.expr) -> bool:
    """True if `node` is the bare name `Literal` or an attribute access ending
    in `.Literal` (i.e. `typing.Literal`) -- the two spellings a
    `Literal[...]` subscript's `value` can take.
    """
    if isinstance(node, ast.Name):
        return node.id == "Literal"
    if isinstance(node, ast.Attribute):
        return node.attr == "Literal"
    return False


def _literal_subscript_exempt_ids(tree: ast.AST) -> set[int]:
    """Return the `id()` of every AST node nested inside a `Literal[...]` (or
    `typing.Literal[...]`) subscript's slice.

    A `Literal[...]` is a TYPE-LEVEL enumeration, not a membership predicate:
    it declares the domain a value may take at type-check time and cannot be
    computed from a function call (`kind_values_for_canonical(...)` cannot
    appear inside a type subscript), and per the plan's D4 rule the wire enum
    deliberately carries every historical AND new `kind` value forever and
    must never narrow. Flagging it as an illegal retired/successor pairing
    would be asking the ONE place that must hold the full union to shrink
    itself -- exactly backwards. `summaries.py`'s `HandoffKind` is the
    concrete case this exempts; the exemption is by AST SHAPE (any
    `Literal[...]`/`typing.Literal[...]` subscript, anywhere), not a
    per-module carve-out, so it does not touch `_EXEMPT_MODULE_PATHS`.
    """
    exempt_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_literal_name(node.value):
            for descendant in ast.walk(node.slice):
                exempt_ids.add(id(descendant))
    return exempt_ids


def _collections_in_module(tree: ast.AST) -> list[tuple[int, set[str]]]:
    """Walk `tree` and return every (lineno, string-member-set) pair for
    every literal-collection shape this test polices: `ast.Set`/`List`/
    `Tuple`, `frozenset(...)`/`set(...)`/`tuple(...)` calls, `ast.Compare`
    with `In`/`NotIn` against a literal collection, and `ast.BoolOp` or-chains
    of `==` comparisons against string constants.

    Nodes nested inside a `Literal[...]`/`typing.Literal[...]` subscript are
    skipped entirely -- see `_literal_subscript_exempt_ids`.
    """
    found: list[tuple[int, set[str]]] = []
    exempt_ids = _literal_subscript_exempt_ids(tree)

    for node in ast.walk(tree):
        if id(node) in exempt_ids:
            continue

        if isinstance(node, (ast.Set, ast.List, ast.Tuple, ast.Call, ast.Dict)):
            values = _string_constants(node)
            if values:
                found.append((node.lineno, values))

        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    values = _string_constants(comparator)
                    if values:
                        found.append((node.lineno, values))

        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            values: set[str] = set()
            all_eq_string = True
            for value_node in node.values:
                if (
                    isinstance(value_node, ast.Compare)
                    and len(value_node.ops) == 1
                    and isinstance(value_node.ops[0], ast.Eq)
                ):
                    comparator = value_node.comparators[0]
                    if isinstance(comparator, ast.Constant) and isinstance(
                        comparator.value, str
                    ):
                        values.add(comparator.value)
                        continue
                    left = value_node.left
                    if isinstance(left, ast.Constant) and isinstance(left.value, str):
                        values.add(left.value)
                        continue
                all_eq_string = False
                break
            if all_eq_string and values:
                found.append((node.lineno, values))

    return found


def _find_illegal_pairings(
    py_files: Iterable[Path], alias_pairs: list[tuple[str, str]]
) -> list[str]:
    offenders: list[str] = []
    for path in py_files:
        rel = path.relative_to(_REPO_ROOT)
        if rel in _EXEMPT_MODULE_PATHS:
            continue
        if _is_test_path(rel):
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(rel))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for lineno, values in _collections_in_module(tree):
            for retired, target in alias_pairs:
                if retired in values and target in values:
                    offenders.append(
                        f"{rel}:{lineno}: literal collection pairs retired "
                        f"{retired!r} with successor {target!r}"
                    )
    return offenders


def test_exactly_one_module_is_exempt_from_the_aliasing_ban() -> None:
    """Self-check: the exemption list must have exactly one entry, so a
    future author cannot quietly widen the exemption instead of routing
    through the canonical `baton_class` function.
    """
    assert len(_EXEMPT_MODULE_PATHS) == 1
    exempt_path = _REPO_ROOT / _EXEMPT_MODULE_PATHS[0]
    assert exempt_path.is_file(), f"exempt module path does not exist: {exempt_path}"


def test_alias_pairs_are_coherent_with_the_schema() -> None:
    """Sanity check on the sourced data itself: every retired value's D1
    target must be a member of the schema's known-kind universe, and no
    retired value should also appear as a live schema enum member (it was
    retired precisely because C10 narrowed the enum to exclude it).
    """
    alias_pairs = _load_alias_pairs()
    assert alias_pairs, "_PRE_RENAME_ALIASES is empty — nothing to source"
    known_universe = _load_known_kind_universe()
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    live_enum = set(schema.get("properties", {}).get("kind", {}).get("enum", []))

    for retired, target in alias_pairs:
        assert target in known_universe, (
            f"alias target {target!r} (for retired {retired!r}) is not a "
            f"member of the schema's known-kind universe"
        )
        assert retired not in live_enum, (
            f"retired value {retired!r} is still a live schema enum member — "
            f"it should have been narrowed out by C10"
        )


def test_no_module_outside_baton_class_pairs_a_retired_value_with_its_successor() -> None:
    """The core structural assertion (AC4): outside `baton_class.py` and test
    files, no `.py` file in the repo contains a literal collection —
    frozenset/set/tuple/list literal, a `frozenset(...)`/`set(...)`/
    `tuple(...)` call, an `in (...)`/`not in (...)` compare, or an or-chain
    of `==` comparisons — that contains BOTH a retired D1 pre-rename `kind`
    value and its own D1 target successor.
    """
    alias_pairs = _load_alias_pairs()
    py_files = list(_iter_py_files())
    offenders = _find_illegal_pairings(py_files, alias_pairs)

    assert not offenders, (
        "Found module(s) outside coordinator_core/frontmatter/baton_class.py "
        "re-implementing the legacy<->target kind aliasing via a literal "
        "collection. Route through baton_class.baton_class() / "
        "canonical_kind() / kind_values_for_canonical() instead of pairing "
        "a retired value with its successor directly.\nOffenders:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "same_class_members",
    [
        pytest.param({"spinoff", "goal-seed"}, id="deflection-class-cross-kind"),
        pytest.param({"roadmap-baton", "roadmap-seed"}, id="intention-class-both-targets"),
    ],
)
def test_same_class_membership_sets_are_not_flagged_by_the_detector(
    same_class_members: set[str],
) -> None:
    """Regression guard on the detector itself, not the tree: a same-class
    (or otherwise PM-ruled) membership set that does NOT pair a retired
    value with its own successor must never be reported as illegal. Exercises
    `_find_illegal_pairings` directly against a synthetic module rather than
    depending on any specific file in the live tree still containing such a
    set at test-run time.
    """
    alias_pairs = _load_alias_pairs()
    synthetic_source = (
        "_LEGAL_SET = " + repr(same_class_members) + "\n"
    )
    tree = ast.parse(synthetic_source, filename="synthetic_legal.py")
    collections = _collections_in_module(tree)

    offenders = []
    for lineno, values in collections:
        for retired, target in alias_pairs:
            if retired in values and target in values:
                offenders.append((lineno, retired, target))

    assert not offenders, (
        f"detector incorrectly flagged a legal same-class set {same_class_members!r} "
        f"as an illegal retired/successor pairing: {offenders}"
    )
