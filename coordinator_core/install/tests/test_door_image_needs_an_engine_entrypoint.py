"""A door image whose name the engine cannot resolve is REPORTED, not skipped.

Bug backlog: state/bug-backlog/2026-08-29-a-door-image-is-installed-for-names-the-engine-cannot-resolve.yaml

THE DIVERGENCE. The name a door image is installed under comes from the
GENERATOR's `coordinator/bin/`. The image dials the PUBLISHED engine, and
`ops/invoke_from_argv.py :: _resolve_entrypoint_script` resolves
`<engine_root>/coordinator/bin/<name>.py` there. Those namespaces differ two
ways -- a publisher-side CLI excluded from the product it publishes (`publish`,
`percolate-push`, `percolate-round`, `coordinator-publish`), and a
repo-identifying name rewritten by `percolate-store.yaml`'s `substitute`
section (`check-claude-klabauter-doctor-sentinel` lands as
`check-claude-klabauter-doctor-sentinel`). Measured 2026-08-29: 14 names.

Such a name fails BOTH ways -- warm raises a plain `ValueError` (-32603),
which the door rightly declines to read as proof of non-dispatch, so it emits
-32004 and fails rather than degrading; cold, `door.c :: fall_through` spawns
the same absent path.

AND THE INSTALLER MUST STILL WRITE THE IMAGE. This is the correction the first
revision of this fix earned the hard way. Under ONE ENTRYPOINT PER PLATFORM
(PM ruling 2026-08-29, see `substrate._write_agent_helper_forwarders`) the door
image is the ONLY launcher a name gets: no `.cmd` is written for any name and
`_write_agent_cmd_forwarder` is deleted. Skipping the image therefore does not
leave the name on a slower leg -- it leaves the name with no launcher at all.
An install run carrying the skip removed 14 names from a live box outright,
`publish` and `percolate-push` among them. A broken launcher is bad; an absent
one is worse, and choosing between them is not the installer's call. The
detector stays and warns; the repair (the engine carries a script for every
name the door is installed under, or those names leave the map) is upstream and
trades against that ruling.

NO SUITE-LEVEL ASSERTION OVER THE LIVE ROSTER AND THE LIVE ENGINE. The obvious
test is a trap: asked from a checkout, `warm.engine_root.current_engine_clone()`
answers with the CHECKOUT, where every name's script exists by construction, so
the assertion passes vacuously and certifies the state it exists to catch. The
honest oracle is the `door.engine-root.txt` sidecar beside the installed images,
and `coordinator_core/conftest.py` points nearly every test's
`COORDINATOR_SETTINGS_HOME` at a registry-less `tmp_path`, so a suite run cannot
read it (measured: it skipped, it did not pass). A permanently-skipped test
reads as coverage while providing none.
"""

from __future__ import annotations

import pytest

from coordinator_core.install import door_install, substrate


def _skip_if_no_prebuilt() -> None:
    if not door_install._PREBUILT_DOOR_EXE.exists():
        pytest.skip("no committed prebuilt door for this platform in this checkout")


def _engine_root(tmp_path, *, names=(), stamped=True):
    root = tmp_path / "engine"
    (root / "coordinator" / "bin").mkdir(parents=True)
    if stamped:
        (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
        (root / "coordinator_core" / "_engine_stamp").write_text("stamp\n", encoding="utf-8")
    for name in names:
        (root / "coordinator" / "bin" / f"{name}.py").write_text(
            "def main(argv):\n    return 0\n", encoding="utf-8"
        )
    return root


def test_engine_carries_entrypoint_script_is_keyed_on_the_name_the_door_sends(tmp_path):
    """`<name>.py`, not the on-disk target the generator resolved and not a
    close relative of the name. A rename at publish time leaves a script that
    IS published under a name the door will never ask for, and treating that
    as coverage is the exact miss that shipped."""
    root = _engine_root(tmp_path, names=["check-claude-klabauter-doctor-sentinel"])

    assert door_install.engine_carries_entrypoint_script(
        root, "check-claude-klabauter-doctor-sentinel"
    )
    assert not door_install.engine_carries_entrypoint_script(
        root, "check-claude-klabauter-doctor-sentinel"
    )


def test_a_name_absent_from_the_engine_is_reported_but_still_installed(tmp_path, capsys):
    """The image is STILL WRITTEN, and the warning is the deliverable.

    The regression pin for the first revision of this fix, which skipped the
    write and stripped 14 names off a live box. There is no Python pair left
    to fall back to, so the skip did not degrade the name -- it deleted it.
    """
    _skip_if_no_prebuilt()
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    result = substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert result is not None and result.exists()
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "no coordinator/bin/publish.py" in err


def test_an_existing_image_is_never_taken_away(tmp_path):
    """An install run must not leave a name launcher-less, including a name
    an earlier run already cut over."""
    _skip_if_no_prebuilt()
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    existing = door_install.named_forwarder_path(bin_dst, "publish")
    existing.write_bytes(b"MZ-an-earlier-image")

    substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert existing.exists()


def test_a_name_the_engine_carries_installs_without_a_warning(tmp_path, capsys):
    """Negative-spec companion: the warning must name only the broken names.
    A detector that fires on every name is noise an operator learns to skip,
    which is how the 14 stayed unnoticed in the first place."""
    _skip_if_no_prebuilt()
    root = _engine_root(tmp_path, names=["coordinator-invoke", "handoff-housekeeping"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    dest = substrate._write_native_door_forwarder(
        "handoff-housekeeping", bin_dst, check_only=False, engine_root=root
    )

    assert dest is not None and dest.exists()
    assert "WARNING" not in capsys.readouterr().err


def test_the_canonical_door_is_never_removed_as_stale(tmp_path):
    """`remove_stale_named_forwarder` is retained for the repair path, and its
    one hard invariant is that it never takes the canonical door: for
    `coordinator-invoke`, `named_forwarder_path` resolves to the path
    `install_door` writes, and removing it strips every session on the box of
    its engine entrypoint."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    canonical = bin_dst / door_install.DOOR_INSTALLED_NAME
    canonical.write_bytes(b"MZ-the-real-door")

    removed = door_install.remove_stale_named_forwarder(bin_dst, canonical.stem)

    assert removed is None
    assert canonical.exists()
