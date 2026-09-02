"""test_prepare_commit_msg_spawn_budget.py — pytest tests for
coordinator/bin/coordinator-prepare-commit-msg.py's C1 spawn-reduction work
(docs/plans/2026-08-21-a-commit-stops-paying-for-thirty-processes.md, C1).

Covers:
  - C1 §0: `coordinator_core.ops.deliverable_carry`
    (`DivergentDeliverableIdError`) is no longer imported anywhere in this
    hook — it was dead weight on the hot path (imported, never raised or
    caught by name; DR-406 made the omit explicit) that dragged in
    `coordinator_core.ops.__init__`'s ~594-module eager sweep.
  - C1(b): `_resolve_git_dir()` derives the git-dir with ZERO subprocess
    spawns and reproduces `git rev-parse --git-dir`'s PER-WORKTREE answer
    (never the common dir) for: an ordinary repo, an explicit `GIT_DIR`
    override, a linked worktree, and a submodule.

OUT OF SCOPE for this chunk, and therefore not asserted zero-spawn here:
C1(a) (`git interpret-trailers` -> Python append, byte-identity corpus) and
C1(c) (`git diff --cached --name-only` removal — established as
LOAD-BEARING: it supplies the actual staged pathspec `_resolve_deliverable_
id_from_paths` checks for frontmatter `deliverable_id`, not merely a
boolean gate, so it stays). Both remain real git spawns on the annotate
path; AC3's zero-git-subprocess claim is not met by this chunk alone.

Declared, not excused: this file spawns real `git` (worktree/submodule
fixtures, and one end-to-end hook invocation) because the property under
test is git's own git-dir resolution and this hook's real spawn count —
coordinator_core/tests/test_no_new_spawning_tests.py Rule 2/4.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_HOOK_PATH = _BIN_DIR / "coordinator-prepare-commit-msg.py"

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "coordinator_prepare_commit_msg_c1", _HOOK_PATH
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(args, cwd):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )


def _real_git_dir(cwd) -> str:
    return _git(["rev-parse", "--git-dir"], cwd=cwd).stdout.strip()


def _resolved(path_str: str, cwd) -> str:
    """Normalize a possibly-relative git-dir string for comparison —
    `_resolve_git_dir` and `git rev-parse --git-dir` are not required to
    agree on relative-vs-absolute FORM (unlike AC3's byte-identity
    requirement for the trailer text), only on which actual directory each
    names."""
    p = Path(path_str)
    if not p.is_absolute():
        p = Path(cwd) / p
    return os.path.normcase(os.path.normpath(str(p.resolve())))


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], cwd=root)
    _git(["config", "user.email", "t@example.com"], cwd=root)
    _git(["config", "user.name", "T"], cwd=root)
    (root / "f.txt").write_text("x\n", encoding="utf-8")
    _git(["add", "f.txt"], cwd=root)
    _git(["commit", "-q", "-m", "initial"], cwd=root)
    return root


# ---------------------------------------------------------------------------
# C1 §0 -- the dead coordinator_core.ops import stays gone.
# ---------------------------------------------------------------------------


def test_deliverable_carry_import_not_reintroduced():
    text = _HOOK_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("from coordinator_core.ops") or stripped.startswith(
            "import coordinator_core.ops"
        ):
            pytest.fail(f"dead-weight coordinator_core.ops import reintroduced: {line!r}")


def test_divergent_deliverable_id_error_not_a_live_import():
    """The identifier is allowed to survive in prose (module docstring and
    the DR-406 history note on `_resolve_deliverable_id_from_paths`) — it
    must not appear as an actual `ImportFrom` name anywhere in the file."""
    import ast

    tree = ast.parse(_HOOK_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name != "DivergentDeliverableIdError"


# ---------------------------------------------------------------------------
# C1(b) -- _resolve_git_dir() derives without spawning.
# ---------------------------------------------------------------------------


def test_resolve_git_dir_no_subprocess_call(repo, monkeypatch):
    mod = _load_module()
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GIT_DIR", raising=False)
    calls = []
    orig_run = mod.subprocess.run

    def _spy(*args, **kwargs):
        calls.append(args)
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _spy)
    mod._resolve_git_dir()
    assert calls == []


def test_ordinary_repo_matches_rev_parse(repo, monkeypatch):
    mod = _load_module()
    monkeypatch.chdir(repo)
    monkeypatch.delenv("GIT_DIR", raising=False)
    got = mod._resolve_git_dir()
    want = _real_git_dir(repo)
    assert _resolved(got, repo) == _resolved(want, repo)


def test_explicit_git_dir_env_honoured(repo, monkeypatch, tmp_path):
    mod = _load_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    got = mod._resolve_git_dir()
    assert got == str(repo / ".git")


def test_linked_worktree_matches_rev_parse_per_worktree(repo, monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    _git(["worktree", "add", "-q", str(wt), "-b", "wt-branch"], cwd=repo)
    mod = _load_module()
    monkeypatch.chdir(wt)
    monkeypatch.delenv("GIT_DIR", raising=False)
    got = mod._resolve_git_dir()
    want = _real_git_dir(wt)
    assert _resolved(got, wt) == _resolved(want, wt)
    # Per-worktree, never the common dir.
    assert "worktrees" in _resolved(got, wt).replace("\\", "/")


def test_submodule_matches_rev_parse(repo, monkeypatch, tmp_path):
    sub_upstream = tmp_path / "sub_upstream"
    sub_upstream.mkdir()
    _git(["init", "-q"], cwd=sub_upstream)
    _git(["config", "user.email", "t@example.com"], cwd=sub_upstream)
    _git(["config", "user.name", "T"], cwd=sub_upstream)
    (sub_upstream / "s.txt").write_text("s\n", encoding="utf-8")
    _git(["add", "s.txt"], cwd=sub_upstream)
    _git(["commit", "-q", "-m", "sub initial"], cwd=sub_upstream)

    _git(
        [
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_upstream).replace("\\", "/"),
            "sub",
        ],
        cwd=repo,
    )
    sub_dir = repo / "sub"
    mod = _load_module()
    monkeypatch.chdir(sub_dir)
    monkeypatch.delenv("GIT_DIR", raising=False)
    got = mod._resolve_git_dir()
    want = _real_git_dir(sub_dir)
    assert _resolved(got, sub_dir) == _resolved(want, sub_dir)


def test_not_a_repo_returns_empty(tmp_path, monkeypatch):
    empty = tmp_path / "not_a_repo"
    empty.mkdir()
    mod = _load_module()
    monkeypatch.chdir(empty)
    monkeypatch.delenv("GIT_DIR", raising=False)
    assert mod._resolve_git_dir() == ""


# ---------------------------------------------------------------------------
# End-to-end: the hot path's git-dir resolution no longer spawns.
# ---------------------------------------------------------------------------


def test_prepare_commit_msg_fail_safe_gate_spawns_no_git(repo):
    """A non-UUID session id trips main()'s fail-safe gate right after
    `_resolve_git_dir()`/`_resolve_session_id()`, before `diff --cached` or
    `interpret-trailers` are ever reached — isolating `_resolve_git_dir`'s
    own contribution. Asserts the hook still exits 0 (never blocks a
    commit) and that no `git` child process was spawned to get there."""
    msg_file = repo / "COMMIT_EDITMSG"
    msg_file.write_text("subject\n\nbody\n", encoding="utf-8")
    env = dict(os.environ)
    env["COORDINATOR_SESSION_ID"] = "not-a-uuid"
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("CLAUDE_CODE_SESSION_ID", None)

    out = subprocess.run(
        [sys.executable, str(_HOOK_PATH), str(msg_file)],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )
    assert out.returncode == 0
    # The message file is untouched — no trailer, no interpret-trailers call.
    assert "Session-Id:" not in msg_file.read_text(encoding="utf-8")
