"""
coordinator_core.staff_session_assemble — the `staff-session-assemble`
computed-skill engine.

Purpose: resolves `coordinator/skills/staff-session/SKILL.md`'s three
roster tables (domain-signal -> default persona pair, persona-slug ->
agent-file, persona-slug -> subagent_type) server-side into one read-only
roster decision, from a domain signal + session mode + optional explicit
slug override. Mirrors `sizing_assemble.route()`'s shape (a compute layer
over a frozen contract) rather than the tables living as prose the operator
self-navigates (the "deterministic table in a markdown fence" defect
`invisible-doctrine.md` realization #6 forbids).

Contract (frozen, reviewed): example-doctrine-repo coordinator/docs/wiki/computed-skills.md
DR-090 (example-doctrine-repo docs/decisions/DR-090-the-unit-of-extraction-is-the-mechanical-step.md)
Spec backlink: example-doctrine-repo docs/plans/2026-07-24-computed-skills-b5-planning-cluster.md,
chunk C11 (Design Option A, AC17)
Registration seam: this module ships no bash veneer and needs none — it is
consumed directly by the `coordinator/bin/staff-session-assemble` trampoline
(mirrors `coordinator/bin/sizing-assemble`'s direct-import template-variant
#1).

**F1 reconciliation (PM 2026-07-24) — roster DATA stays doctrine-side, this
resolver READS it, it does not own/hardcode it as new engine-side data.**
The three tables formerly hardcoded at `staff-session/SKILL.md:72-78`
(domain-signal -> default pair), `:89-98` (slug -> agent-file), and
`:169-178` (slug -> subagent_type) fold to example-doctrine-repo's `coordinator/
routing.md` (AC7(a)/C9 — the single home). This module has no compiled-in
roster dict; every call re-reads and re-parses `routing.md` fresh
(recompute-never-trust-caller, same discipline as `pickup_assemble`).

**Doctrine-side read contract (what C9 must land in routing.md).** This
module expects THREE markdown pipe-tables in example-doctrine-repo's
`coordinator/routing.md`, each immediately preceded by an exact H3 heading
line (see `_HEADING_DOMAIN_PAIR` / `_HEADING_AGENT_FILE` /
`_HEADING_SUBAGENT_TYPE` below) — table shape mirrors the three tables as
they exist verbatim in `staff-session/SKILL.md` today (2-column pipe table,
`Slug`-keyed backtick cells for the two persona-keyed tables, a
`` `slug` + `slug` `` cell for the pair table). C9 is responsible for
adding these three sections to `routing.md` and deleting the tables from
`staff-session/SKILL.md`; this module does not write routing.md and does
not assume the sections already exist there — a missing section is a
fail-loud `StaffSessionAssembleError`, never a silent empty roster.

READ-ONLY, by construction: `resolve_roster()` only reads `routing.md` (or
a caller-supplied text, for testing) — it never writes, never shells out.

Negative-spec:
    - Do NOT add a mutating code path here. `resolve_roster()` is a pure
      read+compute function.
    - Do NOT hardcode a roster dict as a module-level constant. The whole
      point of the F1 reconciliation is that the roster DATA lives in
      example-doctrine-repo's `routing.md`, not in a second engine-owned copy here —
      a hardcoded fallback dict would silently reintroduce the very
      duplication AC7(a)/AC17 close.
    - Do NOT resolve `the Director of Engineering` as a debater slug — he is the staff-session
      synthesizer, never a debater (mirrors `staff-session/SKILL.md`
      Step 4's explicit rejection). `resolve_roster()` fails loud
      (`StaffSessionAssembleError`) if `the Director of Engineering` appears in an explicit
      `--slug` override or a resolved default pair.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

_HEADING_DOMAIN_PAIR = "### Domain Signal -> Default Pair"
_HEADING_AGENT_FILE = "### Persona Slug -> Agent File"
_HEADING_SUBAGENT_TYPE = "### Persona Slug -> subagent_type"

SESSION_MODE_ENUM = ("plan", "review")

_SYNTHESIZER_SLUG = "the Director of Engineering"


class StaffSessionAssembleError(ValueError):
    """Raised for a malformed input, an unresolvable doctrine-side read, or
    a missing/incomplete routing.md roster section — a usage/data-contract
    error, never a business-logic divergence (this module has no fork/
    divergence concept, only resolve-or-fail-loud)."""


def _resolve_content_root() -> str:
    """Resolves the coordinator content root via the shared
    resolve-coordinator-clone `--for-content` resolver — works on a pure OSS
    install (no example-doctrine-repo clone present), unlike the former
    `read_doe_root_pointer()`-based resolution which only ever resolved a
    example-doctrine-repo clone and left `/staff-session` roster resolution unreachable on an
    OSS-only machine."""
    from coordinator_core.resolve_coordinator_clone import (
        ResolveCoordinatorCloneError,
        resolve_content_root,
    )

    try:
        return resolve_content_root()
    except ResolveCoordinatorCloneError as exc:
        raise StaffSessionAssembleError(
            f"coordinator content root unresolvable — cannot read the "
            f"doctrine-side routing.md roster data: {exc}"
        ) from exc


def _read_routing_md(content_root: str) -> str:
    path = os.path.join(content_root, "routing.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as exc:
        raise StaffSessionAssembleError(f"could not read {path!r}: {exc}") from exc


def _strip_md_cell(cell: str) -> str:
    return cell.strip().strip("`").strip()


def _strip_full_backtick_wrap(cell: str) -> str:
    """Strips a SINGLE full backtick-wrap (`` `x` `` -> `x`) but leaves a
    multi-token cell like "`the Staff Engineer` + `sid`" untouched — that cell is parsed
    by `_split_pair_cell`'s own regex, which needs the backticks intact."""
    cell = cell.strip()
    if len(cell) >= 2 and cell.startswith("`") and cell.endswith("`") and cell.count("`") == 2:
        return cell[1:-1]
    return cell


_SEPARATOR_RE = re.compile(r"^:?-+:?$")


def _parse_pipe_table_data_rows(table_lines: list[str]) -> list[tuple[str, str]]:
    """Parses the DATA rows of a 2-column markdown pipe table — callers pass
    only the rows after the header + `|---|---|` separator have already been
    dropped (see `_extract_table_after_heading`)."""
    rows: list[tuple[str, str]] = []
    for line in table_lines:
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(_SEPARATOR_RE.match(c) for c in cells):
            continue
        rows.append((_strip_md_cell(cells[0]), _strip_full_backtick_wrap(cells[1])))
    return rows


def _extract_table_after_heading(text: str, heading: str) -> list[str]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise StaffSessionAssembleError(
            f"routing.md is missing the expected section heading {heading!r} — "
            "the doctrine-side staff-session roster contract (C9) has not "
            "landed this section yet."
        )
    table_lines: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith("|"):
            table_lines.append(line)
        elif table_lines:
            break
        elif stripped == "":
            continue
        else:
            break
    if len(table_lines) < 3:
        raise StaffSessionAssembleError(
            f"routing.md section {heading!r} has no data rows (found "
            f"{len(table_lines)} table line(s), need header + separator + "
            "at least one data row)."
        )
    return table_lines


def _parse_section(text: str, heading: str) -> dict[str, str]:
    table_lines = _extract_table_after_heading(text, heading)
    # First two lines are the header row and the `|---|---|` separator.
    rows = _parse_pipe_table_data_rows(table_lines[2:])
    if not rows:
        raise StaffSessionAssembleError(
            f"routing.md section {heading!r} parsed to zero data rows."
        )
    return dict(rows)


_PAIR_SLUG_RE = re.compile(r"`([a-z0-9_-]+)`")


def _split_pair_cell(cell: str) -> list[str]:
    """Splits a "`the Staff Engineer` + `sid`" default-pair cell into ["the Staff Engineer", "sid"]."""
    slugs = _PAIR_SLUG_RE.findall(cell)
    if not slugs:
        raise StaffSessionAssembleError(f"malformed default-pair cell: {cell!r}")
    return slugs


def load_roster_tables(routing_md_text: str) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Parses all three doctrine-side roster tables out of routing.md's text.
    Exposed standalone (not just inlined into resolve_roster) so a caller —
    or a test fixture — can parse once and inspect the raw tables."""
    domain_pair = _parse_section(routing_md_text, _HEADING_DOMAIN_PAIR)
    agent_file = _parse_section(routing_md_text, _HEADING_AGENT_FILE)
    subagent_type = _parse_section(routing_md_text, _HEADING_SUBAGENT_TYPE)
    return domain_pair, agent_file, subagent_type


def resolve_roster(
    *,
    domain_signal: Optional[str] = None,
    session_mode: str,
    slugs: Optional[list[str]] = None,
    routing_md_text: Optional[str] = None,
) -> dict[str, Any]:
    """Resolves the staff-session persona roster (Option A, AC17).

    Args:
        domain_signal: exact-match key into routing.md's "Domain Signal ->
            Default Pair" table (e.g. "Architecture / infrastructure").
            Required unless `slugs` is given as an explicit override.
        session_mode: "plan" | "review" — passed through for provenance;
            the roster tables do not vary by session mode today.
        slugs: explicit persona-slug override (mirrors staff-session's
            `--members` flag) — when given, `domain_signal` is not consulted
            for the pair, only for provenance/narration (optional then).
        routing_md_text: caller-supplied routing.md text — for testing
            without touching disk. Production callers omit this; the
            doctrine-side root/read happens internally
            (recompute-never-trust-caller).

    Returns:
        {domain_signal, session_mode, personas: [{slug, agent_file,
        subagent_type}, ...], narration, source} — READ-ONLY, mutates
        nothing.
    """
    if session_mode not in SESSION_MODE_ENUM:
        raise StaffSessionAssembleError(
            f"session_mode must be one of {SESSION_MODE_ENUM}, got {session_mode!r}"
        )
    if not slugs and not domain_signal:
        raise StaffSessionAssembleError(
            "either domain_signal or an explicit slugs override is required"
        )

    if routing_md_text is None:
        content_root = _resolve_content_root()
        routing_md_text = _read_routing_md(content_root)
        source = os.path.join(content_root, "routing.md")
    else:
        source = "<caller-supplied routing.md text>"

    domain_pair_table, agent_file_table, subagent_type_table = load_roster_tables(routing_md_text)

    if slugs:
        resolved_slugs = list(slugs)
        narration = f"Explicit slug override: {', '.join(resolved_slugs)}."
    else:
        if domain_signal not in domain_pair_table:
            raise StaffSessionAssembleError(
                f"domain_signal {domain_signal!r} not found in routing.md's "
                f"Domain Signal -> Default Pair table. Known signals: "
                f"{sorted(domain_pair_table)}"
            )
        resolved_slugs = _split_pair_cell(domain_pair_table[domain_signal])
        narration = (
            f"Domain signal {domain_signal!r} resolved to default pair "
            f"{', '.join(resolved_slugs)}."
        )

    if _SYNTHESIZER_SLUG in resolved_slugs:
        raise StaffSessionAssembleError(
            "'the Director of Engineering' cannot appear as a debater slug — he is the staff-session "
            "synthesizer, never a debater (see staff-session/SKILL.md Step 4)."
        )

    personas: list[dict[str, str]] = []
    for slug in resolved_slugs:
        if slug not in agent_file_table:
            raise StaffSessionAssembleError(
                f"persona slug {slug!r} has no agent-file entry in routing.md's "
                f"Persona Slug -> Agent File table. Known slugs: "
                f"{sorted(agent_file_table)}"
            )
        if slug not in subagent_type_table:
            raise StaffSessionAssembleError(
                f"persona slug {slug!r} has no subagent_type entry in "
                f"routing.md's Persona Slug -> subagent_type table. Known "
                f"slugs: {sorted(subagent_type_table)}"
            )
        personas.append(
            {
                "slug": slug,
                "agent_file": agent_file_table[slug],
                "subagent_type": subagent_type_table[slug],
            }
        )

    return {
        "domain_signal": domain_signal,
        "session_mode": session_mode,
        "personas": personas,
        "narration": narration,
        "source": source,
    }


EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3


def _usage(prog: str) -> int:
    import sys

    print(
        f"{prog}: usage: {prog} --session-mode <plan|review> "
        "(--domain-signal <str> | --slug <slug> [--slug <slug> ...])",
        file=sys.stderr,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json
    import sys

    prog = "staff-session-assemble"
    domain_signal = None
    session_mode = None
    slugs: list[str] = []

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--domain-signal" and i + 1 < len(argv):
            domain_signal = argv[i + 1]
            i += 2
        elif tok == "--session-mode" and i + 1 < len(argv):
            session_mode = argv[i + 1]
            i += 2
        elif tok == "--slug" and i + 1 < len(argv):
            slugs.append(argv[i + 1])
            i += 2
        elif tok == "--json":
            i += 1
        else:
            print(f"{prog}: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage(prog)

    if session_mode is None or (domain_signal is None and not slugs):
        return _usage(prog)

    try:
        decision = resolve_roster(
            domain_signal=domain_signal,
            session_mode=session_mode,
            slugs=slugs or None,
        )
    except StaffSessionAssembleError as exc:
        print(f"{prog}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors sizing_assemble
        print(f"{prog}: unexpected failure: {exc}", file=sys.stderr)
        print(json.dumps({"error": str(exc), "transport_failure": True}))
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(decision, indent=2, sort_keys=True))
    return EXIT_OK
