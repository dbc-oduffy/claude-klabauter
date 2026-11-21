"""ask_compose: an indeterminate (-32004) sizing-assemble is reconciled by the scout, halts resumably when the
sizing is unconfirmed, and every refusal halt on a {sizing} submission names a {sizing} refire."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit.ask_compose import (
    SIZING_ASSEMBLE_INDETERMINATE_RE,
    _lit,
    _refire_js,
    compose_ask_script,
    sizing_assemble_reconcile_clause,
)
from coordinator_core.ops.dispatch_emit.landing_targeted_tests import REVERIFY_INDETERMINATE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.warp_fire import fire_command
from pathlib import Path

_SIZING = "state/sizings/x.yaml"
_INDETERMINATE = (
    "warm dispatch indeterminate: door delivered this request to the warm engine and did not get back a response"
)


def _stub_wrap(text):
    return "  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]


def _compose(**over) -> str:
    kw = dict(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=_stub_wrap, plan_blitz_text="stub", **REVIEW_KW,
    )
    kw.update(over)
    return compose_ask_script(**kw)


def test_the_scout_prompt_carries_the_assemble_reconcile_clause_for_its_sizing_path() -> None:
    script = _compose()
    assert "A -32004 is never itself the assemble" in script
    assert "retry it only as the -32004 clause" in script
    clause = sizing_assemble_reconcile_clause(_SIZING)
    assert _SIZING in clause and "exit_code 0" in clause


def test_the_indeterminate_kind_is_an_existing_mechanical_kind_not_a_refusal() -> None:
    assert REVERIFY_INDETERMINATE != "refusal"
    assert hrc.HALT_REMEDY[REVERIFY_INDETERMINATE][0] == hrc.MECHANICAL


def test_the_marker_regex_matches_both_reply_shapes() -> None:
    assert SIZING_ASSEMBLE_INDETERMINATE_RE.startswith("/") and SIZING_ASSEMBLE_INDETERMINATE_RE.endswith("/i")
    assert "-32004" in SIZING_ASSEMBLE_INDETERMINATE_RE and "warm dispatch indeterminate" in SIZING_ASSEMBLE_INDETERMINATE_RE


def test_the_door_indeterminate_text_names_the_landed_check() -> None:
    from pathlib import Path

    src = (Path(__file__).resolve().parents[3] / "warm" / "door" / "door_core.c").read_text(encoding="utf-8")
    body = src[src.index("int build_indeterminate_envelope"):]
    body = body[: body.index("\n}\n")]
    assert "git log -1 -- <path>" in body and "warm.request_status" in body


def test_every_refusal_halt_site_on_a_sizing_submission_names_the_sizing_refire() -> None:
    script = _compose(prompt=None, sizing_rel=_SIZING)
    assert _refire_js(_SIZING) == _lit(fire_command(_SIZING))
    sizing_call = _lit(fire_command(_SIZING))
    staged = re.search(rf"const (_PF\d+) = {re.escape(sizing_call)};", script)
    operand = staged.group(1) if staged else sizing_call
    assert f"if (_halted.halted === 'refusal') return 'Refire: ' + {operand};" in script
    assert "_refireCmd" not in script
    assert 'args: {"ask"' not in script


def test_a_prompt_submission_falls_back_to_the_ask_refire_only_while_no_sizing_exists() -> None:
    script = _compose()
    assert "const _refireCmd = () => (_sizingRel ? " in script
    assert 'args: {"ask"' in script
