"""C5: an already-current forwarder name is skipped, and `install_door`
runs once per install loop, never once per name.

Spec: docs/plans/2026-09-23-klabauter-installer-performance.md (P175-C5)

`door_install.install_door` is faked throughout via a small counting stand-in
that writes fixed bytes to `DOOR_INSTALLED_NAME` -- these tests exercise
`install_named_forwarder`'s own currency check (part a) and
`substrate._write_agent_helper_forwarders`'s hoist (part b), not
`install_door`'s own POSIX-build/prebuilt-copy machinery, which is covered
elsewhere (`test_door_install.py`, `test_door_install_relinks_stranded_
names.py`).
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.install import door_install, substrate


def _stamp_engine_root(root: Path, *entrypoints: str) -> None:
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")
    bin_dir = root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in entrypoints:
        (bin_dir / f"{name}.py").write_text(
            "def main(argv):\n    return 0\n", encoding="utf-8"
        )


def _counting_fake_install_door(payload: bytes, calls: list):
    """Stand-in for `door_install.install_door`: writes `payload` in place at
    `DOOR_INSTALLED_NAME` (an in-place rewrite, same inode on a second call --
    exactly what a real current-and-unchanged install does) and records one
    entry per call."""

    def _install_door(bin_dst, engine_root, *, check_only=False):
        calls.append(1)
        bin_dst = Path(bin_dst)
        bin_dst.mkdir(parents=True, exist_ok=True)
        dest = bin_dst / door_install.DOOR_INSTALLED_NAME
        dest.write_bytes(payload)
        return dest

    return _install_door


# --- Part (a): an already-current name is never unlinked/relinked --------


def test_hardlinked_name_already_current_is_not_unlinked_or_relinked(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo")
    bin_dst = tmp_path / "bin"

    calls: list = []
    monkeypatch.setattr(door_install, "install_door", _counting_fake_install_door(b"door-v1", calls))

    door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")
    assert len(calls) == 1

    def _boom_unlink(self, *a, **kw):
        raise AssertionError("unlink must not be called on an already-current name")

    def _boom_link(*a, **kw):
        raise AssertionError("os.link must not be called on an already-current name")

    monkeypatch.setattr(Path, "unlink", _boom_unlink)
    monkeypatch.setattr(os, "link", _boom_link)

    dest = door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    assert dest == door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    assert len(calls) == 2  # install_door is still called per-call when no `source=` is threaded


def test_copy_fallback_name_with_equal_bytes_is_skipped(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo")
    bin_dst = tmp_path / "bin"

    calls: list = []
    monkeypatch.setattr(door_install, "install_door", _counting_fake_install_door(b"door-v1", calls))
    monkeypatch.setattr(os, "link", lambda *a, **kw: (_ for _ in ()).throw(OSError("no hardlinks")))

    door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    def _boom_unlink(self, *a, **kw):
        raise AssertionError(
            "unlink must not be called on an already-current copy-fallback name"
        )

    monkeypatch.setattr(Path, "unlink", _boom_unlink)

    dest = door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    door_dst = bin_dst / door_install.DOOR_INSTALLED_NAME
    assert dest.read_bytes() == door_dst.read_bytes() == b"door-v1"


def test_copy_fallback_name_with_stale_bytes_is_replaced(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo")
    bin_dst = tmp_path / "bin"

    calls: list = []
    monkeypatch.setattr(door_install, "install_door", _counting_fake_install_door(b"door-v1", calls))
    monkeypatch.setattr(os, "link", lambda *a, **kw: (_ for _ in ()).throw(OSError("no hardlinks")))

    door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")
    dest = door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    dest.write_bytes(b"a-stale-different-image")

    # A fresh door image lands under the same fixed payload this time -- the
    # slot must be replaced to match it, not left carrying the stale bytes.
    dest = door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    door_dst = bin_dst / door_install.DOOR_INSTALLED_NAME
    assert dest.read_bytes() == door_dst.read_bytes() == b"door-v1"


# --- Part (b): install_door runs once per loop, not once per name --------


def test_install_door_runs_once_per_loop_not_once_per_name(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo", "coordinator-doc-new")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)

    calls: list = []
    monkeypatch.setattr(door_install, "install_door", _counting_fake_install_door(b"door-v1", calls))

    substrate._write_agent_helper_forwarders(
        {"cross-repo-memo": "cross-repo-memo", "coordinator-doc-new": "coordinator-doc-new"},
        bin_dst, False,
        engine_root=engine_root,
    )

    assert len(calls) == 1


def test_manifest_lists_skipped_names_on_a_second_current_run(tmp_path, monkeypatch):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root, "cross-repo-memo", "coordinator-doc-new")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)
    target_map = {
        "cross-repo-memo": "cross-repo-memo",
        "coordinator-doc-new": "coordinator-doc-new",
    }

    calls: list = []
    monkeypatch.setattr(door_install, "install_door", _counting_fake_install_door(b"door-v1", calls))

    substrate._write_agent_helper_forwarders(target_map, bin_dst, False, engine_root=engine_root)
    # Second run: both names are already current -- must still be listed in
    # the manifest (the whole point of the per-name check staying per-name),
    # even though neither is re-linked.
    substrate._write_agent_helper_forwarders(target_map, bin_dst, False, engine_root=engine_root)

    manifest = substrate._read_native_forwarder_manifest(bin_dst)
    assert "cross-repo-memo" in manifest
    assert "coordinator-doc-new" in manifest
    assert len(calls) == 2  # one install_door per loop, across the two runs
