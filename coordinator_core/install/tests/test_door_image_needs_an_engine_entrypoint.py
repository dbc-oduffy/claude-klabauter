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

C1 addendum (docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-
are-thoroughly-dead.md): a KILLED op (`.py` deleted from both trees, not
merely publish-excluded) reads as the same "no `.py` HERE" shape this file's
predicate tests already cover, and `launcher_is_installable` therefore
cannot tell it apart from the extensionless twelve. The tests below pin the
separate, roster- and manifest-independent mechanism
(`_sweep_orphaned_agent_helpers`'s `_KILLED_OP_ORPHAN_NAMES` match) that
actually retires a killed op's stale image.

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


def test_a_killed_op_image_is_reaped_by_the_orphan_sweep_even_without_manifest_membership(monkeypatch, tmp_path):
    """C1, docs/plans/2026-08-30-twenty-one-bin-names-reach-the-door-or-are-
    thoroughly-dead.md: `coordinator-write-review-trail`,
    `list-review-trail-records`, and `repair-empty-review-trail-ranges`
    were killed under K-068 -- `.py` deleted from both trees -- which is
    exactly the shape `launcher_is_installable` cannot distinguish from the
    extensionless twelve (see that function's docstring). A killed op's
    stale image therefore is never reaped by the per-name install loop, and
    may never have been recorded in the native-forwarder manifest at all
    (dropped from the roster before C0's manifest fix landed, or written by
    an install that predates the manifest entirely). The sweep's killed-op
    name match (condition 0b) must reap it anyway -- roster- and
    manifest-independent."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "repair-empty-review-trail-ranges.exe"
    stale.write_bytes(b"MZ-a-killed-ops-image")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    assert not stale.exists()


def test_a_killed_op_image_check_only_reports_but_does_not_remove(tmp_path):
    """`check_only` stays read-only across every identification path here,
    including the killed-op one -- a probe must not mutate."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "list-review-trail-records.exe"
    stale.write_bytes(b"MZ-a-killed-ops-image")

    raised = False
    try:
        substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=True)
    except substrate.SubstrateFatalError:
        raised = True

    assert raised
    assert stale.exists()


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


def test_a_renamed_native_image_is_reaped_on_windows_not_only_posix(monkeypatch, tmp_path):
    """Condition 0 (native-forwarder manifest match) must identify a native
    image by the FILENAME it was written under, not by the bare name the
    manifest records.

    The defect this pins, measured live 2026-09-01: `native_written.add(f)`
    stores the bare installed name while `named_forwarder_path` writes
    `<f>.exe` on Windows, so `entry.name in native_forwarder_names` was
    unsatisfiable there. The image then fell through to the text-marker
    branch, whose `read_text()` raises `UnicodeDecodeError` on an opaque
    binary and is silently skipped -- leaving every renamed-away native
    launcher on a Windows box unsweepable by construction. Four survivors
    were found on the reporting box, each an OSS-renamed name with a live
    source-named counterpart (`check-claude-klabauter-doctor-sentinel.exe`,
    `gen-claude-klabauter-root-pointer.exe`,
    `remove-claude-klabauter-precommit-hook.exe`,
    `probe-cwd-example-retrieval-repo-relevance.exe`).

    POSIX passed throughout, because there `named_forwarder_path` adds no
    suffix and the filename IS the bare name -- which is why the platform is
    forced here rather than left to the host.
    """
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(door_install.sys, "platform", "win32")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    # The manifest records the BARE name, exactly as the writer does.
    substrate._write_native_forwarder_manifest(bin_dst, {"renamed-away-cli"})
    stale = bin_dst / "renamed-away-cli.exe"
    stale.write_bytes(b"MZ-an-opaque-native-image")

    substrate._sweep_orphaned_agent_helpers(bin_dst, {}, {}, check_only=False)

    assert not stale.exists()


def test_a_native_image_this_run_still_writes_survives_the_sweep(monkeypatch, tmp_path):
    """The other half, and the one that makes the fix safe: broadening
    condition 0's identification must not broaden DELETION. Condition 2
    (absence from this run's complete write set) still gates it, and a
    door-eligible name's `.exe` reaches that gate via
    `extra_protected_names`. Without this, the fix above would sweep every
    native image on the same run that wrote it."""
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    monkeypatch.setattr(door_install.sys, "platform", "win32")
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    substrate._write_native_forwarder_manifest(bin_dst, {"still-installed-cli"})
    live = bin_dst / "still-installed-cli.exe"
    live.write_bytes(b"MZ-an-opaque-native-image")

    substrate._sweep_orphaned_agent_helpers(
        bin_dst, {}, {}, check_only=False,
        extra_protected_names=frozenset({"still-installed-cli.exe"}),
    )

    assert live.exists()
