"""Seeded git repos for tests: build once per process, copy per test.

A fresh `git init` + config + add + commit costs 5-7 process spawns; across the
suite's thousands of repo-building tests that is the dominant setup cost. A
seed is built once per distinct parameter set and copied with `shutil.copytree`
(zero spawns) into each test's directory.

Invariants:
  - The seed is built with system and global git config disabled, so its
    content does not depend on whichever HOME quarantine the first caller ran
    under. Identity and `commit.gpgsign=false` are written to the repo-local
    config, exactly as the hand-rolled helpers did.
  - Each copy is an independent repo; nothing links it back to the seed.
  - The seed holds exactly one commit containing `README.md`.  Callers that
    need a different first commit keep their own helper.
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_SEEDS: dict[tuple, Path] = {}
_SEED_ROOT: Path | None = None


def _seed_root() -> Path:
    global _SEED_ROOT
    if _SEED_ROOT is None:
        _SEED_ROOT = Path(tempfile.mkdtemp(prefix="mk-git-seed-"))
        atexit.register(shutil.rmtree, _SEED_ROOT, True)
    return _SEED_ROOT


def _build(dest: Path, branch: str | None, email: str, name: str, readme: str) -> None:
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(dest), *args],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=env,
            **no_console_creationflags(),
        )

    dest.mkdir(parents=True)
    git("init", "-q", *(["-b", branch] if branch else []))
    git("config", "commit.gpgsign", "false")
    git("config", "user.email", email)
    git("config", "user.name", name)
    (dest / "README.md").write_text(readme, encoding="utf-8")
    git("add", "README.md")
    git("commit", "-q", "-m", "init")


def _replace_copy(src: str, dst: str) -> str:
    """Git objects are mode 0444, so a re-seed over an existing repo cannot
    overwrite them in place; unlink first (directory-writable is enough)."""
    if os.path.lexists(dst):
        os.unlink(dst)
    return shutil.copy2(src, dst)


def seeded_repo(
    dest: Path,
    *,
    branch: str | None = None,
    email: str = "t@example.com",
    name: str = "t",
    readme: str = "init\n",
) -> Path:
    """Materialize a one-commit repo at `dest` (created if absent; may already
    exist empty-or-not, existing files are kept) and return `dest`."""
    key = (branch, email, name, readme)
    seed = _SEEDS.get(key)
    if seed is None:
        seed = _seed_root() / str(len(_SEEDS))
        _build(seed, branch, email, name, readme)
        _SEEDS[key] = seed
    dest = Path(dest)
    shutil.copytree(seed, dest, dirs_exist_ok=True, symlinks=True, copy_function=_replace_copy)
    return dest
