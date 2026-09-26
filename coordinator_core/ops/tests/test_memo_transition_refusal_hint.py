"""
Item 25 (2026-09-26 inbox-blitz claude-klabauter fixes, DoE thread) — the `MutateAbort`
`_action()` raises on an unexpected status names a recovery verb instead of
just refusing. See `state/cross-repo/archive/2026-09-23-doe-claude-em-memo-
refusal-recovery-staged-in-claude-klabauter.md`: land the hint naming
`claim-memo-stamp` and `resolve-memo` as the next step
(`doe-claude:state/improvement-queue/2026-08-27-memo-inbox-status-open-is-
not-evidence-of-unactioned.yaml`).

Real git spawn is load-bearing here — `_containment_check` resolves real
git-repo boundaries, mirroring the containment-gate pattern used throughout
coordinator_core/ops/memo/tests/test_memo_transition_unit.py.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.memo_transition import _action
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_OPEN_MEMO = """\
---
kind: fyi
status: open
from: sender-session
summary: A test memo.
created: 2026-06-01
---
"""


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, **no_console_creationflags())
    (path / ".gitkeep").touch()
    subprocess.run(["git", "-C", str(path), "add", ".gitkeep"], check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init", "--allow-empty-message"],
        check=True, capture_output=True, env=_ENV, **no_console_creationflags(),
    )


def _git_track(repo: Path, target: Path) -> None:
    rel = str(target.relative_to(repo))
    subprocess.run(["git", "-C", str(repo), "add", "--", rel],
                    check=True, capture_output=True, **no_console_creationflags())
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "deliver memo", "--", rel],
                    check=True, capture_output=True, env=_ENV, **no_console_creationflags())


def _init_repo_with_memo(tmp_path: Path, content: str) -> Path:
    repo = tmp_path / "repo"
    _git_init(repo)
    target_dir = repo / "cross-repo" / "inbox"
    target_dir.mkdir(parents=True)
    memo = target_dir / "memo.md"
    memo.write_text(content, encoding="utf-8")
    _git_track(repo, memo)
    return memo


def test_unexpected_status_refusal_names_the_recovery_verbs(tmp_path):
    memo = _init_repo_with_memo(tmp_path, _OPEN_MEMO)
    before = memo.read_text(encoding="utf-8")

    result = _action(str(memo), {"decision": "accepted", "realized_by": "abc1234"})

    assert result["exit_code"] == 1
    assert result["applied"] is False
    assert "expected in_progress" in result["error"]
    # The recovery hint names both routes forward: the two-step ceremony
    # (claim, then action) and the one-step atomic collapse.
    assert "claim-memo-stamp" in result["error"]
    assert "resolve-memo" in result["error"]
    # Refusal is read-only — no partial write on the way to the abort.
    assert memo.read_text(encoding="utf-8") == before
