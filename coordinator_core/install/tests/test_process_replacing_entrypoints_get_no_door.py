"""An entrypoint that replaces its own process never gets a native door
forwarder -- and never loses its Python one either.

WHAT THIS PREVENTS. The door's warm leg runs the named entrypoint's
`main()` INSIDE the warm server. `coordinator/bin/claude-doe.py` exists to
`os.execv` into `claude --plugin-dir <clone>/coordinator`, so served warm it
overlays the SERVER process: the engine every peer on this box is queued
behind vanishes mid-request, and the caller -- someone starting an
interactive session -- gets a socket reply instead of a TUI. The
direct-child invariant in `docs/reference/interactive-launch-chain.md` is a
second, independent reason the same install is wrong.

WHY THE PRESENCE ORACLE COULD NOT CATCH IT. `launcher_is_installable` asks
whether the published engine carries `coordinator/bin/<name>.py`.
`claude-doe.py` IS carried -- it is a live, published, PATH-resolved tool.
It passed, and the 2026-09-02 multi-name install hardlinked the door over
it. The distinguishing fact is what the entrypoint DOES, which no artifact
in the payload states, so `door_install._EXEC_SHAPED_NAMES` is a roster
(see its own comment on the marker that should eventually replace it).

THE TWO HALVES ARE ONE PROPERTY. "No native image" alone would be satisfied
by leaving the name off PATH entirely -- which is the correct end state for
the publish-excluded population and a broken one here, because under ONE
ENTRYPOINT PER PLATFORM the image is the only launcher a cut-over name
gets. An unservable name must land on the Python pair, so this file asserts
the fallback as well as the refusal.
"""

from __future__ import annotations

import pytest

from coordinator_core.install import door_install, substrate


def test_claude_doe_is_not_warm_servable():
    """The roster is reachable through a named predicate, not read directly
    by its callers -- `substrate` asks the question, `door_install` owns the
    answer."""
    assert not door_install.name_is_warm_servable("claude-doe")


def test_an_ordinary_op_name_is_warm_servable():
    """The roster is a carve-out, not a gate: everything else still cuts
    over. `blocked` is the name the 2026-09-02 report reproduced on."""
    assert door_install.name_is_warm_servable("blocked")


def test_no_native_image_is_written_for_a_process_replacing_name(tmp_path):
    """`_write_native_door_forwarder` returns None BEFORE it consults the
    engine root at all -- so the refusal holds on a box whose root would
    otherwise supply a perfectly good door."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()

    result = substrate._write_native_door_forwarder(
        "claude-doe", bin_dst, check_only=False, engine_root=tmp_path / "engine"
    )

    assert result is None
    assert not (bin_dst / "claude-doe").exists()


def test_a_stale_image_from_an_earlier_install_is_taken_back(tmp_path):
    """The fix has to reach a box that already ran the broken cutover. On
    POSIX the image is not merely outranked by the Python forwarder -- it IS
    the name's only file, so leaving it is leaving the break in place."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "claude-doe"
    stale.write_bytes(b"\xcf\xfa\xed\xfe a door image hardlinked over the trampoline")

    substrate._write_native_door_forwarder(
        "claude-doe", bin_dst, check_only=False, engine_root=tmp_path / "engine"
    )

    assert not stale.exists()


def test_check_only_reports_but_removes_nothing(tmp_path):
    """A probe must not mutate -- the same contract every other branch of
    this function honours."""
    bin_dst = tmp_path / "bin"
    bin_dst.mkdir()
    stale = bin_dst / "claude-doe"
    stale.write_bytes(b"\xcf\xfa\xed\xfe")

    result = substrate._write_native_door_forwarder(
        "claude-doe", bin_dst, check_only=True, engine_root=tmp_path / "engine"
    )

    assert result is None
    assert stale.exists()


@pytest.mark.parametrize("name", sorted(door_install._EXEC_SHAPED_NAMES))
def test_every_rostered_name_actually_execs(name):
    """The roster is only honest while its members still exec. A name whose
    entrypoint stopped replacing its process should REJOIN the door rather
    than sit here collecting an exemption it no longer needs -- the mistake
    `cross-repo-memo` would be, whose `os.execvp` survives only in a comment
    recording that its cutover removed the call."""
    script = door_install._GENERATOR_BIN_DIR / f"{name}.py"
    assert script.is_file(), f"{name} is rostered but has no CLI here"

    source = script.read_text(encoding="utf-8")
    exec_calls = [
        line
        for line in source.splitlines()
        if ("os.execv" in line or "os.execvp" in line)
        and not line.lstrip().startswith("#")
    ]
    assert exec_calls, (
        f"{name} no longer calls os.execv outside a comment -- if its "
        "cutover removed the call, remove it from _EXEC_SHAPED_NAMES so it "
        "cuts over to the door like every other name."
    )
