"""Tests for `coordinator_core.env_locality`.

Every test here is host-independent by construction: the environment is
injected, never read ambiently, and the platform flags are monkeypatched.
A test that only passes on the box that wrote it would defeat the purpose of a
primitive whose whole job is to tell hosts apart.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from coordinator_core import env_locality as EL


# ------------------------------------------------------------ rung 0
def test_documented_contract_is_the_first_rung():
    """`CLAUDE_CODE_REMOTE` is the documented variable with an explicit
    negative guarantee; it must answer before any undocumented marker."""
    hit = EL.harness_rung({"CLAUDE_CODE_REMOTE": "true"})
    assert hit.call == "cloud"
    assert hit.confidence == "certain"
    assert hit.basis == "CLAUDE_CODE_REMOTE=true"


def test_documented_beats_undocumented_when_both_present():
    hit = EL.harness_rung({
        "CLAUDE_CODE_REMOTE": "true",
        "CLAUDE_CODE_CONTAINER_ID": "container_x--claude_code_remote--y",
    })
    assert hit.basis == "CLAUDE_CODE_REMOTE=true", (
        "an undocumented corroborator must never displace the documented "
        "contract as the stated basis"
    )


def test_undocumented_markers_are_high_not_certain():
    """They may raise confidence toward cloud; they may not claim certainty."""
    for env in ({"CLAUDE_CODE_CONTAINER_ID": "c--claude_code_remote--z"},
                {"CLAUDE_CODE_ENTRYPOINT": "remote_mobile"}):
        hit = EL.harness_rung(env)
        assert hit.call == "cloud"
        assert hit.confidence == "high"


def test_claudecode_without_remote_marker_reads_attended():
    hit = EL.harness_rung({"CLAUDECODE": "1"})
    assert hit.call == "attended"


def test_harness_rung_is_silent_when_harness_says_nothing():
    assert EL.harness_rung({}) is None
    assert EL.harness_rung({"PATH": "/usr/bin"}) is None


def test_remote_false_is_not_remote():
    """The documented guarantee is about the literal string `true`."""
    assert EL.harness_rung({"CLAUDE_CODE_REMOTE": "false"}) is None


def test_env_is_a_parameter_never_an_ambient_read(monkeypatch):
    """Engine-side, the warm server's own `os.environ` belongs to its spawner.
    An explicitly-passed env must be the ONLY thing consulted."""
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert EL.harness_rung({}) is None, (
        "passing an explicit env must not fall back to os.environ"
    )


# ------------------------------------------------------------ silicon
@pytest.mark.parametrize("brand,expected", [
    ("Intel(R) Core(TM) Ultra 9 285K", "consumer"),
    ("AMD Ryzen 9 7950X 16-Core Processor", "consumer"),
    ("Apple M3 Pro", "consumer"),
    ("Intel(R) Xeon(R) Processor @ 2.80GHz", "server-masked"),
    ("Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz", "server-real"),
    ("Intel(R) Xeon(R) Platinum 8175M CPU @ 2.50GHz", "server-real"),
    ("AMD EPYC 7763 64-Core Processor", "server-real"),
    ("QEMU Virtual CPU version 2.5+", "explicit-vm"),
    ("", "unknown"),
])
def test_silicon_classification(brand, expected):
    assert EL._silicon_class(brand) == expected


def test_masked_server_is_what_separates_cloud_from_a_rack():
    """A SKU designator is the difference between real server silicon (which a
    homelab may also have) and a hypervisor's generic model (which is the
    cloud tell)."""
    assert EL._silicon_class("Intel(R) Xeon(R) Processor @ 2.80GHz") == "server-masked"
    assert EL._silicon_class("Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz") == "server-real"


def test_cpu_brand_read_is_bounded(tmp_path):
    """The reader must not depend on the whole file: /proc/cpuinfo repeats a
    block per core, and both fields live in the first block."""
    f = tmp_path / "cpuinfo"
    f.write_text("processor\t: 0\nmodel name\t: Intel(R) Core(TM) Ultra 9 285K\n"
                 "microcode\t: 0x114\n" + ("filler\t: x\n" * 5000))
    brand, micro = EL._cpu_brand_linux(str(f))
    assert brand == "Intel(R) Core(TM) Ultra 9 285K"
    assert micro == "0x114"


def test_cpu_brand_read_survives_a_missing_file():
    assert EL._cpu_brand_linux("/nonexistent/cpuinfo") == ("", "")


# ------------------------------------------------------------ rung 1
def _force_linux(monkeypatch):
    monkeypatch.setattr(EL, "IS_WINDOWS", False)
    monkeypatch.setattr(EL, "IS_DARWIN", False)
    monkeypatch.setattr(EL, "IS_LINUX", True)


class _Uname:
    def __init__(self, release):
        self.release = release
        self.machine = "x86_64"
        self.nodename = "host"


def test_wsl_is_attended_and_says_windows(monkeypatch):
    """The highest-stakes case: WSL presents as Linux but needs the WINDOWS
    guards, so the basis must name the Windows host, not merely 'not cloud'."""
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL.os, "uname",
                        lambda: _Uname("6.6.114.1-microsoft-standard-WSL2"))
    got = EL._machine_rung_uncached({})
    assert got.call == "attended"
    assert got.confidence == "certain"
    assert "Windows host" in got.basis


def test_consumer_silicon_rescues_a_nested_vm_on_a_laptop(monkeypatch):
    """A Docker-Desktop-class VM is a headless VM by shape, but the host CPU
    passes through -- consumer silicon means somebody's machine is underneath."""
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("6.1.0-linuxkit"))
    monkeypatch.setattr(EL, "_cpu_brand_linux",
                        lambda *a: ("AMD Ryzen 9 7950X 16-Core Processor", "0xa601206"))
    got = EL._machine_rung_uncached({})
    assert got.call == "attended"
    assert got.confidence == "high"


def test_masked_silicon_with_synthetic_microcode_is_cloud(monkeypatch):
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("6.18.44-fc-v24"))
    monkeypatch.setattr(EL, "_cpu_brand_linux",
                        lambda *a: ("Intel(R) Xeon(R) Processor @ 2.80GHz", "0x1"))
    got = EL._machine_rung_uncached({})
    assert got.call == "cloud"
    assert got.confidence == "high"


def test_generic_hypervisor_brand_stays_suspect(monkeypatch):
    """The one irreducible band. Rounding this to either answer is the defect
    the third state exists to prevent."""
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("5.15.0-generic"))
    monkeypatch.setattr(EL, "_cpu_brand_linux",
                        lambda *a: ("QEMU Virtual CPU version 2.5+", "0x1"))
    monkeypatch.setattr(EL.os.path, "exists", lambda p: False)
    got = EL._machine_rung_uncached({})
    assert got.call == "suspect"
    assert got.confidence == "low"


def test_windows_falls_back_to_env_when_registry_unavailable(monkeypatch):
    monkeypatch.setattr(EL, "IS_WINDOWS", True)
    monkeypatch.setattr(EL, "IS_DARWIN", False)
    monkeypatch.setattr(EL, "IS_LINUX", False)
    got = EL._machine_rung_uncached({"PROCESSOR_IDENTIFIER": "Intel64 Family 6"})
    assert got.call == "attended"


def test_darwin_is_settled_without_touching_the_disk(monkeypatch):
    monkeypatch.setattr(EL, "IS_WINDOWS", False)
    monkeypatch.setattr(EL, "IS_DARWIN", True)
    monkeypatch.setattr(EL, "IS_LINUX", False)

    def _explode(*a, **k):
        raise AssertionError("darwin must not read the filesystem")

    monkeypatch.setattr(EL.os, "open", _explode)
    assert EL._machine_rung_uncached({}).call == "attended"


# ------------------------------------------------------------ contract
def test_calls_and_confidences_come_from_the_closed_vocabularies():
    got = EL.locality()
    assert got.call in EL.CALLS
    assert got.confidence in EL.CONFIDENCES


def test_harness_rung_wins_over_machine_rung(monkeypatch):
    """Rung 0 is a contract, rung 1 an inference. Where they disagree, the
    contract wins."""
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL, "_cpu_brand_linux",
                        lambda *a: ("Intel(R) Core(TM) Ultra 9 285K", "0x114"))
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("6.1.0"))
    EL._MACHINE_CACHE.clear()
    got = EL.locality({"CLAUDE_CODE_REMOTE": "true"})
    assert got.call == "cloud" and got.rung == "harness"


def test_cross_check_surfaces_disagreement(monkeypatch):
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL, "_cpu_brand_linux",
                        lambda *a: ("Intel(R) Core(TM) Ultra 9 285K", "0x114"))
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("6.1.0"))
    out = EL.cross_check({"CLAUDE_CODE_REMOTE": "true"})
    assert out["agree"] is False, "harness says cloud, silicon says attended"
    assert out["effective"].rung == "harness"


def test_cross_check_agree_is_none_when_harness_is_silent(monkeypatch):
    _force_linux(monkeypatch)
    monkeypatch.setattr(EL.os, "uname", lambda: _Uname("6.1.0"))
    out = EL.cross_check({})
    assert out["agree"] is None


def test_machine_rung_is_memoized(monkeypatch):
    EL._MACHINE_CACHE.clear()
    EL.machine_rung({})
    calls = []
    monkeypatch.setattr(EL, "_machine_rung_uncached",
                        lambda env: calls.append(1) or EL.Locality("x", "low", "m", "b"))
    EL.machine_rung({})
    assert calls == [], "rung 1 is machine-constant and must not recompute"
    EL._MACHINE_CACHE.clear()


def test_module_spawns_nothing():
    """The primitive exists because the shell-out alternative costs a spawn and
    a 127.8 ms tail. Checked over the parsed AST, not the source text: the
    module docstring names `platform.system()` and `systemd-detect-virt` in
    order to explain why it does NOT use them, and a grep cannot tell an
    explanation apart from a call site."""
    import ast

    tree = ast.parse(pathlib.Path(EL.__file__).read_text(encoding="utf-8"))
    banned_modules = {"subprocess", "platform", "commands", "pty"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & banned_modules), (
        f"env_locality must not import {sorted(imported & banned_modules)}"
    )

    banned_calls = {"system", "popen", "spawnv", "execv", "check_output", "run"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in banned_calls, (
                f"env_locality must not call {node.func.attr!r} "
                f"(line {node.lineno})"
            )


def test_winreg_key_path_has_single_separators():
    """A raw-string registry path resolves to DOUBLED separators and is not a
    valid key. It would raise on Windows, be swallowed by the fallback's broad
    except, and present as the env-var path working normally -- a silent
    fallback masking the bug it was written to cover. Not reproducible from a
    non-Windows host, so it is pinned here instead."""
    src = pathlib.Path(EL.__file__).read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if "CentralProcessor" in ln)
    literal = line[line.index('"'):line.rindex('"') + 1]
    assert eval(literal) == "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"
