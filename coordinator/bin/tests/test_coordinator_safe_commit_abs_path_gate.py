"""`coordinator-safe-commit` at default arguments refuses a plan ADDING an absolute machine path.

The CLI's `cc_invoke` transport is replaced by an in-process call to the registered
`ceremony.commit_v2` handler, so the refusal under test is the engine's own gate and no warm or
spawned engine is involved. The path literal is built from parts so this file carries none.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_DRIVE = "C" + ":" + "\\" + "Users" + "\\" + "jdoe_dev" + "\\" + "repo"
_PLAN = "docs/plans/2026-11/abs-gate-fixture.md"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit_abs_path_gate", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit_abs_path_gate", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "r"
    (root / "docs" / "plans" / "2026-11").mkdir(parents=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def cli(monkeypatch):
    mod = _load_cli_module()
    mod._bootstrap_engine()
    mod.require_engine_on_path(mod.__file__)
    import cc_invoke as transport
    from coordinator_core import ipc

    def in_process(op, params, repo_root, **_kw):
        return ipc.get_op_handler(op)(params, repo_root=pathlib.Path(repo_root) / ".git")

    monkeypatch.setattr(transport, "cc_invoke", in_process)
    monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda paths, root: None)
    return mod


def _commit(cli, text: str, repo: pathlib.Path):
    (repo / _PLAN).write_text(text, encoding="utf-8")
    return cli.main(["add the plan", "--", _PLAN])


def test_a_plan_adding_an_absolute_path_exits_nonzero_with_the_one_line_fix(cli, repo, capsys):
    before = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(SystemExit) as exc:
        _commit(cli, f"# plan\nsee {_DRIVE}\n", repo)

    err = capsys.readouterr().err
    assert exc.value.code == 1
    assert "abs_path_citation_gate" in err
    assert f"{_PLAN}:2" in err
    assert "abs-path-ok" in err
    assert _git(repo, "rev-parse", "HEAD") == before


def test_the_clean_variant_of_the_same_plan_commits(cli, repo, capsys):
    before = _git(repo, "rev-parse", "HEAD")

    _commit(cli, "# plan\nsee docs/other.md\n", repo)

    assert "committed sha=" in capsys.readouterr().err
    assert _git(repo, "rev-parse", "HEAD") != before
