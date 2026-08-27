"""
Wire-id path-separator contract for the ceremony.* ops.

Sibling of ``coordinator_core/ops/fleet/tests/test_wire_id_separator.py`` — same
defect class, second surface.  The ceremony ops emitted repo-relative wire values
(``receipt_path``, record ``path``, ``acted[]`` ids, consumed-handoff ids) built as
``str(p.relative_to(root))`` or f-string-interpolated ``{p.relative_to(root)}``,
both of which render with ``os.sep``.  On Windows that produced
``state\\ceremony\\wsc\\<sid>.json`` where every consumer — and git — speaks
``state/ceremony/wsc/<sid>.json``.

``coordinator_core.ops.fleet._common.rel_id`` is the single sanctioned
construction fleet-wide; ceremony reuses it rather than growing a second helper
(the ceremony modules already import ``main_worktree_root`` from that module, so
the dependency direction is pre-existing, not new).

These tests are host-independent: they assert the POSIX shape unconditionally, so
they fail on Windows if the defect returns AND fail on POSIX if someone
re-introduces a native-separator construction.
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.ops.fleet._common import rel_id

CEREMONY_DIR = Path(__file__).resolve().parent.parent

# The modules this guard was written for.  Listed explicitly (rather than
# globbing) so a NEW ceremony module does not silently join the guarded set
# without a human noticing — see test_guarded_module_set_is_complete.
#
# wsc_commit.py / wsc_resolve.py were removed 2026-07-29 (kill-list op
# removal — both ops' JSON-RPC registrations had no live caller).
# wsc_resolve.py's engine (still live, imported by ceremony.session_instructions)
# survives as branch_resolution.py, which replaces it on this list; wsc_commit.py
# had no live consumer and was deleted outright, with no replacement entry.
#
# wsc_tail.py was removed 2026-08-23 in the eleven-op kill (c07062c99;
# state/kill-ledger.md, `ceremony.wsc_tail` — 150021ms against a 2000ms bar).
# No replacement entry: the op is a REBUILD CANDIDATE, not a rename, so there is
# no successor module to guard yet. Whatever rebuilds /workstream-complete's tail
# step joins this tuple deliberately, the same way any new ceremony module does.
GUARDED_MODULES = (
    "branch_resolution.py",
    "git_native.py",
    "records_query.py",
    "renderers.py",
    "resolver.py",
)


def _offending_lines(py: Path) -> list[str]:
    """Return ``name:lineno: src`` for every native-separator relative-path render.

    A wire value must be built with ``rel_id(path, root)`` (or, where the helper
    is not importable, an explicit ``.as_posix()``).  A bare ``.relative_to()``
    whose result is stringified — via ``str()`` or f-string interpolation —
    renders with ``os.sep`` and is the defect this guard exists to catch.

    Escape hatch for a genuinely filesystem-only use (a Path kept as a Path and
    never stringified): such a line still trips this guard, so append
    ``.as_posix()`` if it is a wire value, or add a ``# fs-only`` marker if it
    truly is not.  Silence it deliberately; never by weakening an assertion.
    """
    offenders = []
    for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        # Skip comments and docstring prose (rst literals are backticked).
        if stripped.startswith("#") or "``" in line or "# fs-only" in line:
            continue
        if ".relative_to(" not in line:
            continue
        if ".as_posix()" in line or "rel_id(" in line:
            continue
        offenders.append(f"{py.name}:{lineno}: {stripped}")
    return offenders


def test_no_ceremony_module_builds_a_wire_id_with_native_separator():
    """Source-level guard: no ceremony module hand-rolls an os.sep-rendered id."""
    offenders: list[str] = []
    for name in GUARDED_MODULES:
        offenders.extend(_offending_lines(CEREMONY_DIR / name))

    assert not offenders, (
        "native-separator relative-path render found in a ceremony module — use "
        "coordinator_core.ops.fleet._common.rel_id():\n" + "\n".join(offenders)
    )


def test_guarded_module_set_is_complete():
    """Every guarded module exists, and no ceremony module escapes the sweep.

    The explicit GUARDED_MODULES tuple is the scoped set from the 2026-07-20 fix.
    This asserts the whole package is in fact clean, so a module added later
    cannot reintroduce the defect just by not being on the list.
    """
    for name in GUARDED_MODULES:
        assert (CEREMONY_DIR / name).is_file(), f"guarded module missing: {name}"

    offenders: list[str] = []
    for py in sorted(CEREMONY_DIR.glob("*.py")):
        offenders.extend(_offending_lines(py))

    assert not offenders, (
        "native-separator relative-path render found in an UNGUARDED ceremony "
        "module — fix it and add it to GUARDED_MODULES:\n" + "\n".join(offenders)
    )


def test_receipt_path_wire_value_is_posix_and_round_trips(tmp_path):
    """``receipt_path`` (wsc_resolve/wsc_commit/wsc_tail) is POSIX AND re-joinable.

    All three phase results emit ``receipt_path`` as a repo-relative string.
    Anything that turns it back into a filesystem path does so as
    ``worktree_root / receipt_path``; pathlib accepts an embedded '/' on Windows
    too, so normalising the wire value does not break that round trip.  Pinned
    here because the round trip is the reason a reader might be tempted to
    "restore" the native separator.
    """
    root = tmp_path / "repo"
    receipt = root / "state" / "ceremony" / "wsc" / "s-2026-07-20-000000.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}", encoding="utf-8")

    wire = rel_id(receipt, root)

    assert wire == "state/ceremony/wsc/s-2026-07-20-000000.json"
    assert "\\" not in wire, f"native separator leaked into receipt_path: {wire!r}"
    assert (root / wire).resolve() == receipt.resolve()
    assert (root / wire).exists()


def test_nested_record_path_wire_value_is_posix(tmp_path):
    """Multi-segment ids (archive/handoffs/<yyyy-mm>/x.md) are the failure shape.

    A single-segment relative path is separator-agnostic and would hide the
    defect; the ceremony record scanners (records_query, renderers, resolver) all
    emit multi-segment ids, so the guard uses one.
    """
    root = tmp_path / "repo"
    target = root / "archive" / "handoffs" / "2026-07" / "2026-07-20_090000_x.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\n---\n", encoding="utf-8")

    assert rel_id(target, root) == "archive/handoffs/2026-07/2026-07-20_090000_x.md"


def test_os_sep_assumption_documented():
    """Sanity: on POSIX hosts os.sep is already '/', so the guards above are
    tautological there — they earn their keep on Windows.  Recorded explicitly so
    a future reader does not delete them as no-ops after a green Linux CI run."""
    assert os.sep in ("/", "\\")
