"""A shadowed door passes every check the path-resolution probe already ran.

`test_door_bare_name_ordering.py` pins the ORDER that keeps the shadow from
surviving an install. This module covers what that ordering cannot: a box that
drifted afterwards. The strip (`door_install.claim_bare_name`) runs once, on the
path that lands the door; nothing re-reads the directory later, so a hand-edit
or a partial re-install can put `coordinator-invoke.ps1` back with no error and
no exit code. Every existing signal stays green -- the name resolves, the
exec-proof succeeds -- while every PowerShell caller silently pays an
interpreter start instead of the door's native relay.

`path_resolution_report._detect_bare_name_shadows` is the surface that makes
that state say so. These tests pin the two properties it exists for: it fires
when a door and a shadow share a directory, and it stays quiet in every state
where the shadow is legitimate -- above all when NO door is installed, where the
`.ps1` is the fallback forwarder `door_uninstall._reemit_fallback_forwarder`
deliberately writes back so the bare name still answers (door_install.py's Hard
Invariant 1).

Negative-spec: this does NOT test that anything repairs the shadow. Detection is
deliberately all this leg does -- repair has to know whether a door is SUPPOSED
to be present, which the installer knows and a probe does not.
"""

from __future__ import annotations

import sys
from pathlib import Path

from coordinator_core.install import door_install
from coordinator_core.install.path_resolution_report import (
    EntrypointCheck,
    PathResolutionReport,
    _detect_bare_name_shadows,
)

_DOOR = door_install.DOOR_INSTALLED_NAME
_SHADOW = door_install.BARE_FORWARDER_NAME + door_install._SHADOWING_SIBLING_SUFFIXES[0]


def _bin(tmp_path: Path, *names: str) -> Path:
    """A bin dir holding exactly `names`, each a stub file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in names:
        (bin_dir / name).write_text("stub", encoding="utf-8")
    return bin_dir


def test_shadow_beside_an_installed_door_is_reported(tmp_path: Path) -> None:
    bin_dir = _bin(tmp_path, _DOOR, _SHADOW)

    warnings = _detect_bare_name_shadows([str(bin_dir / _DOOR)])

    assert len(warnings) == 1
    assert _SHADOW in warnings[0]
    assert _DOOR in warnings[0]


def test_no_door_means_no_warning(tmp_path: Path, monkeypatch) -> None:
    """The fallback-forwarder state, not a defect: with no door installed the
    `.ps1` is what keeps the bare name answering at all. Warning here would tell
    an operator to delete the only thing still serving the name.

    DRIVEN AS WINDOWS ON EVERY BOX, because the state under test is
    unrepresentable on POSIX rather than merely untested there. Since the
    native-door cutover, `DOOR_INSTALLED_NAME` is `coordinator-invoke.exe` on
    Windows but the extensionless `coordinator-invoke` on POSIX -- the SAME
    string as `BARE_FORWARDER_NAME`. "A fallback forwarder with no door beside
    it" and "an installed door" are then the same directory, and the detector
    (correctly) cannot tell them apart. `_detect_bare_name_shadows` is reached
    only from `_check_windows`, so nothing in production ever asks it this
    question on POSIX.

    Patching the constant is the honest simulation: it drives the detector's
    real branch with the two-name arrangement Windows actually installs, and
    nothing about the on-disk shape is invented -- the files are the ones a
    Windows box carries. The POSIX name collapse that makes the patch necessary
    is itself pinned by `test_posix_door_and_fallback_forwarder_share_one_name`
    below, so the platform is covered rather than excused.
    """
    monkeypatch.setattr(door_install, "DOOR_INSTALLED_NAME", "coordinator-invoke.exe")
    bin_dir = _bin(tmp_path, door_install.BARE_FORWARDER_NAME, _SHADOW)

    assert _detect_bare_name_shadows([str(bin_dir / door_install.BARE_FORWARDER_NAME)]) == []


def test_posix_door_and_fallback_forwarder_share_one_name() -> None:
    """The platform fact the test above has to patch around, asserted rather
    than assumed.

    On Windows the door (`coordinator-invoke.exe`) and the bare fallback
    forwarder (`coordinator-invoke`) are two distinct filenames, which is what
    makes "no door installed" a state the detector can recognise. On POSIX the
    cutover collapsed them onto one extensionless name. If a later change
    reintroduces a POSIX suffix -- or drops the Windows one -- this fails
    loudly instead of letting the sibling test's patch quietly stop matching
    the platform it claims to simulate.
    """
    if sys.platform == "win32":
        assert door_install.DOOR_INSTALLED_NAME != door_install.BARE_FORWARDER_NAME
        assert door_install.DOOR_INSTALLED_NAME == door_install.BARE_FORWARDER_NAME + ".exe"
    else:
        assert door_install.DOOR_INSTALLED_NAME == door_install.BARE_FORWARDER_NAME, (
            "the POSIX door is the extensionless bare name itself; a suffix here "
            "would mean the native-door cutover's POSIX shape changed"
        )


def test_door_without_a_shadow_is_quiet(tmp_path: Path) -> None:
    bin_dir = _bin(tmp_path, _DOOR, door_install.BARE_FORWARDER_NAME + ".cmd")

    assert _detect_bare_name_shadows([str(bin_dir / _DOOR)]) == []


def test_one_warning_per_directory_not_per_entrypoint(tmp_path: Path) -> None:
    """Both entrypoints resolve into the same settings-home `bin/`; the shadow
    is a property of that directory, so a two-entrypoint report must not say it
    twice."""
    bin_dir = _bin(tmp_path, _DOOR, _SHADOW, "coordinator-cockpit-emit-schema.cmd")

    warnings = _detect_bare_name_shadows([
        str(bin_dir / _DOOR),
        str(bin_dir / "coordinator-cockpit-emit-schema.cmd"),
    ])

    assert len(warnings) == 1


def test_shadow_does_not_fail_the_probe(tmp_path: Path) -> None:
    """A shadowed door resolves and executes -- `all_ok` is about whether the
    chain works, and it does. Folding the warning in would turn a performance
    defect into an install failure and send operators repairing a working PATH."""
    resolved = _bin(tmp_path, _DOOR, _SHADOW) / _DOOR
    report = PathResolutionReport(
        platform="Windows",
        method="test",
        checks=[EntrypointCheck(
            name=door_install.BARE_FORWARDER_NAME, resolved_path=str(resolved),
            executed_ok=True, detail="resolved and executed",
        )],
        shadow_warnings=_detect_bare_name_shadows([str(resolved)]),
    )

    assert report.shadow_warnings
    assert report.all_ok is True
