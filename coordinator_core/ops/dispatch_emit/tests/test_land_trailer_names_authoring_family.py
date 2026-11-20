"""The land message's Co-Authored-By names the family whose invocation record authored the rows' paths."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coordinator_core.git.provider_attribution import ATTRIBUTIONS, caller_attribution
from coordinator_core.ops.dispatch_emit import family_invoke_record as fir
from coordinator_core.ops.dispatch_emit import run_landing

RUN = "run-1"


def _record(root, row, stage, family, *, exit_code=0, authored=True, ended="2026-11-18T10:00:00Z"):
    path = fir.record_path(root, RUN, row, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "family": family, "stage": stage, "exit_code": exit_code, "ended_at": ended,
        "pre": {"a.py": "x"}, "post": {"a.py": "y" if authored else "x"},
    }), encoding="utf-8")


def _trailer(root, rows=("C1",)):
    request = SimpleNamespace(
        chunks=[SimpleNamespace(id=r, title="t") for r in rows], deliverable_id=None, plan_path=None,
        review_run_base=None)
    params = {"inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0}}
    with caller_attribution({"COORDINATOR_AGENT_PROVIDER": "claude"}):
        msg = run_landing._message(root, params, request, list(rows), [], "", RUN)
    return [ln for ln in msg.splitlines() if ln.startswith("Co-Authored-By:")]


@pytest.mark.parametrize("family", ["agy", "grok", "codex"])
def test_trailer_names_the_authoring_family(tmp_path, family):
    _record(tmp_path, "C1", "write", family)
    assert _trailer(tmp_path) == [f"Co-Authored-By: {ATTRIBUTIONS[family]}"]


def test_no_record_falls_back_to_caller_attribution(tmp_path):
    assert _trailer(tmp_path) == [f"Co-Authored-By: {ATTRIBUTIONS['claude']}"]


def test_record_that_authored_nothing_never_names_a_family(tmp_path):
    _record(tmp_path, "C1", "write", "agy", authored=False)
    _record(tmp_path, "C1", "review", "grok", exit_code=1)
    assert _trailer(tmp_path) == [f"Co-Authored-By: {ATTRIBUTIONS['claude']}"]


def test_unknown_family_falls_back(tmp_path):
    _record(tmp_path, "C1", "write", "nonesuch")
    assert _trailer(tmp_path) == [f"Co-Authored-By: {ATTRIBUTIONS['claude']}"]


def test_latest_authoring_record_wins_across_rows(tmp_path):
    _record(tmp_path, "C1", "write", "agy", ended="2026-11-18T10:00:00Z")
    _record(tmp_path, "C2", "write", "grok", ended="2026-11-18T11:00:00Z")
    assert _trailer(tmp_path, ("C1", "C2")) == [f"Co-Authored-By: {ATTRIBUTIONS['grok']}"]
