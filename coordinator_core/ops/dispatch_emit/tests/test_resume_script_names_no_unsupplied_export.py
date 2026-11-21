"""A recovery (resume/refire) composition hands workers only exports the engine builds.

Witness: example-retrieval-repo run 59c7c7 / recovery 33fa0e. The resumed script told the test runner to pass
`--base-export <your run-base export>` to landing_export, a directory nothing in the run supplies, so the
leg ended review_blocked. `halt_resume_build.build` composes through `compose_ask_script` with the run's
`run_base_sha`, the same path as a fresh run: the run-base re-run is `baseline_export --sha`, which the
engine builds itself.
"""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_judge_evidence_and_run_base import _script

SHA = "f" * 40


def test_a_resume_composition_names_no_export_the_worker_must_hand_make(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _script(monkeypatch, run_base_sha=SHA)
    assert "--base-export " not in script and "run-base export" not in script and "test-landing" not in script
    for tail in re.findall(r"landing_export([^`]*)`", script):
        assert "--base-sha" not in tail, tail
    assert f"coordinator_core.testing.baseline_export --sha {SHA}" in script
