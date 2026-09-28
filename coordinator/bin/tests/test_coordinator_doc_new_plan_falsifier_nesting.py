"""test_coordinator_doc_new_plan_falsifier_nesting.py -- coverage for F23
(klabauter#71): --type plan must scaffold the commented `falsifier:` block
NESTED under `prime_exit_criterion`, not as a top-level sibling.

Purpose: close_out's goal_gate refuses a plan whose (uncommented) falsifier
is a top-level sibling of prime_exit_criterion (falsifier_misnested) or
whose baseline_ref reads as prose rather than a SHA (baseline_ref_malformed).
The scaffold previously commented `falsifier:` at column 0, so uncommenting
it verbatim reproduced exactly the misnested shape the gate refuses. This
suite pins:

1. Every commented falsifier line (`falsifier:`, `how:`, `baseline_output:`,
   `baseline_ref:`, `expected_when_true:`) is indented 2 spaces -- nested
   under `prime_exit_criterion:`, not a sibling of it.
2. `baseline_ref`'s placeholder text reads as a SHA slot, not prose.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_plan_falsifier_nesting.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_plan_falsifier_nesting_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_plan_falsifier_nesting_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class TestFalsifierNestedUnderPrimeExitCriterion(unittest.TestCase):
    def test_falsifier_block_is_indented_under_prime_exit_criterion(self):
        content = _cli._scaffold_plan(title="a plan", branch="b", author="me")
        lines = content.splitlines()
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == "prime_exit_criterion:")
        # Collect the contiguous commented falsifier lines that follow.
        falsifier_lines = [
            ln for ln in lines[idx:idx + 20]
            if "falsifier" in ln or "baseline_ref" in ln or "baseline_output" in ln
            or "expected_when_true" in ln or ln.strip() == "how:" or "  #   how:" in ln
        ]
        self.assertTrue(falsifier_lines, "no falsifier block found after prime_exit_criterion")
        for ln in falsifier_lines:
            # Nested (nonzero indent before the `#`) — never a bare column-0 `# falsifier:`.
            self.assertRegex(
                ln, r"^\s+#",
                f"falsifier line is not nested under prime_exit_criterion: {ln!r}",
            )

    def test_baseline_ref_placeholder_names_a_sha(self):
        content = _cli._scaffold_plan(title="a plan", branch="b", author="me")
        baseline_ref_line = next(
            ln for ln in content.splitlines() if "baseline_ref:" in ln and ln.strip().startswith("#")
        )
        self.assertIn("SHA", baseline_ref_line)


if __name__ == "__main__":
    unittest.main()
