"""plan.seam_check flags a capability click_path that contradicts the sizing's PM words."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.tests.test_plan_seam_check import _check, _plan, _root, _row, git  # noqa: F401

pytestmark = pytest.mark.complete

CLASS = "click-path-contradicts-governing-source"
SIZING = "state/sizings/2026-11/connect.yaml"
MODAL_ASK = "Connect should open a dismissable connect-accounts modal"


def _sizing(root):
    p = root / SIZING
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"pm_verbatims": [{"text": MODAL_ASK}]}), encoding="utf-8")


def _fm(click, extra=""):
    return (
        f"sizing_object: {SIZING}\ncapabilities:\n  - id: cap1\n    statement: s\n    role: member\n"
        f"    click_path: '{click}'\n    ui_consumer: {{chunk: R1}}\n{extra}"
    )


def _flags(reply):
    return [f for f in reply["findings"] if f["class"] == CLASS]


def test_incident_click_path_is_one_blocking_finding(tmp_path, git):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_fm("Connect (to /welcome)"))
    reply = _check(root, [a])
    flags = _flags(reply)
    assert len(flags) == 1 and flags[0]["blocking"] is True and flags[0]["capability"] == "cap1"
    assert reply["verdict"] == "REFUSED"


def test_a_contradiction_ruling_downgrades_the_finding(tmp_path, git):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"),
              fm=_fm("Connect (to /welcome)", "    contradiction_ruling: 'PM: the welcome page is fine'\n"))
    flags = _flags(_check(root, [a]))
    assert len(flags) == 1 and flags[0]["blocking"] is False


def test_an_empty_ruling_does_not_downgrade(tmp_path, git):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_fm("Connect (to /welcome)", "    contradiction_ruling: '  '\n"))
    assert _flags(_check(root, [a]))[0]["blocking"] is True


def test_a_matching_click_path_has_no_finding(tmp_path, git):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_fm("Connect opens the connect-accounts modal"))
    reply = _check(root, [a])
    assert _flags(reply) == [] and reply["verdict"] == "CLEAN"


def test_no_sizing_means_no_finding(tmp_path, git):
    root = _root(tmp_path)
    a = _plan(root, "a", _row("R1", "[x.py]"),
              fm="capabilities:\n  - id: c\n    statement: s\n    role: m\n    click_path: 'Connect (to /welcome)'\n"
                 "    ui_consumer: {chunk: R1}\n")
    assert _flags(_check(root, [a])) == []


def test_the_check_spawns_no_process(tmp_path, git, monkeypatch):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"), fm=_fm("Connect (to /welcome)"))

    def boom(*args, **kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert len(_flags(_check(root, [a]))) == 1


def test_a_polarity_only_contradiction_is_advisory(tmp_path, git):
    root = _root(tmp_path)
    _sizing(root)
    a = _plan(root, "a", _row("R1", "[x.py]"),
              fm=_fm("Connect should not open a dismissable connect-accounts modal"))
    reply = _check(root, [a])
    flags = _flags(reply)
    assert len(flags) == 1 and flags[0]["blocking"] is False
    assert reply["verdict"] != "REFUSED"
