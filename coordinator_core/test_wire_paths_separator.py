"""
Wire-id path-separator contract — repo-wide guard for the handoff/misc op surface.

Sibling of ``coordinator_core/ops/fleet/tests/test_wire_id_separator.py``, which
guards ``ops/fleet/`` only.  The same defect class spanned ``ops/handoff_*``,
``ops/records_query``, ``ops/changelog_ops``, ``ops/cruft_sweep``,
``distill/delete_guard``, ``percolate/guards``, ``percolate/rewrite_basename`` and
``session_ledger/aggregate_chain_loe``: every one built a repo-relative string with
``str(path.relative_to(root))``, which renders with ``os.sep``.  On Windows that put
``state\\handoffs\\x.md`` into JSON-RPC result fields, git pathspec arguments, an
``rg --fixed-strings`` guard needle, rendered changelog text, and a rename-manifest
lookup key — none of which speak the native separator.

``coordinator_core.wire_paths.rel_id`` is THE sanctioned construction.  These tests
are host-independent: they assert the POSIX shape unconditionally, so they fail on
Windows if the defect returns AND fail on POSIX if someone reintroduces a
backslash-producing construction.

Harness: plain sync tests + tmp_path; no pytest-asyncio dependency.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.wire_paths import rel_id


# Modules swept by the 2026-07-20 handoff/misc pass.  Paths are repo-relative from
# the coordinator_core package root.
_SWEPT_MODULES = (
    "ops/handoff_archive_transition.py",
    "ops/handoff_close_origin_stub.py",
    "ops/handoff_normalize.py",
    "ops/handoff_reconcile.py",
    "ops/handoff_repoint_origin.py",
    "ops/handoff_ship_archive.py",
    "ops/records_query.py",
    "ops/changelog_ops.py",
    "ops/cruft_sweep.py",
    "distill/delete_guard.py",
    "percolate/guards.py",
    "percolate/rewrite_basename.py",
    "session_ledger/aggregate_chain_loe.py",
)


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def test_rel_id_is_forward_slash_regardless_of_os_sep(tmp_path):
    """rel_id emits forward slashes on every host OS — never os.sep."""
    root = tmp_path / "repo"
    target = root / "state" / "handoffs" / "2026-07-20-baton.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    got = rel_id(target, root)

    assert got == "state/handoffs/2026-07-20-baton.md"
    assert "\\" not in got, f"native separator leaked into wire id: {got!r}"


def test_rel_id_round_trips_back_to_a_filesystem_path(tmp_path):
    """A POSIX wire id re-joined as ``root / cid`` resolves to the original file."""
    root = tmp_path / "repo"
    target = root / "docs" / "plans" / "2026-07-20-plan.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n", encoding="utf-8")

    cid = rel_id(target, root)

    assert (root / cid).resolve() == target.resolve()
    assert (root / cid).exists()


def test_fleet_common_reexports_the_single_implementation():
    """ops.fleet._common.rel_id must BE wire_paths.rel_id — not a second copy.

    The helper was promoted out of ``ops/fleet/_common`` so non-fleet modules could
    depend on it without importing a private module of that subpackage.  If someone
    re-adds a local definition in _common, this identity check fails and the "one
    construction" invariant is visibly broken rather than silently forked.
    """
    from coordinator_core.ops.fleet import _common

    assert _common.rel_id is rel_id


def test_no_swept_module_builds_a_wire_id_with_native_separator():
    """Source-level guard: none of the swept modules reintroduces str(x.relative_to(y)).

    The defect class is a native separator leaking into a value that crosses a
    process boundary. ``rel_id()`` is the single sanctioned construction; this fails
    loudly if any swept module hand-rolls the os.sep-rendering form again.
    """
    root = _package_root()
    offenders = []
    for relpath in _SWEPT_MODULES:
        py = root / relpath
        assert py.is_file(), f"swept module vanished — update _SWEPT_MODULES: {relpath}"
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # Skip comments and docstring prose (rst literals are backticked).
            if stripped.startswith("#") or "``" in line:
                continue
            if "str(" in line and ".relative_to(" in line and ".as_posix()" not in line:
                offenders.append(f"{relpath}:{lineno}: {stripped}")

    assert not offenders, (
        "native-separator wire id construction found — use coordinator_core.wire_paths.rel_id():\n"
        + "\n".join(offenders)
    )


def test_no_swept_module_interpolates_a_raw_relative_to_into_an_fstring():
    """Second shape of the same defect: ``f"...{p.relative_to(root)}..."``.

    ``str()`` is implicit inside an f-string, so the ``str(`` scan above misses it.
    This is exactly how ``handoff_archive_transition``'s archived-to message and
    ``records_query``'s PARSE_ERROR line emitted backslashes on Windows.
    """
    root = _package_root()
    offenders = []
    for relpath in _SWEPT_MODULES:
        py = root / relpath
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or "``" in line:
                continue
            if "{" in line and ".relative_to(" in line and ".as_posix()" not in line:
                offenders.append(f"{relpath}:{lineno}: {stripped}")

    assert not offenders, (
        "relative_to() interpolated into an f-string without .as_posix() — "
        "use coordinator_core.wire_paths.rel_id():\n" + "\n".join(offenders)
    )


def test_os_sep_assumption_documented():
    """On POSIX hosts os.sep is already '/', so the guards above are tautological
    there — they earn their keep on Windows.  Recorded explicitly so a future reader
    does not delete them as no-ops after reading a green Linux CI run."""
    assert os.sep in ("/", "\\")
