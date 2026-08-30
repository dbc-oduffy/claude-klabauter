"""Guard: every tracked file class whose CORRECTNESS depends on byte-exact
line endings carries an explicit `.gitattributes` pin, and carries the RIGHT
one.

Why this is scoped rather than blanket. A "every extension must be pinned"
guard would be noise: `.md`, `.py`, `.yaml`, `.json` and friends are read by
parsers that tolerate either ending, so pinning them buys nothing and the
failures would train readers to append exemptions without thinking. This guard
covers only the classes where a mangled ending is a real defect, not a
cosmetic one:

  - `*.sha`  — a recorded git SHA, compared byte-for-byte against a freshly
               resolved HEAD to decide whether a frozen review artifact is
               still current. A trailing "\\r" makes a recorded hash differ
               from the hash it records.
  - `*.diff` / `*.patch` — frozen unified diffs. CRLF-mangled, they no longer
               apply; `git apply` rejects them as a corrupt patch.
  - `*.sh`   — a CRLF shebang makes the kernel look for "/bin/sh\\r" and fail
               with `bad interpreter: ^M`, the standard Git-for-Windows /
               MinGit bash failure.
  - `*.cmd`  — the inverse case, and the reason the expected value is checked
               and not merely its presence: cmd.exe genuinely requires CRLF, so
               "pinned" is not the invariant — "pinned to the ending that
               class's interpreter needs" is. Without the value check, a
               red `*.sha` could be silenced by pinning it `eol=crlf`.
  - `*.ps1`  — CRLF for a reason unlike every other class here: not an
               interpreter requirement (PowerShell parses LF fine) but a
               byte-exact one. `gen-launcher-shim.py` writes the launcher
               twins `newline="\\r\\n"`, and
               `coordinator_core/test_bin_launcher_parity.py` compares their
               ON-DISK bytes against `render_ps1(...).replace("\\n", "\\r\\n")`.
               An LF checkout fails that guard for all 26 non-exempt
               launchers.

The forward half is `ACKNOWLEDGED_UNPINNED`: a census, taken 2026-08-03, of
every tracked extension deliberately left unpinned. A NEW extension entering
the repo matches nothing here and fails the guard, which forces the
correctness-affecting-or-not call to be made once, at the moment the class is
introduced, instead of silently joining the unpinned set the way `.sha` and
`.diff` did.

Source of truth is `git ls-files --eol`, whose `attr/` column is git's OWN
resolution of the attribute stack — so a nested `.gitattributes` that overrides
the repo-root pin for some subtree is caught, which grepping the root file for
a glob would miss.

Two halves, and the second is why the same command is read for its `i/` column
as well as its `attr/` one. Asserting a pin EXISTS with the right value says
nothing about whether the repo CONFORMS to it: a blob committed before its pin
landed keeps its original bytes forever, because `.gitattributes` governs
future normalization and renormalizes nothing retroactively. That is exactly
how this repo accumulated a split `.ps1` class (9 blobs stored CRLF against 21
stored LF) under a guard that only ever checked the rule's text. A rule nothing
checks compliance with is prose, not a gate.

That `.ps1` split is now closed, and how it was closed is the cautionary half.
The obvious repair — pin `eol=lf`, the ending 21 of the 30 already had — would
have made this module green while breaking `test_bin_launcher_parity.py` on
every fresh clone, because `git add --renormalize` rewrites the INDEX and
leaves the local worktree's CRLF bytes in place: the repairing session's own
run of both guards would have passed. `eol=crlf` closes the same split (`text`
normalizes to LF in the index in BOTH directions) while preserving the CRLF
checkout the parity guard byte-compares against.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

import pytest

from coordinator_core.win_portability import no_console_creationflags

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# extension -> the eol attribute every tracked file of that class must resolve.
CORRECTNESS_PINS: dict[str, str] = {
    ".sha": "text eol=lf",
    ".diff": "text eol=lf",
    ".patch": "text eol=lf",
    ".sh": "text eol=lf",
    ".cmd": "text eol=crlf",
    ".ps1": "text eol=crlf",
}

# Tracked extensions deliberately carrying no pin, each because a mangled line
# ending in that class is cosmetic rather than correctness-affecting: the
# consumer is a parser (Python, YAML, JSON, Markdown, TOML, JS) or a
# human, and every one of them reads LF and CRLF identically.
#
# `.ps1` used to be listed here as an acknowledged JUDGEMENT call. It has since
# moved to CORRECTNESS_PINS — the judgement was wrong, because the class's
# correctness constraint is not PowerShell's parser (which is indeed
# ending-agnostic) but the launcher-parity byte compare. See the module
# docstring.
ACKNOWLEDGED_UNPINNED: frozenset[str] = frozenset(
    {
        "",  # extensionless: hook shims, CLI entrypoints, LICENSE-likes
        ".allow",
        ".archived",
        # `coordinator_core/**/<module>.py.wip.<pid>.bak` — tool-written WIP
        # backups of a source file. Inert: nothing reads them, nothing compares
        # them byte-exactly. Classified unpinned rather than pinned because the
        # honest answer is that they should not be tracked at all — see the
        # bug-backlog entry on tracked scratch; pinning would dignify them.
        ".bak",
        ".bats",
        ".body",
        # `tasks/**/<name>.err` — captured stderr from a scratch run. `tasks/`
        # is swept ephemera by CLAUDE.md; read by humans, never byte-compared.
        ".err",
        # `setup/.percolate-identity.example` — a template/comment-only file,
        # human-read, never byte-compared.
        ".example",
        ".ini",
        ".js",
        ".json",
        ".jsonl",
        ".legacy-backup",
        ".lock",
        ".log",
        ".md",
        ".mjs",
        # `.structural-index/symbols.ndjson` — a generated index, parsed
        # line-wise by a JSON reader that strips trailing whitespace. Regenerated
        # wholesale, never patched or byte-compared.
        ".ndjson",
        # `setup/publish-targets.portable` — a `#`-commented config file,
        # parsed line-by-line and human-read, never byte-compared.
        ".portable",
        ".py",
        ".scm",
        ".toml",
        ".touch",
        ".tsv",
        ".txt",
        ".yaml",
        # Arrived with the mirrored CI harness preserved in 79a16ab05
        # (state/mirror-native-source/), after this census was taken. Cosmetic
        # like its `.yaml` sibling — the consumer is a YAML parser.
        ".yml",
    }
)

# What the `i/` column must read for a file resolving ANY `text` pin.
#
# Determined empirically rather than assumed, because the intuitive expectation
# is wrong: `git ls-files --eol` reports `i/lf` for all 395 tracked `*.cmd`
# files despite their `text eol=crlf` pin. `eol=` sets the CHECKOUT direction
# only — `text` normalizes to LF on the way INTO the object store in BOTH
# directions, so the index expectation is `i/lf` for an `eol=crlf` class just
# as much as for an `eol=lf` one. Asserting `i/crlf` for the `.cmd` class would
# have made this guard permanently red against a perfectly correct repo.
#
# `i/none` is conformant rather than an exemption: git reports it for a blob
# carrying no line terminator at all — a single-line `.sha` written without a
# trailing newline. There is no ending to mangle, so it cannot be the defect
# this guard exists to catch. The failure states are `i/crlf` (a CRLF blob that
# predates its pin, never renormalized) and `i/mixed`.
INDEX_CONFORMANT: frozenset[str] = frozenset({"i/lf", "i/none"})

# The `w/` column a conformant CHECKOUT produces, per pin direction — this is
# the half `eol=` actually governs. `w/none` joins each set for the same
# no-line-terminator reason as `i/none` above.
WORKTREE_CONFORMANT: dict[str, frozenset[str]] = {
    "text eol=lf": frozenset({"w/lf", "w/none"}),
    "text eol=crlf": frozenset({"w/crlf", "w/none"}),
}


class EolRow(NamedTuple):
    """One `git ls-files --eol` row.

    `index` / `worktree` carry git's `i/` and `w/` prefixes verbatim rather
    than stripped, so a value read out of a failure message can be grepped for
    in raw `git ls-files --eol` output without translation.
    """

    path: str
    index: str
    worktree: str
    attr: str


def _ls_files_eol() -> list[EolRow]:
    """Index EOL, worktree EOL, and resolved eol attribute per tracked file.

    The attribute is git's own resolution, read off `git ls-files --eol`'s
    `attr/` column, so nested `.gitattributes` overrides are reflected. The
    column is empty for a path no rule matches.

    Parsed by splitting on the TAB that separates metadata from the path, not
    by whitespace: the attribute value itself contains spaces ("text eol=lf"),
    and a tracked path may contain them too.
    """
    result = subprocess.run(
        ["git", "ls-files", "--eol"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return [_parse_eol_line(line) for line in result.stdout.splitlines() if "\t" in line]


def _parse_eol_line(line: str) -> EolRow:
    meta, path = line.split("\t", 1)
    index, worktree, *attr_parts = meta.split()
    return EolRow(
        path=path,
        index=index,
        worktree=worktree,
        attr=" ".join(attr_parts).removeprefix("attr/").strip(),
    )


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _pinned(rows: list[EolRow], ext: str, expected_attr: str) -> list[EolRow]:
    """Rows of `ext` that actually resolve `expected_attr`.

    Conformance is asserted only over files whose pin already resolves
    correctly. A file with a wrong or missing pin is the OTHER guard's
    offender; reporting it in both places would double-count one defect and
    bury the conformance signal in noise.
    """
    return [r for r in rows if _ext(r.path) == ext and r.attr == expected_attr]


def _index_nonconformant(rows: list[EolRow], ext: str, expected_attr: str) -> list[EolRow]:
    return sorted(
        (r for r in _pinned(rows, ext, expected_attr) if r.index not in INDEX_CONFORMANT),
        key=lambda r: r.path,
    )


def _worktree_divergent(rows: list[EolRow], ext: str, expected_attr: str) -> list[EolRow]:
    allowed = WORKTREE_CONFORMANT[expected_attr]
    return sorted(
        (r for r in _pinned(rows, ext, expected_attr) if r.worktree not in allowed),
        key=lambda r: r.path,
    )


def _cite(rows: list[EolRow], limit: int = 20) -> str:
    shown = "\n".join(f"  {r.path}  {r.index} {r.worktree}" for r in rows[:limit])
    if len(rows) > limit:
        shown += f"\n  ... and {len(rows) - limit} more"
    return shown


@pytest.fixture(scope="module")
def tracked() -> list[EolRow]:
    return _ls_files_eol()


@pytest.mark.parametrize("ext", sorted(CORRECTNESS_PINS), ids=sorted(CORRECTNESS_PINS))
def test_correctness_class_carries_its_required_eol_pin(ext, tracked):
    expected = CORRECTNESS_PINS[ext]
    offenders = sorted(
        (r.path, r.attr) for r in tracked if _ext(r.path) == ext and r.attr != expected
    )
    assert not offenders, (
        f"{len(offenders)} tracked '{ext}' file(s) do not resolve the required "
        f"line-ending pin '{expected}'. A wrong or missing pin here is "
        f"correctness-affecting, not cosmetic — see this module's docstring for "
        f"what breaks per class. Fix by adding/correcting the rule in "
        f".gitattributes (check for a nested .gitattributes overriding it), "
        f"then renormalizing the affected paths via "
        f"`coordinator/bin/coordinator-renormalize-index`.\n"
        + "\n".join(f"  {p} -> attr/{a or '(none)'}" for p, a in offenders[:20])
    )


@pytest.mark.parametrize("ext", sorted(CORRECTNESS_PINS), ids=sorted(CORRECTNESS_PINS))
def test_pinned_class_conforms_to_its_pin_in_the_index(ext, tracked):
    """The pin is obeyed by the stored blobs, not merely declared.

    This is the half the pin-value guard above cannot see. `.gitattributes`
    normalizes on the way in from here forward; it does not reach back into the
    object store, so a class pinned today can carry CRLF blobs committed
    yesterday indefinitely and every clone gets them. The index is what SHIPS —
    a nonconformant blob is wrong for every consumer on every platform, which
    is why this one gates rather than reports.
    """
    expected = CORRECTNESS_PINS[ext]
    offenders = _index_nonconformant(tracked, ext, expected)
    assert not offenders, (
        f"{len(offenders)} tracked '{ext}' file(s) resolve '{expected}' but their "
        f"INDEX blob does not conform: the stored bytes carry line endings the "
        f"pin forbids, which every clone of this repo inherits. A pin only "
        f"normalizes commits made after it landed — these blobs predate theirs.\n"
        f"Fix (from the repo root, worktree clean):\n"
        f"  git add --renormalize -- {' '.join(r.path for r in offenders[:5])}"
        + (" ..." if len(offenders) > 5 else "")
        + "\nthen commit the restaged blobs. Expected i/ one of "
        f"{sorted(INDEX_CONFORMANT)} — note `text eol=crlf` also normalizes to "
        f"i/lf in the index, since eol= governs checkout only.\n"
        + _cite(offenders)
    )


@pytest.mark.designed_red
@pytest.mark.parametrize("ext", sorted(CORRECTNESS_PINS), ids=sorted(CORRECTNESS_PINS))
def test_pinned_class_worktree_matches_its_checkout_direction(ext, tracked):
    """Worktree drift — reported, deliberately not gated.

    Why this is `designed_red` and the index assertion is not. A `w/` column
    that disagrees with its pin means THIS checkout is stale: the file was
    written to disk before the pin existed, or by a tool that bypassed the
    filter. Nothing is wrong with the repo — a fresh clone of the same commit
    materializes it correctly — so failing here would paint every stale
    worktree red for a defect that ships to nobody, and train readers to ignore
    the module. The index assertion above is the one that gates, because the
    index is what other clones actually receive.

    Its failure output is a per-machine worklist, in the marker's own terms:
    run this node explicitly to see what your checkout should be refreshed to.
    """
    expected = CORRECTNESS_PINS[ext]
    divergent = _worktree_divergent(tracked, ext, expected)
    paths = " ".join(r.path for r in divergent[:5]) + (" ..." if len(divergent) > 5 else "")
    assert not divergent, (
        f"{len(divergent)} tracked '{ext}' file(s) are pinned '{expected}' but "
        f"this WORKTREE holds a different ending. Local staleness only — the "
        f"index is conformant and other clones are unaffected — so this is a "
        f"refresh list, not a repo defect.\n"
        f"Fix (worktree clean, no unsaved edits to these paths) — the path must "
        f"be DELETED and restored, because git will not rewrite it in place: "
        f"`git checkout`, `git checkout-index -f` and a touch-then-checkout all "
        f"exit 0 and change nothing once the index stat already matches. "
        f"Verified, not assumed.\n"
        f'  python3 -c "import pathlib,subprocess,sys; '
        f"[pathlib.Path(p).unlink() for p in sys.argv[1:]]; "
        f'subprocess.check_call([\'git\',\'checkout-index\',\'--\',*sys.argv[1:]])" {paths}\n'
        f"Expected w/ one of {sorted(WORKTREE_CONFORMANT[expected])}.\n" + _cite(divergent),
    )


def test_no_unclassified_extension_joins_the_tracked_corpus(tracked):
    """A new tracked extension must be classified, not silently unpinned.

    This is the forward half of the guard: it is what stops the next `.sha`.
    """
    known = set(CORRECTNESS_PINS) | ACKNOWLEDGED_UNPINNED
    unclassified: dict[str, list[str]] = {}
    for row in tracked:
        ext = _ext(row.path)
        if ext not in known:
            unclassified.setdefault(ext, []).append(row.path)
    assert not unclassified, (
        "New tracked file extension(s) are classified neither as a "
        "line-ending correctness class nor as acknowledged-unpinned. Decide "
        "which: if a CRLF checkout would break how these files are consumed "
        "(byte-exact comparison, `git apply`, a shebang), add the extension to "
        "CORRECTNESS_PINS and pin it in .gitattributes; otherwise add it to "
        "ACKNOWLEDGED_UNPINNED.\n"
        + "\n".join(
            f"  {ext}: {len(paths)} file(s), e.g. {paths[0]}"
            for ext, paths in sorted(unclassified.items())
        )
    )


def test_acknowledged_unpinned_and_correctness_pins_are_disjoint():
    """An extension cannot be both pinned-for-correctness and acknowledged-unpinned.

    Guards against a future edit that adds a class to CORRECTNESS_PINS but
    leaves its old ACKNOWLEDGED_UNPINNED entry behind — harmless today, but it
    would make the classification ambiguous for the next reader.
    """
    overlap = set(CORRECTNESS_PINS) & ACKNOWLEDGED_UNPINNED
    assert not overlap, f"extensions classified twice: {sorted(overlap)}"


def test_parser_reads_attribute_values_containing_spaces():
    """Red-case for the parser itself, not for the repo's current state.

    `attr/text eol=lf` contains a space; a whitespace-split parse would read
    the attribute as "text" and every correctness assertion above would pass
    vacuously against a repo pinned to the WRONG ending.
    """
    row = _parse_eol_line("i/lf    w/crlf   attr/text eol=lf   \tstate/review-trail/diffs/x.diff")
    assert row.attr == "text eol=lf"
    assert row.path.endswith("x.diff")
    assert (row.index, row.worktree) == ("i/lf", "w/crlf")


def test_conformance_predicates_bite_on_a_synthetic_nonconformant_row():
    """Red-case for the conformance predicates themselves.

    A conformance check that silently matches nothing is worse than none — it
    reads as a passing gate. This pins the two failure shapes the live repo
    does not currently exhibit (so the assertions above cannot demonstrate
    them) plus the two near-misses that must NOT be flagged: `i/none`, and an
    `eol=crlf` class whose index is correctly `i/lf`.
    """
    rows = [
        EolRow("ok.diff", "i/lf", "w/lf", "text eol=lf"),
        EolRow("no-terminator.sha", "i/none", "w/none", "text eol=lf"),
        EolRow("stale-blob.diff", "i/crlf", "w/crlf", "text eol=lf"),
        EolRow("mangled.diff", "i/mixed", "w/mixed", "text eol=lf"),
        EolRow("unpinned.diff", "i/crlf", "w/crlf", ""),
        EolRow("launcher.cmd", "i/lf", "w/crlf", "text eol=crlf"),
        EolRow("stale-checkout.cmd", "i/lf", "w/lf", "text eol=crlf"),
    ]

    assert [r.path for r in _index_nonconformant(rows, ".diff", "text eol=lf")] == [
        "mangled.diff",
        "stale-blob.diff",
    ]
    # An eol=crlf class is index-conformant at i/lf, and a wrongly-pinned file
    # belongs to the pin guard, not this one.
    assert _index_nonconformant(rows, ".cmd", "text eol=crlf") == []

    assert [r.path for r in _worktree_divergent(rows, ".cmd", "text eol=crlf")] == [
        "stale-checkout.cmd"
    ]
    assert [r.path for r in _worktree_divergent(rows, ".diff", "text eol=lf")] == [
        "mangled.diff",
        "stale-blob.diff",
    ]


def test_every_pin_direction_has_a_worktree_expectation():
    """A new pin direction cannot silently skip the worktree report.

    `_worktree_divergent` indexes `WORKTREE_CONFORMANT` by the pin string; a
    direction added to `CORRECTNESS_PINS` alone would KeyError at runtime
    rather than fail with a legible message.
    """
    missing = sorted(set(CORRECTNESS_PINS.values()) - set(WORKTREE_CONFORMANT))
    assert not missing, (
        f"pin direction(s) {missing} appear in CORRECTNESS_PINS with no "
        f"WORKTREE_CONFORMANT entry — add the `w/` values a correct checkout "
        f"of that direction produces."
    )
