"""
coordinator_core.warm.door.tests.test_stdin_reading_entrypoint_population —
the mechanical derivation of "which allowlisted entrypoints read stdin".

Purpose: C2's parity test needs a set of names to compare against the static
name table `door_core.c` hardcodes — this module derives that set from live
inputs (the committed allowlist plus each name's resolved `.py` body) rather
than the twelve names the plan's own Problem section lists today, which are
today's ANSWER, not the rule (per this chunk's own dispatch brief). A test
helper, not a script, because C2 imports it directly.

What this derivation IS: a one-level, static, TEXTUAL scan of each resolved
entrypoint's own source text for one of three spellings that reach
`sys.stdin`: `sys.stdin` itself, `from sys import stdin`, and
`argparse.FileType('-')`. "One-level" and "textual" are exact, not modest —
this does NOT parse an AST, does NOT resolve an import graph, and does NOT
execute anything. A read reached only through an imported helper (e.g. a
shared `_read_stdin_payload()` in another module) is OUT OF THIS SCAN'S
REACH and will not enrol the caller. A match inside a docstring, a comment,
or a dead branch enrols the name exactly as if it were live code — this scan
cannot tell the difference, and a name enrolled that way stays enrolled until
someone hand-excludes it (see `HAND_EXCLUDED_NAMES` below).

What this derivation is NOT: a claim that the returned set is complete or
that every member's match is a live, reachable stdin read. C2's parity test
is the falsifier for "is the static table in door_core.c still true of the
tree" — this module only proposes the set that check runs against.

Cross-repo consequence (staff-eng finding 5, EM-accepted): each allowlisted
name is resolved to a `.py` body under the CONFIGURED bin roots —
`repos.claude_klabauter` and `repos.doe_claude` in the machine-local
registry (`coordinator_core.machine_resolver.registry_get`), both SIBLING
WORKING TREES, not this repo. Neither this module nor C2's parity test can
be made to pass or fail by a claude-klabauter commit alone: an edit landed only in
klabauter's or DoE-claude's own `coordinator/bin/` changes this derivation's
answer with no corresponding commit here. GREEN IS THE DANGEROUS POLARITY: a
newly stdin-reading DoE entrypoint whose body this derivation cannot resolve
(bin root unset, tree absent, or the file missing) silently drops off the
returned set rather than failing loud — `resolve_entrypoint_body` reports an
unresolved name as such (see `UNRESOLVED_REASON_NO_BIN_ROOT` /
`UNRESOLVED_REASON_FILE_ABSENT`) so a caller CAN surface it, but this
module's own test only asserts the derivation runs and returns a non-empty
set — it does not, and cannot, assert every name in the allowlist resolved.

Negative-spec: this module does NOT hardcode the twelve names the plan's
Problem section names as today's known-broken population. Hardcoding them
as the definition of the set is exactly the mistake this chunk's dispatch
brief forbids — they are today's answer, not the rule.

Spec backlink: docs/plans/2026-09-12-warm-door-drops-stdin-for-every-entrypoint.md, chunk C1
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.machine_resolver import registry_get

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ALLOWLIST_PATH = _REPO_ROOT / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"

#: The two machine-local registry keys naming the sibling working trees this
#: derivation resolves entrypoint bodies under. Neither is this repo -- see
#: the module docstring's Cross-repo consequence section.
_BIN_ROOT_REGISTRY_KEYS = ("repos.claude_klabauter", "repos.doe_claude")

#: Hand-maintained exclusion the derivation honours (a name enrolled by a
#: docstring/comment/dead-branch match, or otherwise a known false positive
#: this textual scan cannot itself tell apart from a live read). Empty
#: today -- add a name here only with a citation of WHY the match is not a
#: live stdin read; `explain_population` still prints an excluded name's
#: matched spelling so a false enrolment stays diagnosable without
#: re-running the regex by hand.
HAND_EXCLUDED_NAMES: frozenset = frozenset()

#: Per-spelling patterns this scan matches against a body's raw text, in
#: report order. Keys are the human-readable spelling name surfaced in
#: `explain_population`'s per-name diagnosis; values are the compiled
#: pattern. `sys.stdin` and `from sys import stdin` are Python's two import
#: shapes for reaching the same stream; `argparse.FileType('-')` is the
#: argparse idiom that opens stdin without ever spelling `sys.stdin`.
_STDIN_SPELLING_PATTERNS: "dict[str, re.Pattern]" = {
    "sys.stdin": re.compile(r"\bsys\.stdin\b"),
    "from sys import stdin": re.compile(r"from\s+sys\s+import\s+[^\n]*\bstdin\b"),
    "argparse.FileType('-')": re.compile(r"argparse\.FileType\(\s*['\"]-['\"]\s*\)"),
}

UNRESOLVED_REASON_NO_BIN_ROOT = "no_bin_root_configured"
UNRESOLVED_REASON_FILE_ABSENT = "file_absent_under_every_configured_bin_root"


@dataclass(frozen=True)
class EntrypointDiagnosis:
    """Per-name derivation outcome. `matched_spelling` is None when the body
    resolved but no pattern matched; `unresolved_reason` is set (and
    `matched_spelling` is None) when no configured bin root has this name's
    `.py` body at all."""

    name: str
    resolved_path: "Optional[Path]"
    matched_spelling: "Optional[str]"
    unresolved_reason: "Optional[str]"
    hand_excluded: bool


def load_allowlisted_names() -> "frozenset[str]":
    """The union of `entrypoints` and `door_eligible_entrypoints` from the
    committed allowlist -- the population this derivation classifies."""
    data = json.loads(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    names = set(data.get("entrypoints", ())) | set(data.get("door_eligible_entrypoints", ()))
    return frozenset(names)


def configured_bin_roots() -> "list[Path]":
    """The `coordinator/bin` directories under every configured sibling
    working tree, in registry-key order, filtered to directories that exist
    on this machine. An unset registry key or an absent tree is silently
    skipped here -- callers see the consequence per-name via
    `UNRESOLVED_REASON_NO_BIN_ROOT` / `UNRESOLVED_REASON_FILE_ABSENT`."""
    roots = []
    for key in _BIN_ROOT_REGISTRY_KEYS:
        raw = registry_get(key)
        if not raw:
            continue
        candidate = Path(raw) / "coordinator" / "bin"
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def resolve_entrypoint_body(name: str, bin_roots: "list[Path]") -> "Optional[Path]":
    """The first `<root>/<name>.py` that exists, in `bin_roots` order, or
    None when no configured root has it."""
    for root in bin_roots:
        candidate = root / f"{name}.py"
        if candidate.is_file():
            return candidate
    return None


def diagnose_entrypoint(name: str, bin_roots: "list[Path]") -> EntrypointDiagnosis:
    """The full per-name derivation: resolve the body, scan it for one of
    the known stdin spellings, and record enough to diagnose a false
    enrolment (or a silent non-resolution) without re-running the regex by
    hand."""
    resolved = resolve_entrypoint_body(name, bin_roots)
    if resolved is None:
        reason = UNRESOLVED_REASON_NO_BIN_ROOT if not bin_roots else UNRESOLVED_REASON_FILE_ABSENT
        return EntrypointDiagnosis(
            name=name,
            resolved_path=None,
            matched_spelling=None,
            unresolved_reason=reason,
            hand_excluded=name in HAND_EXCLUDED_NAMES,
        )

    text = resolved.read_text(encoding="utf-8", errors="replace")
    matched_spelling = None
    for spelling, pattern in _STDIN_SPELLING_PATTERNS.items():
        if pattern.search(text):
            matched_spelling = spelling
            break

    return EntrypointDiagnosis(
        name=name,
        resolved_path=resolved,
        matched_spelling=matched_spelling,
        unresolved_reason=None,
        hand_excluded=name in HAND_EXCLUDED_NAMES,
    )


def explain_population() -> "dict[str, EntrypointDiagnosis]":
    """Every allowlisted name's diagnosis, keyed by name -- the full
    per-name report C2 (or a human) can read to see WHICH spelling matched,
    or why a name did not resolve, without re-deriving anything."""
    bin_roots = configured_bin_roots()
    return {name: diagnose_entrypoint(name, bin_roots) for name in load_allowlisted_names()}


def derive_stdin_reading_entrypoints() -> "frozenset[str]":
    """The set this chunk exists to produce: every allowlisted name whose
    resolved body matched a stdin spelling AND is not hand-excluded. C2's
    parity test compares this against the static name table in
    `door_core.c`; this module does not hardcode that comparison itself."""
    diagnoses = explain_population()
    return frozenset(
        d.name for d in diagnoses.values() if d.matched_spelling is not None and not d.hand_excluded
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


# Every test below is a READ-ONLY parity oracle against the live sibling
# working trees (`repos.claude_klabauter` / `repos.doe_claude`), the exact
# shape `coordinator_core/conftest.py`'s `_quarantine_real_home` docstring
# names as the sanctioned use of `@pytest.mark.real_home`: this suite's
# autouse home quarantine redirects every registry read into an empty
# per-test tmpdir, which would make `configured_bin_roots()` resolve to
# nothing on every box regardless of what is actually configured there.
# Nothing here WRITES -- see that fixture's own docstring for why the
# marker is scoped to read-only oracles.


@pytest.mark.real_home
def test_derivation_runs_and_returns_non_empty_set():
    """The derivation runs end-to-end over the live allowlist and configured
    bin roots and returns at least one name. This test does NOT assert the
    set equals any particular list of names (see module docstring's
    Negative-spec) -- C2's parity test is where the set is compared against
    the static table.

    Skips (rather than fails) when this machine has neither configured bin
    root available -- per `no-single-machine-assumptions`, a box carrying
    neither sibling clone is a portability fact, not a derivation defect."""
    if not configured_bin_roots():
        pytest.skip(
            "neither repos.claude_klabauter nor repos.doe_claude resolves to an "
            "existing coordinator/bin on this machine"
        )
    result = derive_stdin_reading_entrypoints()
    assert isinstance(result, frozenset)
    assert len(result) > 0, (
        "derive_stdin_reading_entrypoints() returned an empty set with at least one "
        "bin root configured -- no allowlisted name's resolved body matched a known "
        "stdin spelling"
    )


@pytest.mark.real_home
def test_every_returned_name_is_allowlisted():
    """Every name the derivation enrols is a member of the allowlist union
    it started from -- the derivation classifies the allowlist, it does not
    invent names outside it."""
    allowlisted = load_allowlisted_names()
    result = derive_stdin_reading_entrypoints()
    assert result <= allowlisted


@pytest.mark.real_home
def test_every_returned_name_has_a_diagnosable_matched_spelling():
    """Per-name diagnosability (staff-eng finding 5): every enrolled name's
    diagnosis records WHICH spelling matched, so a false enrolment is
    diagnosable without re-running the regex by hand."""
    diagnoses = explain_population()
    result = derive_stdin_reading_entrypoints()
    for name in result:
        diagnosis = diagnoses[name]
        assert diagnosis.matched_spelling in _STDIN_SPELLING_PATTERNS
        assert diagnosis.resolved_path is not None


@pytest.mark.real_home
def test_hand_excluded_names_are_never_in_the_derived_set():
    """The exclusion hatch actually excludes -- a name in
    `HAND_EXCLUDED_NAMES` is never in the derived set even if its body
    matches a spelling, and its diagnosis still records the match (not
    silently, as an UNPROBEABLE-style reason)."""
    diagnoses = explain_population()
    result = derive_stdin_reading_entrypoints()
    for name in HAND_EXCLUDED_NAMES:
        assert name not in result
        assert diagnoses[name].hand_excluded is True
