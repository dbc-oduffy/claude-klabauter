"""compose_ask_script: the no-folded-manifest error names why no fold adopted a manifest; a no-change commit halts as no-op."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import NO_MANIFEST_PREFIX, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.write_baseline import (
    DIRTY_BEFORE_RUN_KEY,
    MEANT_TO_LAND_KEY,
    NO_CHANGE_REFUSAL,
)

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(**over):
    kw = dict(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        in_run_commit=True,
        **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


@pytest.fixture
def script(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "xs")
    return _compose(writes=["a.py"])


def test_no_manifest_error_references_the_cause_binding(script):
    err = script[script.index(f"error: '{NO_MANIFEST_PREFIX}") :][:300]
    assert "_noFoldCause" in err
    assert "let _noFoldCause = null;" in script


def test_every_guarded_fold_site_that_can_decline_assigns_the_cause(script):
    guarded = re.findall(r"if \([^\n]*?\) \{ await _adoptStageReply\((?:_gate|_scouted|_apm)\.stage_reply\); \}", script)
    assert guarded
    for site in guarded:
        tail = script[script.index(site) + len(site) :][:80]
        assert tail.startswith(" else if (!_noFoldCause) { _noFoldCause = "), site


def test_a_reply_that_is_absent_records_its_cause(script):
    assert "stage_reply absent from the agent return" in script


def test_the_three_named_causes_are_composed(script):
    assert "scout not clean (exit_code/halt/arm)" in script
    assert "is outside the fold set" in script
    assert "gate halt: " in ask_compose._GATE_HALT_CAUSE_JS


def test_no_change_commit_halts_as_no_op_carrying_the_paths(script):
    assert f"if (_halted?.refused === '{NO_CHANGE_REFUSAL}') {{ Object.assign(_halted, {{ halted: 'no-op'" in script
    assert f"{MEANT_TO_LAND_KEY}: _commit?.{MEANT_TO_LAND_KEY} ?? null" in script
    assert f"{DIRTY_BEFORE_RUN_KEY}: _commit?.{DIRTY_BEFORE_RUN_KEY} ?? null" in script
    lines = ask_compose.roadmap_commit_lines(head="h", anchor="a", repo_root="r", run_id="run1")
    assert any("Object.assign(_halted" in ln for ln in lines)


def test_the_ledger_charter_excludes_pre_existing_dirt():
    charter = ask_compose._folded_ledger_charter(
        relayed=False, ledger_rel="l.md", record_dir="r", run_dir="d", in_run_commit=False
    )
    assert "write_baseline shows unchanged since stage was not delivered by this run" in charter
