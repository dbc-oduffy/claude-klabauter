"""guard_disk_fill.check through its real entry point: disk_usage stubbed, paths under tmp_path."""

from __future__ import annotations

import shutil
from collections import namedtuple

import pytest

from coordinator_core.bash_guards import _disk_fill_contract as contract
from coordinator_core.bash_guards import guard_disk_fill as g

GiB = 1024**3
_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture()
def free(monkeypatch):
    """Set the volume's free bytes the guard sees; floor pinned to the 10 GiB default."""
    monkeypatch.delenv("COORDINATOR_DISK_BUDGET_VOLUME_FREE_FLOOR", raising=False)

    def _set(n):
        monkeypatch.setattr(shutil, "disk_usage", lambda p: _Usage(100 * GiB, 0, n))

    return _set


def _payload(cmd, cwd, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(cwd)}


def _text(rec):
    return rec.em_text


@pytest.fixture()
def tree(tmp_path):
    src = tmp_path / "big"
    src.mkdir()
    (src / "a.bin").write_bytes(b"x" * 4096)
    return tmp_path


@pytest.mark.parametrize(
    "cmd,tool",
    [
        ("cp -r big dst", "Bash"),
        ("Copy-Item -Recurse big dst", "PowerShell"),
        ("robocopy big dst /E", "PowerShell"),
        ("dd if=/dev/zero of=out.img bs=1M count=100", "Bash"),
        ("fsutil file createnew out.img 1048576", "PowerShell"),
        ("truncate -s 1M out.img", "Bash"),
    ],
)
def test_bulk_write_under_floor_is_denied_naming_redirect(tree, free, cmd, tool):
    free(10 * GiB)
    rec = g.check(_payload(cmd, tree, tool))
    assert rec is not None and rec.verdict_kind == "deny"
    assert rec.guard_name == "guard-disk-fill"
    assert "Alternative:" in _text(rec) and "disk_budget sweep" in _text(rec)


@pytest.mark.parametrize(
    "cmd,tool",
    [
        ("cp -r big dst", "Bash"),
        ("Copy-Item -Recurse big dst", "PowerShell"),
        ("robocopy big dst /E", "PowerShell"),
        ("dd if=/dev/zero of=out.img bs=1M count=100", "Bash"),
        ("truncate -s 1M out.img", "Bash"),
    ],
)
def test_bulk_write_with_room_is_allowed(tree, free, cmd, tool):
    free(500 * GiB)
    assert g.check(_payload(cmd, tree, tool)) is None


@pytest.mark.parametrize("cmd", ["echo x > f", "git commit -m x", "cp a b", "ls -la"])
def test_non_bulk_write_is_allowed_even_when_full(tree, free, cmd):
    free(0)
    assert g.check(_payload(cmd, tree)) is None


def test_unestimable_source_is_allowed(tree, free, monkeypatch):
    free(0)
    real = contract.estimate_output_bytes
    monkeypatch.setattr(
        contract, "estimate_output_bytes", lambda bulk, cwd=None: real(bulk, cwd, max_entries=0)
    )
    assert g.check(_payload("cp -r big dst", tree)) is None
    assert g.check(_payload("dd if=/dev/zero of=out.img", tree)) is None


def test_relative_destination_resolves_against_payload_cwd(tree, free, monkeypatch):
    seen = []
    monkeypatch.setattr(
        shutil, "disk_usage", lambda p: seen.append(p) or _Usage(100 * GiB, 0, 0)
    )
    rec = g.check(_payload("cp -r big new/deep/dst", tree))
    assert rec is not None
    assert seen == [tree]


def test_non_command_tool_and_malformed_payload_allow(tree, free):
    free(0)
    assert g.check({"tool_name": "Write", "tool_input": {"command": "cp -r big dst"}}) is None
    assert g.check({"tool_name": "Bash", "tool_input": None}) is None
    assert g.check("nope") is None


def test_internal_failure_allows(tree, free, monkeypatch):
    free(0)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(contract, "load_policy", boom)
    assert g.check(_payload("cp -r big dst", tree)) is None
