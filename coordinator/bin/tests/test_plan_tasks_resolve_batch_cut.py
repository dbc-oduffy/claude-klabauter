"""plan-tasks-resolve --resolves-file closes an approved multi-row cut in one call."""
from __future__ import annotations

import asyncio
import importlib.machinery
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.frontmatter.schema_validate import compute_grouping_digest
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.ops.tests.test_plan_tasks_mutate import _make_git_repo, _seed_plan

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "plan_tasks_resolve_batch_cut_subject", str(_BIN_DIR / "plan-tasks-resolve")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()

_ROW = """\
- id: {id}
  title: Row {id}
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do {id}.
"""

_CUT = [
    {"id": "C2", "disposition": "wont_do"},
    {"id": "C3", "disposition": "wont_do"},
]


def _plan_text() -> str:
    digest = compute_grouping_digest(_CUT, "ruled_out")
    rows = "".join(_ROW.format(id=i) for i in ("C1", "C2", "C3"))
    return f"""\
---
title: "Cut Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  ruled_out:
    status: approved
    approver: pm
    approved_at: 2026-07-29
    pm_utterance: 'yes, drop C2 and C3'
    digest: '{digest}'
---

# Cut Plan

## Tasks

```yaml plan-tasks
{rows}```

## Trailer

Prose.
"""


def _batch(path: Path) -> Path:
    path.write_text(
        json.dumps(
            [
                {
                    "id": r["id"],
                    "disposition": "wont_do",
                    "disposition_detail": f"PM approved the cut of {r['id']}",
                    "case_against": f"{r['id']} fixes a real papercut",
                }
                for r in _CUT
            ]
        ),
        encoding="utf-8",
    )
    return path


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


def test_approved_cut_closes_in_one_call(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "cut.md", _plan_text())
    batch = _batch(tmp_path / "cut.json")
    calls = []

    def route(op, params, repo_root, legacy_fn):
        calls.append(params)
        result = asyncio.run(_handler(params, repo_root=repo / ".git"))
        assert result["exit_code"] == 0, result
        return result

    rc, _, err = _run(["--plan", str(plan), "--resolves-file", str(batch)], route)

    assert rc == 0, err
    assert len(calls) == 1 and "resolves" in calls[0]
    text = plan.read_text(encoding="utf-8")
    assert text.count("disposition: wont_do") == 2


def test_single_row_call_still_refuses_with_digest_text(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "cut.md", _plan_text())
    before = plan.read_text(encoding="utf-8")

    def route(op, params, repo_root, legacy_fn):
        result = asyncio.run(_handler(params, repo_root=repo / ".git"))
        if result["exit_code"] != 0:
            raise _cli.cc_invoke.RouteMutationError(result.get("error", "refused"), result)
        return result

    rc, _, err = _run(
        ["--id", "C2", "--plan", str(plan), "--wont-do",
         "--disposition-detail", "d", "--case-against", "c"],
        route,
    )

    assert rc == 1
    assert "different cut-set" in err
    assert plan.read_text(encoding="utf-8") == before


def test_repeated_id_refusal_names_resolves_file():
    calls = []
    rc, _, err = _run(
        ["--id", "C1", "--id", "C2", "--plan", "p.md", "--wont-do",
         "--disposition-detail", "d", "--case-against", "c"],
        lambda *a, **k: calls.append(a),
    )
    assert rc == 2 and "--resolves-file" in err and "one call per row" not in err
    assert calls == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--id", "C1"],
        ["--coded", "abc"],
        ["--disposition-detail", "d"],
    ],
)
def test_resolves_file_is_exclusive_with_id_and_exit_flags(tmp_path, extra):
    batch = _batch(tmp_path / "cut.json")
    calls = []
    rc, _, _ = _run(
        ["--plan", "p.md", "--resolves-file", str(batch), *extra],
        lambda *a, **k: calls.append(a),
    )
    assert rc == 2
    assert calls == []


@pytest.mark.parametrize("content", ["not json", "{}", "[]", '[{"id": "C1"}]'])
def test_malformed_resolves_file_refused_locally(tmp_path, content):
    bad = tmp_path / "bad.json"
    bad.write_text(content, encoding="utf-8")
    calls = []
    rc, _, _ = _run(
        ["--plan", "p.md", "--resolves-file", str(bad)], lambda *a, **k: calls.append(a)
    )
    assert rc == 2
    assert calls == []
