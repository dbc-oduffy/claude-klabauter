"""`coordinator-safe-commit` refuses a staged plan that ADDS an absolute machine path, through
`ceremony.commit_v2`'s in-process abs-path gate; the clean variant commits.

The op call is routed in-process to `commit_v2._handler` so the gate under test is this checkout's,
never a warm engine's. Path literals are built from parts so this file carries none.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_PLAN = "docs/plans/p.md"
_DRIVE = "C" + ":" + "\\" + "Users" + "\\" + "jdoe_dev" + "\\" + "repo"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit_abs_path", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit_abs_path", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "docs" / "plans").mkdir(parents=True)
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    (r / _PLAN).write_text("# plan\nclean line\n", encoding="utf-8")
    _git(["add", "-A"], r)
    _git(["commit", "-q", "-m", "seed"], r)
    monkeypatch.chdir(r)
    return r


@pytest.fixture
def run_cli(repo, monkeypatch):
    """Drive `main` with `ceremony.commit_v2` served in-process; returns the SystemExit code (0 on a clean return)."""
    mod = _load_cli_module()
    mod._bootstrap_engine()
    import cc_invoke
    from coordinator_core.ops.ceremony import commit_v2

    def _in_process(op, params, worktree_root, *a, **k):
        assert op == "ceremony.commit_v2"
        return commit_v2._handler(params, repo_root=pathlib.Path(worktree_root) / ".git")

    monkeypatch.setattr(cc_invoke, "cc_invoke", _in_process)

    def _run(subject: str) -> int:
        try:
            mod.main([subject, "--", _PLAN])
        except SystemExit as exc:
            return int(exc.code or 0)
        return 0

    return _run


def test_a_plan_adding_an_absolute_path_is_refused_with_the_one_line_fix(repo, run_cli, capsys):
    (repo / _PLAN).write_text("# plan\nclean line\nsee " + _DRIVE + "\n", encoding="utf-8")
    before = _git(["rev-parse", "HEAD"], repo).strip()

    code = run_cli("edit plan")

    err = capsys.readouterr().err
    assert code != 0, err
    assert "abs_path_citation_gate" in err
    assert f"{_PLAN}:3" in err
    assert "abs-path-ok" in err
    assert _git(["rev-parse", "HEAD"], repo).strip() == before


def test_the_clean_plan_commits(repo, run_cli, capsys):
    (repo / _PLAN).write_text("# plan\nclean line\nmore prose\n", encoding="utf-8")
    before = _git(["rev-parse", "HEAD"], repo).strip()

    code = run_cli("edit plan")

    assert code == 0, capsys.readouterr().err
    assert _git(["rev-parse", "HEAD"], repo).strip() != before
