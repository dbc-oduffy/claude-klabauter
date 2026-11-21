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
from pathlib import Path
from typing import NamedTuple

import pytest


# Whole-tree `git ls-files --eol` costs seconds, so every test reading the
# `tracked` fixture is cadence; the attribute-file parser and tmp_path fixtures
# stay in the fast tier.
cadence = pytest.mark.cadence

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CATCH_ALL = "* text=auto eol=lf"

# Classes whose checkout ending is CRLF by contract, as `.gitattributes` globs.
CRLF_GLOBS: tuple[str, ...] = ("*.cmd", "*.bat", "*.ps1")

_ISOLATED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "LC_ALL": "C",
}

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
        ".bak",
        ".bats",
        ".body",
        ".building",
        ".c",  # compiler reads either ending
        ".cjwsnswh",  # tracked atomic-write temp in state/warp run artifacts; text, ending-agnostic
        ".java",
        ".pre-ruling",
        ".reds",  # tracked red-set audit lists under state/audits; line-oriented text
        ".stale-missing-prompts",
        ".tmp",  # tracked research entry drafts under docs/research; plain text
        ".err",
        ".example",
        ".exe",  # binary; git detects it, no conversion applies
        ".h",
        ".ini",
        ".jpg",  # binary
        ".js",
        ".json",
        ".jsonl",
        ".legacy-backup",
        ".lock",
        ".log",
        ".md",
        ".mjs",
        ".ndjson",
        ".out",
        ".output",
        ".portable",
        ".py",
        ".scm",
        ".stderr",
        ".toml",
        ".touch",
        ".ts",
        ".tsv",
        ".txt",
        ".yaml",
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

    path: str
    index: str
    worktree: str
    attr: str


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


def test_acknowledged_unpinned_and_correctness_pins_are_disjoint():
    """An extension cannot be both pinned-for-correctness and acknowledged-unpinned.

    Guards against a future edit that adds a class to CORRECTNESS_PINS but
    leaves its old ACKNOWLEDGED_UNPINNED entry behind — harmless today, but it
    would make the classification ambiguous for the next reader.
    """
    overlap = set(CORRECTNESS_PINS) & ACKNOWLEDGED_UNPINNED
    assert not overlap, f"extensions classified twice: {sorted(overlap)}"


def test_parser_reads_attribute_values_containing_spaces():
    row = _parse_eol_line("i/lf    w/crlf   attr/text eol=lf   \tstate/review-trail/diffs/x.diff")
    assert row.attr == "text eol=lf"
    assert row.path.endswith("x.diff")
    assert (row.index, row.worktree) == ("i/lf", "w/crlf")


def test_conformance_predicates_bite_on_a_synthetic_nonconformant_row():
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


def check_gitattributes(text: str) -> list[str]:
    """Failures of a `.gitattributes` body against the fleet EOL policy.

    Trap: later rules win, so the catch-all must be the FIRST rule line or it
    would override the class pins that follow it.
    """
    rules = [
        line.split() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")
    ]
    failures: list[str] = []
    if not rules or " ".join(rules[0]) != CATCH_ALL:
        failures.append(f"first rule line is not the catch-all `{CATCH_ALL}`")
    for glob in CRLF_GLOBS:
        if not any(r[0] == glob and "eol=crlf" in r[1:] for r in rules):
            failures.append(f"missing `{glob} text eol=crlf` pin")
    return failures


def test_repo_gitattributes_carries_catch_all_first_and_crlf_pins():
    body = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert check_gitattributes(body) == []


def test_gitattributes_check_names_a_missing_catch_all_and_a_missing_pin():
    body = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8")
    without_catch_all = "\n".join(line for line in body.splitlines() if line != CATCH_ALL)
    assert any(CATCH_ALL in f for f in check_gitattributes(without_catch_all))

    catch_all_late = "*.cmd text eol=crlf\n" + CATCH_ALL + "\n"
    failures = check_gitattributes(catch_all_late)
    assert any("first rule line" in f for f in failures)
    assert any("*.bat" in f for f in failures) and any("*.ps1" in f for f in failures)
    assert not any("*.cmd" in f for f in failures)


def scan_rows(rows: list[EolRow]) -> list[str]:
    """Whole-tree violations: CRLF held in the index of a non-`-text` file, or
    a text file with no attribute declaration at all."""
    bad: list[str] = []
    for r in rows:
        if r.index in ("i/crlf", "i/mixed") and r.attr != "-text":
            bad.append(f"{r.path}  {r.index} attr/{r.attr or '(none)'}")
        elif r.index == "i/lf" and not r.attr:
            bad.append(f"{r.path}  {r.index} attr/(none)")
    return sorted(bad)


def test_scan_rows_flags_missing_attribute_and_crlf_blob_but_spares_text_off():
    rows = [
        EolRow("a.yaml", "i/lf", "w/lf", "text=auto eol=lf"),
        EolRow("no-attr.yaml", "i/lf", "w/lf", ""),
        EolRow("crlf.txt", "i/crlf", "w/crlf", "text=auto eol=lf"),
        EolRow("golden.txt", "i/crlf", "w/crlf", "-text"),
        EolRow("pic.png", "i/-text", "w/-text", "-text"),
    ]
    assert [b.split()[0] for b in scan_rows(rows)] == ["crlf.txt", "no-attr.yaml"]
