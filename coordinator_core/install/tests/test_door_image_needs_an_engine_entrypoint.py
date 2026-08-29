"""A name the engine cannot resolve gets no launcher at all.

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

SO THEY GET NO LAUNCHER (PM ruling 2026-08-29, on `publish`: "we shouldn't
need a publish.exe nor a publish.cmd"). Under ONE ENTRYPOINT PER PLATFORM the
door image is the only launcher a name gets -- no `.cmd` is written for any
name and `_write_agent_cmd_forwarder` is deleted -- so declining to write it
takes the name off PATH entirely. That is the intended end state here: these
are repo-side tools, run from their own checkout as
`python coordinator/bin/<name>.py`, and a launcher for them only ever produced
one that fails.

An intermediate revision of this file asserted the opposite, on the reasoning
that an absent launcher is worse than a broken one. It is not, for THESE
names -- they were never PATH tools. What that revision got right, and what
the `check_only` test below still pins, is that the removal must be
deliberate and never a side effect of a read-only probe.

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


def test_a_name_the_engine_cannot_serve_gets_no_launcher(tmp_path, capsys):
    """PM ruling 2026-08-29, on `publish`: "we shouldn't need a publish.exe
    nor a publish.cmd".

    These are repo-side tools -- the publisher chain, claude-klabauter's own migrations
    and probes -- deliberately not carried into the published engine. Neither
    leg of the door can work for them, and they were never PATH tools: they
    run from their own checkout. So no launcher is written, and the message
    says how to run it instead.
    """
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    result = substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert result is None
    assert not door_install.named_forwarder_path(bin_dst, "publish").exists()
    err = capsys.readouterr().err
    assert "no launcher installed" in err
    assert "python coordinator/bin/publish.py" in err


def test_a_stale_launcher_from_an_earlier_install_is_taken_back(tmp_path):
    """The removal is the point, not a side effect: a box that already ran an
    install which wrote these images keeps a launcher that fails both warm and
    cold until the next run takes it back."""
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = door_install.named_forwarder_path(bin_dst, "publish")
    stale.write_bytes(b"MZ-an-earlier-image")

    substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=False, engine_root=root
    )

    assert not stale.exists()


def test_check_only_removes_nothing(tmp_path):
    """`check_only` is read-only across this module; a probe that deleted a
    launcher would make `--check-only` a mutating verb."""
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = door_install.named_forwarder_path(bin_dst, "publish")
    stale.write_bytes(b"MZ-an-earlier-image")

    substrate._write_native_door_forwarder(
        "publish", bin_dst, check_only=True, engine_root=root
    )

    assert stale.exists()

def test_a_name_the_engine_carries_still_gets_its_launcher(tmp_path, capsys):
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
    assert "no launcher installed" not in capsys.readouterr().err


def test_a_name_with_no_py_twin_in_either_tree_keeps_its_launcher(tmp_path, capsys):
    """The extensionless twelve -- chunk-commits, static-check,
    with-suite-mutex, coordinator-precommit-foreign-platform-check and
    siblings -- have no .py in EITHER tree, so _resolve_entrypoint_script
    has never resolved them (a separate, already-recorded defect). They are
    live PATH tools the git hooks this installer writes invoke by name.

    The engine-only predicate could not tell them from the publish-excluded
    set and queued all 26 for removal, hook CLIs included. This is the pin for
    the narrowing."""
    _skip_if_no_prebuilt()
    root = _engine_root(tmp_path, names=["coordinator-invoke"])
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    # with-suite-mutex has no coordinator/bin/with-suite-mutex.py in this
    # repo, which is exactly what distinguishes it from publish.
    dest = substrate._write_native_door_forwarder(
        "with-suite-mutex", bin_dst, check_only=False, engine_root=root
    )

    assert dest is not None and dest.exists()
    assert "no launcher installed" not in capsys.readouterr().err


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
