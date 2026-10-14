"""plan-tasks-resolve --delivered-elsewhere: flag contract and warn passthrough."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest.mock
from contextlib import redirect_stderr
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "plan_tasks_resolve_de_subject", str(_BIN_DIR / "plan-tasks-resolve")
    )
    spec = importlib.util.spec_from_loader("plan_tasks_resolve_de_subject", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()
_BASE = ["--id", "C1", "--plan", "docs/plans/foo.md", "--delivered-elsewhere"]


def _run(argv, reply=None):
    calls = []

    def fake(op, params, repo_root, legacy_fn):
        calls.append(dict(params))
        return reply or {"applied": True, "message": "ok"}

    err = io.StringIO()
    with unittest.mock.patch.object(_cli.cc_invoke, "route_mutation", fake), \
            unittest.mock.patch.object(_cli.cc_invoke, "require_engine_on_path", lambda *_: None), \
            unittest.mock.patch.object(_cli, "_resolve_repo_root", lambda: "/repo"), \
            redirect_stderr(err):
        try:
            rc = _cli.main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, calls, err.getvalue()


def test_dispatches_delivered_elsewhere_with_via_ref_and_detail():
    rc, calls, _ = _run(
        _BASE + ["--via", "memo", "--ref", "other@abc1234", "--disposition-detail", "landed there"]
    )
    assert rc == 0
    assert calls == [{
        "verb": "resolve", "plan_path": "docs/plans/foo.md", "id": "C1",
        "disposition": "delivered_elsewhere", "disposition_ref": "other@abc1234",
        "disposition_detail": "landed there", "delivered_via": "memo",
    }]


@pytest.mark.parametrize(
    "extra",
    [
        ["--ref", "r", "--disposition-detail", "d"],
        ["--via", "memo", "--disposition-detail", "d"],
        ["--via", "memo", "--ref", "r"],
        ["--via", "carrier-pigeon", "--ref", "r", "--disposition-detail", "d"],
    ],
)
def test_missing_via_ref_or_detail_is_refused_locally(extra):
    rc, calls, _ = _run(_BASE + extra)
    assert rc != 0
    assert calls == []


def test_op_warnings_are_printed_on_stderr_verbatim():
    reply = {"applied": True, "message": "ok", "warnings": ["completion asserted, not commit-proven: r via memo"]}
    rc, _calls, err = _run(
        _BASE + ["--via", "memo", "--ref", "r", "--disposition-detail", "d"], reply
    )
    assert rc == 0
    assert "completion asserted, not commit-proven: r via memo" in err


def test_via_without_delivered_elsewhere_is_refused():
    rc, calls, _ = _run(
        ["--id", "C1", "--plan", "docs/plans/foo.md", "--coded", "a1b2c3d",
         "--disposition-detail", "d", "--via", "memo"]
    )
    assert rc != 0
    assert calls == []
