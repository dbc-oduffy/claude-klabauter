"""A row a revise adds after the plan point is minted into the late-row read and dispatched through the
same `_launchRow` arms (reviewer, kept, route, family, group) and brief materialisation as an original row."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _compose(monkeypatch, arm: str) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt=None, sizing_rel=REL, run_id="run-1", session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        script_path="x.mjs", in_run_commit=True, **REVIEW_KW,
    )


def _late_leg(script: str) -> str:
    start = script.index("_lateLeg = async (_ids) =>")
    return script[start:script.index("\n  };", start)]


@pytest.mark.parametrize("arm", ["s", "m_plus"])
def test_late_rows_launch_through_the_one_row_dispatch(monkeypatch, arm) -> None:
    script = _compose(monkeypatch, arm)
    late = _late_leg(script)
    assert "_launchRow(r)" in late
    assert "_runRow(" not in late
    assert "const _launchRow = (r) => {" in script
    body = script[script.index("const _launchRow = (r) => {"):script.index("for (const w of _waves)")]
    assert "r.agent_type ===" in body and "_keptRun(r)" in body and "_groupRun(r)" in body
    assert "for (const r of _manifest.rows.filter((x) => x.wave === w)) _launchRow(r);" in script


def test_late_rows_materialise_deferred_briefs_per_wave(monkeypatch) -> None:
    late = _late_leg(_compose(monkeypatch, "m_plus"))
    assert "_stageRecoverParams.wave = w" in late
    assert "sort((a, b) => a - b)" in late


def test_a_revise_added_row_is_read_as_late_for_dispatch(repo, monkeypatch) -> None:  # noqa: F811
    doc = {
        "schema": "sizing-object", "name": "fixture", "intent": "Fix the thing",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "spec-dispatch",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fix the thing"}],
        "exit_criterion": {"statement": "it works", "accepted": None},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    rl.start(repo, "ra1")
    rl.bind(repo, "ra1", SIZING_REL)
    before = rl.late_rows(repo, "ra1", served=[])
    _, _, added = rl.revise(repo, "ra1", verbatim="also cover the new surface")
    assert added is not None
    assert added not in before
    assert added in rl.late_rows(repo, "ra1", served=[])
