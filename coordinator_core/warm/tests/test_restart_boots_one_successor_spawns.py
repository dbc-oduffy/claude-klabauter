"""A restart boots one successor: concurrent spawn decisions after the server dies
elect exactly one booter, including when the claimer has already exited and when
the young record's endpoint probe fails.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from coordinator_core.warm import breadcrumb

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

N_CALLERS = 8

_CLAIM_AND_EXIT = """
import sys
sys.path.insert(0, {repo_root!r})
from pathlib import Path
from coordinator_core.warm import breadcrumb
sys.stdout.write("claimed\\n" if breadcrumb.try_claim_boot(Path({lock_path!r})) else "denied\\n")
"""


def _repo_root() -> str:
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


def _claim_in_exiting_process(lock_path: Path) -> str:
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [sys.executable, "-c", _CLAIM_AND_EXIT.format(repo_root=_repo_root(), lock_path=str(lock_path))],
        capture_output=True,
        text=True,
        timeout=60,
        **kwargs,
    )
    return out.stdout.strip()


def test_a_claimer_that_exited_after_spawning_still_vouches_for_its_boot(tmp_path: Path) -> None:
    lock_path = tmp_path / "warm.json.boot.lock"

    assert _claim_in_exiting_process(lock_path) == "claimed"

    assert breadcrumb.try_claim_boot(lock_path) is False, (
        "the claimer's process is gone and its lock is free, but its boot is seconds old: "
        "a second caller must not spawn a second server"
    )
    assert breadcrumb.should_spawn_decision(None, lock_path=lock_path) is False


def test_the_exited_claimers_vouch_ends_after_the_released_claim_grace(tmp_path: Path) -> None:
    lock_path = tmp_path / "warm.json.boot.lock"
    assert _claim_in_exiting_process(lock_path) == "claimed"

    after = time.time() + breadcrumb.BOOT_CLAIM_RELEASED_GRACE_SECS + 0.5
    assert breadcrumb.try_claim_boot(lock_path, now=after) is True, (
        "a boot that published nothing within the grace has failed; the next caller spawns"
    )


_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
_SKEW = (
    b'{"jsonrpc":"2.0","id":1,"error":{"code":%d,"message":"retired"}}\n' % -32002
)
