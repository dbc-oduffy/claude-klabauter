"""
coordinator_core.ops.tests.test_handoff_transition_sync_core — `transition_sync` /
`ship_apply` run without an event loop and match the async handler.

Purpose: pins that `handoff_transition` carries no module-level `asyncio`, that
`transition_sync` returns the same reply and writes the same bytes as
`asyncio.run(_handler(...))`, and that `ship_apply` refuses by raising
`MutateAbort` carrying the error reply dict.

Run (from repo root):
    python -m pytest coordinator_core/ops/tests/test_handoff_transition_sync_core.py -q
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import coordinator_core.ops.handoff_transition as ht
from coordinator_core.locked_write import MutateAbort
from coordinator_core.win_portability import no_console_creationflags

# Real `git` is spawned: locked_rmw resolves the git common dir through it.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}
_SID = "22222222-2222-2222-2222-222222222222"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        check=True,
        **no_console_creationflags(),
    )


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("repo")
    _git(path, "init")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("init\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")
    return path


def _baton(repo: Path, name: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: claimed\n"
        'predecessor: "none"\n'
        "deployment_state: in_flight\n"
        "pickup_ready: true\n"
        f'claimed_at: 2026-01-01T00:00:00Z\nclaimed_by: "{_SID}"\n'
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


def _params(path: Path, verb: str, **extra: str) -> dict:
    return {"verb": verb, "handoff_path": str(path), **extra}


def test_ship_parity_with_async_handler(repo: Path) -> None:
    a, b = _baton(repo, "20260101-ship-a.md"), _baton(repo, "20260101-ship-b.md")
    root = repo / ".git"

    sync_reply = ht.transition_sync(_params(a, "ship"), root)
    async_reply = asyncio.run(ht._handler(_params(b, "ship"), root))

    assert sync_reply["exit_code"] == 0 and sync_reply["applied"] is True
    assert sync_reply["applied"] == async_reply["applied"]
    assert sync_reply["message"].replace(str(a), "X") == async_reply["message"].replace(str(b), "X")
    assert a.read_text(encoding="utf-8").replace("ship-a", "X") == b.read_text(
        encoding="utf-8"
    ).replace("ship-b", "X")


def test_refused_verb_parity(repo: Path) -> None:
    a = _baton(repo, "20260101-close-a.md")
    before = a.read_bytes()
    params = _params(a, "close", reason="bogus")

    sync_reply = ht.transition_sync(params, repo / ".git")
    async_reply = asyncio.run(ht._handler(params, repo / ".git"))

    assert sync_reply["exit_code"] == 1
    assert sync_reply == async_reply
    assert a.read_bytes() == before


def test_ship_apply_returns_text_without_writing(repo: Path) -> None:
    a = _baton(repo, "20260101-apply.md")
    before = a.read_bytes()

    out = ht.ship_apply(a.read_text(encoding="utf-8"), _params(a, "ship"), repo_root=repo / ".git")

    assert "deployment_state: shipped" in out and "pickup_ready: false" in out
    assert a.read_bytes() == before
    assert ht.ship_apply(out, _params(a, "ship"), repo_root=repo / ".git") == out


def test_ship_apply_refusal_carries_error_reply() -> None:
    with pytest.raises(MutateAbort) as info:
        ht.ship_apply("no frontmatter here\n", {"handoff_path": "x.md"}, repo_root=None)

    reply = info.value.args[0]
    assert reply["exit_code"] == 1 and reply["applied"] is False
    assert "no parseable YAML frontmatter" in reply["error"]


def test_transition_sync_ship_loads_no_asyncio(repo: Path) -> None:
    a = _baton(repo, "20260101-fresh.md")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "import coordinator_core.ops.handoff_transition as ht\n"
        f"r = ht.transition_sync({{'verb': 'ship', 'handoff_path': {str(a)!r}}}, Path({str(repo / '.git')!r}))\n"
        "assert r['exit_code'] == 0 and r['applied'], r\n"
        "print('ASYNCIO_LOADED' if 'asyncio' in sys.modules else 'ASYNCIO_ABSENT')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "ASYNCIO_ABSENT" in proc.stdout, proc.stdout
