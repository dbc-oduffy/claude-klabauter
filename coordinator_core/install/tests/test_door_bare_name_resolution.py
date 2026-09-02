"""Which file the bare name selects, pinned as logic rather than as a machine.

`test_door_bare_name_ordering.py` pins the INSTALLER: that `install_warm_door`'s
`claim_bare_name` is sequenced after the `.ps1`-emitting `install_bin_forwarders`,
as a static AST property of `scripts/setup.py`. It cannot see the environment the
installed door actually lives in, and PATH is not owned by this repo.

WHY THE MACHINE IS NOT TESTED HERE, stated so a successor does not "fix" it by
reaching for the real home. A first version of this module asserted against the
live install and SKIPPED all three of its tests: `coordinator_core`'s suite
quarantines `Path.home()` to a temp dir by design, so a hermetic suite structurally
cannot see a property about the real machine. A skip reads as coverage and is not.
The split that survives: `resolve_bare_name` is pure over PATH and PATHEXT and is
pinned HERE against injected directories; `forwarder_door_census.bare_name_door_report`
applies it to the live environment and REPORTS. Do not re-point these tests at
`Path.home()` -- the quarantine will win and the coverage will be imaginary.

THE HAZARD the logic exists to catch, found 2026-08-27 by resolving the bare name
instead of assuming it: a pip-installed console-script shim at
`<python>/Scripts/coordinator-invoke.exe` exists by construction, because this
package declares the console entry point. It loses to the settings-home door on
PATH ORDERING ALONE, with no error and no runtime signal if that flips -- only
~94ms of interpreter start plus engine import per call, against a ~2.34ms relay.

Negative-spec: does NOT test that the door works (`test_warm_door_process_time_gate.py`),
and does NOT test that the installer removes the `.ps1`
(`test_door_install.py :: _remove_shadowing_forwarder_siblings`).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinator_core.install.forwarder_door_census import (
    bare_name_starts_an_interpreter,
    resolve_bare_name,
)

_STEM = "coordinator-invoke"

#: PATHEXT as Windows actually spells it. It is ALWAYS `;`-separated -- the
#: separator is a property of the variable, not of the host reading it.
_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY"


@pytest.fixture
def windows_pathext_semantics(monkeypatch):
    """Drive `resolve_bare_name`'s PATHEXT branch on EVERY box, POSIX included,
    rather than excusing the platform with a skip.

    `resolve_bare_name` splits its `pathext` argument on `os.pathsep`, which is
    `;` on Windows and `:` on POSIX. That single read is the function's only
    platform coupling and it contradicts the function's own docstring, which
    calls it "PURE over its arguments ... so the ordering logic is testable on
    a machine that has no door installed" -- on POSIX a real `;`-separated
    PATHEXT parses as ONE opaque extension, every candidate misses, and these
    tests inverted silently for a week after landing (2026-08-27).

    Patching the separator is the honest simulation: it tells the resolver it
    is parsing a Windows PATHEXT, which is the only kind that exists, and
    fabricates nothing on disk. The POSIX arrangement the local box actually
    produces is pinned separately by
    `test_posix_empty_pathext_resolves_the_extensionless_native_door` -- both
    platforms are covered, neither is asserted by proxy for the other.

    NOT a substitute for fixing the coupling: `resolve_bare_name` should split
    on a literal `";"`. That is product code and out of this module's scope.
    """
    monkeypatch.setattr(os, "pathsep", ";")


def _touch(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("x", encoding="utf-8")
    return p


def _resolve(dirs: "list[Path]") -> "list[Path]":
    return resolve_bare_name(_STEM, [str(d) for d in dirs], _PATHEXT)


# WHICH FILE WON, not how the resolver spelled it. `resolve_bare_name` builds
# each candidate as `stem + <PATHEXT entry as written>`, and PATHEXT is
# conventionally UPPERCASE (`.COM;.EXE;...`) while the installed door is
# lowercase `coordinator-invoke.exe`. The candidate hits on a case-insensitive
# filesystem -- Windows' NTFS and macOS' default APFS alike -- and is then
# recorded under PATHEXT's casing rather than the name on disk. These tests
# are about ORDERING, so they compare identity; an equality-on-the-string
# assertion here was red on every platform, which is why the four Windows
# cases below never passed anywhere.
#
# The casing itself is a PRODUCT finding, reported and deliberately not
# papered over here: `bare_name_door_report` compares its winner against a
# lowercase `DOOR_INSTALLED_NAME` with `!=`, so on Windows it declares a
# correctly-installed door "BROKEN" on casing alone.
def _same(a: Path, b: Path) -> bool:
    return a.exists() and b.exists() and a.samefile(b)


def _index_of(hits: "list[Path]", wanted: Path) -> int:
    for i, h in enumerate(hits):
        if _same(h, wanted):
            return i
    return -1


def test_settings_home_exe_wins_over_a_later_scripts_shim(tmp_path: Path, windows_pathext_semantics) -> None:
    """The live arrangement: the door's bin/ earlier on PATH than a Python
    `Scripts/` carrying a same-named console-script shim."""
    bin_dir = tmp_path / "settings" / "bin"
    scripts = tmp_path / "python" / "Scripts"
    door = _touch(bin_dir, f"{_STEM}.exe")
    shim = _touch(scripts, f"{_STEM}.exe")

    hits = _resolve([bin_dir, scripts])
    assert _same(hits[0], door), f"expected the door to win; order was {hits}"
    assert _index_of(hits, shim) >= 0, (
        "the shim must still be REPORTED, so its presence is visible"
    )


def test_an_earlier_scripts_shim_takes_the_bare_name(tmp_path: Path, windows_pathext_semantics) -> None:
    """The regression this whole module exists for. Nothing but PATH order
    separates this case from the one above, and the flip is silent."""
    bin_dir = tmp_path / "settings" / "bin"
    scripts = tmp_path / "python" / "Scripts"
    _touch(bin_dir, f"{_STEM}.exe")
    shim = _touch(scripts, f"{_STEM}.exe")

    hits = _resolve([scripts, bin_dir])
    assert _same(hits[0], shim), (
        "a Scripts/ shim EARLIER on PATH must be reported as the winner -- if the "
        "resolver cannot see this, the census can never warn about it"
    )
    assert bare_name_starts_an_interpreter(hits[0]), (
        "a pip console-script shim under Scripts/ starts an interpreter per call "
        "and must be classified as such, .exe suffix notwithstanding"
    )


def test_a_ps1_sibling_beats_the_exe_in_the_same_directory(tmp_path: Path) -> None:
    """PowerShell's own rule, which PATHEXT does not describe and `shutil.which`
    cannot see. This is the original hazard `claim_bare_name` strips.

    This pins the MODELLED order `resolve_bare_name` was built to produce -- a
    unit test over a pure function cannot verify an OS behaviour, and asserting
    against the function's own construction is not evidence the model is right.
    The model's fidelity rests on the captured `Get-Command`/PATHEXT trace in
    `_POWERSHELL_FIRST_EXT`'s comment (`forwarder_door_census.py`), not on this
    test passing."""
    bin_dir = tmp_path / "settings" / "bin"
    _touch(bin_dir, f"{_STEM}.exe")
    ps1 = _touch(bin_dir, f"{_STEM}.ps1")

    hits = _resolve([bin_dir])
    assert hits[0] == ps1, (
        "a same-directory .ps1 must resolve ahead of the .exe -- modelling this is "
        "the only reason this resolver exists instead of shutil.which"
    )
    assert bare_name_starts_an_interpreter(hits[0])


def test_pathext_order_is_honoured_within_one_directory(tmp_path: Path, windows_pathext_semantics) -> None:
    """`.EXE` ahead of `.CMD` is what keeps the door ahead of its own forwarder
    sibling, which ships beside it on every install."""
    bin_dir = tmp_path / "settings" / "bin"
    exe = _touch(bin_dir, f"{_STEM}.exe")
    _touch(bin_dir, f"{_STEM}.cmd")

    hits = _resolve([bin_dir])
    assert _same(hits[0], exe), f"PATHEXT ranks .EXE above .CMD; order was {hits}"


def test_extensionless_file_loses_to_every_pathext_entry(tmp_path: Path, windows_pathext_semantics) -> None:
    """An extensionless `coordinator-invoke` ships beside the door too. cmd tries
    it only after PATHEXT is exhausted, so it must never take the name."""
    bin_dir = tmp_path / "settings" / "bin"
    exe = _touch(bin_dir, f"{_STEM}.exe")
    bare = _touch(bin_dir, _STEM)

    hits = _resolve([bin_dir])
    assert _same(hits[0], exe)
    assert _same(hits[-1], bare), f"extensionless must sort last; order was {hits}"


def test_posix_empty_pathext_resolves_the_extensionless_native_door(tmp_path: Path) -> None:
    """The POSIX counterpart, running against the artifact THIS box produces.

    The 2026-09-02 native-door cutover lands the door at the EXTENSIONLESS bare
    name on POSIX (`door_install.DOOR_INSTALLED_NAME`), and POSIX has no PATHEXT
    at all -- `bare_name_door_report` passes `os.environ.get("PATHEXT", "")`,
    i.e. the empty string. The resolver must then select the extensionless
    native image, and must classify it as a native relay rather than an
    interpreter start.

    This exists because the post-mortem of the cutover named the deepest cause
    as a test that fabricated the WINDOWS artifact shape on a POSIX box and so
    never ran a consumer against what the local platform actually builds. The
    Windows-shaped assertions above are simulated (`windows_pathext_semantics`);
    this one is not simulated at all.
    """
    bin_dir = tmp_path / "settings" / "bin"
    door = _touch(bin_dir, _STEM)

    hits = resolve_bare_name(_STEM, [str(bin_dir)], "")

    assert hits == [door], f"the extensionless native door must take the bare name; got {hits}"
    assert not bare_name_starts_an_interpreter(hits[0]), (
        "the POSIX native door is a Mach-O image, not an interpreter start -- "
        "classifying it as one would report every POSIX box BREAK-CLASS"
    )


def test_resolution_is_empty_when_nothing_matches(tmp_path: Path) -> None:
    """An empty result is the "door bin/ is not on PATH" signal the census
    reports as BROKEN -- it must not be confused with a successful resolve."""
    assert _resolve([tmp_path / "empty"]) == []


def test_a_native_exe_outside_scripts_is_not_an_interpreter_start(tmp_path: Path) -> None:
    """The predicate must not over-fire: the whole point is that the real door
    passes it."""
    door = _touch(tmp_path / "settings" / "bin", f"{_STEM}.exe")
    assert not bare_name_starts_an_interpreter(door)


def test_shell_forwarder_suffixes_are_interpreter_starts(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    for suffix in (".cmd", ".bat", ".ps1", ".py"):
        assert bare_name_starts_an_interpreter(_touch(bin_dir, f"{_STEM}{suffix}")), suffix


def test_empty_path_entries_are_skipped_not_resolved_against_cwd() -> None:
    """A trailing `;` in PATH yields an empty string, which `Path("")` turns into
    the CWD -- resolving there would let any directory a caller happens to sit in
    claim the bare name."""
    assert resolve_bare_name(_STEM, ["", os.sep], _PATHEXT) == []
