"""A door image is only written for a name the ENGINE can resolve.

Bug backlog: state/bug-backlog/2026-08-29-a-door-image-is-installed-for-names-the-engine-cannot-resolve.yaml

THE DIVERGENCE. The door-eligible roster (`coordinator_core/ops/
warm_entrypoint_allowlist.json`, regenerated from `forwarder_door_census`) is
derived from the GENERATOR's own `coordinator/bin/`. The image it authorises
dials the PUBLISHED engine, and `ops/invoke_from_argv.py ::
_resolve_entrypoint_script` resolves `<engine_root>/coordinator/bin/<name>.py`
there. Those two namespaces are not the same one, in two independent ways --
a publisher-side CLI excluded from the product it publishes (`publish`,
`percolate-push`, `percolate-round`, `coordinator-publish`), and a
repo-identifying name rewritten by `percolate-store.yaml`'s `substitute`
section (`check-claude-klabauter-doctor-sentinel` lands as
`check-claude-klabauter-doctor-sentinel`). Measured 2026-08-29: 14 of 365.

WHY A BROKEN IMAGE IS WORSE THAN NO IMAGE, which is what these tests pin.
Such a name has no working leg in either direction -- warm raises a plain
`ValueError` (-32603), which the door correctly refuses to treat as proof of
non-dispatch, so it emits -32004 and FAILS rather than degrading; cold,
`door.c :: fall_through` spawns the same absent path. And the `.exe` outranks
the `.cmd` in PATHEXT, so it shadows the Python forwarder that does work.
Cutting a name over therefore BREAKS it. That is why the check lives at
install time against the real root, and why a stale image must be removed
rather than merely not-rewritten.

NO SUITE-LEVEL ASSERTION OVER THE LIVE ROSTER AND THE LIVE ENGINE, and the
reason is worth recording because the obvious test is a trap. Asked from a
checkout, `warm.engine_root.current_engine_clone()` answers with the CHECKOUT
-- where every roster name's script exists by construction -- so the
assertion passes vacuously and certifies the very state it exists to catch.
The honest oracle is the `door.engine-root.txt` sidecar beside the installed
images, and `coordinator_core/conftest.py` points nearly every test's
`COORDINATOR_SETTINGS_HOME` at a registry-less `tmp_path`, so a suite run
cannot read it (measured: the test skipped, it did not pass). A
permanently-skipped test reads as coverage while providing none. The
property is checked where it is checkable -- at install time, against the
root being installed from.
"""

from __future__ import annotations

from coordinator_core.install import door_install, substrate


def _engine_root(tmp_path, *, names=(), stamped=True):
    root = tmp_path / "engine"
    (root / "coordinator" / "bin").mkdir(parents=True)
    if stamped:
        (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
        (root / "coordinator_core" / "_engine_stamp").write_text("stamp\n", encoding="utf-8")
    for name in names:
        (root / "coordinator" / "bin" / f"{name}.py").write_text("def main(argv):\n    return 0\n", encoding="utf-8")
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


def test_a_name_absent_from_the_engine_gets_no_image(tmp_path, capsys):
    """The excluded-CLI case: `publish` is door-eligible in the generator and
    absent from the engine it would dial. No image, and the caller's `None`
    contract is what leaves it on the Python pair that still works."""
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    result = substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert result is None
    assert not (bin_dst / "publish.exe").exists()
    assert not (bin_dst / "publish").exists()
    assert "no coordinator/bin/publish.py" in capsys.readouterr().err


def test_a_stale_image_from_an_earlier_cutover_is_removed(tmp_path):
    """The fix has to reach a box that ALREADY ran the broken cutover. A
    name left with its `.exe` in place keeps failing after the installer
    stops writing one, because the stale image still outranks the `.cmd`."""
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = door_install.named_forwarder_path(bin_dst, "publish")
    stale.write_bytes(b"MZ-stale-door-image")

    substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert not stale.exists()


def test_check_only_removes_nothing(tmp_path):
    """`check_only` is a read-only mode across this module; a check run that
    deleted a launcher would make `--check` a mutating verb."""
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = door_install.named_forwarder_path(bin_dst, "publish")
    stale.write_bytes(b"MZ-stale-door-image")

    substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=True, engine_root=root
    )

    assert stale.exists()


def test_a_name_the_engine_carries_still_cuts_over(tmp_path):
    """Negative-spec companion: the guard must refuse ONLY the names with no
    script, never narrow the cutover generally. A fix that stopped writing
    images would 'fix' this defect by deleting the whole native surface."""
    root = _engine_root(tmp_path, names=["coordinator-invoke", "handoff-housekeeping"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    dest = substrate._write_native_door_forwarder(
        "handoff-housekeeping", bin_dst, check_only=False, engine_root=root
    )

    assert dest is not None and dest.exists()


def test_the_canonical_door_is_never_removed_as_stale(tmp_path):
    """`coordinator-invoke` is itself in the door-eligible population, and
    `named_forwarder_path` resolves it to the path `install_door` writes.
    Removing that strips every session on the box of its engine entrypoint --
    the same self-collision `install_named_forwarder` guards, inverted."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    canonical = bin_dst / door_install.DOOR_INSTALLED_NAME
    canonical.write_bytes(b"MZ-the-real-door")

    removed = door_install.remove_stale_named_forwarder(bin_dst, canonical.stem)

    assert removed is None
    assert canonical.exists()


