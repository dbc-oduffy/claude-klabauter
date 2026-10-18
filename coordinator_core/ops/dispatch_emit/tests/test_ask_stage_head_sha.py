"""ask_stage writes `head_sha` into manifest.json and its reply: the 40-hex HEAD in a repo, null outside one."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.win_portability import no_console_creationflags

_RUN = "headrun"


def _stage(root):
    (root / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")
    return _handler({"run_id": _RUN, "plan_path": PLAN_REL, "writes": []}, repo_root=root)


def _manifest(root) -> dict:
    return json.loads((root / RECORD_DIR_ROOT / _RUN / "manifest.json").read_text(encoding="utf-8"))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_head_sha_is_the_40_hex_head_in_reply_and_manifest(tmp_path):
    git = lambda *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(tmp_path), "-c", "user.email=t@e.x", "-c", "user.name=t", *a],
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()
    git("init", "-q")
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    git("add", "seed.txt")
    git("commit", "-q", "-m", "seed")
    head = git("rev-parse", "HEAD")

    reply = _stage(tmp_path)

    assert "error" not in reply, reply
    assert re.fullmatch(r"[0-9a-f]{40}", reply["head_sha"]) and reply["head_sha"] == head
    assert _manifest(tmp_path)["head_sha"] == head


def test_head_sha_is_null_when_head_does_not_resolve(tmp_path):
    (tmp_path / ".git").mkdir()
    reply = _stage(tmp_path)

    assert "error" not in reply, reply
    assert reply["head_sha"] is None
    assert _manifest(tmp_path)["head_sha"] is None
