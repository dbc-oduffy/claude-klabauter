
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest.mock import patch


def run(coro):
    return asyncio.run(coro)


def make_recording_mover(
    *, response: Optional[Tuple[List[dict], List[dict]]] = None,
) -> Callable:

    async def _mover(worktree_root, moves, subject):
        _mover.captured = list(moves)
        _mover.subject = subject
        if response is not None:
            return response
        acted = [{"id": m.candidate_id, "archived": True} for m in moves]
        return acted, []

    _mover.captured = None
    _mover.subject = None
    return _mover


@contextmanager
def patched_disposition_seam(op_module, *, worktree: Path, mover=None):
    if mover is None:
        mover = make_recording_mover()
    with patch.object(
        op_module, "check_repo_root", lambda param_root, common_dir_arg: None
    ), patch.object(
        op_module, "main_worktree_root", lambda common_dir: worktree
    ), patch.object(
        op_module, "archive_and_commit", mover
    ):
        yield mover
