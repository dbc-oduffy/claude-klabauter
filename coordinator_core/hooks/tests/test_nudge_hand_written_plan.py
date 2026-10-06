"""Tests for the hand-written-plan post-write nudge leg and its dispatcher fan-in."""

from __future__ import annotations

import asyncio
import unittest.mock as mock

import pytest

from coordinator_core.hooks import nudge_hand_written_plan as nhp
from coordinator_core.hooks import postuse_advisory_dispatch as pad
from coordinator_core.ops.artifact_adopt_contract import adopt_command

PLAN = "docs/plans/2026-10-06-x.md"
NO_FM = "# Plan\n\nbody\n"
NO_ID_FM = "---\ntitle: X\n---\n# Plan\n"
MINTED = '---\nplan_id: "pln-abc-123"\n---\n# Plan\n'


@pytest.fixture(autouse=True)
def _sentinel_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(nhp.tempfile, "gettempdir", lambda: str(tmp_path))


def _run(tool="Write", path=PLAN, content=NO_FM, session="test-session-hw-a", root=None):
    return asyncio.run(nhp.advisory_text(tool, path, content, session, root))


@pytest.mark.parametrize("content", [NO_FM, NO_ID_FM])
def test_fires_for_frontmatterless_and_planidless(content):
    text = _run(content=content, session="test-session-hw-fires")
    assert text.startswith("[adopt] ")
    assert adopt_command(PLAN) in text
    assert "\n" not in text


def test_fires_for_absolute_path_under_repo_root(tmp_path):
    abs_path = str(tmp_path / "docs" / "plans" / "2026-10-06-x.md")
    text = _run(path=abs_path, root=tmp_path, session="test-session-hw-abs")
    assert adopt_command(PLAN) in text


@pytest.mark.parametrize(
    "tool,path,content",
    [
        ("Write", PLAN, MINTED),
        ("Write", "docs/plans/2026-10-06-x.review.md", NO_FM),
        ("Edit", PLAN, NO_FM),
        ("MultiEdit", PLAN, NO_FM),
        ("Write", "docs/research/x.md", NO_FM),
        ("Write", "docs/plans/sub/x.md", NO_FM),
    ],
)
def test_silent_cases(tool, path, content):
    assert _run(tool=tool, path=path, content=content, session="test-session-hw-silent") == ""


def test_second_identical_write_same_session_is_silent():
    assert _run(session="test-session-hw-dup")
    assert _run(session="test-session-hw-dup") == ""


def test_other_session_and_no_session_still_fire():
    assert _run(session="test-session-hw-s1")
    assert _run(session="test-session-hw-s2")
    assert _run(session="")
    assert _run(session="")


def test_dispatcher_merges_into_one_post_advisory():
    params = {
        "session_id": "test-session-hw-merge",
        "transcript_path": "",
        "agent_id": "",
        "tool_name": "Write",
        "file_path": PLAN,
        "content": NO_FM,
    }
    with mock.patch.object(pad, "_check_context_pressure_sync", return_value="cp text"):
        with mock.patch.object(pad, "_check_runtime_tripwire_sync", return_value="rt text"):
            result = asyncio.run(pad._handler(params))
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("cp text") < context.index("rt text") < context.index("[adopt]")
    assert context.count("[adopt]") == 1


def test_dispatcher_fires_without_session_id():
    params = {"tool_name": "Write", "file_path": PLAN, "content": NO_FM}
    result = asyncio.run(pad._handler(params))
    assert "[adopt]" in result["hookSpecificOutput"]["additionalContext"]
