"""The composed verify prompt names the lifecycle acts, and the composed _ledgerBlocking treats them as post-commit."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.spawns_process

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def test_the_verify_prompt_names_the_lifecycle_acts(monkeypatch):
    script = _script(monkeypatch).replace("\\'", "'")
    assert "baton_archive" in script and "plan_implemented" in script
    assert "post_commit_acts from (commit, sha_reply, sha_memo, baton_archive, plan_implemented, bug_close)" in script


def _blocking(script: str, items: list[dict]) -> list[dict]:
    start = script.index("const _postCommitActs")
    anchor = script.index("const _ledgerBlocking")
    end = script.index("\n    });", anchor) + len("\n    });")
    program = (
        "const _manifest = { gated: [] }; const _gate = {};\n"
        + script[start:end]
        + f"\nprocess.stdout.write(JSON.stringify(_ledgerBlocking({json.dumps({'all_of_pm_ask': False, 'items': items})})));"
    )
    proc = subprocess.run(
        [shutil.which("node"), "-"], input=program, capture_output=True, text=True, timeout=60, check=True,
        **no_console_creationflags(),
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_lifecycle_only_remaining_does_not_block(monkeypatch):
    item = {"id": "R1", "status": "not_met", "remaining": ["baton_archive", "plan_implemented"]}
    assert _blocking(_script(monkeypatch), [item]) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node absent")
def test_a_plain_words_gap_beside_a_lifecycle_act_still_blocks(monkeypatch):
    item = {"id": "R1", "status": "not_met", "remaining": ["plan_implemented", "typecheck not re-run"]}
    assert [i["id"] for i in _blocking(_script(monkeypatch), [item])] == ["R1"]
