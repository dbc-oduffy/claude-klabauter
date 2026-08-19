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
    return False


def _blocked_by_tainted_names(func: ast.AST) -> frozenset:
    """Local names bound, anywhere in `func`, to an expression containing a
    `blocked_by` read.

    WHY THIS EXISTS (review: code-reviewer slice A, major). Without it the
    guard only fires when the read is textually nested INSIDE the verdict
    write's own value expression, so the obvious one-line refactor

        blockers = fm["blocked_by"]
        return {"pickup_ready": not blockers, ...}

    walks straight through it. That is not an obscure spelling — it is the
    natural shape of the withdrawn `_is_ready` helper this AC exists to
    prevent, so a guard blind to it would have missed the very near-miss
    that motivated AC7.

    Taint propagates through assignment to a FIXPOINT, because one hop is not
    enough for the shape above: `blockers` is bound from the read, then
    `ready` is bound from `blockers`, and only `ready` reaches the write.

    It propagates through assignment ONLY, which is what keeps the narrowing
    below intact. The measured false positive (`pickup_assemble.brief`) reads
    `blocked_by` into a local and separately writes `deployment_state` from an
    unrelated value; no assignment chain connects the two, so it stays clean.
    Propagating through mere co-presence in a function would re-break it.
    """
    tainted: set = set()

    def _bindings():
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        yield target.id, node.value
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if node.value is not None and isinstance(node.target, ast.Name):
                    yield node.target.id, node.value
            elif isinstance(node, ast.NamedExpr):
                if isinstance(node.target, ast.Name):
                    yield node.target.id, node.value

    bindings = list(_bindings())
    changed = True
    while changed:
        changed = False
        for name, value in bindings:
            if name in tainted:
                continue
            if _contains_blocked_by_read(value, frozenset(tainted)):
                tainted.add(name)
                changed = True
    return frozenset(tainted)


def _contains_blocked_by_read(node: ast.AST, tainted: frozenset = frozenset()) -> bool:
    if any(_is_blocked_by_read(n) for n in ast.walk(node)):
        return True
    return any(
        isinstance(n, ast.Name) and n.id in tainted for n in ast.walk(node)
    )


def _dict_literal_verdict_write_reads_blocked_by(
    node: ast.AST, tainted: frozenset = frozenset()
) -> bool:
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
            and _contains_blocked_by_read(value, tainted)
        ):
            return True
    return False


def _subscript_assign_verdict_write_reads_blocked_by(
    assign: ast.Assign, tainted: frozenset = frozenset()
) -> bool:
    """True iff `assign` targets `x["pickup_ready"|"deployment_state"] = ...`
    AND the assigned VALUE expression itself contains a `blocked_by` read —
    the dataflow-narrowed form for the subscript-assignment write shape."""
    for target in assign.targets:
        if isinstance(target, ast.Subscript):
            key = target.slice
            if (
                isinstance(key, ast.Constant)
                and key.value in _TARGET_KEYS
                and _contains_blocked_by_read(assign.value, tainted)
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
            tainted = _blocked_by_tainted_names(node)
            for inner in ast.walk(node):
                if inner is node:
                    continue
                if _dict_literal_verdict_write_reads_blocked_by(inner, tainted):
                    violated = True
                    break
                if isinstance(inner, ast.Assign) and _subscript_assign_verdict_write_reads_blocked_by(
                    inner, tainted
                ):
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


def _classify_source(source: str) -> bool:
    """Run the SAME per-function logic `find_gate_verdict_co_occurrences`
    runs, taint included, over one source string. Kept in lockstep with the
    production walk so a self-test cannot pass against a weaker path than
    the real guard uses."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = _blocked_by_tainted_names(node)
        for inner in ast.walk(node):
            if inner is node:
                continue
            if _dict_literal_verdict_write_reads_blocked_by(inner, tainted):
                return True
            if isinstance(inner, ast.Assign) and _subscript_assign_verdict_write_reads_blocked_by(
                inner, tainted
            ):
                return True
    return False


class TestCollectorSelfTest:
    """Fixture-level self-test of the AST collector, independent of the live
    tree — pins the collector's own true-positive/true-negative behaviour."""

    def test_detects_planted_co_occurrence(self, tmp_path, monkeypatch) -> None:
        """Drives the REAL entry point, not just the classifier helper.

        Review (code-reviewer slice A, minor): exercising the predicate
        directly left the file-discovery pipeline unproven, so the guard
        could have failed to find a real on-disk violation while this test
        stayed green.
        """
        planted_dir = tmp_path / "coordinator_core"
        planted_dir.mkdir()
        (planted_dir / "planted_second_evaluator.py").write_text(
            "def _is_ready(handoff):\n"
            "    return {\n"
            "        'pickup_ready': not handoff.get('blocked_by'),\n"
            "        'deployment_state': 'x',\n"
            "    }\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "coordinator_core.reconcile.tests.test_no_second_gate_verdict_walk._SCOPE_ROOTS",
            (planted_dir,),
        )

        violations = find_gate_verdict_co_occurrences()

        assert [fn for _p, fn, _ln in violations] == ["_is_ready"]

    def test_detects_the_hoisted_read_shape(self) -> None:
        """The withdrawn `_is_ready` refactored one step — the read bound to a
        local first. Review (code-reviewer slice A, major): the guard was blind
        to this, which is the natural shape of the very near-miss AC7 exists to
        catch, not an obscure spelling."""
        assert _classify_source(
            "def _is_ready(handoff):\n"
            "    blockers = handoff['blocked_by']\n"
            "    ready = not blockers\n"
            "    return {'pickup_ready': ready, 'deployment_state': 'x'}\n"
        ) is True

    def test_detects_the_hoisted_read_in_subscript_assignment(self) -> None:
        assert _classify_source(
            "def _stamp(handoff, out):\n"
            "    blockers = handoff.get('blocked_by')\n"
            "    out['deployment_state'] = 'awaiting_gate' if blockers else 'ready_to_fire'\n"
        ) is True

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
