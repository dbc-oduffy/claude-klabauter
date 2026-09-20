"""`commit_scoped(..., detect_rollback=True)` -- AC-P2-4 (P2e,
docs/plans/2026-09-11-close-the-three-silent-failure-gaps.md).

Mirrors `test_commit_refuses_staged_rollback.py`'s history-building pattern,
adapted to `commit_scoped`'s own contract: it never raises, it always
returns a `GitResult` (`ok is False` on refusal, `stderr` names the
findings, `returncode == -1`, no `git commit` spawned). Also pins that both
of `coordinator-safe-commit.py`'s live `commit_scoped` call sites pass
`detect_rollback=True`, and that `--declared-revert` reaches
`declared_reverts`.
"""

import subprocess

import pytest

from coordinator_core.ops.ceremony import git_native

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path, name="r"):
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _commit_scoped(repo, path, content, msg, **kwargs):
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content, encoding="utf-8", newline="\n")
    msg_path = repo / "MSG"
    msg_path.write_text(msg + "\n", encoding="utf-8", newline="\n")
    return git_native.commit_scoped([path], str(msg_path), cwd=str(repo), **kwargs)


def _head_sha(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _head_tree_sha(repo):
    return _git(repo, "rev-parse", "HEAD^{tree}").stdout.strip()


def test_refuses_depth_2_rollback(tmp_path):
    """v0 -> v1 -> a third commit whose staged bytes restore v0's exact
    blob at depth 2: refused, `ok is False`, findings named, nothing
    landed."""
    repo = _repo(tmp_path)
    _commit_scoped(repo, "p.txt", "v0\n", "v0")
    _commit_scoped(repo, "p.txt", "v1\n", "v1")
    before = _head_sha(repo)

    result = _commit_scoped(repo, "p.txt", "v0\n", "revert p", detect_rollback=True)

    assert result.ok is False
    assert result.returncode == -1
    assert "p.txt" in result.stderr
    assert "2" in result.stderr
    assert _head_sha(repo) == before


def test_no_tree_or_commit_object_written_on_refusal(tmp_path):
    """No ref move, and HEAD's tree is unchanged -- the refusal fires
    before `_commit_scoped_private_index` runs."""
    repo = _repo(tmp_path)
    _commit_scoped(repo, "p.txt", "v0\n", "v0")
    _commit_scoped(repo, "p.txt", "v1\n", "v1")
    before_head = _head_sha(repo)
    before_tree = _head_tree_sha(repo)

    result = _commit_scoped(repo, "p.txt", "v0\n", "revert p", detect_rollback=True)

    assert result.ok is False
    assert _head_sha(repo) == before_head
    assert _head_tree_sha(repo) == before_tree


def test_declared_reverts_lands_the_commit(tmp_path):
    """`declared_reverts=[p]` excludes it from the candidate set -- the
    same rollback lands rather than refusing."""
    repo = _repo(tmp_path)
    _commit_scoped(repo, "p.txt", "v0\n", "v0")
    _commit_scoped(repo, "p.txt", "v1\n", "v1")

    result = _commit_scoped(
        repo,
        "p.txt",
        "v0\n",
        "revert p, declared",
        detect_rollback=True,
        declared_reverts=["p.txt"],
    )
    assert result.ok is True
    assert (repo / "p.txt").read_text(encoding="utf-8") == "v0\n"


def test_detect_rollback_false_default_lands_unchanged(tmp_path):
    """`detect_rollback=False` (the default) never refuses, even over the
    identical planted rollback."""
    repo = _repo(tmp_path)
    _commit_scoped(repo, "p.txt", "v0\n", "v0")
    _commit_scoped(repo, "p.txt", "v1\n", "v1")

    result = _commit_scoped(repo, "p.txt", "v0\n", "revert p, default off")
    assert result.ok is True


def test_clean_edit_not_refused(tmp_path):
    """An ordinary edit that never restores an older exact blob is
    unaffected by `detect_rollback=True`."""
    repo = _repo(tmp_path)
    _commit_scoped(repo, "p.txt", "v0\n", "v0")

    result = _commit_scoped(repo, "p.txt", "v1\n", "ordinary edit", detect_rollback=True)
    assert result.ok is True


def test_breadth_3_refused(tmp_path):
    """Three distinct paths each restoring an older version at any depth
    refuses on breadth alone (K-016's rule, via `rollback_check.refusal`)."""
    repo = _repo(tmp_path)
    for name, v in (("a.txt", "a0"), ("b.txt", "b0"), ("c.txt", "c0")):
        (repo / name).write_text(v + "\n", encoding="utf-8", newline="\n")
    msg_path = repo / "MSG"
    msg_path.write_text("seed abc\n", encoding="utf-8", newline="\n")
    r = git_native.commit_scoped(["a.txt", "b.txt", "c.txt"], str(msg_path), cwd=str(repo))
    assert r.ok is True

    for name, v in (("a.txt", "a1"), ("b.txt", "b1"), ("c.txt", "c1")):
        (repo / name).write_text(v + "\n", encoding="utf-8", newline="\n")
    msg_path.write_text("abc v1\n", encoding="utf-8", newline="\n")
    r = git_native.commit_scoped(["a.txt", "b.txt", "c.txt"], str(msg_path), cwd=str(repo))
    assert r.ok is True

    for name, v in (("a.txt", "a0"), ("b.txt", "b0"), ("c.txt", "c0")):
        (repo / name).write_text(v + "\n", encoding="utf-8", newline="\n")
    msg_path.write_text("revert all three\n", encoding="utf-8", newline="\n")
    result = git_native.commit_scoped(
        ["a.txt", "b.txt", "c.txt"], str(msg_path), cwd=str(repo), detect_rollback=True
    )
    assert result.ok is False


def test_do_scoped_pin_passes_detect_rollback(tmp_path):
    """Pin: `do_scoped`'s `commit_scoped` call always passes
    `detect_rollback=True`."""
    import importlib.util
    import inspect

    bin_path = (
        __import__("pathlib").Path(__file__).resolve().parents[4]
        / "coordinator" / "bin" / "coordinator-safe-commit.py"
    )
    spec = importlib.util.spec_from_file_location("coordinator_safe_commit", bin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    src = inspect.getsource(module.do_scoped)
    assert "detect_rollback=True" in src


def test_do_scope_from_pin_passes_detect_rollback(tmp_path):
    """Pin: `do_scope_from`'s `commit_scoped` call always passes
    `detect_rollback=True`."""
    import importlib.util
    import inspect

    bin_path = (
        __import__("pathlib").Path(__file__).resolve().parents[4]
        / "coordinator" / "bin" / "coordinator-safe-commit.py"
    )
    spec = importlib.util.spec_from_file_location("coordinator_safe_commit", bin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    src = inspect.getsource(module.do_scope_from)
    assert "detect_rollback=True" in src


def test_declared_revert_flag_parses(tmp_path):
    """`--declared-revert <path>` is repeatable and reaches
    `args.declared_reverts`."""
    import importlib.util

    bin_path = (
        __import__("pathlib").Path(__file__).resolve().parents[4]
        / "coordinator" / "bin" / "coordinator-safe-commit.py"
    )
    spec = importlib.util.spec_from_file_location("coordinator_safe_commit", bin_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    args = module.parse_args(
        ["--declared-revert", "a.txt", "--declared-revert", "b.txt", "subject"]
    )
    assert args.declared_reverts == ["a.txt", "b.txt"]
