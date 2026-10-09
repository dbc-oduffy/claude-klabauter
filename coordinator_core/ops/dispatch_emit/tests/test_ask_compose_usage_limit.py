"""The ask script turns a usage-limit failure at the terminal test agent into a resumable halt."""

from __future__ import annotations

import re
import shutil
import subprocess
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
    assert re.search(r"try \{\s*_testResult = await .*?\} catch \(e\) \{ _haltOnUsageLimit\(e\); \}", script, re.S) or (
        "catch (e) { _haltOnUsageLimit(e); }" in script
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
@pytest.mark.spawns_process
@pytest.mark.cadence
def test_helper_halts_on_limit_and_rethrows_other_errors(monkeypatch):
    helper = ask_compose._usage_limit_helper_js()
    js = (
        "let _halted = null; const _runId = 'run-1';\n" + helper + "\n"
        "try { throw new Error(\"You've hit your session limit - resets 3:00pm\"); } catch (e) { _haltOnUsageLimit(e); }\n"
        "if (_halted.halted !== 'usage_limit' || _halted.run_id !== 'run-1') throw new Error('no halt');\n"
        "_halted = null;\n"
        "let rethrown = false;\n"
        "try { try { throw new Error('boom'); } catch (e) { _haltOnUsageLimit(e); } } catch { rethrown = true; }\n"
        "if (!rethrown || _halted) throw new Error('boom swallowed');\n"
    )
    r = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert r.returncode == 0, r.stderr
