"""test_cc_invoke_mutating_caller_gate — the repo-wide gate C31 exists to add.

Per-family tests (C11/C12/C13/C26/C27) each pin one hand-fixed file; none of
them says anything about caller ELEVEN. This is the construct that binds
every future caller of a MUTATING op to `route_mutation()` /
`mutation_refusal_message()` — the two names `cc_invoke.py` exports beside
`route_mutation` (per its own docstring) as the shared refusal-inspection
seam every mutating caller is expected to route through, rather than
hand-copying a fresh exit_code/error ladder into a new file.

`OP_CLASSIFICATION` (`coordinator_core.authz.classification`) is the oracle
for "which ops are MUTATING" (C17 completes its coverage). This module
statically (AST) enumerates every `coordinator/bin/*.py` CLI's call sites
that pass a MUTATING op's literal name as the first positional argument to
one of the four transport entry points (`route`, `cc_invoke`,
`cc_invoke_bare`, `route_mutation`) and requires each such call site's own
file to show ONE of:

  (a) the call itself IS `route_mutation(...)` — the raising helper, compliant
      by construction;
  (b) `mutation_refusal_message(` appears in the file — the extracted
      inspection helper is in use (`route()`/`cc_invoke()` callers that need
      custom partial-success handling, e.g. reap-sessions.py's best-effort
      log-and-continue);
  (c) the file inspects the in-envelope refusal shape by hand (`exit_code`,
      or a per-item `"error"` field check) — the DETERMINATE-PARTIAL act-call
      shape (`build_act_result`, exit_code=2) that `route_mutation()` cannot
      correctly express (it would hard-raise on a legitimate partial
      success — see prune-closed-bugs.py's Review comment), so a manual
      ladder is the documented, correct shape there, not the anti-pattern;
  (d) the file's own docstring states the op's response "carries no error
      signal" of its own on success (e.g. archive-paper-trail.py,
      advance-tracker-status.py) — a documented, op-level reason no
      refusal-shape inspection is possible, not an omission.

A call site with NONE of the above — the "caller ten" shape: bare
`route()`/`cc_invoke()`, result printed, exit 0 unconditionally — fails this
gate. `TestGateDetectorCatchesANaiveCaller` proves the detector itself
flags exactly that shape, independent of which real files currently exist,
so the gate stays meaningful even as the file population drifts.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C31.md
"""
from __future__ import annotations

import ast
import os
import re
import unittest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)

_TRANSPORT_NAMES = {"route", "cc_invoke", "cc_invoke_bare", "route_mutation"}

_EVIDENCE_TOKENS = (
    "mutation_refusal_message(",
    "exit_code",
    '.get("error")',
    ".get('error')",
    '["error"]',
    "['error']",
)
_EVIDENCE_PHRASE_RE = re.compile(r"carries\s+no\s+error", re.IGNORECASE)


def mutating_op_names() -> set[str]:
    """Every op name OP_CLASSIFICATION marks MUTATING — the oracle (C17)."""
    return {name for name, cls in OP_CLASSIFICATION.items() if cls == OpClass.MUTATING}


def _func_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def find_mutating_call_sites(source: str, muts: set[str]) -> list[tuple[str, str]]:
    """Return [(op_name, transport_fn_name), ...] for every call in `source`
    to a transport entry point whose first positional arg is a literal
    MUTATING op name. Best-effort: a file that fails to parse yields []."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hits: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        fn = _func_name(node.func)
        if fn not in _TRANSPORT_NAMES:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) and first.value in muts:
            hits.append((first.value, fn))
    return hits


def has_refusal_evidence(source: str) -> bool:
    """True if `source` shows some form of in-envelope refusal handling
    (see module docstring (a)-(d)) beyond a bare transport call."""
    if any(tok in source for tok in _EVIDENCE_TOKENS):
        return True
    normalised = re.sub(r"\s+", " ", source)
    return bool(_EVIDENCE_PHRASE_RE.search(normalised))


def gate_violations(bin_dir: str, muts: set[str]) -> list[str]:
    """Return one message per (file, op) call site that routes a MUTATING op
    through a bare transport call with no refusal-inspection evidence."""
    violations: list[str] = []
    for name in sorted(os.listdir(bin_dir)):
        if not name.endswith(".py") or name.startswith("test_"):
            continue
        path = os.path.join(bin_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                source = fh.read()
        except OSError:
            continue
        hits = find_mutating_call_sites(source, muts)
        if not hits:
            continue
        naked = [(op, fn) for op, fn in hits if fn != "route_mutation"]
        if not naked:
            continue
        if has_refusal_evidence(source):
            continue
        for op, fn in naked:
            violations.append(
                f"{name}: op={op!r} called via {fn}() with no refusal-inspection "
                "evidence (route_mutation/mutation_refusal_message/exit_code/"
                "error-field/'carries no error' rationale)"
            )
    return violations


class TestMutatingCallerGate(unittest.TestCase):
    """Repo-wide: every coordinator/bin/*.py caller of a MUTATING op routes
    through the refusal-raising helper (or documents why it need not)."""

    def test_no_naked_mutating_call_sites(self) -> None:
        muts = mutating_op_names()
        self.assertGreater(len(muts), 0, "OP_CLASSIFICATION reported zero MUTATING ops")
        violations = gate_violations(_BIN_DIR, muts)
        self.assertEqual(
            violations,
            [],
            "MUTATING op call site(s) with no refusal-inspection evidence:\n"
            + "\n".join(violations),
        )


class TestGateDetectorCatchesANaiveCaller(unittest.TestCase):
    """Detector self-test: proves gate_violations() actually flags the
    'caller ten' shape, independent of which real files exist today."""

    _FAKE_OP = "fake.mutating_op_for_gate_test"

    def test_bare_route_call_with_no_evidence_is_flagged(self) -> None:
        source = (
            "import cc_invoke\n"
            f'result = cc_invoke.route("{self._FAKE_OP}", {{}}, "/repo", lambda: None)\n'
            "print(result)\n"
        )
        muts = {self._FAKE_OP}
        hits = find_mutating_call_sites(source, muts)
        self.assertEqual(hits, [(self._FAKE_OP, "route")])
        self.assertFalse(has_refusal_evidence(source))

    def test_route_mutation_call_is_compliant_by_construction(self) -> None:
        source = (
            "import cc_invoke\n"
            f'result = cc_invoke.route_mutation("{self._FAKE_OP}", {{}}, "/repo", lambda: None)\n'
        )
        hits = find_mutating_call_sites(source, {self._FAKE_OP})
        self.assertEqual(hits, [(self._FAKE_OP, "route_mutation")])

    def test_bare_call_with_exit_code_inspection_is_not_flagged(self) -> None:
        source = (
            "import cc_invoke\n"
            f'result = cc_invoke.route("{self._FAKE_OP}", {{}}, "/repo", lambda: None)\n'
            'if result.get("exit_code"):\n'
            "    raise RuntimeError('refused')\n"
        )
        self.assertTrue(has_refusal_evidence(source))

    def test_gate_violations_end_to_end_on_a_synthetic_bin_dir(self) -> None:
        import tempfile

        muts = {self._FAKE_OP}
        with tempfile.TemporaryDirectory() as tmp:
            naive_path = os.path.join(tmp, "naive-caller.py")
            with open(naive_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import cc_invoke\n"
                    f'result = cc_invoke.route("{self._FAKE_OP}", {{}}, "/repo", lambda: None)\n'
                    "print(result)\n"
                )
            compliant_path = os.path.join(tmp, "compliant-caller.py")
            with open(compliant_path, "w", encoding="utf-8") as fh:
                fh.write(
                    "import cc_invoke\n"
                    f'result = cc_invoke.route_mutation("{self._FAKE_OP}", {{}}, "/repo", lambda: None)\n'
                )

            violations = gate_violations(tmp, muts)

        self.assertEqual(len(violations), 1)
        self.assertIn("naive-caller.py", violations[0])


if __name__ == "__main__":
    unittest.main()
