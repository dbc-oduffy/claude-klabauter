"""`brief()`-level wiring for the reply-owed obligation.

`test_reply_owed_is_armed_at_action_time.py` and `test_fold_into_plan_
disposition.py` both exercise the pure functions/dicts directly
(`pa.reply_obligation_at_open`, `pa._KIND_DISPOSITIONS`,
`pa._MEMO_ACTION_DECISION_MAP`) — none call `pa.brief(...)` and assert on the
resulting `next_move`/`preflight.reply_obligation`. A wiring bug in `brief()`
itself (wrong `fm` passed to `reply_obligation_at_open`, an inverted
condition, wrong concatenation order) would ship green under those two files
alone. This closes that gap (code-reviewer P2,
2026-09-01-codereview-sliceC-memo-lifecycle-coordinator-core-pickup-assemble-
init-py.md).

Run: python -m pytest coordinator_core/pickup_assemble/tests/test_reply_owed_wiring_at_brief.py -q
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.pickup_assemble as pa

# Declared, not excused: real git spawns to build the memo fixture, same
# convention as test_brief_claim_lease.py in this directory.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _isolated_git_env(anchor: Path) -> dict[str, str]:
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent),
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_memo(repo: Path, name: str, *, kind: str) -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f"kind: {kind}\n"
        "status: open\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Memo\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def test_an_ask_kind_memo_pickup_produces_the_reply_owed_signal(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m1.md", kind="ask")

    result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

    obj = result.decision_object
    assert obj["preflight"]["reply_obligation"] == "reply-owed-on-action"
    assert obj["next_move"].startswith(pa.reply_obligation_at_open({"kind": "ask"}))


def test_an_fyi_kind_memo_pickup_does_not_produce_the_reply_owed_signal(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_memo(repo, "m2.md", kind="fyi")

    result = pa.brief("cross-repo/inbox/m2.md", repo_root=repo)

    obj = result.decision_object
    assert obj["preflight"]["reply_obligation"] is None
    assert "reply-owed-on-action" not in str(obj["preflight"]["closure_signals"])
    assert pa.reply_obligation_at_open({"kind": "fyi"}) is None
