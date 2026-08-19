"""AC7 enforcement (C1, docs/plans/2026-08-19-gate-notes-are-advisory-blocked-by-
derives-readiness.md § C1): a static, fast-tier guard for the "exactly one gate
evaluator" constraint `coordinator_core/reconcile/gate_eval.py`'s own module
docstring already states in prose — "a second file that reads handoff
frontmatter and independently decides gate status would be the shape to avoid".
A docstring is not a consumer scan (module docstring, "ENFORCE IT MECHANICALLY").

AC7 AS ACTUALLY SCOPED (Review: eng-director/the Director of Engineering, Finding 4 — major): the
original wording ("no module outside reconcile/gate_eval.py resolves blocked_by
entries itself") is unimplementable — ~20 non-test modules read/act on
`blocked_by` today (`handoff_reconcile.py`, `handoff_children.py`,
`roadmap_link_stubs.py`, `roadmap/graph.py`, `roadmap/audit.py`,
`reconcile/ac27_differential_oracle.py`, `pickup_assemble/__init__.py`, ...) —
a guard on that sentence reds on day one and only lands via a 20-entry
allowlist. The actual, narrower invariant this guard checks: no module OTHER
THAN `reconcile/gate_eval.py` may co-locate a `blocked_by` READ with a
`pickup_ready`/`deployment_state` WRITE inside the SAME function. That
co-occurrence is the near-miss that motivated AC7 (a peer session's withdrawn
`_is_ready` helper, days from landing exactly this shape) — reading
`blocked_by` elsewhere is normal and expected; independently re-DECIDING
readiness from it, in the same function, is the sibling-evaluator error.

STRUCTURAL DISCRIMINATORS (deliberately conservative — false negatives are
acceptable, a static co-occurrence check cannot see everything; false
positives on `reconcile/gate_eval.py` itself are prevented by the scope
exclusion below, not by a discriminator):
  - `blocked_by` READ: a string constant `"blocked_by"` used as a dict
    subscript key (`x["blocked_by"]`) or as the first positional/`key=`
    argument to a `.get(...)` call (`x.get("blocked_by")`).
  - `pickup_ready`/`deployment_state` WRITE: a dict LITERAL entry keyed
    `"pickup_ready"`/`"deployment_state"` whose OWN VALUE expression contains
    a `blocked_by` read (covers `return {"deployment_state": ... handoff.get
    ("blocked_by") ..., ...}`), OR a subscript assignment `x["pickup_ready"]
    = <expr containing a blocked_by read>`.

DATAFLOW-NARROWED, not merely "both appear somewhere in the function" (an
earlier, coarser version of this check false-positived on
`pickup_assemble.brief`, a ~900-line decision-object assembler that reads
`blocked_by` for one unrelated display purpose and separately echoes
`deployment_state` off already-computed frontmatter elsewhere in the same
function — no dataflow from the read into that write). The write's value
expression must itself CONTAIN the read for a violation to fire; this is
still conservative (false negatives remain acceptable per the header above),
not a weakening of what a real second evaluator looks like — `_is_ready`'s
withdrawn shape (`{"pickup_ready": not handoff.get("blocked_by"), ...}`) is
exactly this dataflow pattern.

Both conditions must be true WITHIN THE SAME FunctionDef/AsyncFunctionDef (not
merely the same file/class) for a violation to fire — matching the "same
function" wording of the narrowed invariant.

Kept on the fast tier: a static, in-process AST parse over already-imported
source files, no subprocess, no git spawn, no network, no clock read.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, List, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: The ONE module permitted to co-locate a `blocked_by` read with a
#: `pickup_ready`/`deployment_state` write — this is the evaluator itself.
_EXEMPT_MODULE = _REPO_ROOT / "coordinator_core" / "reconcile" / "gate_eval.py"

#: Scope roots: the same "code that could plausibly decide gate status"
#: surface AC7 cares about. Test files are excluded — a test fixture
#: legitimately constructs `{"blocked_by": [...], "pickup_ready": ...}` dicts
#: to exercise the evaluator, which is not a second evaluator.
_SCOPE_ROOTS = (_REPO_ROOT / "coordinator_core",)

_TARGET_KEYS = frozenset({"pickup_ready", "deployment_state"})


def _discover_source_files() -> Iterator[Path]:
    for root in _SCOPE_ROOTS:
        for path in root.rglob("*.py"):
            if path == _EXEMPT_MODULE:
                continue
            parts = path.parts
            if "tests" in parts or path.name.startswith("test_"):
                continue
            if "__pycache__" in parts:
                continue
            yield path


def _is_blocked_by_read(node: ast.AST) -> bool:
    """True iff `node` is `x["blocked_by"]` or `x.get("blocked_by", ...)`."""
    if isinstance(node, ast.Subscript):
        key = node.slice
        return isinstance(key, ast.Constant) and key.value == "blocked_by"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr != "get":
            return False
        args = node.args
        if args and isinstance(args[0], ast.Constant) and args[0].value == "blocked_by":
            return True
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant) and kw.value.value == "blocked_by":
                return True
    return False


def _contains_blocked_by_read(node: ast.AST) -> bool:
    return any(_is_blocked_by_read(n) for n in ast.walk(node))


def _dict_literal_verdict_write_reads_blocked_by(node: ast.AST) -> bool:
    """True iff `node` is a dict LITERAL carrying a `pickup_ready`/
    `deployment_state` key whose OWN VALUE expression (not some unrelated
    sibling entry, and not merely "appears somewhere in the same function")
    itself contains a `blocked_by` read — the DATAFLOW-narrowed form of the
    write half of the co-occurrence check.

    Narrowed (not merely "both appear in this function") deliberately: a
    large orchestrator function may legitimately read `blocked_by` for
    display purposes AND separately echo `pickup_ready`/`deployment_state`
    off frontmatter elsewhere in the same function without either informing
    the other — that shape is not a second gate-verdict decision and must not
    trip this guard (measured false positive: `pickup_assemble.brief`, a
    ~900-line decision-object assembler that reads `blocked_by` for one
    unrelated purpose and separately echoes `deployment_state` from
    already-computed frontmatter elsewhere in the same function).
    """
    if not isinstance(node, ast.Dict):
        return False
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value in _TARGET_KEYS
            and value is not None
            and _contains_blocked_by_read(value)
        ):
            return True
    return False


def _subscript_assign_verdict_write_reads_blocked_by(assign: ast.Assign) -> bool:
    """True iff `assign` targets `x["pickup_ready"|"deployment_state"] = ...`
    AND the assigned VALUE expression itself contains a `blocked_by` read —
    the dataflow-narrowed form for the subscript-assignment write shape."""
    for target in assign.targets:
        if isinstance(target, ast.Subscript):
            key = target.slice
            if (
                isinstance(key, ast.Constant)
                and key.value in _TARGET_KEYS
                and _contains_blocked_by_read(assign.value)
            ):
                return True
    return False


def find_gate_verdict_co_occurrences() -> List[Tuple[Path, str, int]]:
    """Return `(file, function_qualname, lineno)` for every function outside
    `gate_eval.py` that maps a `blocked_by` read DIRECTLY onto a
    `pickup_ready`/`deployment_state` write — i.e. the write's own value
    expression contains the read, not merely "both appear somewhere in this
    function" (see the dataflow-narrowing docstrings above for why the
    coarser form false-positives on ordinary display-assembly code).
    """
    violations: List[Tuple[Path, str, int]] = []
    for path in _discover_source_files():
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            violated = False
            for inner in ast.walk(node):
                if inner is node:
                    continue
                if _dict_literal_verdict_write_reads_blocked_by(inner):
                    violated = True
                    break
                if isinstance(inner, ast.Assign) and _subscript_assign_verdict_write_reads_blocked_by(inner):
                    violated = True
                    break
            if violated:
                violations.append((path, node.name, node.lineno))
    return violations


class TestNoSecondGateVerdictWalk:
    def test_no_module_outside_gate_eval_co_locates_blocked_by_read_with_verdict_write(self) -> None:
        violations = find_gate_verdict_co_occurrences()
        assert violations == [], (
            "AC7 violation — a function outside reconcile/gate_eval.py co-locates "
            "a `blocked_by` read with a `pickup_ready`/`deployment_state` write "
            "(a second gate-verdict walk): "
            + ", ".join(f"{p}:{ln} ({fn})" for p, fn, ln in violations)
        )


class TestCollectorSelfTest:
    """Fixture-level self-test of the AST collector, independent of the live
    tree — pins the collector's own true-positive/true-negative behaviour."""

    def test_detects_planted_co_occurrence(self, tmp_path) -> None:
        planted = tmp_path / "planted_second_evaluator.py"
        planted.write_text(
            "def _is_ready(handoff):\n"
            "    return {\n"
            "        'pickup_ready': not handoff.get('blocked_by'),\n"
            "        'deployment_state': 'x',\n"
            "    }\n"
        )
        tree = ast.parse(planted.read_text())
        fn = tree.body[0]
        violated = any(
            _dict_literal_verdict_write_reads_blocked_by(n)
            for n in ast.walk(fn)
            if n is not fn
        )
        assert violated is True

    def test_unrelated_blocked_by_read_and_verdict_write_in_same_function_is_not_a_violation(
        self,
    ) -> None:
        """The dataflow-narrowed shape (`pickup_assemble.brief`'s measured
        false positive): a function reads `blocked_by` for one purpose and
        separately writes `deployment_state` from an unrelated value — no
        dataflow from the read into the write."""
        source = (
            "def orchestrator(handoff, computed_state):\n"
            "    blocked = handoff.get('blocked_by')\n"
            "    log(blocked)\n"
            "    return {'deployment_state': computed_state, 'other': 1}\n"
        )
        tree = ast.parse(source)
        fn = tree.body[0]
        violated = any(
            _dict_literal_verdict_write_reads_blocked_by(n)
            for n in ast.walk(fn)
            if n is not fn
        ) or any(
            isinstance(n, ast.Assign) and _subscript_assign_verdict_write_reads_blocked_by(n)
            for n in ast.walk(fn)
            if n is not fn
        )
        assert violated is False

    def test_ordinary_blocked_by_read_alone_is_not_a_violation(self) -> None:
        source = (
            "def read_only(handoff):\n"
            "    return handoff.get('blocked_by')\n"
        )
        tree = ast.parse(source)
        fn = tree.body[0]
        has_read = any(_is_blocked_by_read(n) for n in ast.walk(fn) if n is not fn)
        assert has_read is True
