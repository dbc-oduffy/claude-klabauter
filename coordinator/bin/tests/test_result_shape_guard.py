"""test_result_shape_guard — coverage for the C27 result-shape guards.

Three `result.get(...)` call sites subscripted a cc_invoke/route() result with
no `isinstance(result, dict)` guard first: `reassess-goal-krs.py :: main`,
`reap-integrated-review-findings.py :: _reap_native`, and
`coordinator-queue-append.py :: _schema_cli_validate`. A non-dict result
(list/scalar) previously turned into an unhandled `AttributeError` traceback;
each site now checks the shape first and reports the module's documented
single-line stderr message instead.

Spec backlink: state/dispatch-briefs/2026-08-20-a-refusal-cannot-exit-zero/C27.md
"""
from __future__ import annotations

import importlib.util
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

# Loads real bin/ modules and drives argv/subprocess-shaped call sites.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).parent.parent


def _load_module(name: str, filename: str):
    loader = SourceFileLoader(name, str(_BIN_DIR / filename))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture()
def git_repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# reassess-goal-krs.py :: main
# ---------------------------------------------------------------------------


def test_reassess_goal_krs_main_non_dict_result(git_repo, monkeypatch, capsys):
    mod = _load_module("reassess_goal_krs", "reassess-goal-krs.py")
    monkeypatch.setattr(mod, "cc_invoke", lambda op, params, cwd: ["not", "a", "dict"])
    monkeypatch.setattr(mod.sys, "argv", ["reassess-goal-krs"])

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "reassess-goal-krs: malformed result from cc_invoke: not a dict" in err


# ---------------------------------------------------------------------------
# reap-integrated-review-findings.py :: _reap_native
# ---------------------------------------------------------------------------


def test_reap_native_non_dict_result(git_repo, monkeypatch, capsys):
    mod = _load_module("reap_integrated_review_findings", "reap-integrated-review-findings.py")

    def _fake(op_name, params, repo_root, _claude_klabauter_root=None):
        assert op_name == "fleet.reap_integrated_findings"
        return ["not", "a", "dict"]

    monkeypatch.setattr(mod.cc_invoke, "cc_invoke", _fake)

    rc = mod._reap_native(dry_run=False, commit_prefix="")

    assert rc == 0
    err = capsys.readouterr().err
    assert (
        "fleet.reap_integrated_findings malformed result: not a dict" in err
    )


# ---------------------------------------------------------------------------
# coordinator-queue-append.py :: _schema_cli_validate
# ---------------------------------------------------------------------------


def test_queue_append_schema_cli_validate_non_dict_result(git_repo, monkeypatch, capsys):
    mod = _load_module("coordinator_queue_append", "coordinator-queue-append.py")

    def _fake_route(op_name, params, repo_root, legacy_fn):
        assert op_name == "schema.validate"
        return ["not", "a", "dict"]

    monkeypatch.setattr(mod, "_cc_route", _fake_route)

    with pytest.raises(SystemExit) as exc:
        mod._schema_cli_validate("debt-backlog", {"title": "x"})

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "schema validation malformed result for 'debt-backlog': not a dict" in err
