"""test_published_lib_layout.py — payload-shape assertion for chunk C4
(docs/plans/2026-08-08-the-engine-root-s-own-codename-ladder-an.md).

Why this exists: `cruft-sweep` and `identity-cli` locate their own helpers
RELATIVE to their own `__file__`, not via an import path resolved some other
way — so what matters for them is not whether `coordinator/lib` is published
(it always was, allowlist gap aside), but WHERE it lands relative to
`coordinator/bin` in the published mirror. `setup/publish-targets.portable`'s
`claude-klabauter-lib` row (row 5) publishes `coordinator/lib` to the mirror's
top-level `lib/` — a sibling of `coordinator/bin`, not of `coordinator/bin`'s
own parent `coordinator/` — so a CLI doing `<bin>/../lib` never finds it there.
C4 adds a SECOND row (`claude-klabauter-coordinator-lib`) publishing the same
source to `coordinator/lib` instead, so that lookup resolves.

This suite does not republish (no live mirror, no `publish.py` invocation) —
it builds a throwaway scratch tree shaped like the union of what row 6
(`claude-klabauter-coordinator-bin`, dest `coordinator/bin`) and the new row
(`claude-klabauter-coordinator-lib`, dest `coordinator/lib`) would place
side by side, using real file copies off this checkout's own source tree, and
asserts the two path computations `cruft-sweep` and `identity-cli` actually
run resolve inside it.

TWO prologue path computations, not one — a fix verified against only one
proves nothing (see coordinator/bin/cruft-sweep's own prologue, ~line 203 for
`_LIB_DIR` / cc_invoke, ~line 217 for `_COORDINATOR_LIB_DIR` / settings_home):

  - `<bin>/lib`      — `cruft-sweep`'s `_LIB_DIR`, resolving `cc_invoke.py`.
                        Already shipping via row 6's own `coordinator/bin/lib`
                        subtree — this suite asserts it did not regress.
  - `<bin>/../lib`   — `cruft-sweep`'s `_COORDINATOR_LIB_DIR` (settings_home)
                        and `identity-cli`'s `_LIB_DIR` (session/identity) —
                        the leg C4's new row actually fixes.

Spec backlink: docs/plans/2026-08-08-the-engine-root-s-own-codename-ladder-an.md
chunk C4.
"""
from __future__ import annotations

import os
import shutil

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)
_REPO_ROOT = os.path.dirname(_COORDINATOR_DIR)

_SOURCE_BIN_LIB_CC_INVOKE = os.path.join(_BIN_DIR, "lib", "cc_invoke.py")
_SOURCE_COORDINATOR_LIB = os.path.join(_COORDINATOR_DIR, "lib")
_SOURCE_SETTINGS_HOME = os.path.join(_SOURCE_COORDINATOR_LIB, "settings_home.py")
_SOURCE_SESSION_IDENTITY = os.path.join(
    _SOURCE_COORDINATOR_LIB, "session", "identity.py"
)


def _build_published_mirror(root: str) -> str:
    """Assemble a scratch tree shaped like <mirror>/coordinator/{bin,lib}
    after BOTH row 6 (claude-klabauter-coordinator-bin, dest coordinator/bin)
    and the new C4 row (claude-klabauter-coordinator-lib, dest coordinator/lib)
    have published — real file copies off this checkout's source tree, not
    fixtures, so a drift in the real files' existence fails this suite too.
    Returns the path to the mirror's `coordinator/bin/` directory.
    """
    mirror_bin = os.path.join(root, "coordinator", "bin")
    mirror_lib = os.path.join(root, "coordinator", "lib")

    # Row 6 payload slice: coordinator/bin/lib/cc_invoke.py (already shipping).
    os.makedirs(os.path.join(mirror_bin, "lib"), exist_ok=True)
    shutil.copy2(
        _SOURCE_BIN_LIB_CC_INVOKE,
        os.path.join(mirror_bin, "lib", "cc_invoke.py"),
    )
    # A stand-in for the CLI itself, so __file__-relative math has somewhere
    # real to compute from.
    open(os.path.join(mirror_bin, "cruft-sweep"), "w", encoding="utf-8").close()
    open(os.path.join(mirror_bin, "identity-cli"), "w", encoding="utf-8").close()

    # New C4 row payload slice: coordinator/lib/{settings_home.py,session/}.
    os.makedirs(os.path.join(mirror_lib, "session"), exist_ok=True)
    shutil.copy2(
        _SOURCE_SETTINGS_HOME,
        os.path.join(mirror_lib, "settings_home.py"),
    )
    shutil.copy2(
        _SOURCE_SESSION_IDENTITY,
        os.path.join(mirror_lib, "session", "identity.py"),
    )

    return mirror_bin


_PUBLISH_TARGETS = os.path.join(_REPO_ROOT, "setup", "publish-targets.portable")


def _rows():
    """Parse setup/publish-targets.portable into pipe-split field lists,
    skipping comments and blanks."""
    with open(_PUBLISH_TARGETS, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            yield line.split("|")


def test_a_row_publishes_coordinator_lib_to_coordinator_lib():
    """The layout assertions below build their own mirror shape, so they would
    all still pass if the C4 row were deleted from the config — they pin the
    premise, not the thing that produces it. This pins the producer: some row
    must publish source `coordinator/lib` to dest `coordinator/lib`, or the
    `<bin>/../lib` leg those tests exercise never exists on a real mirror."""
    matches = [r for r in _rows() if len(r) > 4 and r[3] == "coordinator/lib" and r[4] == "coordinator/lib"]
    assert matches, (
        "no publish-targets row maps source coordinator/lib -> dest coordinator/lib; "
        "the <bin>/../lib lookup asserted below would not resolve on a real mirror"
    )


def test_row_five_admits_percolate():
    """AC8, machine-checked rather than left to the row's own prose: `percolate`
    is one of coordinator/lib's five tracked top-level directories, and an
    allowlist omission is invisible to the rot guard (which only verifies that
    LISTED entries resolve, never that the list is complete)."""
    lib_rows = [r for r in _rows() if len(r) > 6 and r[3] == "coordinator/lib"]
    assert lib_rows, "expected at least one coordinator/lib publish row"
    for row in lib_rows:
        assert "percolate" in row[6].split(","), (
            f"row {row[0]!r} does not admit percolate/; the published mirror's own "
            "coordinator/bin/publish.py would have no lib/percolate/publish_sync.py"
        )


def test_source_fixtures_exist():
    """Sanity: the source-tree files this test copies from are real, so a
    failure below reflects the published LAYOUT, not a stale fixture path."""
    assert os.path.isfile(_SOURCE_BIN_LIB_CC_INVOKE)
    assert os.path.isfile(_SOURCE_SETTINGS_HOME)
    assert os.path.isfile(_SOURCE_SESSION_IDENTITY)


def test_cruft_sweep_bin_lib_resolves(tmp_path):
    """cruft-sweep's `_LIB_DIR = <bin>/lib` (cc_invoke) — already shipping via
    row 6, asserted here so a future edit cannot silently regress it while
    fixing the sibling leg below."""
    mirror_bin = _build_published_mirror(str(tmp_path))
    cli_path = os.path.join(mirror_bin, "cruft-sweep")

    lib_dir = os.path.join(os.path.dirname(os.path.abspath(cli_path)), "lib")
    assert os.path.isfile(os.path.join(lib_dir, "cc_invoke.py"))


def test_cruft_sweep_coordinator_lib_resolves(tmp_path):
    """cruft-sweep's `_COORDINATOR_LIB_DIR = <bin>/../lib` (settings_home) —
    the leg C4's new publish row fixes. This is AC5's critical assertion:
    a fix verified only against the `<bin>/lib` leg above proves nothing
    about this one."""
    mirror_bin = _build_published_mirror(str(tmp_path))
    cli_path = os.path.join(mirror_bin, "cruft-sweep")

    coordinator_lib_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(cli_path))), "lib"
    )
    assert os.path.isfile(os.path.join(coordinator_lib_dir, "settings_home.py"))


def test_identity_cli_lib_session_resolves(tmp_path):
    """identity-cli's `_LIB_DIR = <bin>/../lib/session` (session/identity) —
    same `<bin>/../lib` leg as above, distinct consumer, distinct target
    module (session/identity.py rather than settings_home.py)."""
    mirror_bin = _build_published_mirror(str(tmp_path))
    cli_path = os.path.join(mirror_bin, "identity-cli")

    lib_session_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(cli_path))),
        "lib",
        "session",
    )
    assert os.path.isfile(os.path.join(lib_session_dir, "identity.py"))
