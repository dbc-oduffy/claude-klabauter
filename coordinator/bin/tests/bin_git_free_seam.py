
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest.mock import patch


def make_fake_repo_root(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    (repo_root / ".git").mkdir(exist_ok=True)
    return repo_root


def make_recording_git(
    *, response: Optional[str] = None,
) -> Callable:

    def _fake_git(cwd, *args):
        _fake_git.captured.append((str(cwd), tuple(args)))
        return response

    _fake_git.captured: List[Tuple[str, tuple]] = []
    return _fake_git


@contextmanager
def patched_git_seam(op_module, *, git_name: str = "_git", fake=None):
    if fake is None:
        fake = make_recording_git()
    with patch.object(op_module, git_name, fake):
        yield fake
