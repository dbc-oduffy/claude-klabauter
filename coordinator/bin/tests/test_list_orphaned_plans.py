"""test_list_orphaned_plans.py — direct unit coverage for
`coordinator/bin/list-orphaned-plans.py`'s own logic (argv parsing, usage
errors, output formatting), stubbing `list_orphaned` so this suite never
performs a real disk scan.

Loaded by file path (`importlib.util.spec_from_file_location`) — this file
has a `.py` extension (unlike the hyphenated extensionless polyglot
entrypoints tested elsewhere in this directory), so it is directly
loadable without `importlib.machinery.SourceFileLoader`.

Spec backlink: pln-plan-orphan-ownership-resolver-3e68bb, chunk C4
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_MODULE_PATH = _BIN_DIR / "list-orphaned-plans.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "list_orphaned_plans_cli", _MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_EMPTY_RESULT = {
    "authorized_orphan": [],
    "parked_count": 0,
    "parked_below_threshold_count": 0,
    "legacy_unjoinable_count": 0,
    "unrecognized_status": [],
    "population_count": 0,
    "owned_count": 0,
    "non_plan_excluded_count": 0,
}


@pytest.fixture()
def cli(monkeypatch, tmp_path):
    module = _load_module()
    fake_repo_root = str(tmp_path / "fake-repo")
    monkeypatch.setattr(module, "_resolve_repo_root", lambda positional: fake_repo_root)
    return module


def test_threshold_days_parses_as_int_and_is_forwarded(cli, monkeypatch, capsys, tmp_path):
    captured = {}
    fake_repo_root = str(tmp_path / "fake-repo")

    def _fake_list_orphaned(repo_root, threshold_days):
        captured["repo_root"] = repo_root
        captured["threshold_days"] = threshold_days
        return dict(_EMPTY_RESULT)

    monkeypatch.setattr(cli, "list_orphaned", _fake_list_orphaned)

    exit_code = cli.main(["--threshold-days", "21"])

    assert exit_code == 0
    assert captured["threshold_days"] == 21
    assert str(captured["repo_root"]) == fake_repo_root


def test_threshold_days_non_integer_is_a_usage_error(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_orphaned", lambda repo_root, threshold_days: dict(_EMPTY_RESULT))

    exit_code = cli.main(["--threshold-days", "not-a-number"])

    assert exit_code == cli._USAGE_FAIL
    err = capsys.readouterr().err
    assert "must be an integer" in err
    assert "usage:" in err


def test_no_orphaned_plans_prints_the_empty_summary_branch(cli, monkeypatch, capsys):
    result = dict(_EMPTY_RESULT)
    result["population_count"] = 4
    result["owned_count"] = 4
    monkeypatch.setattr(cli, "list_orphaned", lambda repo_root, threshold_days: result)

    exit_code = cli.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no orphaned plans: 4 non-terminal plan(s), 4 owned" in out


def test_unrecognized_extra_positional_argument_is_a_usage_error(cli, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "list_orphaned", lambda repo_root, threshold_days: dict(_EMPTY_RESULT))

    exit_code = cli.main([str(tmp_path), "unexpected-extra-arg"])

    assert exit_code == cli._USAGE_FAIL
    err = capsys.readouterr().err
    assert "unrecognized extra argument" in err


@pytest.mark.parametrize(
    "result",
    [
        dict(_EMPTY_RESULT, authorized_orphan=[
            {"path": "docs/plans/x.md", "execution_authorized_by": "alice"}
        ]),
        dict(_EMPTY_RESULT, parked_count=3),
        dict(_EMPTY_RESULT, legacy_unjoinable_count=2),
        dict(_EMPTY_RESULT, unrecognized_status=[
            {"path": "docs/plans/y.md", "status": "weird"}
        ]),
        dict(_EMPTY_RESULT, non_plan_excluded_count=2),
    ],
)
def test_exit_code_is_always_zero_advisory_only(cli, monkeypatch, result):
    monkeypatch.setattr(cli, "list_orphaned", lambda repo_root, threshold_days: result)

    exit_code = cli.main([])

    assert exit_code == 0


def test_non_plan_excluded_count_narrated_counted_never_alarmed(cli, monkeypatch, capsys):
    result = dict(_EMPTY_RESULT, non_plan_excluded_count=2)
    monkeypatch.setattr(cli, "list_orphaned", lambda repo_root, threshold_days: result)

    exit_code = cli.main([])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "non_plan_excluded: 2 file(s) in docs/plans/ carry no YAML frontmatter" in out


def test_cannot_resolve_repo_root_is_a_usage_error(monkeypatch, capsys):
    module = _load_module()
    monkeypatch.setattr(module, "_resolve_repo_root", lambda positional: None)
    monkeypatch.setattr(module, "list_orphaned", lambda repo_root, threshold_days: dict(_EMPTY_RESULT))

    exit_code = module.main([])

    assert exit_code == module._USAGE_FAIL
    err = capsys.readouterr().err
    assert "cannot resolve git repo root" in err
