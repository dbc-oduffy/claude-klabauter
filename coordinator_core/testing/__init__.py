"""
coordinator_core.testing — claude-klabauter's cross-repo full-test runner sub-package.

Purpose: aggregates the four test-file conventions found across coordinator-family
repos (JS x2, Python x2) into a single discover -> run -> report pipeline,
with mandatory bundled-venv exclusion and real pass/fail exit-code semantics.

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: docs/plans/2026-07-19-claude-klabauter-doe-full-test-runner.md

Negative-spec: this package ships NO committed `test_*.py` fixture dummies — every
synthetic fixture file used by this package's own tests is generated under pytest's
`tmp_path` at runtime (Finding 9). Do not add a fixture file directly under this
package tree; claude-klabauter's own pytest config (`testpaths = ["coordinator_core"]`,
`python_files = ["test_*.py"]`) would auto-collect it into claude-klabauter's real suite.
"""

from __future__ import annotations
