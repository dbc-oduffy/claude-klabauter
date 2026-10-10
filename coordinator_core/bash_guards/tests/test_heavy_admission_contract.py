"""Pins the heavy-admission contract names and its stdlib-only import closure."""

import ast
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from coordinator_core.bash_guards import _heavy_admission_contract as c


def test_guard_name_and_legs():
    assert c.GUARD_NAME == "guard-heavy-command-admission"
    assert c.LEGS == ("identity", "ram-floor", "session-cap")


def test_override_keys_one_per_leg():
    assert c.OVERRIDE_KEYS == {
        "identity": "COORDINATOR_ALLOW_HEAVY_IDENTITY",
        "ram-floor": "COORDINATOR_ALLOW_HEAVY_RAM_FLOOR",
        "session-cap": "COORDINATOR_ALLOW_HEAVY_SESSION_CAP",
    }


def test_machine_local_keys():
    assert c.MACHINE_LOCAL_KEYS == (
        "heavy_admission.free_ram_floor_mb",
        "heavy_admission.session_heavy_cap",
        "heavy_admission.session_background_cap",
        "heavy_admission.lease_reserve_mb",
        "heavy_admission.vitest_max_workers",
        "heavy_admission.worker_rss_ceiling_mb",
    )


def test_heavy_class_members():
    assert {m.value for m in c.HeavyClass} == {"typecheck", "build", "test_tier", "ue", "reindex"}


def test_allowlist_path_and_ttl():
    assert c.ALLOWLIST_PATH.name == "heavy_command_allowlist.txt"
    assert c.ALLOWLIST_PATH.parent == Path(c.__file__).resolve().parent
    assert isinstance(c.LEASE_ATTRIBUTION_TTL_S, int) and c.LEASE_ATTRIBUTION_TTL_S > 0


def test_record_fields():
    assert c.Classification.__dataclass_fields__.keys() == {"heavy_class", "scoped", "background", "noemit_tsc"}
    assert c.MemoryReading.__dataclass_fields__.keys() == {"avail_mb", "trusted", "source"}
    assert c.ProcRow.__dataclass_fields__.keys() == {"pid", "ppid", "ctime", "name"}
    assert c.LeaseRecord.__dataclass_fields__.keys() == {
        "holder_pid", "holder_ctime", "session_pid", "session_ctime", "heavy_class", "admitted_at",
        "launch_ctime",
    }
    with pytest.raises(FrozenInstanceError):
        c.ProcRow(1, 0, 1, "x").pid = 2


def test_process_primitives_protocol_members():
    class Stub:
        def alive(self, pid, ctime): return True
        def creation_time(self, pid): return 1
        def snapshot(self): return []

    assert isinstance(Stub(), c.ProcessPrimitives)
    assert not isinstance(object(), c.ProcessPrimitives)


def test_imports_stdlib_only():
    tree = ast.parse(Path(c.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            roots.add((node.module or "").split(".")[0])
    assert roots <= set(sys.stdlib_module_names)
