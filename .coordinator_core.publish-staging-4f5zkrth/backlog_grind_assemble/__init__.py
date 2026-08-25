"""
coordinator_core.backlog_grind_assemble — the `backlog-grind-assemble`
computed-skill engine.

Purpose: one cadence-parameterized read-only compute replacing the five
duplicated backlog-grind spines (`bug-blitz.md`, `mise-en-place.md`,
`bug-sweep/SKILL.md`, `debt-triage/SKILL.md`, `dogfood/SKILL.md`). Cadence
here names WHICH of the five mirror surfaces is asking
(`"bug-blitz" | "mise-en-place" | "bug-sweep" | "debt-triage" | "dogfood"`)
— it is `orient_assemble.CADENCES`'s severity/depth-knob naming convention
reused over a disjoint surface-selection set, never a severity tier (D-2).

`orient_assemble`, NOT `pickup_assemble`/`baton_assemble`, is the model for
THIS layer (`coordinator_core/orient_assemble/__init__.py`, esp. its
negative-spec at lines 38-50 against re-deriving `_emit`/`build_envelope`
locally). `pickup_assemble`/`baton_assemble` are the model only for the
CLI trampoline (C5) and package layout, never for this envelope/seam.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C3 (D-1..D-6 live in that plan's "Key decisions" section).

Reader wiring: `brief(cadence)` calls each of the five C3a-C3e reader
modules' `collect(cadence, *, run_id)` (`readers_blitz`, `readers_mise`,
`readers_sweep`, `readers_debt`, `readers_dogfood` — exposed here under
`readers_bug_blitz`/`readers_mise_en_place`/`readers_bug_sweep`/
`readers_debt_triage`/`readers_dogfood` aliases matching the cadence
vocabulary, per the C1 contract test's `getattr(bga, f"readers_{cadence...}")`
lookup) and CONCATENATES their `directives`/`judgment_points` into the
envelope. Cadence self-gating (each reader is a no-op `ReaderResult()` for
every cadence but its own) is decided INSIDE each reader's own `collect()`,
never re-decided here — this seam calls all five unconditionally for every
cadence and trusts each reader to self-gate, exactly mirroring
`orient_assemble`'s health-reaper precedent.

`run_id` (the CLI's `--run-id`) is threaded through that same seam the same
way, and for the same reason: it is passed to ALL five readers on every
cadence, and each self-gates on it exactly as it self-gates on `cadence`.
This module does not know — and must never learn — that `--run-id` names a
`state/mise-inventory/<run-id>.md` record, nor that mise-en-place is the
only cadence reading it today. That is the same constraint that rejected a
`--phase` flag: what is forbidden here is a PER-SURFACE BRANCH, not a
parameter. A `if cadence == "mise-en-place"` guard around the threading
would be the violation; uniform threading is not.
Carrier ratified 2026-08-04, `cross-repo/inbox/2026-08-04-doe-claude-em-
mise-run-id-carrier-env-breaks-windows.md`.

READ-ONLY, by construction (AC2): this module only reads disk/git state via
its five readers and `resolve_operator_config()`. Mutating actions are
returned as `directives[]` entries naming an existing atomic CLI — this
module never shells out to a mutating verb, never writes a file.

Envelope discipline (AC1): every exit routes through the SHIPPED
`coordinator_core.contract.decision_object.envelope.build_envelope`/`_emit`
chokepoint — this module does NOT re-derive its own key-validation
chokepoint.

Roots (AC5): `resolve_operator_config()` from
`coordinator_core.resolution.facade` is called directly rather than
re-deriving `settings_home`/`makima_bin`/`makima_root`/`doe_root` locally
— this module defines no `_settings_home`/`_resolve_settings_home` helper
of its own.

Negative-spec:
    - Do NOT add a mutating code path here — a finding that "the assembler
      should just do X" for any X that writes to disk belongs in a
      `directives[]` entry, not a new function body in this module.
    - Do NOT re-derive `_emit`/`build_envelope`/`build_judgment_point`
      locally — import and call the shipped `contract/decision_object/`
      chokepoints.
    - Do NOT reshape a reader's own `directives`/`judgment_points` entries
      inside this module — normalize only at this seam (concatenation), not
      by mutating a reader's dict shape; every reader already emits the
      same duck-typed `ReaderResult` shape (`.directives`/`.judgment_points`
      attributes), so no per-reader translation is needed.
    - Do NOT add a per-surface branch here. Cadence self-gating lives
      inside each reader; this seam stays `orient_assemble`-sized
      (~250 lines), not a grinding executor concentrating all five
      surfaces' compute.
    - Do NOT branch on `run_id` here either, do NOT validate it against any
      surface's own vocabulary, and do NOT resolve it to a path — it is
      forwarded verbatim to every reader and interpreted only by whichever
      reader claims it. A reader that receives a `run_id` it has no use for
      ignores it; that is self-gating, not seam logic.
    - Do NOT add a SECOND carrier for `run_id` (an env var read here, a
      session-state lookup): PM ruling 2026-08-04, with DoE-claude
      concurring. The flag is the only path.
    - Do NOT construct, parse, or validate a run id in the `mint-run-id`
      dispatch either (AC7, 2026-08-04 `docs/plans/2026-08-04-engine-
      minted-mise-run-identity.md` chunk C1). `_main_mint_run_id` asks each
      reader's own `mint_run_id(cadence)` and takes the first non-`None`
      result verbatim — it does not know what a run id looks like, does not
      re-derive whatever shape pattern a reader validates one against, and
      does not touch the id's own characters beyond forwarding the
      reader's own two returned fields into the printed JSON. A reader
      without a `mint_run_id` attribute abstains via `getattr(..., None)`,
      never a crash — not every reader family owns a run-identity record.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coordinator_core.backlog_grind_assemble import readers_blitz as readers_bug_blitz
from coordinator_core.backlog_grind_assemble import readers_debt as readers_debt_triage
from coordinator_core.backlog_grind_assemble import readers_dogfood
from coordinator_core.backlog_grind_assemble import readers_mise as readers_mise_en_place
from coordinator_core.backlog_grind_assemble import readers_sweep as readers_bug_sweep
from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    _emit as _envelope_emit,
)
from coordinator_core.resolution.facade import resolve_operator_config

#: The five mirror-surface cadences `backlog-grind-assemble brief` accepts.
#: Cadence names WHICH surface is asking, not a severity/depth knob (D-2) —
#: see the module docstring for the `orient_assemble.CADENCES` naming
#: reuse. Order matches `_READER_MODULES` below 1:1.
CADENCES: tuple[str, ...] = (
    "bug-blitz",
    "mise-en-place",
    "bug-sweep",
    "debt-triage",
    "dogfood",
)

#: The five reader families this seam wires into `brief()`. Each exposes
#: `collect(cadence) -> ReaderResult` (directives + judgment_points);
#: cadence self-gating lives inside each reader's own `collect()`, not
#: here. Exposed above under the `readers_<cadence-with-underscores>` alias
#: names the C1 contract test resolves via `getattr`.
_READER_MODULES = (
    readers_bug_blitz,
    readers_mise_en_place,
    readers_bug_sweep,
    readers_debt_triage,
    readers_dogfood,
)

#: Exit-code contract, locally scoped to this CLI (mirrors
#: `orient_assemble`'s/`pickup_assemble`'s/`baton_assemble`'s own locally-
#: scoped plain-int convention — the C1 contract test resolves these as
#: bare module attributes, not an `IntEnum`).
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

#: The one carrier for "which run is asking" (ratified 2026-08-04,
#: `cross-repo/inbox/2026-08-04-doe-claude-em-mise-run-id-carrier-env-breaks-
#: windows.md`). A flag rather than an environment variable because
#: `VAR=value command` is not a line `cmd.exe` parses — the Windows
#: `backlog-grind-assemble.cmd` launcher is the P0 path — and because each
#: EM Bash call is a fresh shell, so nothing exported survives to the next.
#: `readers_mise` spells this string a second time for its own judgment-point
#: prose; that is a user-facing label, deliberately not imported across the
#: seam (this package imports the readers, so the reverse edge would be
#: circular).
_RUN_ID_FLAG = "--run-id"


@dataclass(frozen=True)
class BriefResult:
    """`brief()`'s return shape — the computed decision object plus the
    inputs it was computed from, for a caller (C4's `apply.py`, which
    recomputes the brief in-process before dispatching) to recover without
    a second `resolve_operator_config()` round-trip."""

    decision_object: dict[str, Any]
    cadence: str
    repo_root: Path | None = None
    run_id: str | None = None


def brief(
    cadence: str, *, run_id: str | None = None, repo_root: Path | None = None
) -> BriefResult:
    """Compute the cadence-selected backlog-grind decision object.

    Calls all five reader families' `collect(cadence, run_id=run_id)`
    unconditionally and concatenates their `directives`/`judgment_points`
    into the emitted envelope — never reshapes a reader's own dict (per
    `orient_assemble`'s own negative-spec). Each reader self-gates to its
    own cadence; every other reader contributes an empty `ReaderResult` for
    a given cadence.

    `run_id` names WHICH run of the asking surface is asking, opaque at this
    seam: it is forwarded verbatim to every reader for every cadence and
    interpreted only by a reader that has a use for it (today, only
    `readers_mise`, which resolves it to `state/mise-inventory/<run-id>.md`).
    `None` means the caller named no run — a reader that needs one says so
    in its own judgment point rather than this seam deciding on its behalf.

    Raises `ValueError` for a `cadence` outside `CADENCES`. Read-only
    (AC2): performs no disk mutation and no git mutation. Calls
    `resolve_operator_config()` (AC5) rather than re-deriving
    `settings_home`/`makima_bin`/`makima_root`/`doe_root` locally — the
    resolved config is not currently consumed further by this thin seam,
    but the call itself is the AC5 contract (proven by a spy test), and
    keeps roots resolved through one chokepoint for any future reader that
    needs them.
    """
    if cadence not in CADENCES:
        raise ValueError(
            f"backlog-grind-assemble: unrecognized cadence {cadence!r}; "
            f"must be one of {CADENCES}"
        )

    resolve_operator_config()

    directives: list[dict[str, Any]] = []
    judgment_points: list[dict[str, Any]] = []
    for reader in _READER_MODULES:
        result = reader.collect(cadence, run_id=run_id)
        directives.extend(result.directives)
        judgment_points.extend(result.judgment_points)

    envelope = build_envelope(
        artifact={"cadence": cadence, "run_id": run_id},
        preflight={},
        gates={},
        directives=directives,
        judgment_points=judgment_points,
        decisions={},
        narration=(
            f"backlog-grind-assemble brief {cadence}: {len(directives)} "
            f"directive(s), {len(judgment_points)} judgment point(s) "
            "across five reader families."
        ),
        next_move=(
            "Review directives[] and judgment_points[] below."
            if (directives or judgment_points)
            else f"No findings for {cadence} this pass."
        ),
    )
    decision_object = dict(_envelope_emit(envelope))
    return BriefResult(
        decision_object=decision_object,
        cadence=cadence,
        repo_root=repo_root,
        run_id=run_id,
    )


#: The usage line, spelled once for both the emitted decision object and
#: stderr. `--run-id` is offered for every cadence, not documented per
#: surface — the seam does not know which readers consume it.
_USAGE_LINE = (
    f"backlog-grind-assemble brief <{'|'.join(CADENCES)}> [--run-id <run-id>]"
)


def _usage() -> int:
    """Print a usage error and return `EXIT_USAGE` — a decision object is
    emitted on every `brief` exit, including a usage error, never a bare
    exit code (contract § round-trip classification)."""
    envelope = build_envelope(
        narration="backlog-grind-assemble: usage error.",
        next_move=f"Run: {_USAGE_LINE}",
    )
    print(json.dumps(dict(_envelope_emit(envelope)), indent=2, sort_keys=True))
    print(f"usage: {_USAGE_LINE}", file=sys.stderr)
    return EXIT_USAGE


def _main_mint_run_id(rest: list[str]) -> int:
    """`mint-run-id <cadence>` — see the module docstring's mint-dispatch
    negative-spec bullet. Exactly one positional argument, the cadence;
    zero args or more than one is the generic usage error (same posture as
    `brief`'s own missing/bogus-cadence handling).

    Dispatches to every reader family the same self-gating way `brief`
    dispatches `collect(cadence, run_id=...)`: ask each reader's own
    `mint_run_id(cadence)` (a reader lacking the attribute abstains via
    `getattr(..., None)`, never a crash) and take the first non-`None`
    result. This module never learns what a run id looks like — it prints
    the reader's own `.run_id`/`.inventory_path` fields verbatim (AC1, AC7).

    No reader claiming `cadence` is a usage error (exit `EXIT_USAGE`) naming
    the cadence — never an exit-0 mint (AC5). Same exit code for a missing
    or malformed cadence argument.
    """
    if len(rest) != 1:
        return _usage()
    cadence = rest[0]

    if cadence not in CADENCES:
        print(
            f"backlog-grind-assemble: unrecognized cadence {cadence!r}; "
            f"must be one of {CADENCES}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    for reader in _READER_MODULES:
        minter = getattr(reader, "mint_run_id", None)
        if minter is None:
            continue
        minted = minter(cadence)
        if minted is not None:
            print(
                json.dumps(
                    {"run_id": minted.run_id, "inventory_path": minted.inventory_path},
                    indent=2,
                    sort_keys=True,
                )
            )
            return EXIT_OK

    # Review: coordinator-code-reviewer — this branch and the
    # unrecognized-cadence branch above both exit EXIT_USAGE (AC5), but now
    # print distinct messages so an operator can tell a typo from a real
    # cadence nothing mints for yet. The `cadence not in CADENCES` check is
    # a cadence-vocabulary fact `main()` already tests elsewhere in this
    # file, not a run-id-shape fact — stays on the AC7-opaque side.
    print(
        "backlog-grind-assemble: no reader claims mint-run-id for cadence "
        f"{cadence!r}",
        file=sys.stderr,
    )
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    """`backlog-grind-assemble brief <cadence> [--run-id <run-id>]` CLI
    entrypoint, plus the sibling `mint-run-id <cadence>` verb
    (`_main_mint_run_id`, 2026-08-04 `docs/plans/2026-08-04-engine-minted-
    mise-run-identity.md` chunk C1).

    Only `brief` and `mint-run-id` are wired here — `apply`/`drop` are C4's
    `apply.py`'s own entrypoints (`main_apply`/`main_drop`), dispatched by
    the C5 trampoline, not by this module's `main()`.

    `--run-id` is parsed CADENCE-AGNOSTICALLY: accepted after any cadence,
    forwarded verbatim to `brief()`, never validated or resolved here (a
    reader that cannot use the value it was given says so in its own
    judgment point). A missing value after the flag, a repeat of the flag,
    or any unrecognized token is a usage error — never a silently ignored
    argument, which is the shape that lets a caller believe they named a run
    when they did not. `--mint-run-id` is NOT a recognized token here either
    — `brief` has exactly one carrier for a run id (`--run-id`) and stays
    frozen; minting is the separate `mint-run-id` verb (AC6).
    """
    if not argv:
        return _usage()

    if argv[0] == "mint-run-id":
        return _main_mint_run_id(argv[1:])

    if argv[0] != "brief":
        return _usage()

    rest = argv[1:]
    if not rest:
        return _usage()

    cadence = rest[0]
    if cadence not in CADENCES:
        return _usage()

    run_id: str | None = None
    remaining = rest[1:]
    while remaining:
        token = remaining[0]
        if token != _RUN_ID_FLAG or len(remaining) < 2 or run_id is not None:
            return _usage()
        run_id = remaining[1]
        remaining = remaining[2:]

    try:
        result = brief(cadence, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 - structural backstop, mirrors pickup_assemble's own
        print(f"backlog-grind-assemble: unexpected failure: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(result.decision_object, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
