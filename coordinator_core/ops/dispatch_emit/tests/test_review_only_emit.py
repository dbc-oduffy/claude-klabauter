"""--review-only: a review-and-terminal-only script over rows a prior run landed."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.ops.dispatch_emit.emit import (
    _keep_landed_rows,
    assert_zero_errors,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.spine_read import EmitterRow
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row

_BASE = "abc1234def"
_SESSION = "9d860631-c114-462a-a6ee-f685cc671ced"
_FORBIDDEN = (
    "_runRow",
    "_commitWave",
    "_waveCommit",
    "_checkpointPushes",
    "label: 'commit:",
    "label: 'push",
)


def _script(host):
    return compose_script(
        [[_write_row("C1"), _write_row("C2")]],
        name="wf",
        description="d",
        run_base_sha=_BASE,
        session_id=_SESSION,
        agent_type_host=host,
        review_only=True,
        plan_path="docs/plans/p.md",
        **REVIEW_KW,
    )


@pytest.mark.parametrize("host", ["coordinator", "host"])
def test_no_row_commit_or_push_machinery(host):
    script = _script(host)

    for needle in _FORBIDDEN:
        assert needle not in script
    assert "Review-only: rows C1, C2, base " + _BASE in script
    assert_zero_errors(script)


def test_landed_seed_stands_outside_any_commit_wave():
    script = _script("coordinator")

    assert "const _landed = { 'C1': { paths: ['pkg/C1.py'" in script
    assert "_commitWave" not in script


def test_given_base_reaches_the_prep_prompt():
    script = _script("coordinator")

    assert _BASE in script.split("phase('Review prep')", 1)[1]


def test_unnamed_spine_row_appears_nowhere():
    script = compose_script(
        [[_write_row("C1")]],
        name="wf", description="d", run_base_sha=_BASE, review_only=True, **REVIEW_KW,
    )

    assert "C2" not in script and "pkg/C2.py" not in script


def _row(id_, deps=()):
    return EmitterRow(
        id=id_, title=id_, surface="s", writes=[f"p/{id_}.py"], reads=(),
        depends_on=[{"chunk": d} for d in deps],
    )


def test_keep_landed_strips_edges_to_dropped_rows():
    kept = _keep_landed_rows([_row("A"), _row("B", ["A"]), _row("C", ["B"])], frozenset({"B", "C"}))

    assert [r.id for r in kept] == ["B", "C"]
    assert kept[0].depends_on == []
    assert kept[1].depends_on == [{"chunk": "B"}]


def test_keep_landed_refuses_an_unknown_id():
    with pytest.raises(ValueError):
        _keep_landed_rows([_row("A")], frozenset({"Z"}))


def _run(args, capsys):
    code = cli.main(args)
    return code, capsys.readouterr().err


def test_flags_require_each_other(tmp_path, capsys):
    run = tmp_path / "run.txt"
    run.write_text("x checkpoint(wave 1): 1 rows — A", encoding="utf-8")

    assert _run(["--plan", "p.md", "--review-only", str(run)], capsys)[0] == cli.EXIT_USAGE
    assert _run(["--plan", "p.md", "--run-base", _BASE], capsys)[0] == cli.EXIT_USAGE


def test_bad_sha_and_missing_plan_refused(tmp_path, capsys):
    run = tmp_path / "run.txt"
    run.write_text("x", encoding="utf-8")

    assert _run(["--plan", "p.md", "--review-only", str(run), "--run-base", "HEAD"], capsys)[0] == cli.EXIT_USAGE
    assert _run(["--review-only", str(run), "--run-base", _BASE], capsys)[0] == cli.EXIT_USAGE


@pytest.mark.parametrize(
    "extra",
    [
        ["--only-incomplete", "x"],
        ["--resume-from", "x"],
        ["--reverify-delivery", "x"],
        ["--chatty"],
        ["--queue", "x"],
        ["--ask"],
        ["--profile", "x"],
        ["--sizing", "x"],
        ["--pipeline", "x"],
    ],
)
def test_conflicting_flags_refused(tmp_path, capsys, extra):
    run = tmp_path / "run.txt"
    run.write_text("x", encoding="utf-8")

    code, err = _run(["--plan", "p.md", "--review-only", str(run), "--run-base", _BASE, *extra], capsys)

    assert code == cli.EXIT_USAGE
    assert "exclusive of" in err


def test_review_only_takes_plan_or_inventory_not_both(tmp_path, capsys):
    run = tmp_path / "run.txt"
    run.write_text("x", encoding="utf-8")

    code, err = _run(["--plan", "p.md", "--inventory", "x", "--review-only", str(run), "--run-base", _BASE], capsys)

    assert code == cli.EXIT_USAGE
    assert "--review-only takes --plan or --inventory, not both" in err


_PLAN = (
    "---\nplan_id: pln-x\n---\n\n## Tasks\n\n"
    "```yaml plan-tasks\n- id: C1\n  title: t\n  writes: [a.py]\n```\n"
)


def test_empty_landed_set_refused(tmp_path, capsys):
    run = tmp_path / "run.txt"
    run.write_text("no checkpoint subjects here", encoding="utf-8")
    plan = tmp_path / "p.md"
    plan.write_text(_PLAN, encoding="utf-8")

    code, err = _run(["--plan", str(plan), "--review-only", str(run), "--run-base", _BASE], capsys)

    assert code == cli.EXIT_DATA_ERROR
    assert "names no row" in err
