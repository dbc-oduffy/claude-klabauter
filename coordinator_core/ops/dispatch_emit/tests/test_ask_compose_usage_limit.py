"""The ask script turns a usage-limit failure at the terminal test agent into a resumable halt."""

from __future__ import annotations

import re
from pathlib import PurePath

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import HALT_USAGE_LIMIT, compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def test_test_call_is_guarded_by_the_limit_catch(monkeypatch):
    script = _script(monkeypatch)
    assert HALT_USAGE_LIMIT in script
    assert re.search(
        r"try \{ .*?_verify = await .*?\} catch \(e\) \{[^}]*_USAGE_LIMIT_RE\.test\(_m\)\) \{ _haltOnUsageLimit\(e\); \}",
        script,
        re.S,
    )


_WEEKLY = "You've hit your weekly limit - resets Nov 17, 3am (Europe/London)"


def test_stop_rule_halt_routes_limit_text_before_stop_rule(monkeypatch):
    script = _script(monkeypatch)
    limit_at = script.index("_limitTextIn(..._stoppedBy.map")
    assert limit_at < script.index("'stop rule fired on '")
