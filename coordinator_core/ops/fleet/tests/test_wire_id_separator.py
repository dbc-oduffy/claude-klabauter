"""
Wire-id path-separator contract for the fleet.* archival ops.

Regression guard for the 2026-07-20 defect: every fleet.* op built its wire
``id`` with ``str(path.relative_to(root))``, which renders with ``os.sep``.  On
Windows that emitted ``cross-repo\\inbox\\x.md`` where the contract — and every
consumer, and git itself — speaks ``cross-repo/inbox/x.md``.  A wire value whose
shape depends on the producing host's OS is a contract defect, not a fixture nit.

``coordinator_core.ops.fleet._common.rel_id`` is now the single sanctioned
construction.  These tests are host-independent: they assert the POSIX shape
unconditionally, so they fail on Windows if the defect returns AND fail on POSIX
if someone re-introduces a backslash-producing construction.

Harness: plain sync tests + tmp_path; no pytest-asyncio dependency.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from coordinator_core.ops.fleet._common import rel_id


def test_rel_id_is_forward_slash_regardless_of_os_sep(tmp_path):
    """rel_id emits forward slashes on every host OS — never os.sep."""
    root = tmp_path / "repo"
    target = root / "cross-repo" / "inbox" / "2026-07-20-memo.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")

    got = rel_id(target, root)

    assert got == "cross-repo/inbox/2026-07-20-memo.md"
    assert "\\" not in got, f"native separator leaked into wire id: {got!r}"


def test_rel_id_round_trips_back_to_a_filesystem_path(tmp_path):
    """A POSIX wire id re-joined as ``root / cid`` resolves to the original file.

    This is the round-trip archive_plans / prune_bugs perform on the act path.
    pathlib accepts an embedded '/' on Windows too, so normalising the wire id
    does not break the consumers that rebuild a filesystem path from it.
    """
    root = tmp_path / "repo"
    target = root / "state" / "bug-backlog" / "2026-07-20-bug.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("status: closed\n", encoding="utf-8")

    cid = rel_id(target, root)

    assert (root / cid).resolve() == target.resolve()
    assert (root / cid).exists()


def test_rel_id_matches_git_own_path_shape(fleet_repo):
    """rel_id agrees byte-for-byte with the path shape git itself reports.

    git only ever speaks forward-slash paths (``ls-files``, ``--name-only``,
    ``--porcelain``), so an os.sep-rendered id could never be matched against
    git output on Windows — which is how this defect first surfaced.
    """
    bug_path = fleet_repo.seed_bug("2026-04-09-relid-shape.yaml", "closed")
    wire_id = rel_id(bug_path, fleet_repo.root)

    proc = subprocess.run(
        ["git", "ls-files"],
        cwd=str(fleet_repo.root),
        capture_output=True,
        check=True,
    )
    tracked = proc.stdout.decode(errors="replace").splitlines()

    assert wire_id in tracked, (
        f"rel_id {wire_id!r} does not match git's own path shape; tracked={tracked}"
    )


def test_no_fleet_module_builds_a_wire_id_with_native_separator():
    """Source-level guard: no fleet module reintroduces str(x.relative_to(y)).

    The defect class is a native separator leaking into a wire value.  rel_id()
    is the single sanctioned construction; this fails loudly if any fleet module
    hand-rolls the os.sep-rendering form again.
    """
    fleet_dir = Path(__file__).resolve().parent.parent
    offenders = []
    for py in sorted(fleet_dir.glob("*.py")):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            # Skip comments and docstring prose (rst literals are backticked).
            if stripped.startswith("#") or "``" in line:
                continue
            if "str(" in line and ".relative_to(" in line and ".as_posix()" not in line:
                offenders.append(f"{py.name}:{lineno}: {stripped}")

    assert not offenders, (
        "native-separator wire id construction found — use _common.rel_id():\n"
        + "\n".join(offenders)
    )


def test_os_sep_assumption_documented():
    """Sanity: on POSIX hosts os.sep is already '/', so the guards above are
    tautological there — they earn their keep on Windows.  Recorded explicitly
    so a future reader does not delete them as no-ops after reading a green
    Linux CI run."""
    assert os.sep in ("/", "\\")
