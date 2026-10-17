"""handoff_stamp.stamp_sync / stamp_apply: no-asyncio sync core, parity with _handler."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.locked_write import MutateAbort
from coordinator_core.ops.handoff_stamp import _handler, stamp_apply, stamp_sync
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BATON = "---\nhandoff_id: hnd-x-abc123\nclaimed_at: 2026-10-17T00:00:00Z\n---\nbody\n"


def _repo(tmp_path: Path, name: str) -> "tuple[Path, Path]":
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    d = root / "state" / "handoffs"
    d.mkdir(parents=True)
    f = d / "b.md"
    f.write_text(_BATON, encoding="utf-8", newline="")
    return root, f


def _params(sha="abcdef12", kind="ship-commit", path="state/handoffs/b.md"):
    return {"handoff_path": path, "sha": sha, "kind": kind}


def test_stamp_sync_leaves_asyncio_unloaded(tmp_path):
    root, _ = _repo(tmp_path, "r")
    code = (
        "import sys, pathlib\n"
        "from coordinator_core.ops.handoff_stamp import stamp_sync\n"
        f"r = stamp_sync({_params()!r}, pathlib.Path({str(root / '.git')!r}))\n"
        "assert r['exit_code'] == 0 and r['applied'], r\n"
        "assert 'asyncio' not in sys.modules, 'asyncio loaded'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize(
    "params",
    [_params(), _params(kind="bogus"), _params(path="state/handoffs/missing.md")],
    ids=["success", "bad-kind", "not-found"],
)
def test_sync_and_handler_agree(tmp_path, params):
    root_a, file_a = _repo(tmp_path, "a")
    root_b, file_b = _repo(tmp_path, "b")
    reply_a = stamp_sync(params, root_a / ".git")
    reply_b = asyncio.run(_handler(params, root_b / ".git"))
    assert reply_a == reply_b
    assert file_a.read_bytes() == file_b.read_bytes()


def test_stamp_apply_refusal_carries_reply():
    with pytest.raises(MutateAbort) as ei:
        stamp_apply(_BATON, _params(kind="bogus"), repo_root=Path("."))
    reply = ei.value.args[0]
    assert reply["exit_code"] == 1 and reply["kind"] == "bogus"
    with pytest.raises(MutateAbort) as ei:
        stamp_apply("no frontmatter", _params(), repo_root=Path("."))
    assert ei.value.args[0]["exit_code"] == 1


def test_stamp_apply_inserts_text_only():
    out = stamp_apply(_BATON, _params(), repo_root=Path("."))
    assert "shipped_in: abcdef12" in out and "shipped_in_kind: ship-commit" in out
