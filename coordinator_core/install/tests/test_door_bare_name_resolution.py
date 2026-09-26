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

_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.PY"


@pytest.fixture
def windows_pathext_semantics(monkeypatch):
    monkeypatch.setattr(os, "pathsep", ";")


def _touch(d: Path, name: str) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("x", encoding="utf-8")
    return p


def _resolve(dirs: "list[Path]") -> "list[Path]":
    return resolve_bare_name(_STEM, [str(d) for d in dirs], _PATHEXT)


# conventionally UPPERCASE (`.COM;.EXE;...`) while the installed door is
# are about ORDERING, so they compare identity; an equality-on-the-string
# lowercase `DOOR_INSTALLED_NAME` with `!=`, so on Windows it declares a
def _same(a: Path, b: Path) -> bool:
    return a.exists() and b.exists() and a.samefile(b)


def _index_of(hits: "list[Path]", wanted: Path) -> int:
    for i, h in enumerate(hits):
        if _same(h, wanted):
            return i
    return -1


def test_settings_home_exe_wins_over_a_later_scripts_shim(tmp_path: Path, windows_pathext_semantics) -> None:
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
    bin_dir = tmp_path / "settings" / "bin"
    exe = _touch(bin_dir, f"{_STEM}.exe")
    _touch(bin_dir, f"{_STEM}.cmd")

    hits = _resolve([bin_dir])
    assert _same(hits[0], exe), f"PATHEXT ranks .EXE above .CMD; order was {hits}"


def test_extensionless_file_loses_to_every_pathext_entry(tmp_path: Path, windows_pathext_semantics) -> None:
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
    assert _resolve([tmp_path / "empty"]) == []


def test_a_native_exe_outside_scripts_is_not_an_interpreter_start(tmp_path: Path) -> None:
    door = _touch(tmp_path / "settings" / "bin", f"{_STEM}.exe")
    assert not bare_name_starts_an_interpreter(door)


def test_shell_forwarder_suffixes_are_interpreter_starts(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    for suffix in (".cmd", ".bat", ".ps1", ".py"):
        assert bare_name_starts_an_interpreter(_touch(bin_dir, f"{_STEM}{suffix}")), suffix


def test_empty_path_entries_are_skipped_not_resolved_against_cwd() -> None:
    assert resolve_bare_name(_STEM, ["", os.sep], _PATHEXT) == []


def test_exact_case_wins_its_own_folded_bucket(tmp_path: Path, windows_pathext_semantics) -> None:
    bin_dir = tmp_path / "settings" / "bin"
    lower = _touch(bin_dir, f"{_STEM}.exe")
    upper = bin_dir / f"{_STEM}.EXE"
    if upper.exists():
        pytest.skip("case-insensitive filesystem: the two spellings are one file here")
    upper.write_text("x", encoding="utf-8")

    hits = resolve_bare_name(_STEM, [str(bin_dir)], ".EXE")
    assert hits[0] == upper, (
        f"an exact-case `.EXE` candidate must win over the folded `.exe`; got {hits}"
    )
    assert lower.is_file()


def test_exact_case_directory_falls_back_to_the_real_file_in_its_bucket(
    tmp_path: Path, windows_pathext_semantics
) -> None:
    """Regression (S6 review, finding 1): a folded bucket can hold BOTH a
    DIRECTORY spelled exactly `<stem><ext>` and a FILE of the same stem
    differing only in case. The prior implementation committed to the
    exact-case spelling before calling `is_file()`, resolved to the
    directory, failed `is_file()`, and `continue`d past the whole extension
    arm instead of trying the sibling file still sitting in the same
    bucket -- so a directory could shadow a real, differently-cased file.
    The fix must fall through to the next candidate in preference order.
    """
    bin_dir = tmp_path / "settings" / "bin"
    bin_dir.mkdir(parents=True)
    directory = bin_dir / f"{_STEM}.exe"
    directory.mkdir()
    real_file = bin_dir / f"{_STEM}.EXE"
    if real_file.exists():
        pytest.skip("case-insensitive filesystem: the two spellings are one entry here")
    real_file.write_text("x", encoding="utf-8")

    hits = resolve_bare_name(_STEM, [str(bin_dir)], ".EXE")
    assert hits == [real_file], (
        f"a same-bucket directory must not shadow the real file; got {hits}"
    )


def test_posix_rules_ignore_a_powershell_sibling(tmp_path: Path) -> None:
    """`rules="posix"` drops the `.ps1` arm: no shell there executes a `.ps1`, so
    ranking it ahead of the real image reports a file no POSIX caller can run as
    the winner -- and then as a BREAK-CLASS interpreter start."""
    bin_dir = tmp_path / "settings" / "bin"
    door = _touch(bin_dir, _STEM)
    door.chmod(0o755)
    _touch(bin_dir, f"{_STEM}.ps1")

    hits = resolve_bare_name(_STEM, [str(bin_dir)], "", rules="posix")
    assert hits == [door], f"the `.ps1` must not rank at all under POSIX rules; got {hits}"


@pytest.mark.skipif(os.name == "nt", reason="needs POSIX mode bits; chmod(0o644) cannot clear an exec bit Windows never had")
def test_posix_rules_skip_a_non_executable_candidate(tmp_path: Path) -> None:
    bin_dir = tmp_path / "settings" / "bin"
    later_dir = tmp_path / "settings" / "bin2"
    unreadable = _touch(bin_dir, _STEM)
    unreadable.chmod(0o644)
    real = _touch(later_dir, _STEM)
    real.chmod(0o755)

    hits = resolve_bare_name(_STEM, [str(bin_dir), str(later_dir)], "", rules="posix")
    assert hits == [real], f"a 0644 file is not executable and is not a hit; got {hits}"

    permissive = resolve_bare_name(_STEM, [str(bin_dir), str(later_dir)], "")
    assert permissive[0] == unreadable, (
        "the default must stay the Windows model and mode-blind -- the POSIX "
        "permission check is carried by `rules`, not by the pure default"
    )


def test_the_platform_axis_is_one_named_model_not_a_set_of_flags() -> None:
    import inspect

    from coordinator_core.install.forwarder_door_census import _PLATFORM_RULES

    params = inspect.signature(resolve_bare_name).parameters
    assert set(_PLATFORM_RULES) == {"windows", "posix"}
    assert [p for p in params if params[p].kind is inspect.Parameter.KEYWORD_ONLY] == ["rules"], (
        f"the platform axis must stay one keyword; got {list(params)}"
    )
    assert params["rules"].default == "windows"

    with pytest.raises(ValueError):
        resolve_bare_name(_STEM, [], "", rules="plan9")
