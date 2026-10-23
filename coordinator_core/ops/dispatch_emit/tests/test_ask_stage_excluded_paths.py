"""An XS mint drops writes the sizing's intent names inside a do-not-touch sentence."""

from __future__ import annotations

import json

import pytest
import yaml

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, stage

SIZING_REL = "state/sizings/2026-10-16-fixture.yaml"
INTENT = (
    "Fix the thing in pkg/a.py and tests/test_a.py. "
    "Do not touch the uncommitted peer edits in lib/dep.py or tests/fixtures.py."
)


def _sizing(repo, intent=INTENT):
    doc = {"intent": intent, "exit_criterion": {"statement": "it works"}, "estimate": {"tshirt": "XS"}}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def _x1_writes(manifest):
    return [w for r in manifest.rows if r.id == "X1" for w in r.writes]


def test_paths_the_ask_excludes_leave_the_footprint_and_others_stay(tmp_path):
    _sizing(tmp_path)
    m = stage(
        tmp_path, run_id="r1", sizing_rel=SIZING_REL,
        writes=["pkg/a.py", "tests/test_a.py", "lib/dep.py", "tests/fixtures.py"],
    )
    assert {"pkg/a.py", "tests/test_a.py"} <= set(_x1_writes(m))
    assert "lib/dep.py" not in json.dumps(m.to_json()["rows"])


def test_a_path_the_ask_does_not_exclude_is_kept_even_when_named_elsewhere(tmp_path):
    _sizing(tmp_path)
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py", "lib/dep.py"])
    assert "pkg/a.py" in _x1_writes(m)
    assert "lib/dep.py" not in _x1_writes(m)


def test_no_exclusion_sentence_keeps_every_write(tmp_path):
    _sizing(tmp_path, intent="Fix lib/dep.py and pkg/a.py.")
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py", "lib/dep.py"])
    assert {"lib/dep.py", "pkg/a.py"} <= set(_x1_writes(m))


def test_a_footprint_wholly_excluded_is_refused_as_no_writes(tmp_path):
    _sizing(tmp_path)
    with pytest.raises(AskStageError, match="^XS-MINT-NO-WRITES: "):
        stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["lib/dep.py"])
