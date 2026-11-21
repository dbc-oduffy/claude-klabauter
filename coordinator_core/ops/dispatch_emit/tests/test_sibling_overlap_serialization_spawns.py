import subprocess
import sys

import pytest

from coordinator_core import _settings_home
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def _isolated_machine_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def _root(tmp_path):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


_CHILD = """
import sys
from pathlib import Path
from coordinator_core import _settings_home
_settings_home.machine_local_dir = lambda: Path(sys.argv[2]) / "ml-home"
from coordinator_core.ops.dispatch_emit import live_run_index as idx, live_run_writes
root = Path(sys.argv[1])
rid = sys.argv[3]
live_run_writes.index_mint(root, rid, 1.0)
r = idx.claim(root, rid, ["shared/file.py"])
print("CLAIMED" if r == {} else "REFUSED" if r else "UNCLAIMED")
"""


def test_two_processes_claiming_same_path_exactly_one_wins(tmp_path: Path) -> None:
    root = _root(tmp_path)
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(root), str(tmp_path), rid],
            stdout=subprocess.PIPE, text=True,
        )
        for rid in ("p1", "p2")
    ]
    outs = sorted(p.communicate(timeout=60)[0].strip() for p in procs)
    assert outs == ["CLAIMED", "REFUSED"]


_PLAN = """---
title: t
---

## Tasks

```yaml plan-tasks
- id: C1
  title: t
  writes:
    - a/b.py
    - a/c.py
```
"""
