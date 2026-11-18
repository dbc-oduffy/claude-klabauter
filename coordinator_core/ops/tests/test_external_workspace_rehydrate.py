"""external_workspace.rehydrate: clones only the named key into its registered path, resets a
worktree-less .git, no-ops a healthy one, and reports (never clones) under all_missing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.external_workspace.vcs_guard_contract import TOKEN_ENV
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import external_workspace_rehydrate as op
from coordinator_core.ops._registry_map import OP_MODULE_MAP

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IDENT = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(args, cwd=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, creationflags=_NO_WINDOW
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated git config and registry dir; returns (source repo, write_registry)."""
    cfg = tmp_path / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))

    src = tmp_path / "src"
    _git(["init", "-q", "-b", "main", str(src)])
    (src / "a.txt").write_text("one", encoding="utf-8")
    _git(["add", "a.txt"], cwd=src)
    _git([*_IDENT, "commit", "-q", "-m", "one"], cwd=src)
    first = _git(["rev-parse", "HEAD"], cwd=src)
    (src / "a.txt").write_text("two", encoding="utf-8")
    _git([*_IDENT, "commit", "-q", "-am", "two"], cwd=src)

    def write_registry(entries: dict) -> None:
        lines = []
        for key, fields in entries.items():
            lines.append(f"[external_workspaces.{key}]")
            for k, v in fields.items():
                lines.append(f'{k} = "{Path(v).as_posix() if k in ("path", "url") else v}"')
        (reg / "registry.local.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    return src, first, write_registry


def test_key_clones_into_the_registered_path_at_ref(tmp_path, env):
    src, first, write_registry = env
    dest = tmp_path / "ws" / "alpha"
    write_registry({"alpha": {"path": dest, "url": src, "ref": first}})
    out = op._handler({"key": "alpha"})
    assert out["action"] == "cloned"
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one"
    assert _git(["rev-parse", "HEAD"], cwd=dest) == first


def test_key_without_ref_clones_the_default_branch(tmp_path, env):
    src, _first, write_registry = env
    dest = tmp_path / "alpha"
    write_registry({"alpha": {"path": dest, "url": src}})
    assert op._handler({"key": "alpha"})["action"] == "cloned"
    assert (dest / "a.txt").read_text(encoding="utf-8") == "two"


def test_healthy_worktree_is_a_noop(tmp_path, env):
    src, _first, write_registry = env
    dest = tmp_path / "alpha"
    write_registry({"alpha": {"path": dest, "url": src}})
    op._handler({"key": "alpha"})
    (dest / "a.txt").write_text("local edit", encoding="utf-8")
    assert op._handler({"key": "alpha"})["action"] == "noop"
    assert (dest / "a.txt").read_text(encoding="utf-8") == "local edit"


def test_git_dir_without_worktree_is_reset_to_ref(tmp_path, env):
    src, first, write_registry = env
    dest = tmp_path / "alpha"
    write_registry({"alpha": {"path": dest, "url": src, "ref": first}})
    op._handler({"key": "alpha"})
    (dest / "a.txt").unlink()
    assert op._handler({"key": "alpha"})["action"] == "reset"
    assert (dest / "a.txt").read_text(encoding="utf-8") == "one"


def test_unregistered_key_and_missing_url_are_refused(tmp_path, env):
    _src, _first, write_registry = env
    write_registry({"nourl": {"path": tmp_path / "nourl"}})
    with pytest.raises(CallerFacingValidationError, match="not registered"):
        op._handler({"key": "ghost"})
    with pytest.raises(CallerFacingValidationError, match="url is not set"):
        op._handler({"key": "nourl"})
    assert not (tmp_path / "nourl").exists()


def test_nonempty_directory_without_git_is_not_cloned_over(tmp_path, env):
    src, _first, write_registry = env
    dest = tmp_path / "alpha"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine", encoding="utf-8")
    write_registry({"alpha": {"path": dest, "url": src}})
    with pytest.raises(CallerFacingValidationError, match="not empty"):
        op._handler({"key": "alpha"})
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_p4_workspace_is_refused(tmp_path, env):
    src, _first, write_registry = env
    write_registry({"p": {"path": tmp_path / "p", "url": src, "vcs": "p4"}})
    with pytest.raises(CallerFacingValidationError, match="p4"):
        op._handler({"key": "p"})


def test_all_missing_lists_missing_rows_and_clones_nothing(tmp_path, env):
    src, first, write_registry = env
    missing = tmp_path / "missing"
    present = tmp_path / "present"
    write_registry({
        "missing": {"path": missing, "url": src, "ref": first},
        "present": {"path": present, "url": src},
    })
    op._handler({"key": "present"})
    out = op._handler({"all_missing": True})
    assert out == {"missing": [{
        "key": "missing", "path": Path(missing).as_posix(), "url": Path(src).as_posix(), "ref": first,
    }]}
    assert not missing.exists()


@pytest.mark.parametrize("params", [{}, {"key": "a", "all_missing": True}, {"all_missing": False}])
def test_exactly_one_of_key_or_all_missing(env, params):
    with pytest.raises(CallerFacingValidationError, match="exactly one"):
        op._handler(params)


def test_token_is_in_the_spawn_env_only(tmp_path, env, monkeypatch):
    src, _first, write_registry = env
    write_registry({"alpha": {"path": tmp_path / "alpha", "url": src}})
    seen = []
    real_run = op.run_git

    def spy(*args, **kwargs):
        seen.append(kwargs["env"].get(TOKEN_ENV))
        assert TOKEN_ENV not in os.environ
        return real_run(*args, **kwargs)

    monkeypatch.setattr(op, "run_git", spy)
    op._handler({"key": "alpha"})
    assert seen and all(v for v in seen)
    assert TOKEN_ENV not in os.environ


def test_op_is_registered_in_the_lazy_map_and_eager_list():
    from coordinator_core.ops import _EAGER_OP_MODULES

    assert OP_MODULE_MAP[op.OP_KEY] == "coordinator_core.ops.external_workspace_rehydrate"
    assert any(m == "coordinator_core.ops.external_workspace_rehydrate" for m, _ in _EAGER_OP_MODULES)
    assert op._SCHEMA["additionalProperties"] is False
