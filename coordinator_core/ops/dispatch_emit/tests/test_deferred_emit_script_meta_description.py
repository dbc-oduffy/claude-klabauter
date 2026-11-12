"""write_deferred_script's launcher meta description names the deferral branch truthfully."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import deferred_emit_script as des
from coordinator_core.ops.dispatch_emit.fan_launcher import LAUNCHER_CHILD_EMIT_PARAM


def _description(script) -> str:
    m = re.search(r"description: (\"[^\n]*\") \}", script.read_text(encoding="utf-8"))
    assert m, "meta description not found"
    import json

    return json.loads(m.group(1))


def test_premint_params_do_not_claim_an_overrun(tmp_path):
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x", "preminted_run_id": "ask-1"})
    assert "outran" not in _description(script)


def test_unminted_overrun_says_it_outran_the_hook_budget(tmp_path):
    script = des.write_deferred_script(tmp_path, {"ask_brief": "x"})
    assert "outran the hook budget" in _description(script)


def test_fan_child_and_reemit_carry_their_own_text(tmp_path):
    child = _description(des.write_deferred_script(tmp_path, {"ask_brief": "x", LAUNCHER_CHILD_EMIT_PARAM: True}))
    reemit = _description(des.write_deferred_script(tmp_path, {"ask_brief": "x", "preminted_run_id": "a"}, branch="reemit"))
    assert child == des.BRANCH_DESCRIPTIONS["fan_child"]
    assert reemit == des.BRANCH_DESCRIPTIONS["reemit"]
    assert "outran" not in child and "outran" not in reemit
    assert len(set(des.BRANCH_DESCRIPTIONS.values())) == 4


def test_unknown_branch_is_refused(tmp_path):
    with pytest.raises(ValueError):
        des.write_deferred_script(tmp_path, {"ask_brief": "x"}, branch="nope")


def test_reemit_op_passes_its_branch(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import reemit

    seen = {}

    def fake(root, params, *, branch=None):
        seen["branch"] = branch
        p = root / "scratch" / "warp" / "x.workflow.mjs"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        return p

    monkeypatch.setattr(des, "write_deferred_script", fake)
    monkeypatch.setattr(reemit, "main_worktree_root", lambda p: tmp_path)
    monkeypatch.setattr(reemit, "_read_params", lambda root, params: ({"ask_brief": "x"}, None))
    out = reemit._handler({}, repo_root=tmp_path)
    assert out["ok"] is True and seen["branch"] == "reemit"
