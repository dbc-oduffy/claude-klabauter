"""coordinator_core.source_edit_gate -- base-vs-candidate test-regression gate.

Answers one question for a bulk source-editing tool (the comment stripper is
the first caller, `bin/strip-comments.py` / C3): "did this edit make any test
newly fail?" It never touches file bytes itself -- the caller applies its own
edit before calling `run_gate`, and supplies two callbacks (`restore_originals`,
`reapply_stripped`) that `run_gate` invokes between and after its two test
runs. See `gate.py` module docstring for the full contract.

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md § C1.
"""

from __future__ import annotations

from .gate import (
    GateResult,
    TestReport,
    run_gate,
)

__all__ = [
    "GateResult",
    "TestReport",
    "run_gate",
]
