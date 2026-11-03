"""dispatch.land_run stamps the ask-* record's run-stage.json `landed`, naming the warp run, once the commit lands."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import land_run, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, write_run_stage_at
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import (  # noqa: F401  (repo is a fixture)
    RUN,
    SCRIPT,
    SELF,
    _ask_script,
    _author,
    _git,
    _land,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _stage(root) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / RUN / "run-stage.json").read_text(encoding="utf-8"))


def test_a_landed_ask_run_reads_landed_and_names_the_warp_run(repo, monkeypatch):  # noqa: F811
    _author(repo, "src/a.py", "a = 1\n")
    _ask_script(repo, "src/a.py")
    write_run_stage_at(repo / RECORD_DIR_ROOT / RUN, "staged")
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))

    out = _land(repo)

    assert out["committed"] is True, out
    stage = _stage(repo)
    assert stage["stage"] == "landed" and stage["warp_run"] == RUN


def test_a_refused_landing_leaves_the_stage_untouched(repo, monkeypatch):  # noqa: F811
    _author(repo, "src/a.py", "a = 1\n")
    _ask_script(repo, "src/a.py")
    write_run_stage_at(repo / RECORD_DIR_ROOT / RUN, "staged")
    token = run_token.mint(repo, RUN, SCRIPT, session_id=SELF)
    refusal = {"committed": False, "sha": None, "error": "unmet", "refused": "ledger-unmet"}
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (refusal, None, []))

    out = _land(repo, run_token=token)

    assert out["refused"] == "ledger-unmet"
    stage = _stage(repo)
    assert stage["stage"] == "staged" and "warp_run" not in stage
    assert _git(["status", "--porcelain", "--", "src/a.py"], repo)
