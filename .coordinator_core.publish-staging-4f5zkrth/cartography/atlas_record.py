"""
coordinator_core.cartography.atlas_record — RECORDED architecture-atlas parser.

Purpose: parses the RECORDED state of `docs/architecture/file-index.md` (the
mapping rule and its `## Directory → system` package table) plus the
RECORDED per-system `files:` fingerprints from `docs/architecture/systems/
<system>.md` frontmatter, into a frozen, in-memory mapping. This artifact
answers "what does the atlas CLAIM is catalogued?" — it is authoritative
about what the atlas *claims*, NEVER about what the repo *is*. A doc/tree
disagreement discovered via this module is drift to report, not evidence
the repo is wrong; see the plan's "Decision" section for why file-index.md
is not a custody surface in the DR-236 sense.

The mapping rule and package table are parsed from `file-index.md`'s
recorded prose/tables, not recomputed from a live tree walk — `last_mapped`
freezes the reference point a consumer compares against
(coordinator_core.cartography.churn.compare_against_recorded_atlas).

Rules 2, 3, 4-9, 11, 12 are hardcoded here from the recorded table's literal
text (docs/architecture/file-index.md:23-37) — they do not vary run to run
without a doc edit, and parsing free-form English glob prose out of a
markdown table cell for those rows would add fragility with no benefit. Only
rule 10's `<pkg>` -> system table is actually scanned out of the doc body
(`package_systems`), since that table's membership is the part expected to
grow over time.

Spec backlink: pln-cartography-churn-emergent-det-8f59ce
§ chunk C2 (atlas_record — parse and expand file-index.md's recorded mapping
rule into a frozen catalogued set); § "Decision — source of the catalogued
set" (ruled: derive from the artifact, not the mapping-rule implementation).

Negative-spec:
  - Does NOT walk the live tree or shell out to git — `load_recorded_atlas`
    reads exactly two things: file-index.md and systems/*.md, both under the
    given root. `expand_recorded_mapping` takes an already-enumerated
    tracked-path list from the caller; it never calls git itself, keeping it
    independently testable without a git fixture.
  - Does NOT raise on a missing/unreadable/unparseable atlas — always
    returns a RecordedAtlas, with `error=ATLAS_UNREADABLE` and a non-empty
    `error_detail` on failure (AC8): an explicit discriminated failure,
    never a silently-empty mapping mistaken for "fully catalogued".
  - Does NOT reconcile `systems/cartography.md`'s recorded `files: 7` against
    file-index.md's remaining-systems table's `8` — both are recorded state;
    the system page is preferred as the fingerprint source of record per
    file-index.md's own "Recomputing the fingerprints" section, and the
    inconsistency is left exactly as recorded, not "fixed" here.
  - Does NOT extend, import, or reuse coordinator_core.cartography.file_index
    — that module answers an unordered, unfiltered, all-tracked-files
    question; this module answers the recorded, ordered, filtered one.
  - Does NOT hand-edit or regenerate file-index.md / systems/*.md — those
    are generated substrate, read-only from this module's perspective.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Sequence

ATLAS_UNREADABLE = "atlas_unreadable"

_FILE_INDEX_RELPATH = "docs/architecture/file-index.md"
_SYSTEMS_DIR_RELPATH = "docs/architecture/systems"

#: Basenames the recorded input filter excludes outright (rule verbatim from
#: file-index.md:19-21).
_EXCLUDED_BASENAME_PATTERNS = (
    re.compile(r"^test_.*"),
    re.compile(r".*_test\.py$"),
    re.compile(r"^conftest\.py$"),
    re.compile(r".*\.test\.js$"),
)

#: Path-prefix directories the recorded input filter excludes outright.
_EXCLUDED_DIR_PREFIXES = ("archive/", "tasks/", "dist/", "pip/")

#: Rules 2-9, 11, 12 — literal, hardcoded from file-index.md:26-36. Rule 1
#: (state/**, not a system) and rule 10 (<pkg> table) are handled specially
#: in recorded_system_for_path / _build_rules, not listed here.
_HARDCODED_RULE_SPECS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("coordinator/", "bin/"), "operator-cli"),
    (("scripts/",), "install-substrate"),
    (("coordinator_core/ops/emit/",), "emit-engine"),
    (("coordinator_core/ops/ceremony/",), "ceremony-ops"),
    (("coordinator_core/ops/fleet/",), "fleet-ops"),
    (("coordinator_core/ops/session/",), "session-ops"),
    (("coordinator_core/ops/docgen/",), "docgen"),
    (("coordinator_core/ops/",), "ops-flat"),
)

_ASSEMBLER_SUFFIXES = ("_assemble", "_complete")


@dataclass(frozen=True)
class MappingRule:
    """One ordered row of the recorded mapping rule (file-index.md:23-37).

    Attributes:
        ordinal: 1-based rule number, matching the recorded table's `#`
            column — first-match-wins over ascending ordinal.
        patterns: path-prefix patterns this rule matches (e.g.
            ``("coordinator_core/ops/emit/",)``). Rule 10 and 11 are
            represented with a synthetic marker pattern; see
            ``recorded_system_for_path`` for how they are actually applied.
        system: the system name this rule maps to, or ``None`` when the rule
            matches but names no system (rule 1: "not a system").
    """

    ordinal: int
    patterns: tuple[str, ...]
    system: str | None


@dataclass(frozen=True)
class RecordedAtlas:
    """The frozen, RECORDED architecture-atlas state — never a live re-read.

    Attributes:
        last_mapped: the `last_mapped` stamp from file-index.md's frontmatter
            (e.g. ``"2026-08-06"``), or ``None`` on failure.
        rules: ordered mapping rules, first-match-wins.
        package_systems: ``<pkg>`` -> system name, for rule 10
            (``coordinator_core/<pkg>/**``), scanned out of the recorded
            `## Directory → system` body.
        system_files: system -> RECORDED file count, sourced from each
            `docs/architecture/systems/<system>.md` page's `files:`
            frontmatter key (the fingerprint source of record per
            file-index.md's "Recomputing the fingerprints" section).
        error: ``None`` on success, else ``ATLAS_UNREADABLE``.
        error_detail: human-readable failure reason when ``error`` is set,
            else ``None``.
    """

    last_mapped: str | None
    rules: tuple[MappingRule, ...]
    package_systems: dict[str, str]
    system_files: dict[str, int]
    error: str | None
    error_detail: str | None


def _empty_atlas(detail: str) -> RecordedAtlas:
    return RecordedAtlas(
        last_mapped=None,
        rules=(),
        package_systems={},
        system_files={},
        error=ATLAS_UNREADABLE,
        error_detail=detail,
    )


def _parse_last_mapped(text: str) -> str | None:
    match = re.search(r"^last_mapped:\s*(\S+)\s*$", text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def _parse_package_systems(text: str) -> dict[str, str]:
    """Scan the `## Directory → system` body for `coordinator_core/<pkg>/`
    tokens, attributing each to the enclosing `###` section heading, plus
    the `### The remaining systems` table rows (per EM decision, binding for
    this chunk).
    """
    package_systems: dict[str, str] = {}

    section_match = re.search(
        r"^## Directory → system\s*$(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
    )
    if not section_match:
        return package_systems
    body = section_match.group(1)

    pkg_token_re = re.compile(r"`coordinator_core/([A-Za-z0-9_]+)/")

    # Per-###-section attribution (rule text + tables under each heading,
    # excluding "The remaining systems", handled separately below).
    heading_re = re.compile(r"^### (.+?)\s*$", re.MULTILINE)
    headings = list(heading_re.finditer(body))
    for idx, heading_match in enumerate(headings):
        heading_text = heading_match.group(1)
        if heading_text.strip() == "The remaining systems":
            continue
        # System name is the text up to the first " — " / " -- " / em-dash.
        system_name = re.split(r"\s+—\s+|\s+--\s+", heading_text, maxsplit=1)[0].strip()
        start = heading_match.end()
        end = headings[idx + 1].start() if idx + 1 < len(headings) else len(body)
        section_body = body[start:end]
        for pkg_match in pkg_token_re.finditer(section_body):
            package_systems[pkg_match.group(1)] = system_name

    # "The remaining systems" table: | System | Directories | Files | Lines |
    remaining_match = re.search(
        r"^### The remaining systems\s*$(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL
    )
    if remaining_match:
        remaining_body = remaining_match.group(1)
        for line in remaining_body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|") or stripped.startswith("|---"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) < 2:
                continue
            system_cell, directories_cell = cells[0], cells[1]
            if system_cell.lower() == "system":
                continue
            for pkg_match in pkg_token_re.finditer(directories_cell):
                package_systems[pkg_match.group(1)] = system_cell

    return package_systems


def _build_rules(package_systems: dict[str, str]) -> tuple[MappingRule, ...]:
    rules: list[MappingRule] = [MappingRule(ordinal=1, patterns=("state/",), system=None)]
    ordinal = 2
    for patterns, system in _HARDCODED_RULE_SPECS:
        rules.append(MappingRule(ordinal=ordinal, patterns=patterns, system=system))
        ordinal += 1
    # Rule 10 — represented with a synthetic marker; actual dispatch happens
    # in recorded_system_for_path via package_systems.
    rules.append(MappingRule(ordinal=ordinal, patterns=("coordinator_core/<pkg>/",), system=None))
    ordinal += 1
    # Rule 11 — assemblers, synthetic marker (suffix-based, not prefix-based).
    rules.append(MappingRule(ordinal=ordinal, patterns=("coordinator_core/<pkg-suffix>/",), system="assemblers"))
    ordinal += 1
    # Rule 12 — flat top-level *.py.
    rules.append(MappingRule(ordinal=ordinal, patterns=("coordinator_core/*.py",), system="engine-runtime"))
    return tuple(rules)


def load_recorded_atlas(root: str | Path) -> RecordedAtlas:
    """Parse the RECORDED atlas from ``<root>/docs/architecture/``.

    Reads ``file-index.md`` for ``last_mapped``, the rule ordering, and the
    rule-10 package table, and each ``systems/<system>.md`` page's `files:`
    frontmatter for ``system_files``. Never raises — an unreadable, missing,
    or unparseable atlas yields ``error=ATLAS_UNREADABLE`` with a non-empty
    ``error_detail`` and empty ``rules``/``package_systems``/``system_files``
    (AC8).
    """
    try:
        root_path = Path(root)
        file_index_path = root_path / _FILE_INDEX_RELPATH
        if not file_index_path.is_file():
            return _empty_atlas(f"missing atlas file: {file_index_path}")

        text = file_index_path.read_text(encoding="utf-8")
        last_mapped = _parse_last_mapped(text)
        package_systems = _parse_package_systems(text)
        rules = _build_rules(package_systems)

        systems_dir = root_path / _SYSTEMS_DIR_RELPATH
        system_files: dict[str, int] = {}
        if systems_dir.is_dir():
            for page_path in sorted(systems_dir.glob("*.md")):
                page_text = page_path.read_text(encoding="utf-8")
                system_match = re.search(r"^system:\s*(\S+)\s*$", page_text, re.MULTILINE)
                files_match = re.search(r"^files:\s*(\d+)\s*$", page_text, re.MULTILINE)
                if system_match and files_match:
                    system_files[system_match.group(1).strip()] = int(files_match.group(1))

        return RecordedAtlas(
            last_mapped=last_mapped,
            rules=rules,
            package_systems=package_systems,
            system_files=system_files,
            error=None,
            error_detail=None,
        )
    except Exception as exc:  # noqa: BLE001
        return _empty_atlas(f"failed to parse atlas: {exc}")


def is_source_candidate(relpath: str) -> bool:
    """The recorded input filter (file-index.md:19-21), applied to one path.

    `.py`/`.js` only, excluding ``archive/``, ``tasks/``, ``dist/``,
    ``pip/``, and excluding any file whose basename is ``test_*``,
    ``*_test.py``, ``conftest.py``, ``*.test.js``, or whose path contains
    ``/tests/``.
    """
    normalized = relpath.replace("\\", "/").lstrip("/")
    if not (normalized.endswith(".py") or normalized.endswith(".js")):
        return False
    for prefix in _EXCLUDED_DIR_PREFIXES:
        if normalized == prefix.rstrip("/") or normalized.startswith(prefix):
            return False
    if "/tests/" in f"/{normalized}/":
        return False
    basename = normalized.rsplit("/", 1)[-1]
    for pattern in _EXCLUDED_BASENAME_PATTERNS:
        if pattern.match(basename):
            return False
    return True


def _matches_prefix(normalized: str, prefix: str) -> bool:
    stripped = prefix.rstrip("/")
    return normalized == stripped or normalized.startswith(prefix)


def recorded_system_for_path(relpath: str, atlas: RecordedAtlas) -> str | None:
    """First-match-wins system lookup over ``atlas.rules``.

    Returns the system name, ``None`` when the recorded mapping covers the
    path but names no system (rule 1), or ``None`` when no rule covers the
    path at all.
    """
    normalized = relpath.replace("\\", "/").lstrip("/")

    for rule in atlas.rules:
        if rule.ordinal == 1:
            if _matches_prefix(normalized, "state/"):
                return None
            continue

        if rule.patterns == ("coordinator_core/<pkg>/",):
            # Rule 10: coordinator_core/<pkg>/** for <pkg> in package_systems.
            core_prefix = "coordinator_core/"
            if normalized.startswith(core_prefix):
                rest = normalized[len(core_prefix):]
                if "/" in rest:
                    pkg = rest.split("/", 1)[0]
                    if pkg in atlas.package_systems:
                        return atlas.package_systems[pkg]
            continue

        if rule.patterns == ("coordinator_core/<pkg-suffix>/",):
            # Rule 11: coordinator_core/<pkg>/** where <pkg> ends _assemble/_complete.
            core_prefix = "coordinator_core/"
            if normalized.startswith(core_prefix):
                rest = normalized[len(core_prefix):]
                if "/" in rest:
                    pkg = rest.split("/", 1)[0]
                    if pkg.endswith(_ASSEMBLER_SUFFIXES):
                        return "assemblers"
            continue

        if rule.patterns == ("coordinator_core/*.py",):
            # Rule 12: coordinator_core/*.py, flat top-level only.
            core_prefix = "coordinator_core/"
            if normalized.startswith(core_prefix):
                rest = normalized[len(core_prefix):]
                if "/" not in rest and rest.endswith(".py"):
                    return rule.system
            continue

        # Rules 2-9: plain path-prefix patterns.
        for pattern in rule.patterns:
            if _matches_prefix(normalized, pattern):
                return rule.system

    return None


@dataclass(frozen=True)
class RecordedExpansion:
    """Live tracked-file membership under the RECORDED mapping rule.

    Attributes:
        by_system: system -> sorted tuple of live paths the recorded rule
            maps there.
        uncatalogued: sorted tuple of candidates the recorded rule maps to
            no system at all (neither rule 1's "not a system" nor any
            catalogued rule matched).
        considered_count: number of candidates that passed
            ``is_source_candidate`` — the stated denominator.
    """

    by_system: dict[str, tuple[str, ...]]
    uncatalogued: tuple[str, ...]
    considered_count: int


def expand_recorded_mapping(
    tracked: Sequence[str], atlas: RecordedAtlas
) -> RecordedExpansion:
    """Expand the recorded mapping rule over an already-enumerated path list.

    Pure — no git shell-out. Applies ``is_source_candidate`` then
    ``recorded_system_for_path`` to each candidate.
    """
    by_system: dict[str, list[str]] = {}
    uncatalogued: list[str] = []
    considered_count = 0

    for raw_path in tracked:
        relpath = raw_path.replace("\\", "/").lstrip("/")
        if not is_source_candidate(relpath):
            continue
        considered_count += 1

        system = recorded_system_for_path(relpath, atlas)
        if system is None:
            # Distinguish rule-1 "not a system" coverage from "no rule
            # covered this path at all" — both report as uncatalogued here,
            # since neither is a catalogued system.
            if _is_rule1_covered(relpath):
                continue
            uncatalogued.append(relpath)
            continue

        by_system.setdefault(system, []).append(relpath)

    return RecordedExpansion(
        by_system={system: tuple(sorted(paths)) for system, paths in by_system.items()},
        uncatalogued=tuple(sorted(uncatalogued)),
        considered_count=considered_count,
    )


def _is_rule1_covered(relpath: str) -> bool:
    return _matches_prefix(relpath, "state/")
