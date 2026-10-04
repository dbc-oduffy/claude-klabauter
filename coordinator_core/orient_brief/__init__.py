"""
coordinator_core.orient_brief -- the rebuilt `orient-assemble brief` engine.

Built from the decision-object envelope contract and the plan's requirement
inventory, never from the op it replaces. The seam is fixed here; each
family module fills in its own probes.

Contract:
    CADENCES                       -- the closed set of cadences.
    brief(cadence, *, repo_root)   -- the decision-object envelope (a dict);
                                      ValueError on an unknown cadence.
    main(argv)                     -- `brief --cadence {session|day|week}
                                      [--target-root <path>]`, `--help`;
                                      exit 0 ok, 2 usage, 3 transport.

Family protocol: each `_work`, `_branch`, `_health` module exposes
`collect(cadence, *, repo_root) -> ReaderResult` (type from
`contract.decision_object.reader_result` only). A family is read-only: a needed
heal is a `directives[]` entry naming an existing CLI, never an in-op write.

Requirement rows held here (docs/research/2026-10-04-orient-brief-requirements.md):
REQ-C1..C3 argv and root resolution (`main`); REQ-C4 exit codes and JSON shape;
REQ-C5/C6 envelope keys, narration and next_move (`brief`); REQ-C7 cadence
ValueError; REQ-C8 reportable partition; REQ-C9 family isolation (`_collect_all`);
REQ-C11 every family scans the resolved root it is handed, never cwd; REQ-C12
external text goes through `truncate_external_text` in each family and the session
brief stays under 30000 bytes; REQ-C13/C14 directives and judgment points are built
only by the decision-object builders with stable ids; REQ-C15 no second process-time
ceiling exists in this package (the brightline test is the only bar).

Success and argv usage errors print one envelope built by `build_envelope` and
validated by the `_emit` chokepoint. `--help`, a bad target root, and a
transport failure print none (REQ-C1, REQ-C3).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract.decision_object.envelope import (
    build_envelope,
    emit,
)
from coordinator_core.contract.decision_object.judgment import partition_reportable
from coordinator_core.contract.decision_object.reader_result import ReaderResult
from coordinator_core.git import repo_root as _repo_root_mod
from coordinator_core.orient_brief import _branch, _health, _work

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

CADENCES = ("session", "day", "week")

# Order is the order directives and judgment points appear in the envelope.
_FAMILIES = (_work, _branch, _health)

_PROG = "orient-assemble"
_USAGE = f"usage: {_PROG} brief --cadence {{{'|'.join(CADENCES)}}}"
_NULL_RECOMMENDATION_REASONS = frozenset({"insufficient-evidence", "recommendation-forbidden"})


def _resolve_root(repo_root: Optional[Path]) -> Path:
    if repo_root is not None:
        return Path(repo_root)
    top = _repo_root_mod.show_toplevel(None)
    return Path(top) if top else Path.cwd()


def _collect_all(cadence: str, root: Path) -> list[ReaderResult]:
    """REQ-C9: a raising family costs its own entries and one stderr line, never the brief."""
    results: list[ReaderResult] = []
    for family in _FAMILIES:
        try:
            result = family.collect(cadence, repo_root=root)
            for point in result.judgment_points:
                _checked(point)
            results.append(result)
        except Exception as exc:  # noqa: BLE001 - one family's failure is not the brief's
            name = family.__name__.rsplit(".", 1)[-1].lstrip("_")
            print(
                f"{_PROG}: reader {name} failed ({type(exc).__name__}: {exc}); "
                "its directives and judgment points are absent from this brief",
                file=sys.stderr,
            )
    return results


def _checked(point: dict) -> dict:
    if point.get("recommendation") is None and point.get("reason") not in _NULL_RECOMMENDATION_REASONS:
        raise ValueError(
            f"{_PROG}: judgment point {point.get('id')!r} has recommendation=null but "
            f"reason={point.get('reason')!r}; expected one of {sorted(_NULL_RECOMMENDATION_REASONS)}"
        )
    return point


def brief(cadence: str, *, repo_root: Optional[Path] = None) -> dict[str, Any]:
    if cadence not in CADENCES:
        raise ValueError(
            f"unknown cadence {cadence!r}; expected one of {', '.join(CADENCES)}"
        )
    results = _collect_all(cadence, _resolve_root(repo_root))
    directives = [d for r in results for d in r.directives]
    found = [p for r in results for p in r.judgment_points]
    # REQ-C8: a recommendation-carrying point that gates nothing is reported, not asked.
    _asked, reported = partition_reportable(
        [p for p in found if p.get("recommendation") is not None], directives
    )
    reported_ids = {p.get("id") for p in reported}
    points = [p for p in found if p.get("id") not in reported_ids]

    narration = (
        f"{_PROG} brief --cadence {cadence}: {len(directives)} directive(s), "
        f"{len(points)} judgment point(s) asked across {len(_FAMILIES)} reader families "
        f"({len(found)} found total)."
    )
    if reported:
        narration += " Reported (gate nothing, not asked): " + "; ".join(
            f"{p.get('id', '')} ({p.get('question', '')}) — "
            f"{(p.get('recommendation') or {}).get('rationale', '')}"
            for p in reported
        ) + "."
    return dict(emit(build_envelope(
        artifact={"cadence": cadence},
        directives=directives,
        judgment_points=points,
        narration=narration,
        next_move=(
            "Review directives[] and judgment_points[] below."
            if directives or points
            else "No findings from any reader family this pass."
        ),
    )))


def _usage_exit(message: Optional[str] = None) -> int:
    """REQ-C2: the usage envelope on stdout, the reason and usage line on stderr."""
    envelope = emit(build_envelope(
        narration=f"{_PROG}: usage error.",
        next_move=f"Run: {_PROG} brief --cadence {{{'|'.join(CADENCES)}}}",
    ))
    print(json.dumps(dict(envelope), indent=2, sort_keys=True))
    if message:
        print(f"{_PROG}: {message}", file=sys.stderr)
    print(_USAGE, file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    if argv[:1] and argv[0] in ("--help", "-h"):
        print(f"{_USAGE} [--target-root <path>]")
        return EXIT_OK
    if not argv or argv[0] != "brief":
        return _usage_exit()

    rest = list(argv[1:])
    cadence: Optional[str] = None
    target: Optional[str] = None
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--help", "-h"):
            print(f"{_USAGE} [--target-root <path>]")
            return EXIT_OK
        if tok.startswith(("--cadence=", "--target-root=")):
            flag, _, value = tok.partition("=")
            rest[i:i + 1] = [flag, value]
            continue
        if tok in ("--cadence", "--target-root"):
            if i + 1 >= len(rest):
                return _usage_exit(f"{tok} requires a value")
            if tok == "--cadence":
                cadence = rest[i + 1]
            else:
                target = rest[i + 1]
            i += 2
        else:
            return _usage_exit(f"unrecognized argument {tok!r}")
    if cadence not in CADENCES:
        return _usage_exit(f"--cadence must be one of {CADENCES}, got {cadence!r}")

    # REQ-C3: a bad root is a usage failure with no envelope.
    if target is not None:
        root = Path(target).resolve()
        if not root.is_dir():
            print(f"{_PROG}: --target-root {target!r} is not a directory (resolved to {root})", file=sys.stderr)
            return EXIT_USAGE
    else:
        top = _repo_root_mod.show_toplevel(None)
        if not top:
            print(
                f"{_PROG}: no --target-root given and cwd is not inside a git working tree. "
                "Run from a git repo or pass --target-root <path>.",
                file=sys.stderr,
            )
            return EXIT_USAGE
        root = Path(top)

    try:
        envelope = brief(cadence, repo_root=root)
    except Exception as exc:  # noqa: BLE001 - structural backstop: exit 3, never a traceback
        print(f"{_PROG}: transport failure: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_TRANSPORT_FAIL

    print(json.dumps(envelope, indent=2, sort_keys=True))
    return EXIT_OK
