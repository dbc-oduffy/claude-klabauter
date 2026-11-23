"""`review.reverify` classes a red the run's own delta causes as the run's own regression: an untouched
test file importing an edited module, green at the run base and red in the export of base plus only the
run's delta, stays `unattributed` (blocking) even when its cause file is dirty versus HEAD and unheld."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.review_test_blocker import from_reverify
from coordinator_core.ops.tests.test_review_reverify import (  # noqa: F401  (world is a fixture)
    BASE, PREFIX, SESSION, world,
)
from coordinator_core.ops.tests.test_review_reverify_sibling_owned import SIBLING_FILE, _dirty_sibling

IMPORTER = "tests/test_untouched_importer.py"
PARAM_NODE = f"{IMPORTER}::test_deny_alternatives_pass_own_classifier"
TRACE = f"{IMPORTER}:9: in test_deny_alternatives_pass_own_classifier\n    {SIBLING_FILE}:4: SyntaxError\n"

OK = json.dumps({"baseline_export": {"sha": "a" * 40, "export": "ok", "why": None, "runner_exit": 1, "removed": True}})


def _export(root, body: str, name: str) -> str:
    path = root / name
    path.write_text(body + "\n" + OK + "\n", encoding="utf-8")
    return str(path)


def _call(root, *, base_red: str, export_red: str):
    (root / IMPORTER).write_text("def test_deny_alternatives_pass_own_classifier():\n    assert True\n", encoding="utf-8")
    return rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", failing_output={PARAM_NODE: TRACE},
        base_export_path=_export(root, base_red, "base.txt"),
        export_failing_path=_export(root, export_red, "export.txt"),
    )


def test_green_at_base_red_with_only_the_run_delta_is_the_runs_own(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _call(root, base_red="PASSED tests/other.py::test_x", export_red=f"FAILED {PARAM_NODE} - SyntaxError")
    assert out["foreign_red"] == {} and out["sibling_owned"] == [] and out["foreign"] == []
    assert out["peer_dirt"] == [] and out["pre_existing"] == []
    assert out["unattributed"] == [PARAM_NODE]
    assert from_reverify({**out, "failing": [PARAM_NODE]}) != []


def test_green_at_base_red_with_only_the_run_delta_beats_a_halt_held_cause(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=True)
    out = _call(root, base_red="PASSED tests/other.py::test_x", export_red=f"FAILED {PARAM_NODE}")
    assert out["sibling_owned"] == [] and out["foreign_red"] == {} and out["unattributed"] == [PARAM_NODE]


def test_green_in_the_run_delta_export_stays_peer_dirt(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    out = _call(root, base_red="PASSED tests/other.py::test_x", export_red="PASSED tests/other.py::test_x")
    assert out["peer_dirt"] == [PARAM_NODE] and out["unattributed"] == [] and out["foreign_red"] == {}


def test_red_without_a_run_delta_export_measure_keeps_foreign_red(world, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _orig, _declared = world
    _dirty_sibling(root, monkeypatch, held=False)
    (root / IMPORTER).write_text("def test_deny_alternatives_pass_own_classifier():\n    assert True\n", encoding="utf-8")
    out = rr.review_reverify(
        root, base=BASE, slice_prefix=PREFIX, session_id=SESSION, failing=[PARAM_NODE],
        run_id="me", failing_output={PARAM_NODE: TRACE},
    )
    assert out["foreign_red"] == {PARAM_NODE: [SIBLING_FILE]}
