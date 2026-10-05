"""update_meta_field(s) serialise through locked_rmw: concurrent writers of
different fields both survive."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from coordinator_core.session import core
from coordinator_core.win_portability import no_console_passthrough_kwargs

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _session_dir(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_passthrough_kwargs())
    sdir = tmp_path / ".git" / "coordinator-sessions" / "sid-rmw"
    sdir.mkdir(parents=True)
    (sdir / "meta.json").write_text(json.dumps({"session_id": "sid-rmw"}))
    return sdir


def test_two_writers_of_different_fields_both_survive(tmp_path):
    sdir = _session_dir(tmp_path)
    for i in range(50):
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def _writer(call, *args):
            try:
                barrier.wait(timeout=5)
                assert call(str(sdir), *args) is True
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=_writer, args=(core.update_meta_field, "a", str(i))),
            threading.Thread(
                target=_writer, args=(core.update_meta_fields, {"b": str(i), "c": str(i)})
            ),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        meta = json.loads((sdir / "meta.json").read_text())
        assert (meta["a"], meta["b"], meta["c"]) == (str(i), str(i), str(i))
        assert meta["session_id"] == "sid-rmw"


def test_missing_and_non_object_meta_are_noops(tmp_path):
    sdir = _session_dir(tmp_path)
    (sdir / "meta.json").write_text("[1]")
    assert core.update_meta_field(str(sdir), "a", "1") is False
    (sdir / "meta.json").unlink()
    assert core.update_meta_fields(str(sdir), {"a": "1"}) is False
