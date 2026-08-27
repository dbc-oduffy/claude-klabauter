"""Tests for `_write_bump_sink_shapes.translate_msys_path` /
`.resolve_relative` (C1, `docs/plans/2026-08-07-guard-posix-path-rerooting.md`).

Simulation technique follows the precedent in
`coordinator_core/write_guards/tests/test_windows_platform_simulation.py`:
the module's own `_host_is_windows` seam drives the Windows/POSIX branch,
and `monkeypatch.setattr(os, "path", ntpath)` swaps in the stdlib's `ntpath`
module for native path semantics on any host -- `ntpath` is pure lexical
string manipulation off-Windows too, so this exercises the SAME
`isabs`/`join` code paths a real Windows interpreter takes.

AC1 is the load-bearing case here: it reifies the pre-3.13 (`isabs` True,
verbatim-then-`realpath`) route and the 3.13+ (`isabs` False, `join`-based
re-rooting) route as a PARAMETER over one seam
(`os.path.isabs`/`ntpath.isabs`), rather than requiring a three-interpreter
run to prove the fix is version-independent. Both parametrized cases must
produce the identical native path for the plan's own named incident string,
`/x/claude-klabauter/scratch/t.txt`.
"""

from __future__ import annotations

import ntpath
import os
import posixpath

import pytest

from coordinator_core.bash_guards import _write_bump_sink_shapes as shapes


@pytest.fixture()
def _windows_ntpath(monkeypatch):
    """Simulated Windows host: `_host_is_windows()` forced True, and
    `os.path` swapped to `ntpath` so `isabs`/`join` see native semantics
    regardless of the host this suite actually runs on."""
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: True)
    monkeypatch.setattr(os, "path", ntpath)


@pytest.fixture()
def _posix_host(monkeypatch):
    """Simulated POSIX host: `_host_is_windows()` forced False AND `os.path`
    swapped to `posixpath` -- the mirror image of `_windows_ntpath` above.
    `translate_msys_path` is IDENTITY on this branch and never touches
    `os.path` itself, but `resolve_relative`'s own `expanduser`/`isabs`/
    `join` calls do reach `os.path`, and this session's own interpreter is
    a live Windows host (`os.path` is `ntpath` by default here) -- without
    this swap, AC4's `os.path.join` calls would silently take the WRONG
    (backslash) native semantics regardless of what `_host_is_windows()`
    reports, exactly the version/host-dependent divergence this whole
    module exists to eliminate. `posixpath` is pure lexical string
    manipulation on any host, same rationale as the `ntpath` swap."""
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: False)
    monkeypatch.setattr(os, "path", posixpath)


# ─── AC1 -- the cross-version proof ─────────────────────────────────────────


@pytest.mark.parametrize("isabs_seam", [True, False])
def test_resolve_relative_version_independent_for_msys_absolute_target(
    _windows_ntpath, monkeypatch, isabs_seam
):
    """The module's own `isabs` seam is forced to BOTH True (the pre-3.13
    route: the translated target is already absolute, returned verbatim)
    and False (the 3.13+ route: `os.path.join(base, expanded)` is taken) --
    and both must produce the IDENTICAL native path. This is the proof that
    translating `base`/`target` BEFORE either stdlib call sees them makes
    the fix version-independent by construction, not by test-matrix luck."""
    monkeypatch.setattr(os.path, "isabs", lambda p: isabs_seam)
    result = shapes.resolve_relative(
        "/x/claude-klabauter", "/x/claude-klabauter/scratch/t.txt"
    )
    assert result == "X:\\claude-klabauter\\scratch\\t.txt"  # abs-path-ok: brief-pinned AC1 fixture (C1 brief), synthetic


def test_resolve_relative_end_to_end_on_live_interpreter(monkeypatch):
    """ONE non-parametrized end-to-end case, no `isabs` override -- proves
    the real, live interpreter's own `isabs`/`join` (whichever route this
    Python version takes) converges on the same result via the ntpath
    simulation."""
    monkeypatch.setattr(shapes, "_host_is_windows", lambda: True)
    monkeypatch.setattr(os, "path", ntpath)
    result = shapes.resolve_relative(
        "/x/claude-klabauter", "/x/claude-klabauter/scratch/t.txt"
    )
    assert result == "X:\\claude-klabauter\\scratch\\t.txt"  # abs-path-ok: brief-pinned AC1 fixture (C1 brief), synthetic


# ─── AC2 -- drive-mount translation shape ───────────────────────────────────


def test_translate_msys_path_drive_mount_to_native(_windows_ntpath):
    assert shapes.translate_msys_path("/c/Users/example-operator/x.txt") == "C:\\Users\\example-operator\\x.txt"  # abs-path-ok: brief-pinned AC2 example (C1 brief), synthetic


def test_translate_msys_path_bare_drive_mount(_windows_ntpath):
    assert shapes.translate_msys_path("/c") == "C:\\"  # abs-path-ok: bare drive root, universal on every Windows host


def test_translate_msys_path_leaves_native_drive_absolute_unchanged(_windows_ntpath):
    assert (
        shapes.translate_msys_path("C:\\Users\\example-operator\\x.txt")  # abs-path-ok: brief-pinned AC2 example (C1 brief), synthetic
        == "C:\\Users\\example-operator\\x.txt"  # abs-path-ok: brief-pinned AC2 example (C1 brief), synthetic
    )
    assert shapes.translate_msys_path("C:/Users/example-operator/x.txt") == "C:/Users/example-operator/x.txt"  # abs-path-ok: brief-pinned AC2 example (C1 brief), synthetic


def test_translate_msys_path_leaves_relative_path_unchanged(_windows_ntpath):
    assert shapes.translate_msys_path("scratch/t.txt") == "scratch/t.txt"


# ─── AC4 -- POSIX host is pure identity, never returns None ────────────────


@pytest.mark.parametrize(
    "path",
    ["/tmp/x", "/x/foo", "/c/Users/x", "relative/path.txt"],  # abs-path-ok: brief-pinned AC4 fixture set (C1 brief), synthetic
)
def test_translate_msys_path_identity_on_posix(_posix_host, path):
    assert shapes.translate_msys_path(path) == path


def test_resolve_relative_posix_relative_target(_posix_host):
    assert shapes.resolve_relative("/x/claude-klabauter", "scratch/t.txt") == (
        "/x/claude-klabauter/scratch/t.txt"
    )


def test_resolve_relative_posix_absolute_target(_posix_host):
    assert (
        shapes.resolve_relative("/x/claude-klabauter", "/x/other/t.txt")
        == "/x/other/t.txt"
    )


def test_resolve_relative_posix_never_returns_none(_posix_host):
    for target in ["/tmp/x", "/x/foo", "/c/Users/x", "relative/path.txt", ""]:  # abs-path-ok: same AC4 fixture set, synthetic
        assert shapes.resolve_relative("/x/claude-klabauter", target) is not None


# ─── AC6 -- untranslatable shapes on Windows yield None, never fail-closed ──


@pytest.mark.parametrize(
    "path",
    ["/tmp/x", "//server/share", "/cygdrive/c/x"],
)
def test_translate_msys_path_untranslatable_on_windows_returns_none(
    _windows_ntpath, path
):
    assert shapes.translate_msys_path(path) is None


@pytest.mark.parametrize(
    "target",
    ["/tmp/x", "//server/share", "/cygdrive/c/x"],
)
def test_resolve_relative_untranslatable_target_returns_none(_windows_ntpath, target):
    assert shapes.resolve_relative("/x/claude-klabauter", target) is None


def test_resolve_relative_untranslatable_base_returns_none(_windows_ntpath):
    """An untranslatable `base` must also drop the candidate -- `base` is
    translated too (see `resolve_relative`'s own docstring), and a target
    that resolves fine on its own must not paper over a base that doesn't."""
    assert shapes.resolve_relative("/tmp/wherever", "scratch/t.txt") is None
