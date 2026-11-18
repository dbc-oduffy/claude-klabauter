"""Session-built git repo templates, copied per test instead of re-running git init/add/commit.

A template is read-only: each test mutates only its own copy. Copies use shutil.copytree and
pathlib, so they work the same on Windows and POSIX.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_BUILT: dict[str, Path] = {}

_ORIGIN_URL = re.compile(
    r'(\[remote "origin"\][^\[]*?\burl\s*=\s*)[^\r\n]*', re.DOTALL
)


def template_repo(tmp_path_factory, key: str, build: Callable[[Path], None]) -> Path:
    """Return the session template for `key`, calling `build(path)` on an empty dir the first time."""
    cached = _BUILT.get(key)
    if cached is not None and cached.is_dir():
        return cached
    path = tmp_path_factory.mktemp("tpl-" + re.sub(r"[^A-Za-z0-9_-]+", "_", key)[:40])
    build(path)
    _BUILT[key] = path
    return path


def copy_repo(template: Path, dest: Path) -> Path:
    """Copy `template` (including .git) to `dest`, creating or merging into it; return `dest`."""
    shutil.copytree(template, dest, symlinks=True, dirs_exist_ok=True)
    return dest


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _rewrite_origin(clone: Path, bare: Path) -> None:
    """Point clone's origin at `bare` by editing .git/config text; no git spawn."""
    config = clone / ".git" / "config"
    text = config.read_text(encoding="utf-8")
    new_text, n = _ORIGIN_URL.subn(lambda m: m.group(1) + bare.as_posix(), text, count=1)
    if n != 1:
        raise RuntimeError(f"no [remote \"origin\"] url in {config}")
    config.write_text(new_text, encoding="utf-8")


def bare_with_clones(
    tmp_path_factory,
    dest: Path,
    names: Sequence[str] = ("claude-klabauter", "peer"),
    bare_name: str = "remote.git",
) -> tuple[Path, ...]:
    """Return (bare, *clones) under `dest`, copied from one session-built bare plus clones.

    Each clone's origin points at the copied bare, not the template's.
    """
    key = f"bare-with-clones:{bare_name}:{','.join(names)}"

    def _build(root: Path) -> None:
        _git(root, "init", "-q", "--bare", bare_name)
        for name in names:
            _git(root, "clone", "-q", bare_name, name)

    template = template_repo(tmp_path_factory, key, _build)
    copy_repo(template, dest)
    bare = dest / bare_name
    clones = tuple(dest / name for name in names)
    for clone in clones:
        _rewrite_origin(clone, bare)
    return (bare, *clones)
