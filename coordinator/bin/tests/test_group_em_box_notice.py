"""Tests for bin/group-em-box-notice.py -- the box-wide Group EM notice (pure legs).

Zero subprocess spawns. Covers recipient selection and notice rendering. The
`build_notice`/`main` legs load the Group EM skill's `send_pass.py`, which ships
with the doctrine tree, so they are tested there.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[1] / "group-em-box-notice.py"
_spec = importlib.util.spec_from_file_location("group_em_box_notice", _PATH)
assert _spec is not None and _spec.loader is not None
gbn = importlib.util.module_from_spec(_spec)
sys.modules["group_em_box_notice"] = gbn
_spec.loader.exec_module(gbn)

AGENTS = [
    {"sessionId": "sid-me", "name": "coordinator-content-repo-9", "cwd": "/fake/coordinator-content-repo"},
    {"sessionId": "sid-a", "name": "claude-klabauter-e3", "cwd": "/fake/claude-klabauter"},
    {"sessionId": "sid-b", "name": "example-cockpit-repo-67", "cwd": "/fake/example-cockpit-repo"},
    {"sessionId": "sid-bg", "cwd": "/fake/example-store-repo", "kind": "background"},
    {"sessionId": "sid-a", "name": "claude-klabauter-e3", "cwd": "/fake/claude-klabauter"},
]


def test_recipients_are_every_named_session_in_any_repo_minus_self_and_dupes():
    got = gbn.box_recipients(AGENTS, "sid-me")

    assert [r["name"] for r in got] == ["claude-klabauter-e3", "example-cockpit-repo-67"]


def test_a_skipped_session_is_not_a_recipient():
    got = gbn.box_recipients(AGENTS, "sid-me", frozenset({"sid-a"}))

    assert [r["sessionId"] for r in got] == ["sid-b"]


@pytest.mark.parametrize("kind", [gbn.NOTICE_ENTERED, gbn.NOTICE_ENDED])
def test_the_notice_asks_nothing_and_says_no_reply_is_wanted(kind):
    text = gbn.render_notice(kind, {"sessionId": "sid-me", "name": "coordinator-content-repo-9"})

    assert "?" not in text
    assert "no reply is wanted" in text
    assert "coordinator-content-repo-9" in text


def test_the_entered_notice_names_the_send_address():
    text = gbn.render_notice(gbn.NOTICE_ENTERED, {"sessionId": "sid-me", "name": "coordinator-content-repo-9"})

    assert "SendMessage to 'coordinator-content-repo-9'" in text


def test_the_entered_notice_routes_a_second_opinion_before_the_pm():
    text = gbn.render_notice(gbn.NOTICE_ENTERED, {"sessionId": "sid-me", "name": "coordinator-content-repo-9"})

    assert "a second opinion before asking the PM" in text
    assert "?" not in text
