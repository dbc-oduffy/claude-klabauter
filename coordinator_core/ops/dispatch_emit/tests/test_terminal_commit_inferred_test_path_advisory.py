"""An inferred sibling test path in a chunk's ``paths`` is advisory: only the declared subset gates
``declared-write-missing``; the emitter infers no test path under a tests/ directory that is absent."""

from __future__ import annotations


import pytest

from coordinator_core.ops.dispatch_emit.pathspec import candidate_test_additions
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed

_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 1,
    "wave_reviews": reviewed()["wave_reviews"],
}
_SID = "11111111-2222-3333-4444-555555555555"
SRC = "tools/likeness-prototype/likeness/conform_preflight.py"
INFERRED = "tools/likeness-prototype/likeness/tests/test_conform_preflight.py"


def test_emitter_skips_candidate_under_absent_tests_dir(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("", encoding="utf-8")

    assert candidate_test_additions(["pkg/mod.py"], tmp_path) == []

    (tmp_path / "pkg" / "tests").mkdir()
    present = candidate_test_additions(["pkg/mod.py"], tmp_path)
    assert len(present) == 1 and present[0].endswith("pkg/tests/test_mod.py")
