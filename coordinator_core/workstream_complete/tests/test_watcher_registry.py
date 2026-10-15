"""Watcher pattern registry: launcher forms match, near-misses do not."""

import pytest

from coordinator_core.workstream_complete import watcher_registry as reg
from coordinator_core.workstream_complete.watcher_stand_down_contract import OP_NAME

WATCH = [
    r"repo\coordinator\bin\group-em-watch.cmd --until-event",
    r"python.exe repo\coordinator\bin\group-em-watch.py --until-event",
    "/usr/bin/python3 /home/u/repo/coordinator/bin/group-em-watch.py --until-event",
    "/home/u/bin/group-em-watch --until-event",
    r"group-em-watch.exe",
    'python "my repo\\bin\\group-em-watch.py" --x',
    "python -u /x/group-em-watch.py",
    "pwsh group-em-watch.ps1",
]
GOVERNOR = [
    r"python.exe repo\bin\group-em-memory-governor.py",
    "python3 /a/b/group-em-memory-governor.py --interval 5",
    "/a/group-em-memory-governor",
]


@pytest.mark.parametrize("cmd", WATCH)
def test_watch_matches(cmd):
    assert reg.match(cmd).watcher_id == "group-em-watch"


@pytest.mark.parametrize("cmd", GOVERNOR)
def test_governor_matches(cmd):
    assert reg.match(cmd).watcher_id == "group-em-memory-governor"


@pytest.mark.parametrize(
    "cmd",
    [
        "group-em-watch-cli --status",
        r"python.exe repo\bin\group-em-watch-cli.py --status",
        "python -m pytest coordinator_core/tests/test_group_em_watch.py",
        "python -m pytest group-em-watch-tests",
        "node group-em-box-notice.js",
        "",
    ],
)
def test_near_miss_does_not_match(cmd):
    assert reg.match(cmd) is None


def test_notify_when_idle_expires_on_its_own_and_never_matches():
    assert reg.expires_on_its_own() == ("notify_when_idle",)
    assert reg.match("notify_when_idle") is None


def test_op_name():
    assert OP_NAME == "workstream_complete.stand_down_watchers"
