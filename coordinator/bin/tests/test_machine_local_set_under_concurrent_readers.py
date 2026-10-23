"""`machine-local set` lands while other processes repeatedly open registry.local.toml (Windows)."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="read/replace contention is Windows-only")

_REPO = Path(__file__).resolve().parents[3]
_IMPL = _REPO / "coordinator" / "templates" / "bin" / "_machine_local.py"

SET_COUNT = 20
READER_DEADLINE_S = 20.0  # safety cap only; readers stop on the stop flag, so they outlive every set
READY_WAIT_S = 10.0
KEY = "probe_key"

_TEMPLATE_READER = """
import importlib.util, json, sys, time
impl, target, stop, ready, deadline = sys.argv[1:6]
spec = importlib.util.spec_from_file_location("_ml_reader", impl)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
reads = failures = 0
end = time.monotonic() + float(deadline)
while time.monotonic() < end:
    try:
        mod._load_toml(target)
        reads += 1
    except BaseException:
        failures += 1
    if reads == 1:
        open(ready, "w").close()
    try:
        open(stop).close()
        break
    except OSError:
        pass
print(json.dumps({"reads": reads, "failures": failures}))
"""

_SHARED_READER = """
import json, sys, time, tomllib
repo, target, stop, ready, deadline = sys.argv[1:6]
sys.path.insert(0, repo)
from coordinator_core.shared_read import read_bytes_shared
reads = failures = 0
end = time.monotonic() + float(deadline)
while time.monotonic() < end:
    try:
        tomllib.loads(read_bytes_shared(target).decode("utf-8"))
        reads += 1
    except BaseException:
        failures += 1
    if reads == 1:
        open(ready, "w").close()
    try:
        open(stop).close()
        break
    except OSError:
        pass
print(json.dumps({"reads": reads, "failures": failures}))
"""


def _load_template():
    spec = importlib.util.spec_from_file_location("_ml_under_test", _IMPL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _start_reader(code: str, first_arg: str, target: Path, stop: Path, ready: Path) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code, first_arg, str(target), str(stop), str(ready), str(READER_DEADLINE_S)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.mark.spawns_process
def test_set_lands_every_time_under_repeatedly_opening_readers(tmp_path, monkeypatch):
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg_dir))
    target = reg_dir / "registry.local.toml"
    target.write_text("schema = 1\n", encoding="utf-8")
    mod = _load_template()

    stop = tmp_path / "stop.flag"
    readers = []
    for code, first in ((_TEMPLATE_READER, str(_IMPL)), (_SHARED_READER, str(_REPO))):
        ready = tmp_path / f"ready{len(readers)}.flag"
        readers.append((_start_reader(code, first, target, stop, ready), ready))

    try:
        t0 = time.monotonic()
        while not all(r.exists() for _, r in readers):
            if time.monotonic() - t0 > READY_WAIT_S:
                pytest.fail("readers never completed a first read")
            time.sleep(0.01)

        rcs = []
        for i in range(SET_COUNT):
            args = argparse.Namespace(
                key=KEY, value=f"v{i}", dry_run=False, write_global=False, concern=None
            )
            rcs.append(mod.cmd_set(args))
    finally:
        stop.write_text("stop", encoding="utf-8")

    results = []
    for proc, _ in readers:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode == 0, err
        results.append(json.loads(out.strip().splitlines()[-1]))

    assert rcs == [0] * SET_COUNT
    for res in results:
        assert res["reads"] > 0
        assert res["failures"] == 0
    assert mod._load_toml(str(target))[KEY] == f"v{SET_COUNT - 1}"
    assert not list(reg_dir.glob("*.tmp.*"))
