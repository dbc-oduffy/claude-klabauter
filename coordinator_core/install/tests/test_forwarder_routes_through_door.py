"""Tests for the C5 cutover: a door-eligible name's on-disk forwarder body
matches the door emission shape, and a named invocation through it records
`route: warm_server` -- read back from telemetry, never inferred from an
exit code (spec backlink:
docs/dispatch-briefs/2026-08-26-every-forwarder-that-can-reach-the-door-does/C5.md).

VERIFY ON DISK, NOT BY CENSUS: `coordinator_core.install.substrate ::
_door_eligible_forwarder_names` is generator-derived from the SAME committed
allowlist regardless of whether an install ever ran -- it cannot, by itself,
prove the cutover reached disk. These tests exercise the actual writers
(`door_install.install_named_forwarder`, `substrate._write_native_door_
forwarder`, `substrate._write_agent_helper_forwarders`) against a scratch
`bin_dst` and assert on the files they actually produced.

Negative-spec: does not exercise a live warm server -- `read_door_route`'s
own subprocess call is faked exactly as `test_door_route_signal.py` fakes
it, so this suite proves the SAME telemetry-read discriminator the brief
names, not an end-to-end warm-hit measurement (that is C6's job).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.install import door_install, door_route_signal, substrate


def _stamp_engine_root(root: Path) -> None:
    stamp_dir = root / "coordinator_core"
    stamp_dir.mkdir(parents=True, exist_ok=True)
    (stamp_dir / "_engine_stamp").write_text("sha:deadbeef\n", encoding="utf-8")


def _skip_if_no_prebuilt() -> None:
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")


# --- Census artifact sanity ---------------------------------------------


def test_door_eligible_forwarder_names_reads_the_committed_allowlist():
    """The generator-side loader must actually read C2's committed census
    output, not a hardcoded/empty stand-in -- a non-empty result here is
    what makes every other test in this module exercise a real bucket."""
    names = substrate._door_eligible_forwarder_names()
    assert isinstance(names, frozenset)
    assert names, "warm_entrypoint_allowlist.json's door-eligible bucket read back empty"
    assert "cross-repo-memo" in names


def test_door_eligible_forwarder_names_degrades_to_empty_on_missing_allowlist(monkeypatch, tmp_path):
    monkeypatch.setattr(
        substrate, "_DOOR_ELIGIBLE_ALLOWLIST_PATH", tmp_path / "does-not-exist.json"
    )
    assert substrate._door_eligible_forwarder_names() == frozenset()


# --- On-disk emission shape (door_install layer) -------------------------


def test_install_named_forwarder_body_matches_the_door_emission_shape(tmp_path):
    _skip_if_no_prebuilt()
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    dest = door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    assert dest == door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    door_dest = bin_dst / door_install.DOOR_INSTALLED_NAME
    assert door_dest.exists()
    # Byte-identical to the installed door -- a hardlink or a copy, either
    # way the SAME emission shape, never a distinct/lesser body.
    assert dest.read_bytes() == door_dest.read_bytes()


def test_named_forwarder_path_is_exe_direct_on_windows_bare_on_posix(tmp_path):
    path = door_install.named_forwarder_path(tmp_path, "cross-repo-memo")
    if sys.platform == "win32":
        assert path.name == "cross-repo-memo.exe"
    else:
        assert path.name == "cross-repo-memo"


def test_install_named_forwarder_check_only_never_writes(tmp_path):
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    with pytest.raises(door_install.DoorInstallError):
        door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo", check_only=True)

    assert not door_install.named_forwarder_path(bin_dst, "cross-repo-memo").exists()


def test_remove_shadowing_ps1_sibling_removes_the_named_ps1(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    ps1 = bin_dst / "cross-repo-memo.ps1"
    ps1.write_text("# stand-in .ps1 forwarder\n", encoding="utf-8")

    removed = door_install.remove_shadowing_ps1_sibling(bin_dst, "cross-repo-memo")

    assert removed == ps1
    assert not ps1.exists()


def test_remove_shadowing_ps1_sibling_is_a_noop_when_absent(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    assert door_install.remove_shadowing_ps1_sibling(bin_dst, "cross-repo-memo") is None


# --- Generator-level cutover (substrate layer) ---------------------------


def test_write_agent_helper_forwarders_replaces_the_python_pair_for_eligible_names(tmp_path):
    """A door-eligible name gets its native forwarder INSTEAD OF the
    Python pair, not alongside it (PM ruling 2026-08-27). The superseded
    `.cmd` body is wrong-if-reached post-cutover, and was previously kept
    harmless only by PATHEXT ranking plus install ordering -- neither an
    invariant. See `door_install.remove_superseded_python_forwarders`."""
    _skip_if_no_prebuilt()
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    target_map = {"cross-repo-memo": "cross-repo-memo"}
    cmd_map = {"cross-repo-memo": "cross-repo-memo.cmd"}
    bin_dst.mkdir(parents=True, exist_ok=True)

    substrate._write_agent_helper_forwarders(
        target_map, cmd_map, bin_dst, False,
        python3_cmd_resolved_bin="",
        door_eligible_names=frozenset({"cross-repo-memo"}),
        engine_root=engine_root,
    )

    py_dst = bin_dst / "cross-repo-memo"
    native_dst = door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    cmd_dst = bin_dst / "cross-repo-memo.cmd"
    assert not cmd_dst.exists()
    if sys.platform == "win32":
        # Distinct filenames on Windows, so the superseded Python pair is
        # genuinely removable -- and is removed, rather than left as
        # unreachable dead weight behind PATHEXT.
        assert not py_dst.exists()
        assert native_dst.exists()
        assert native_dst != py_dst
    else:
        # Same path on POSIX -- the native image intentionally overwrites
        # the Python bare-name forwarder in place.
        assert native_dst == py_dst
        assert native_dst.exists()

    door_dst = bin_dst / door_install.DOOR_INSTALLED_NAME
    assert native_dst.read_bytes() == door_dst.read_bytes()


def test_doorless_root_keeps_the_python_pair_for_an_eligible_name(tmp_path):
    """The kill removes a superseded artifact, never the only route to the
    engine. An UNSTAMPED engine root cannot supply a door, so a door-
    eligible name falls through to the ordinary Python pair -- correct,
    merely uncut-over."""
    engine_root = tmp_path / "engine"
    engine_root.mkdir(parents=True, exist_ok=True)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir(parents=True, exist_ok=True)

    substrate._write_agent_helper_forwarders(
        {"cross-repo-memo": "cross-repo-memo"},
        {"cross-repo-memo": "cross-repo-memo.cmd"},
        bin_dst, False,
        python3_cmd_resolved_bin="",
        door_eligible_names=frozenset({"cross-repo-memo"}),
        engine_root=engine_root,
    )

    py_dst = bin_dst / "cross-repo-memo"
    assert py_dst.exists()
    assert substrate._AGENT_FORWARDER_MARKER in py_dst.read_text(encoding="utf-8")
    assert (bin_dst / "cross-repo-memo.cmd").exists()
    assert not door_install.named_forwarder_path(bin_dst, "cross-repo-memo").exists() or (
        door_install.named_forwarder_path(bin_dst, "cross-repo-memo") == py_dst
    )


def test_remove_superseded_python_forwarders_never_removes_the_posix_native_image(tmp_path):
    """On POSIX the native image occupies the BARE NAME itself, so removing
    the bare name would uninstall the forwarder the removal follows. Only a
    stray `.cmd` is removable there; on Windows both go."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    bare = bin_dst / "cross-repo-memo"
    bare.write_text("native-image-stand-in", encoding="utf-8")
    cmd = bin_dst / "cross-repo-memo.cmd"
    cmd.write_text("@echo off" + chr(10), encoding="utf-8")

    removed = door_install.remove_superseded_python_forwarders(bin_dst, "cross-repo-memo")

    assert cmd in removed
    assert not cmd.exists()
    if sys.platform == "win32":
        assert bare in removed
        assert not bare.exists()
    else:
        assert bare not in removed
        assert bare.exists()


def test_remove_superseded_python_forwarders_is_a_noop_when_absent(tmp_path):
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    assert door_install.remove_superseded_python_forwarders(bin_dst, "cross-repo-memo") == []


def test_write_agent_helper_forwarders_persists_the_native_forwarder_manifest(tmp_path):
    _skip_if_no_prebuilt()
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"

    target_map = {"cross-repo-memo": "cross-repo-memo"}
    cmd_map = {"cross-repo-memo": "cross-repo-memo.cmd"}
    bin_dst.mkdir(parents=True, exist_ok=True)

    substrate._write_agent_helper_forwarders(
        target_map, cmd_map, bin_dst, False,
        python3_cmd_resolved_bin="",
        door_eligible_names=frozenset({"cross-repo-memo"}),
        engine_root=engine_root,
    )

    manifest = substrate._read_native_forwarder_manifest(bin_dst)
    assert "cross-repo-memo" in manifest


def test_write_agent_helper_forwarders_without_engine_root_is_unaffected(tmp_path):
    """The self-heal caller (`forwarder_self_heal.py`) passes neither
    `door_eligible_names` nor `engine_root` -- must stay byte-for-byte the
    pre-C5 behaviour: no native forwarder, no manifest write."""
    bin_dst = tmp_path / "bin"
    target_map = {"cross-repo-memo": "cross-repo-memo"}
    cmd_map = {"cross-repo-memo": "cross-repo-memo.cmd"}
    bin_dst.mkdir(parents=True, exist_ok=True)

    substrate._write_agent_helper_forwarders(
        target_map, cmd_map, bin_dst, False,
        python3_cmd_resolved_bin="",
    )

    assert not (bin_dst / substrate._NATIVE_FORWARDER_MANIFEST_NAME).exists()
    native_dst = door_install.named_forwarder_path(bin_dst, "cross-repo-memo")
    if sys.platform == "win32":
        assert not native_dst.exists()


def test_emit_and_verify_ps1_forwarders_skips_excluded_names(monkeypatch, tmp_path):
    """Asserts at the call level, not end-state file presence: the policy
    gate itself rolls every emitted `.ps1` back under this test suite's
    machine-mutation-disabled environment (see `_refuse_machine_mutation`),
    which would make an end-state assertion prove nothing either way. What
    matters here is that an excluded name's writer is never CALLED at all."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    cmd_map = {"cross-repo-memo": "cross-repo-memo.cmd", "stub-file-gate": "stub-file-gate.cmd"}

    called_names: "list[str]" = []
    real_writer = substrate._write_agent_ps1_forwarder

    def spy(name, dst, check_only, **kwargs):
        called_names.append(name)
        return real_writer(name, dst, check_only, **kwargs)

    monkeypatch.setattr(substrate, "_write_agent_ps1_forwarder", spy)

    substrate._emit_and_verify_ps1_forwarders(
        bin_dst, cmd_map, False,
        python3_cmd_resolved_bin="",
        exclude_names=frozenset({"cross-repo-memo"}),
    )

    assert "cross-repo-memo" not in called_names
    assert "stub-file-gate" in called_names


# --- Route verification: telemetry, never an exit code -------------------


def _git_common_dir(tmp_path: Path) -> Path:
    common = tmp_path / ".git"
    common.mkdir()
    return common


def _sink_path(common_dir: Path) -> Path:
    return common_dir / "coordinator-sessions" / "logs" / "op-latency.jsonl"


def _write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_named_invocation_records_route_warm_server_not_exit_code(monkeypatch, tmp_path):
    """The brief's own verification instruction: 'assert ... that one named
    invocation records route: warm_server. Never an exit code.' Mirrors
    `test_door_route_signal.py::test_read_door_route_classifies_warm_server_row`
    exactly -- the door subprocess itself is faked (a real warm-server run
    is C6's job), but the discriminator under test -- reading `route` back
    from the sink rather than trusting `returncode` -- is the real one."""
    _skip_if_no_prebuilt()
    engine_root = tmp_path / "engine"
    _stamp_engine_root(engine_root)
    bin_dst = tmp_path / "bin"
    named = door_install.install_named_forwarder(bin_dst, engine_root, "cross-repo-memo")

    common_dir = _git_common_dir(tmp_path)
    monkeypatch.setattr(
        "coordinator_core.lifecycle.git_common_dir", lambda repo_root: common_dir
    )
    sink = _sink_path(common_dir)

    def fake_run(argv, **kwargs):
        assert Path(argv[0]) == named
        now = time.time()
        _write_jsonl(
            sink,
            [{"op": "cross-repo-memo", "t_start": now, "kind": "complete", "route": "warm_server"}],
        )

        class _Result:
            returncode = 1  # deliberately non-zero: exit code must not be consulted

        return _Result()

    monkeypatch.setattr(door_route_signal.subprocess, "run", fake_run)

    result = door_route_signal.read_door_route(named, "cross-repo-memo", repo_root=tmp_path)

    assert result.route == door_route_signal.WARM_SERVER
    assert result.entry is not None
