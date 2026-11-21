"""Ratchet: no leg prompt the engine writes for a Claude Workflow agent carries a bare `python -m coordinator_core`.

A staged `prompts/<name>.md` is read by an agent whose `python` need not be the engine interpreter and whose
cwd need not import `coordinator_core`; `ask_staged_prompts.stage` is the one choke point that binds them.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_staged_prompts
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.workflow_fire.worker_preamble import _PORTABLE_MODULE

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _staged_legs() -> dict[str, str]:
    sink: dict[str, str] = {}
    compose_ask_script(
        repo_root="REPO",
        prompt="add a thing",
        sizing_rel=None,
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        staged_out=sink,
        in_run_commit=True,
        script_path="scratch/warp/a.workflow.mjs",
        **REVIEW_KW,
    )
    return sink


def test_no_staged_leg_prompt_carries_a_bare_engine_module_command() -> None:
    sink = _staged_legs()
    assert sink, "compose staged nothing; the ratchet would pass vacuously"
    bare = {name: m[0] for name, text in sink.items() if (m := _PORTABLE_MODULE.search(text))}
    assert not bare, f"staged prompts with a bare engine command: {bare}"


def test_stage_binds_a_portable_command_and_keeps_other_text() -> None:
    sink: dict[str, str] = {}
    ask_staged_prompts.stage(
        "leg", "Run `python -m coordinator_core.ops.test_leg_run --out x` now.", sink, record_dir="/r"
    )
    body = sink["leg"]
    assert not _PORTABLE_MODULE.search(body)
    assert "coordinator_core.ops.test_leg_run --out x` now." in body


# --- every other renderer: the script written for a Workflow agent -------------------------------------

import pytest

from coordinator_core.ops.dispatch_emit import op as _op
from coordinator_core.ops.dispatch_emit import reverify_delivery as _rd
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import bind_script_literals, stage_literal_runs
from coordinator_core.ops.dispatch_emit.emit import VERIFY_RETEST_CLAUSE, compose_script
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import _STAGE_SCHEMAS_WITH_JUDGE, _V5_FRAGMENT, _row


def _bare(text: str) -> list[str]:
    return [m[0] for m in _PORTABLE_MODULE.finditer(text)]


def test_a_short_portable_command_is_staged_whatever_its_size() -> None:
    sink: dict[str, str] = {}
    js = "'Run `python -m coordinator_core.ops.test_leg_run --out x` once.'"
    out = stage_literal_runs(js, "p", lambda n, t: ask_staged_prompts.stage(n, t, sink, record_dir="/r"))
    assert "/r/prompts/p.md" in out and not _bare(out)
    assert not _bare(sink["p"])


def test_bind_script_literals_binds_one_command_split_across_joined_literals() -> None:
    js = "const p = 'run `python -m ' + 'coordinator_core.ops.test_leg_run --out x` now';"
    out = bind_script_literals(js)
    assert not _bare(out)
    assert out.startswith("const p = '") and out.endswith("--out x` now';")


def test_bind_script_literals_leaves_a_run_with_an_undecoded_escape_alone() -> None:
    js = "'python -m coordinator_core.ops.x " + chr(92) + "u00e9'"
    assert bind_script_literals(js) == js


def test_plan_route_script_is_bound_where_it_is_written(tmp_path) -> None:
    script = compose_script(
        [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]], name="wf", description="d",
        plan_path="docs/plans/p.md", review_roster_fragment=_V5_FRAGMENT, review_stage_schemas=_STAGE_SCHEMAS_WITH_JUDGE,
    )
    assert _bare(script), "composer no longer emits the portable form; the ratchet would pass vacuously"
    assert VERIFY_RETEST_CLAUSE.count("python -m") == 2
    out = tmp_path / "p.workflow.mjs"
    _op._emit_script_file(
        out, script, receipt_plan_path=None, params={}, receipt_extras=None, plan_findings=[],
        session_id="s", agent_type_host=None, before_write=lambda: None,
    )
    assert not _bare(out.read_text(encoding="utf-8"))


@pytest.mark.usefixtures("verifier_roster")
@pytest.mark.parametrize("kind", ["reverify", "rejudge"])
def test_reverify_delivery_scripts_are_bound_where_they_are_written(tmp_path, monkeypatch, kind) -> None:
    from coordinator_core.ops.tests.test_rejudge_hand_worked_plan import _case_one

    repo, plan = _case_one(tmp_path)
    out = repo / f"{kind}.workflow.mjs"
    if kind == "rejudge":
        _rd.emit_rejudge(repo_root=repo, plan_path=str(plan), out_path=str(out))
    else:
        from coordinator_core.ops.dispatch_emit.tests.test_reverify_delivery_null_slot import _null_run, _plan_file

        repo, record, _ = _null_run(tmp_path / "r")
        plan = _plan_file(repo)
        out = repo / "docs" / "plans" / "p.reverify.workflow.mjs"
        monkeypatch.setattr(_rd, "_head_sha", lambda root: "b" * 40)
        # The arm that briefs several test files, which adds the memory-admit command to the tests leg.
        monkeypatch.setattr(_rd, "_tests_stale", lambda record: True)
        monkeypatch.setattr(_rd, "_select_scope", lambda root, base, head: (["a/test_a.py", "a/test_b.py"], "", [], ""))
        monkeypatch.setattr(_rd, "_terminal_of", lambda root, pid, pp: ("c" * 40, record))
        _rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    written = out.read_text(encoding="utf-8")
    assert kind == "rejudge" or "reverify_delivery admit" in written, "the tests leg no longer briefs the admit command"
    assert not _bare(written)
