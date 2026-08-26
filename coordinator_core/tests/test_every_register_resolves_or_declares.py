"""The single derived sweep enrolling the 45 CORE registers (C4).

`docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md` requires
one derived test rather than a hand-edit per register: read the population, and assert every
row of every core register resolves against its declared class (AC7), with the sweep's own
staleness legs guarding population enrolment (AC8) and a canary guarding the sweep's own
derivation (AC2/AC7).

POPULATION. Row resolution runs over `_CORE_45()`, parsed at test-collection time from the
already-committed `state/audits/2026-08-26-the-core-register-inventory.md` -- the 45
`RegisterId`s and their classifier-predicted subject class. Re-deriving that split is out of
bounds for this chunk (§ SCOPE in the C4 chunk body); this module only reads it.

STALENESS (AC8, leg 1 + leg 2). `register_population.json` records a byte-level snapshot of
EVERY module-level uppercase collection constant across the census roots (`coordinator/tests/`
and `coordinator_core/`), not only the core 45 -- this is the leg that notices a brand-new
register-bearing file by existence alone, before anyone classifies it core or outlier.
  - Leg 1 (`_discover_candidate_ids`, ~200ms on the real corpus): a byte-level regex scan, no
    AST. Catches a NEW register-bearing file.
  - Leg 2 (`test_leg2_population_artifact_matches_current_candidates`, ~15ms, CADENCE-marked):
    compares the fresh leg-1 scan against the frozen snapshot recorded in the JSON artifact. A
    mismatch in EITHER direction (new candidate appeared, or a recorded one vanished) means the
    artifact is stale and must be regenerated -- see `_REGENERATE_COMMAND` below, printed
    verbatim in the failure message. It is cadence-marked for noise, not cost: it compares
    against the whole corpus, so on a shared branch any peer's register edit reddens it for
    every other session. The test's own docstring carries the full reasoning.
  - Leg 2's integrity half (`test_leg2_population_artifact_is_internally_consistent`) stays in
    the FAST tier: it reads only the artifact, so no peer can redden it, and it catches the one
    thing regeneration cannot fix -- an artifact that was hand-edited rather than derived.

ROW RESOLUTION (AC1, AC7). Every core-45 row falls into one of two buckets:
  - PATH rows (`repo-path` / `bare-filename`) are self-identifying by shape -- a leaf containing
    "/" or ending ".py" needs no adjacent declaration; `_classify_path_leaf` derives its class
    directly, and `register_rows.resolve_row` answers against the shared tracked-file index.
  - DOTTED rows (`module` / `symbol`) are NOT self-identifying -- "coordinator_core.ops.check_x"
    could name either shape, and the spike's whole finding was that guessing from string shape
    alone is a ~5%-precision disaster (see the C4 chunk body). So a dotted row's register MUST
    declare its class at an adjacent module-level constant, `<REGISTER_NAME>__SUBJECT_CLASS`, a
    plain string literal equal to `"module"` or `"symbol"`, living in the SAME file as the
    register (never a central table -- see the plan's § The declaration site: a central
    register-of-registers table is prohibited because it ages exactly like the thing it exists
    to replace). `_find_adjacent_declaration` reads it by AST.

    A register-shaped constant holding dotted rows with NO adjacent declaration is RED by
    design (AC7's own words) -- this is expected, not a bug, for however many of the 6 known
    dotted registers have not yet had `C5` add their declaration. C5's `writes:` scope names
    exactly those six modules for this reason.

EXEMPTIONS. `_EXEMPTION_CLASSES` is a CLOSED vocabulary (modelled on `_EXEMPTION_CLASSES` in
`test_no_unbatched_per_item_git_spawn.py`): a register may only be exempted from row resolution
under one of these named reasons, never a bare skip. `_EXEMPTED_REGISTERS` holds exactly ONE
entry -- `_SCHEMA_PARAMS` in `coordinator_core/ops/tests/test_queue_parity.py`, under
`input-fixture-not-a-subject` -- and the constant's own comment carries the evidence for it. Do
NOT add an entry here to absorb an undispositioned sweep failure; that inverts the mechanism the
plan exists to build (see the chunk body's own anti-scope line). The discriminator is whether the
register was READ and its rows established as something other than references: `_SCHEMA_PARAMS`
was, and the entry records why. A register added here to turn a red run green has not been.

OPAQUE AGES TOO. `SubjectClass.OPAQUE` rows are, by `register_rows.resolve_row`, always reported
`unadjudicable` without inspecting the string -- so an opaque CLAIM could silently rot into a
LIE if the underlying subject started looking exactly like a resolvable path or dotted symbol.
`_assert_opaque_rows_have_not_aged_out` asserts the CRITERION instead of trusting the claim: it
force-classifies each opaque row's subject under every tractable class and fails if ANY of them
would now resolve. One core-45 register declares `opaque` today:
`_EXPECTED_CALLEE_BY_SUBCOMMAND` in `coordinator_core/test_backlog_grind_assemble.py`, which C5
declared after the inventory's predicted `symbol` proved wrong. So
`test_no_opaque_registers_have_aged_out_of_their_claim` now runs over real rows rather than
passing vacuously -- if any of that register's subjects ever starts resolving as a tractable
class, the opaque claim has aged out and must be re-declared rather than quietly kept.

CANARY (AC2, "reads as THE OLD VALUE"). `_CANARY` is asserted present in `_CORE_45()`'s ids.
Ruled by the EM 2026-08-26 (see the dispatch brief for this chunk, "EM RULING -- the canary set,
resolving the `_EXEMPT_SITES` blocker"): the plan body named `_EXEMPT_SITES` as a canary member,
but no `_EXEMPT_SITES` register is present in EITHER inventory under
`(coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py, _EXEMPT_SITES)` -- it is a
`set[tuple[str, str, str]]`, never enrolled by the census's string-leaf collection shape. The
two `_EXEMPT_SITES` rows that DO appear in the outlier inventory are a DIFFERENT module each
(`test_no_hardcoded_paths.py`, `test_session_dir_has_one_constructor.py`) -- precisely the
bare-name collision `RegisterId` keying exists to prevent. Do not "fix" this back in by widening
population derivation to enrol tuple-valued registers; the committed inventory is the frozen
AC7/AC9 baseline. The canary is therefore three members, not two:

  1. `_ORACLE_CLAIMS` (18 rows, `repo-path`) -- the plan's own still-valid canary member.
  2. `EXCLUDED_PATHS` in `test_no_direct_retired_root_env_reads.py` (13 rows, `repo-path`) --
     large and stable; no chunk of this plan writes that module.
  3. `COLD_PATH_MODULES` in `coordinator/tests/test_cold_path_remediation_is_runnable.py` --
     the WRONG-ROOT discriminator. The core 45 span two roots (38 under `coordinator_core/`, 3
     under `coordinator/`), so a derivation that only walked `coordinator_core/` would return 38
     healthy-looking registers and read green -- exactly the absence-reads-as-THE-OLD-VALUE
     failure this canary exists to catch, and only a canary member on the far side of the root
     boundary discriminates it. `CORE_TRACKED_FILES` (also under `coordinator/`) is excluded
     because C2 converts it to a derivation and it may carry no literal string leaves after that
     lands; `ENGINELESS_SAFE_TRAMPOLINES` is the smaller of the two remaining `coordinator/`
     registers, so `COLD_PATH_MODULES` is the pick.

  This canary set is ITSELF hand-maintained -- the very shape this whole plan is about. The
  trade is correct because a STALE CANARY fails CLOSED (a human notices the assertion citing a
  RegisterId that no longer exists) while a STALE DERIVATION fails OPEN (silently returns a
  smaller-but-still-plausible population and nobody notices). See the plan's own § The two
  readings.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from coordinator_core.tests.register_rows import (
    RegisterId,
    Resolution,
    Row,
    SubjectClass,
    TrackedFileIndex,
    assert_canary_present,
    resolve_row,
    rows_that_do_not_resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

_CORE_INVENTORY_PATH = REPO_ROOT / "state/audits/2026-08-26-the-core-register-inventory.md"
_POPULATION_ARTIFACT_PATH = Path(__file__).resolve().parent / "register_population.json"

_REGENERATE_COMMAND = (
    "python -m coordinator_core.tests.test_every_register_resolves_or_declares --regenerate"
)

#: The census roots this sweep's byte-level candidate-discovery leg walks. Identical to the
#: roots the original AST census used (§ The measured population in the plan body).
_CANDIDATE_ROOTS: tuple[str, ...] = ("coordinator/tests", "coordinator_core")

#: The closed vocabulary a register's exemption reason must name. Modelled on
#: `_EXEMPTION_CLASSES` in `test_no_unbatched_per_item_git_spawn.py`: a small, named, CLOSED set
#: -- never a bare skip.
_EXEMPTION_CLASSES: frozenset[str] = frozenset(
    {
        # A register whose rows are INPUT fixtures -- adversarial commands, realistic-command
        # samples, sink payloads -- that were never meant to name a real subject on disk.
        "input-fixture-not-a-subject",
        # A register naming a subject that lives in a SIBLING repo, not this one, so the
        # tracked-file index built from this repo's own `git ls-files` can never resolve it.
        "cross-repo-subject",
    }
)

#: Exactly one core-45 register claims an exemption. Do NOT add an entry here to absorb an
#: undispositioned sweep failure -- see the module docstring's EXEMPTIONS section. The entry
#: below is a DISPOSITION, not an absorption: the register was read, its rows were established
#: as fixture payloads rather than references, and the claim is typed from the closed vocabulary.
_EXEMPTED_REGISTERS: dict[RegisterId, str] = {
    # `_SCHEMA_PARAMS` is a dict of queue-append FIELD PAYLOADS, one per schema, sitting under
    # its own comment header "Schema-specific test fixtures". Its string leaves are values fed
    # INTO the op under test -- `source: daily-review/2026-01-15`, a `memo:` path, a `surface:`
    # path -- never references to subjects this repo is expected to hold. Two of them
    # (`daily-review/2026-01-15`, `cross-repo/archive/2026-01-15-example-memo.md`) are
    # deliberately synthetic and must never resolve; a third
    # (`coordinator_core/ops/queue_append.py`) happens to name a real file, but it is a fixture
    # value like the rest, not a guarded reference -- which is why the exemption is taken at
    # register level rather than per row.
    #
    # AC9 NOTE: the committed inventory predicted this register path-only, needing no
    # declaration. That prediction was wrong, and this is the SECOND classifier mismatch in the
    # core 45 (the first: `_EXPECTED_CALLEE_BY_SUBCOMMAND` needed `opaque` over the predicted
    # `symbol`). Two of 45 is the published mismatch count -- low enough that the five-class
    # vocabulary stands, per the plan's own falsifier reading.
    RegisterId(
        "coordinator_core/ops/tests/test_queue_parity.py", "_SCHEMA_PARAMS"
    ): "input-fixture-not-a-subject",
}

#: AC2/AC7's canary -- see the module docstring's CANARY section for the full ruling.
_CANARY: frozenset[RegisterId] = frozenset(
    {
        RegisterId("coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py", "_ORACLE_CLAIMS"),
        RegisterId("coordinator_core/tests/test_no_direct_retired_root_env_reads.py", "EXCLUDED_PATHS"),
        RegisterId("coordinator/tests/test_cold_path_remediation_is_runnable.py", "COLD_PATH_MODULES"),
    }
)

#: What a green run of THIS sweep does and does not establish (C6, the baton's last AC). A
#: citation site naming this module as its enforcement mechanism must publish this qualifier in
#: its own citing block -- mirrors `COVERAGE_HORIZON` / `_HORIZON_CITERS` in
#: `test_no_unbatched_per_item_git_spawn.py`, and
#: `test_the_resolution_horizon_is_published_where_this_sweep_is_cited` below plays the role
#: `test_the_one_hop_horizon_is_published_where_this_gate_is_cited` plays there.
#: Deliberately a triple-quoted string, not a parenthesized concatenation: this module's own
#: leg-1 candidate regex (`_CANDIDATE_LINE_RE`) fires on any module-level uppercase name whose
#: FIRST line opens with `frozenset(`/`dict(`/`(`/`[`/`{` -- a bookkeeping string that happened to
#: start with an open paren would falsely enrol itself as a new register candidate and desync
#: `register_population.json`, an artifact outside this chunk's `writes:` scope to regenerate.
COVERAGE_HORIZON = """resolves-or-declares-horizon: a green run of this sweep establishes only \
that every ENROLLED row's subject exists against its declared class. It does NOT establish that \
the derived population is the right population, that an exempted register \
(_EXEMPTED_REGISTERS) is legitimately exempt, or that a row still describes real debt rather \
than merely resolving. Nor does it close residual regex recall-from-birth: leg 2 \
(test_leg2_population_artifact_matches_current_candidates) only catches drift against its OWN \
frozen snapshot -- a register the leg-1 regex never recognized as a candidate in the first place \
stays invisible to both legs (see test_leg1_regex_matches_the_ast_census_once_at_land, which \
guards new recall gaps but cannot retroactively close ones already present)."""

#: Closed list of citation sites that lean on this sweep as their derivation/enforcement
#: mechanism -- each must carry the `_HORIZON_MARKER` in the same citing block that names this
#: module, so a green run here is never read as "nothing to guard" at the site that cites it. This
#: register is ITSELF hand-maintained, the exact recursion `_HORIZON_CITERS` in
#: `test_no_unbatched_per_item_git_spawn.py` already carries -- see that module's own citer note.
#: `tuple(...)` wrapping (not a bare `(`-opening literal), for the same leg-1-candidate-recall
#: reason `COVERAGE_HORIZON` above is a triple-quoted string rather than a parenthesized one: this
#: is bookkeeping FOR the sweep, not a register the sweep enrols, and the AST census
#: (`test_leg1_regex_matches_the_ast_census_once_at_land`) does not recognize a bare `tuple(...)`
#: call as a collection literal either, so this stays consistent across both legs rather than
#: opening a real leg-1/AST divergence.
_HORIZON_CITERS: tuple[str, ...] = tuple(
    [
        "coordinator/tests/test_dr084_single_accessor_guard.py",
        "coordinator_core/tests/test_known_sites_rows_resolve_or_report_depth.py",
    ]
)

#: The marker every citer's block must carry alongside this file's name.
_HORIZON_MARKER = "resolves-or-declares-horizon"

_INVENTORY_LINE_RE = re.compile(r"^([A-Za-z0-9_./-]+\.py)::(\w+)$")
_INVENTORY_ROWS_RE = re.compile(r"rows=\s*(\d+)\s+predicted_class=(\S+)")

_PATH_LEAF_RE = re.compile(r"[A-Za-z0-9_./-]+")
_DOTTED_LEAF_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

#: Register-shaped module-level assignment, byte-level (leg 1). Anchored at column 0 so it only
#: matches a module-level (not indented/nested) constant. Covers every shape the corpus actually
#: uses: plain assign, `AnnAssign`, `frozenset(...)`/`dict(...)` calls, a dict literal, and the
#: opening line of a multi-line tuple (`_KNOWN_SITES`, `_EXEMPTION_CLASSES` are `Call` nodes, so
#: their opening line is `NAME = frozenset(` / `NAME: T = frozenset(`, already matched below).
_CANDIDATE_LINE_RE = re.compile(
    r"^(_*[A-Z][A-Z0-9_]*)\s*(?::[^=\n]+)?=\s*(?:frozenset\(|dict\(|\(|\[|\{)"
)


@dataclass(frozen=True)
class CoreRegister:
    """One row of `state/audits/2026-08-26-the-core-register-inventory.md`."""

    register: RegisterId
    row_count: int
    predicted_class: str


def _parse_core_inventory(path: Path) -> tuple[CoreRegister, ...]:
    """Parse the committed core-45 inventory. Never re-derives the core/outlier split.

    `state/audits/2026-08-26-the-core-register-inventory.md` is a plan-scoped artifact this
    chunk may only READ (it is not in C4's `writes:` list) -- the 45-register population and its
    per-register predicted class are frozen there before this sweep exists, precisely so AC9's
    later mismatch count has a baseline to compare against.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[CoreRegister] = []
    i = 0
    while i < len(lines) - 1:
        m = _INVENTORY_LINE_RE.match(lines[i].strip())
        if m:
            m2 = _INVENTORY_ROWS_RE.match(lines[i + 1].strip())
            if m2:
                out.append(
                    CoreRegister(
                        register=RegisterId(m.group(1), m.group(2)),
                        row_count=int(m2.group(1)),
                        predicted_class=m2.group(2),
                    )
                )
        i += 1
    return tuple(out)


def _core_45() -> tuple[CoreRegister, ...]:
    return _parse_core_inventory(_CORE_INVENTORY_PATH)


def _is_subject_leaf(value: str) -> bool:
    """The subject-shape filter: is this string leaf a candidate register-row subject at all?

    Mirrors the census's own filter closely enough for this sweep's purposes: no whitespace, no
    prose punctuation, and either path-shaped (contains "/" or ends ".py", but not a bare
    trailing-slash directory fragment) or dotted-identifier-shaped (>= 2 segments). This is a
    faithful reimplementation, not a byte-identical replica of the original private census --
    minor edge-case divergence (e.g. a placeholder example path inside an otherwise-path-typed
    register) is exactly the kind of first-run finding C5 is scoped to disposition, not a defect
    in this sweep.
    """
    if not value or not _PATH_LEAF_RE.fullmatch(value):
        return False
    if value.endswith(".py"):
        return True
    if "/" in value and not value.endswith("/"):
        return True
    if _DOTTED_LEAF_RE.fullmatch(value):
        return True
    return False


def _classify_path_leaf(value: str) -> SubjectClass:
    """A path-shaped leaf is self-identifying -- no adjacent declaration needed (AC7)."""
    return SubjectClass.REPO_PATH if "/" in value else SubjectClass.BARE_FILENAME


def _find_first_assignment_value(tree: ast.Module, name: str) -> ast.expr | None:
    """The FIRST module-level assignment to `name`, not the last.

    Deliberately first, not last: `_TARGETS` in `test_hot_path_hook_import_budget.py` is later
    re-bound as `_TARGETS = tuple(_TARGETS) + _query_bin_targets()` to append a dynamically
    computed tail -- that augmenting rebind carries no literal string leaves of its own, and
    walking it instead of the original literal loses every row the register actually declares.
    """
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == name:
                return stmt.value
    return None


def _extract_row_subjects(path: str, name: str) -> list[str] | None:
    """AST-parse `path` and pull every subject-shaped string leaf out of `name`'s value.

    Returns `None` if `name` is not assigned at module level in `path` at all -- a register the
    inventory names but the source no longer defines, which the caller reports as its own
    failure rather than silently skipping.
    """
    file_path = REPO_ROOT / path
    if not file_path.is_file():
        return None
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    node = _find_first_assignment_value(tree, name)
    if node is None:
        return None
    leaves = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return [leaf for leaf in leaves if _is_subject_leaf(leaf)]


def _find_adjacent_declaration(path: str, register_name: str) -> SubjectClass | None:
    """Read `<register_name>__SUBJECT_CLASS` from `path`'s own module, by AST.

    This is the declaration convention AC7 requires: co-located with the register it describes
    (never a central table), a plain string constant equal to `"module"` or `"symbol"`. Returns
    `None` if no such constant exists -- the caller treats that as the RED, undeclared case.
    """
    file_path = REPO_ROOT / path
    if not file_path.is_file():
        return None
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    declaration_name = f"{register_name}__SUBJECT_CLASS"
    value = _find_first_assignment_value(tree, declaration_name)
    if value is None or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
        return None
    try:
        return SubjectClass(value.value)
    except ValueError:
        return None


def _build_rows_for_register(core: CoreRegister) -> tuple[list[Row], list[str]]:
    """Build every resolvable `Row` for one core register, plus any declaration-gap errors.

    Path-shaped leaves self-classify. Dotted-shaped leaves require the adjacent
    `<name>__SUBJECT_CLASS` declaration; its absence is reported as an error string rather than
    silently dropping the row, so a register with zero declared dotted rows still shows up in
    the sweep's failure output.
    """
    subjects = _extract_row_subjects(*core.register)
    if subjects is None:
        return [], [f"{core.register}: register not found in source (inventory is stale)"]

    rows: list[Row] = []
    errors: list[str] = []
    declared_dotted_class: SubjectClass | None = None
    dotted_leaves = [s for s in subjects if not (s.endswith(".py") or "/" in s)]
    if dotted_leaves:
        declared_dotted_class = _find_adjacent_declaration(*core.register)
        if declared_dotted_class is None:
            errors.append(
                f"{core.register}: {len(dotted_leaves)} dotted row(s) but no adjacent "
                f"`{core.register.constant_name}__SUBJECT_CLASS` declaration ({_REGENERATE_COMMAND} "
                "does not fix this -- add the declaration to the register's own module)"
            )

    for subject in subjects:
        if subject.endswith(".py") or "/" in subject:
            declared = _classify_path_leaf(subject)
        elif declared_dotted_class is not None:
            declared = declared_dotted_class
        else:
            continue  # already reported above as a declaration-gap error
        rows.append(Row(register=core.register, subject=subject, declared_class=declared))

    return rows, errors


def _discover_candidate_ids(roots: tuple[str, ...], repo_root: Path) -> frozenset[RegisterId]:
    """Leg 1: byte-level, no-AST candidate discovery over the census roots.

    Regex-scans every `test_*.py` file under `roots` for a module-level register-shaped
    assignment line. This is deliberately cheap and approximate -- it is what makes a brand new
    register-bearing file enrolled BY EXISTENCE the moment it lands, at ~200ms over the real
    corpus, rather than requiring anyone to remember to re-run a full AST census.
    """
    candidates: set[RegisterId] = set()
    for root_name in roots:
        root = repo_root / root_name
        if not root.is_dir():
            continue
        for file_path in root.rglob("test_*.py"):
            relpath = file_path.relative_to(repo_root).as_posix()
            text = file_path.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                m = _CANDIDATE_LINE_RE.match(line)
                if m:
                    candidates.add(RegisterId(relpath, m.group(1)))
    return frozenset(candidates)


def _candidate_ids_hash(ids: frozenset[RegisterId]) -> str:
    canonical = json.dumps(sorted(list(rid) for rid in ids), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_population_artifact() -> dict:
    with _POPULATION_ARTIFACT_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _stored_candidate_ids(artifact: dict) -> frozenset[RegisterId]:
    return frozenset(RegisterId(p, n) for p, n in artifact["candidate_ids"])


def _regenerate_population_artifact(repo_root: Path = REPO_ROOT) -> dict:
    """Byte-deterministic regeneration: same corpus in, same JSON bytes out.

    Sorted candidate list plus its sha256 -- no timestamps, no machine-specific data. Whoever
    lands a NEW register-bearing file (or deletes one) runs `_REGENERATE_COMMAND` and commits the
    regenerated artifact in the SAME change that added or removed the register; this sweep's
    leg 2 is what makes forgetting that loud instead of silent.
    """
    ids = _discover_candidate_ids(_CANDIDATE_ROOTS, repo_root)
    artifact = {
        "schema": 1,
        "generator": _REGENERATE_COMMAND,
        "candidate_roots": list(_CANDIDATE_ROOTS),
        "candidate_ids": sorted([rid.repo_relative_path, rid.constant_name] for rid in ids),
        "candidate_ids_sha256": _candidate_ids_hash(ids),
    }
    return artifact


def _assert_opaque_rows_have_not_aged_out(
    rows: list[Row], index: TrackedFileIndex, repo_root: Path
) -> None:
    """The CRITERION behind an `opaque` claim, not the claim itself.

    `register_rows.resolve_row` reports every `OPAQUE` row `unadjudicable` unconditionally --
    it never actually checks whether the subject WOULD resolve under a tractable class. So an
    opaque declaration can silently go stale the moment its subject starts looking exactly like
    a real path or dotted symbol. This force-classifies each opaque row's subject under every
    tractable class and fails loudly the moment one of them would now resolve -- the "assert the
    criterion, not the claim" move from
    `state/lessons/2026-08-06-a-guard-whose-assertion-is-a-hand-mainta-1557963b78cf.yaml`.
    """
    aged_out: list[str] = []
    for row in rows:
        if row.declared_class is not SubjectClass.OPAQUE:
            continue
        candidate_classes = [SubjectClass.REPO_PATH, SubjectClass.BARE_FILENAME]
        if _DOTTED_LEAF_RE.fullmatch(row.subject):
            candidate_classes.append(SubjectClass.MODULE)
        for forced_class in candidate_classes:
            forced_row = Row(register=row.register, subject=row.subject, declared_class=forced_class)
            resolution = resolve_row(forced_row, index, repo_root)
            if resolution.resolved:
                aged_out.append(
                    f"{row.register}: opaque subject {row.subject!r} now resolves as "
                    f"{forced_class.value} -- the opaque claim has aged out and must be re-declared"
                )
                break
    if aged_out:
        raise AssertionError("\n".join(aged_out))


# ---------------------------------------------------------------------------
# AC8 -- staleness legs (population enrolment)
# ---------------------------------------------------------------------------


def test_leg1_candidate_discovery_finds_the_committed_core_registers() -> None:
    """Sanity: leg 1's own regex actually recognizes every core-45 register's shape."""
    discovered = _discover_candidate_ids(_CANDIDATE_ROOTS, REPO_ROOT)
    missing = [core.register for core in _core_45() if core.register not in discovered]
    assert not missing, (
        f"leg-1 byte-level discovery missed {len(missing)} committed core register(s): "
        f"{missing!r} -- the candidate-line regex has a recall gap"
    )


@pytest.mark.cadence
def test_leg2_population_artifact_matches_current_candidates() -> None:
    """AC8 leg 2: the frozen artifact's candidate snapshot vs. a fresh leg-1 scan.

    A mismatch in EITHER direction is loud: a candidate present now but missing from the
    artifact means a new register-bearing file landed and nobody regenerated; a candidate in the
    artifact but absent now means one vanished (which is also this sweep's canary surface: see
    `test_canary_registers_are_present_in_the_derived_population`).

    CADENCE-MARKED, and the reason is not cost -- this leg is ~15ms. It compares against the
    WHOLE corpus, so on a shared branch any peer adding or removing a register anywhere under
    the census roots reddens it for every other session, on a change they did not make. That was
    observed twice in one execution session. A gate that reddens on other people's work is one
    the fleet learns to regenerate past without reading, and the inherited anti-scope from
    `docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md` names that training effect as
    a reason not to ship a noisy gate -- it would be this workstream's own defect class, one
    level up.

    What this does NOT trade away: enrolment-by-existence still fires, just at cadence gates
    rather than per-commit. The window in which a brand-new register sits unenrolled is one
    cadence gate wide, and nothing depends on that register's rows being swept before then. The
    artifact's own integrity check is deliberately NOT here -- see
    `test_leg2_population_artifact_is_internally_consistent`, which stays in the fast tier
    because it cannot be reddened by anyone else's work.
    """
    artifact = _load_population_artifact()
    stored = _stored_candidate_ids(artifact)
    current = _discover_candidate_ids(_CANDIDATE_ROOTS, REPO_ROOT)

    new_candidates = current - stored
    vanished_candidates = stored - current

    assert not new_candidates and not vanished_candidates, (
        "register_population.json is stale relative to the corpus "
        f"(new: {sorted(new_candidates)!r}, vanished: {sorted(vanished_candidates)!r}). "
        f"Regenerate with: {_REGENERATE_COMMAND}"
    )


def test_leg2_population_artifact_is_internally_consistent() -> None:
    """The artifact's recorded hash matches its own recorded candidate list.

    FAST TIER, deliberately, while its sibling above is cadence-marked. This assertion reads
    only the artifact -- never the corpus -- so no peer's register edit can redden it. It
    catches the one failure regeneration cannot fix and staleness does not imply: an artifact
    someone hand-edited instead of regenerating, which would otherwise let a hand-written
    candidate list masquerade as a derived one.
    """
    artifact = _load_population_artifact()
    stored = _stored_candidate_ids(artifact)

    assert artifact["candidate_ids_sha256"] == _candidate_ids_hash(stored), (
        "register_population.json's recorded hash does not match its own recorded candidate "
        f"list -- the artifact was hand-edited rather than regenerated. Regenerate with: "
        f"{_REGENERATE_COMMAND}"
    )


@pytest.mark.parametrize(
    "shape_name,source",
    [
        ("plain-assign", '_A_NEW_REGISTER = ("some/repo/path.py",)\n'),
        ("ann-assign", '_A_NEW_REGISTER: tuple[str, ...] = ("some/repo/path.py",)\n'),
        ("frozenset-call", '_A_NEW_REGISTER = frozenset({"some/repo/path.py"})\n'),
        ("dict-call", '_A_NEW_REGISTER = dict(a="some/repo/path.py")\n'),
        ("dict-literal", '_A_NEW_REGISTER = {"a": "some/repo/path.py"}\n'),
        (
            "multiline-tuple",
            "_A_NEW_REGISTER = (\n"
            '    "some/repo/path.py",\n'
            '    "another/repo/path.py",\n'
            ")\n",
        ),
    ],
)
def test_leg1_notices_an_organic_write_for_every_register_shape(
    tmp_path: Path, shape_name: str, source: str
) -> None:
    """Leg 1 must fire on a REAL write, not a synthesized mtime/hash.

    Per `state/lessons/2026-08-08-a-freshness-probe-must-be-able-to-observ-46aa6003edfd.yaml`: a
    test that drives the staleness branch with a hand-set stamp proves the branch works, not
    that it fires on the write it exists to catch. So this writes an actual new file into a
    synthetic `coordinator_core/tests/` root for each register shape actually present in the
    corpus (`_KNOWN_SITES` and `_EXEMPTION_CLASSES` are `Call` nodes, not literals -- the
    frozenset/dict-call cases cover that), and asserts the SAME discovery function used by the
    real sweep notices it.
    """
    synthetic_root = tmp_path / "coordinator_core"
    tests_dir = synthetic_root / "tests"
    tests_dir.mkdir(parents=True)
    new_file = tests_dir / f"test_organic_{shape_name.replace('-', '_')}.py"
    new_file.write_text(source, encoding="utf-8")

    discovered = _discover_candidate_ids(("coordinator_core",), tmp_path)
    expected = RegisterId(f"coordinator_core/tests/{new_file.name}", "_A_NEW_REGISTER")
    assert expected in discovered, (
        f"leg-1 discovery did not notice an organic write of shape {shape_name!r}: {new_file}"
    )


def test_leg1_regex_matches_the_ast_census_once_at_land() -> None:
    """Cross-check leg 1's byte regex against a true AST census, over the real corpus.

    Any file the AST finds a module-level uppercase collection constant in, that the regex
    missed, is a RECALL bug in leg 1 -- a register that would never have been enrolled in the
    first place (RECALL-FROM-BIRTH), which the organic-write parametrization above cannot catch
    because it only exercises shapes the regex already recognizes. Residual regex recall is
    named as a fourth non-claim in C6; this check exists so any NEW gap is caught here rather
    than silently joining that residual.
    """
    regex_found = _discover_candidate_ids(_CANDIDATE_ROOTS, REPO_ROOT)

    ast_found: set[RegisterId] = set()
    for root_name in _CANDIDATE_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for file_path in root.rglob("test_*.py"):
            relpath = file_path.relative_to(REPO_ROOT).as_posix()
            try:
                tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for stmt in tree.body:
                names: list[str] = []
                value: ast.expr | None = None
                if isinstance(stmt, ast.Assign):
                    names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                    value = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names = [stmt.target.id]
                    # An annotation with no assignment (`X: tuple[str, ...]`) has no value at
                    # all; it declares a name rather than binding a register.
                    value = stmt.value
                if value is None:
                    continue
                for name in names:
                    if not (name.lstrip("_").isupper() and name.lstrip("_")[:1].isalpha()):
                        continue
                    is_collection_literal = isinstance(value, (ast.Tuple, ast.List, ast.Dict, ast.Set))
                    is_collection_call = (
                        isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id in {"frozenset", "dict"}
                    )
                    if not (is_collection_literal or is_collection_call):
                        continue
                    leaves = [
                        n.value
                        for n in ast.walk(value)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                    ]
                    if any(_is_subject_leaf(leaf) for leaf in leaves):
                        ast_found.add(RegisterId(relpath, name))

    missed = ast_found - regex_found
    assert not missed, (
        f"leg-1 regex missed {len(missed)} register(s) a true AST census finds: {sorted(missed)!r} "
        "-- this is RECALL-FROM-BIRTH (see the module docstring's STALENESS section) and must be "
        "fixed in `_CANDIDATE_LINE_RE`, not exempted"
    )


# ---------------------------------------------------------------------------
# AC2 / AC7 -- the canary
# ---------------------------------------------------------------------------


def test_canary_registers_are_present_in_the_derived_population() -> None:
    """AC2's reads-as-THE-OLD-VALUE check, over the classified core population.

    See the module docstring's CANARY section for why these three, and why `_EXEMPT_SITES` is
    NOT among them despite the plan body naming it.
    """
    derived_ids = frozenset(core.register for core in _core_45())
    assert_canary_present(derived_ids, _CANARY)


def test_canary_members_are_actually_declared_core_by_the_committed_inventory() -> None:
    """A canary member that silently drifted out of the committed core-45 file would defeat
    the canary without anyone noticing -- pin membership at the inventory-parsing layer too."""
    for register in _CANARY:
        assert register.repo_relative_path.startswith(("coordinator/", "coordinator_core/")), (
            f"canary member {register!r} is outside the census roots"
        )


# ---------------------------------------------------------------------------
# C6 -- the resolution horizon is published where this sweep is cited
# ---------------------------------------------------------------------------


def test_the_resolution_horizon_is_published_where_this_sweep_is_cited() -> None:
    """C6's own AC: a green run of THIS sweep must not read as "nothing to guard" at any site
    that cites it as a derivation/enforcement mechanism. Same shape as
    `test_the_one_hop_horizon_is_published_where_this_gate_is_cited` in
    `test_no_unbatched_per_item_git_spawn.py`: a CLOSED citer list, scoped to the citing BLOCK
    (blank-line-delimited) rather than the whole file, so the qualifier cannot silently drift out
    of the bullet that actually names this module while surviving elsewhere in the citer.

    Deliberately an assertion, not prose: a whole-file substring check passes vacuously the
    moment the citing block loses its qualifier while the marker text lingers in some unrelated
    section of the same file."""
    this_file_name = Path(__file__).name
    missing: list[str] = []
    for rel in _HORIZON_CITERS:
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"{rel} -- listed citer does not exist")
            continue
        blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
        citing = [block for block in blocks if this_file_name in block]
        if not citing:
            missing.append(f"{rel} -- no longer cites this sweep; drop it from _HORIZON_CITERS")
        elif not any(_HORIZON_MARKER in block for block in citing):
            missing.append(
                f"{rel} -- cites this sweep without naming the {_HORIZON_MARKER} horizon"
            )
    assert not missing, (
        "the sweep's resolution horizon is not published where it is cited:\n"
        + "\n".join(f"  {row}" for row in missing)
        + f"\n\nhorizon: {COVERAGE_HORIZON}"
    )


# ---------------------------------------------------------------------------
# AC7 -- every core-45 row resolves against its declared class
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _index() -> TrackedFileIndex:
    return TrackedFileIndex.build(REPO_ROOT)


def test_every_core_register_row_resolves_or_declares(_index: TrackedFileIndex) -> None:
    """The sweep's own centrepiece: every row of every core-45 register resolves against its
    declared class, or the register is a named, closed-vocabulary exemption.

    Failures are collected across ALL 45 registers before asserting once, so a single run's
    output is the full disposition list C5 consumes -- not one register at a time.
    """
    declaration_errors: list[str] = []
    dead_rows: list[str] = []

    for core in _core_45():
        if core.register in _EXEMPTED_REGISTERS:
            exemption = _EXEMPTED_REGISTERS[core.register]
            assert exemption in _EXEMPTION_CLASSES, (
                f"{core.register}: exemption {exemption!r} is not in the closed "
                f"_EXEMPTION_CLASSES vocabulary {sorted(_EXEMPTION_CLASSES)!r}"
            )
            continue

        rows, errors = _build_rows_for_register(core)
        declaration_errors.extend(errors)
        if not rows:
            continue

        dead = rows_that_do_not_resolve(rows, _index, REPO_ROOT)
        for row, resolution in dead:
            dead_rows.append(f"{row.register}: {row.subject!r} -- {resolution.detail}")

        _assert_opaque_rows_have_not_aged_out(rows, _index, REPO_ROOT)

    failures = declaration_errors + dead_rows
    assert not failures, (
        f"{len(declaration_errors)} declaration-gap failure(s), {len(dead_rows)} dead-row "
        "failure(s) across the core-45 sweep. Each is C5's to disposition (fixed, re-pointed, "
        "or exempted under a named _EXEMPTION_CLASSES reason) -- do not widen "
        "_EXEMPTED_REGISTERS to make this pass:\n" + "\n".join(failures)
    )


def test_no_opaque_registers_have_aged_out_of_their_claim(_index: TrackedFileIndex) -> None:
    """Runs the opaque-criterion check standalone so it is visible even if the main sweep test
    above is skipped or filtered out of a run. Vacuous today (no core-45 register declares
    `opaque`) -- see the module docstring's OPAQUE AGES TOO section for why that is not the same
    as this check being a no-op."""
    all_rows: list[Row] = []
    for core in _core_45():
        if core.register in _EXEMPTED_REGISTERS:
            continue
        rows, _errors = _build_rows_for_register(core)
        all_rows.extend(rows)
    _assert_opaque_rows_have_not_aged_out(all_rows, _index, REPO_ROOT)


def test_opaque_ages_out_helper_actually_detects_a_resolving_subject(
    _index: TrackedFileIndex,
) -> None:
    """Unit-level proof that `_assert_opaque_rows_have_not_aged_out` is a real check, not a
    vacuously-passing shape: a synthetic opaque row naming a subject that DOES exist on disk
    must be flagged as aged-out, and one naming a subject that plainly does not resolve must not
    be."""
    live_path = "coordinator_core/tests/register_rows.py"
    assert (REPO_ROOT / live_path).is_file()

    aged_out_row = Row(
        register=RegisterId("synthetic.py", "_SYNTHETIC"),
        subject=live_path,
        declared_class=SubjectClass.OPAQUE,
    )
    with pytest.raises(AssertionError, match="aged out"):
        _assert_opaque_rows_have_not_aged_out([aged_out_row], _index, REPO_ROOT)

    genuinely_opaque_row = Row(
        register=RegisterId("synthetic.py", "_SYNTHETIC"),
        subject="totally not a path or symbol shape at all!!",
        declared_class=SubjectClass.OPAQUE,
    )
    _assert_opaque_rows_have_not_aged_out([genuinely_opaque_row], _index, REPO_ROOT)


# ---------------------------------------------------------------------------
# Regeneration entrypoint
# ---------------------------------------------------------------------------


def _write_population_artifact(artifact: dict, path: Path = _POPULATION_ARTIFACT_PATH) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(artifact, fh, indent=2, sort_keys=True)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    """`python -m coordinator_core.tests.test_every_register_resolves_or_declares --regenerate`

    Byte-deterministic: the same corpus produces the same JSON bytes every time (sorted lists,
    no timestamps). Whoever adds or removes a register-bearing file commits the regenerated
    artifact in the SAME change -- that is what keeps leg 2 (`test_leg2_population_artifact_
    matches_current_candidates`) from ever being the one to notice on its own.
    """
    argv = sys.argv[1:] if argv is None else argv
    if argv != ["--regenerate"]:
        print(f"usage: {_REGENERATE_COMMAND}", file=sys.stderr)
        return 2
    artifact = _regenerate_population_artifact()
    _write_population_artifact(artifact)
    print(f"wrote {_POPULATION_ARTIFACT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
