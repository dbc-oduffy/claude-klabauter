"""record_paths: the shared record-prefix set agrees with the JS the review gate emits."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import record_paths
from coordinator_core.ops.review_mint import execute_review


@pytest.mark.parametrize(
    "rel",
    [
        "state/run-evidence/x/evidence.md",
        "archive/a.json",
        "scratch/a/b.py",
        "tasks/t.json",
        "cross-repo/m.json",
        "docs/plans/p.yaml",
        ".coordinator-local/x/y.json",
        "README.md",
        "coordinator_core/notes.md",
        "state\\warp\\r\\halted.json",
        "./state/a.json",
    ],
)
def test_record_paths_accepted(rel):
    assert record_paths.is_record_path(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "coordinator_core/ops/x.py",
        "scripts/setup.py",
        "statex/a.py",
        "state",
        "docs",
        "src/state/a.py",
        "coordinator_local/a.py",
        "plugin/commands/index.md",
        "plugin\\commands\\index.md",
        "skills/x/SKILL.md",
        "docs/reference/hooks.yaml",
    ],
)
def test_product_paths_refused(rel):
    assert not record_paths.is_record_path(rel)


def test_js_regex_matches_the_literal_the_review_gate_hardcodes():
    src = record_paths.record_prefix_js_regex()
    assert src == "^(state|archive|scratch|tasks|cross-repo|docs|\\.coordinator-local)\\/"
    assert "/" + src + "/" in execute_review._DECLARED_PRODUCT_WRITE_JS.replace("\\\\", "\\")
