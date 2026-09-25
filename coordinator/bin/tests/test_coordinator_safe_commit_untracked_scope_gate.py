"""`coordinator-safe-commit.py::_warn_undeclared_untracked_siblings` --
issue #84 item 1: the committer commits exactly the given pathspec and
nothing more, so a new file the fix agent created but never named was
silently left untracked while the rest of the fix landed. This gate refuses
loudly instead, naming the disagreement between the declared pathspec and
the tree, rather than guessing whether the untracked file belongs.

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import subprocess

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _tiny_repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run = lambda *a: subprocess.run(
        ["git", *a], cwd=root, capture_output=True, text=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (root / "pkg").mkdir()
    (root / "pkg" / "existing.py").write_text("x = 1\n", encoding="utf-8")
    run("add", "pkg/existing.py")
    run("commit", "-q", "-m", "seed")
    return root


@pytest.mark.spawns_process
@pytest.mark.cadence
class TestUndeclaredUntrackedSibling:
    def test_new_untracked_module_beside_a_declared_edit_warns(self, tmp_path, capsys):
        """The exact shape from the issue: a fix touches an existing file and
        also creates a brand-new module in the same package, but the pathspec
        names only the edit."""
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "pkg" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (root / "pkg" / "_roundtrip.py").write_text("new module\n", encoding="utf-8")

        mod._warn_undeclared_untracked_siblings(["pkg/existing.py"], str(root))
        err = capsys.readouterr().err
        assert "pkg/_roundtrip.py" in err
        assert "WARNING" in err

    def test_every_touched_file_declared_allows_silently(self, tmp_path, capsys):
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "pkg" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (root / "pkg" / "_roundtrip.py").write_text("new module\n", encoding="utf-8")

        assert (
            mod._warn_undeclared_untracked_siblings(
                ["pkg/existing.py", "pkg/_roundtrip.py"], str(root)
            )
            is None
        )
        assert capsys.readouterr().err == ""

    def test_untracked_file_in_an_unrelated_directory_does_not_block(self, tmp_path, capsys):
        """Scope is the DECLARED paths' own directories -- an untracked file
        elsewhere in the tree is not this commit's business."""
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "pkg" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (root / "elsewhere").mkdir()
        (root / "elsewhere" / "scratch.py").write_text("junk\n", encoding="utf-8")

        assert (
            mod._warn_undeclared_untracked_siblings(["pkg/existing.py"], str(root))
            is None
        )
        assert capsys.readouterr().err == ""

    def test_untracked_file_in_a_deeper_subdirectory_does_not_block(self, tmp_path, capsys):
        """Regression: a directory git pathspec (`-- pkg`) matches every path
        under it AT ANY DEPTH, so an untracked file several levels below a
        declared path's own directory (a live peer's own unrelated work --
        `coordinator_core/roadmap/tests/test_blitz_land_reconciles_sizing.py`
        sitting under a commit naming only `coordinator_core/op_scopes.py`)
        must NOT be flagged. "Same directory" is the declared path's own
        EXACT immediate parent, never anything nested under it."""
        mod = _load_cli_module()
        root = _tiny_repo(tmp_path)
        (root / "pkg" / "existing.py").write_text("x = 2\n", encoding="utf-8")
        (root / "pkg" / "nested").mkdir()
        (root / "pkg" / "nested" / "unrelated_peer_work.py").write_text(
            "not this commit's business\n", encoding="utf-8"
        )

        assert (
            mod._warn_undeclared_untracked_siblings(["pkg/existing.py"], str(root))
            is None
        )
        assert capsys.readouterr().err == ""

    def test_an_unresolvable_repo_fails_open(self, tmp_path):
        """An unanswerable probe is an ADDITIONAL refusal on top of the
        committer's existing checks, not a new hard dependency -- it must not
        turn "cannot tell" into "always refuse"."""
        mod = _load_cli_module()
        assert (
            mod._warn_undeclared_untracked_siblings(
                ["pkg/existing.py"], str(tmp_path / "not-a-repo")
            )
            is None
        )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_probe_is_exactly_one_batched_git_spawn_for_multiple_paths(tmp_path, monkeypatch):
    """The docstring's "One batched git call" claim, pinned: multiple
    declared paths across multiple directories must still resolve via a
    SINGLE `git ls-files` spawn (one `--` pathspec list), never one call
    per declared path."""
    mod = _load_cli_module()
    root = _tiny_repo(tmp_path)
    (root / "pkg2").mkdir()
    (root / "pkg2" / "existing2.py").write_text("y = 1\n", encoding="utf-8")

    calls = []
    real_run = mod.subprocess.run

    def _counting_run(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _counting_run)

    mod._warn_undeclared_untracked_siblings(
        ["pkg/existing.py", "pkg2/existing2.py"], str(root)
    )

    assert len(calls) == 1


def test_do_pathspec_calls_the_gate_before_dispatching():
    source = (_BIN_DIR / "coordinator-safe-commit.py").read_text(encoding="utf-8")
    body = source.split("def do_pathspec(", 1)[1].split("\ndef ", 1)[0]
    assert "_warn_undeclared_untracked_siblings(" in body
    assert body.index("_warn_undeclared_untracked_siblings(") < body.index(
        'cc_invoke("ceremony.commit_v2"'
    ), "gate must run before the commit dispatch"
