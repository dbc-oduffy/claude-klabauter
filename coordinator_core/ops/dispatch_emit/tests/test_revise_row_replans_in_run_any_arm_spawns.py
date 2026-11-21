"""A register row a revise minted after the plan was written is claimed and re-planned in-run on every arm."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl, run_token
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


@pytest.mark.parametrize("tshirt", ["S", "M"])
def test_live_revise_adding_a_row_is_neither_refused_nor_told_to_resize(repo, monkeypatch: pytest.MonkeyPatch, tshirt) -> None:  # noqa: F811
    doc = {
        "schema": "sizing-object", "name": "fixture", "intent": "Fix the thing",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": "spec-dispatch",
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
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)
    out = rl._handler({"run_id": "ra1", "verbatim": "also refuse undeclared writes"}, repo_root=repo)
    assert out["ok"] is True and out["refused"] == []
    assert "resize" not in out and "no re-plan leg" not in out.get("warning", "")


_STAGE_RUN = "ask-20261115T000000-abc125"
_STAGE_PLAN_REL = "docs/plans/2026-11-15-unserved.md"
_STAGE_SIZING_REL = "state/sizings/2026-11-15-unserved.yaml"
