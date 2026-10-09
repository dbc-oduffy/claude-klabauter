"""plan-tasks-resolve --clear-gate forwards to plan.tasks.mutate's clear-gate verb."""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan
from coordinator_core.ops.tests.test_plan_tasks_mutate_clear_gate import _PLAN, _ROW_GATED

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "plan_tasks_resolve_clear_gate_subject", str(_BIN_DIR / "plan-tasks-resolve")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _run(argv, route):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(_cli.cc_invoke, "route_mutation", route), mock.patch.object(
        _cli, "_resolve_repo_root", lambda: "/repo"
    ), redirect_stdout(out), redirect_stderr(err):
        try:
            rc = _cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def test_flag_clears_fixture_gate_end_to_end(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "g.md", _PLAN.format(rows=_ROW_GATED))

    def route(op, params, repo_root, legacy_fn):
        assert op == "plan.tasks.mutate"
        result = asyncio.run(_handler(params, repo_root=repo / ".git"))
        assert result["exit_code"] == 0, result
        return result

    rc, out, _ = _run(
        ["--id", "C1", "--plan", str(plan), "--clear-gate",
         "--owner-repo", "coordinator-content-repo", "--evidence", "DoE commit abc1234 landed"],
        route,
    )

    assert rc == 0
    assert "cleared" in out
    text = plan.read_text(encoding="utf-8")
    assert "cleared: true" in text
    assert "closure_evidence: DoE commit abc1234 landed" in text


def test_forwards_clear_gate_params():
    calls = []

    def route(op, params, repo_root, legacy_fn):
        calls.append(params)
        return {"message": "ok"}

    rc, _, _ = _run(
        ["--id", "C1", "--plan", "p.md", "--clear-gate", "--owner-repo", "R", "--evidence", "e"],
        route,
    )
    assert rc == 0
    assert calls == [
        {"verb": "clear-gate", "plan_path": "p.md", "id": "C1", "owner_repo": "R", "evidence": "e"}
    ]


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--evidence", "   "],
    ],
)
def test_missing_evidence_exits_nonzero_before_any_op_call(extra):
    calls = []
    rc, _, err = _run(
        ["--id", "C1", "--plan", "p.md", "--clear-gate", "--owner-repo", "R", *extra],
        lambda *a, **k: calls.append(a),
    )
    assert rc not in (0, None)
    assert "--evidence" in err
    assert calls == []


def test_missing_owner_repo_and_id_refused_locally():
    calls = []
    route = lambda *a, **k: calls.append(a)  # noqa: E731
    rc, _, err = _run(
        ["--id", "C1", "--plan", "p.md", "--clear-gate", "--evidence", "e"], route
    )
    assert rc not in (0, None) and "--owner-repo" in err
    rc, _, err = _run(
        ["--plan", "p.md", "--clear-gate", "--owner-repo", "R", "--evidence", "e"], route
    )
    assert rc not in (0, None) and "--id" in err
    assert calls == []


def test_mutually_exclusive_with_disposition_exits():
    calls = []
    rc, _, _ = _run(
        ["--id", "C1", "--plan", "p.md", "--clear-gate", "--coded", "abc",
         "--owner-repo", "R", "--evidence", "e"],
        lambda *a, **k: calls.append(a),
    )
    assert rc not in (0, None)
    assert calls == []


def test_evidence_without_clear_gate_refused():
    calls = []
    rc, _, _ = _run(
        ["--id", "C1", "--plan", "p.md", "--coded", "abc",
         "--disposition-detail", "d", "--evidence", "e"],
        lambda *a, **k: calls.append(a),
    )
    assert rc not in (0, None)
    assert calls == []


def test_help_documents_clear_gate_beside_the_exits():
    rc, out, _ = _run(["--help"], lambda *a, **k: None)
    assert rc == 0
    for flag in ("--coded", "--wont-do", "--clear-gate", "--owner-repo", "--evidence"):
        assert flag in out


def test_reopen_flag_forwards_to_the_reopen_verb():
    seen = {}

    def route(op, params, repo_root, legacy_fn):
        seen.update(params)
        return {"message": "reopen: C4 coded -> open"}

    rc, out, _ = _run(
        ["--id", "C4", "--plan", "docs/plans/p.md", "--reopen", "--disposition-detail", "executor BLOCKED"],
        route,
    )

    assert rc == 0
    assert seen == {"verb": "reopen", "plan_path": "docs/plans/p.md", "id": "C4",
                    "disposition_detail": "executor BLOCKED"}
    assert "coded -> open" in out


def test_reopen_flag_requires_a_reason():
    rc, _, err = _run(["--id", "C4", "--plan", "docs/plans/p.md", "--reopen"], lambda *a: {})
    assert rc == 2 and "--disposition-detail" in err
