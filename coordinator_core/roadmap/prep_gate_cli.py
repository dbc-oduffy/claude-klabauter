"""coordinator_core.roadmap.prep_gate_cli — the CLI half of the mise-prep bar.

Every real predicate lives in ``coordinator_core.roadmap.prep_gate`` (``gate_plan``,
``repo_root_names``, ``fleet_siblings``). This module owns exactly the door-served
CLI wrapping: argument parsing, target expansion, ``--json``/``--tally`` rendering,
and the exit-code mapping over ``gate_plan``'s own verdicts. A future bar leg lands
in ``prep_gate.py``, never here — see ``coordinator/bin/mise-prep-gate.py``'s own
docstring and the S1-C8 plan row this module was written against
(``docs/plans/2026-09-18-doe-holds-no-scripts.md``).

Restated from requirement over ``DoE-claude coordinator/bin/mise-prep-gate.py``
(1708 lines), read only to learn which CLI legs a thin door-served wrapper owes:
multi-target walk (default ``docs/plans``), ``--json``, ``--tally``, ``--repo-root``,
and an exit code per verdict. No code from that file is carried here — every
predicate DoE's script re-implemented (SPINE/CENSUS/EXTERNAL_DEPS/PRIME_EXIT/SCHEMA)
is a single call to ``gate_plan`` instead, so the two doors cannot compute the bar
differently.

ONE PLAN PER ``gate_plan`` CALL, one call per target — no corpus-wide re-derivation
here. ``gate_plan`` itself documents the per-call budget
(``coordinator_core/roadmap/prep_gate.py :: gate_plan``); this module adds nothing
to that cost beyond argv parsing and target-list expansion (a directory ``glob``,
not a spawn).

Zero subprocess, zero git: ``gate_plan`` reads the plan body's sha in pure Python,
and target expansion is a plain ``Path.glob``.

Negative-spec:
  - Does NOT re-implement SPINE/CENSUS/EXTERNAL_DEPS/PRIME_EXIT/SCHEMA. Every class
    is whatever ``gate_plan`` returns; a CLI-side divergence is exactly the two-door
    disagreement this rewrite exists to close.
  - Does NOT stamp. This module only reports; ``plan.stamp_prepped`` writes.
  - Does NOT scan a whole corpus in one ``gate_plan`` call — ``--tally`` runs one
    call per target and aggregates the reports, same as the default report mode.
  - Does NOT gate a sidecar named explicitly on the command line. ``_is_plan_sidecar``
    only prunes a DIRECTORY expansion; a caller who types a compound-stem path is
    still gated for it, the same asymmetry DoE-claude's own script keeps and for the
    same reason — silently returning nothing for a path the caller typed would be
    the worse surprise.

Restated from DoE-claude ``coordinator/bin/mise-prep-gate.py :: _is_plan_sidecar``
(2026-09-18, docs/plans/2026-09-18-doe-holds-no-scripts.md legs 1-3): a review or
coverage sidecar (``2026-06-24-baz.prior-art-check.md``,
``2026-06-27-foo.md.plan-coverage-check.md``) is named for the plan it annotates
plus its own kind, so its stem is compound (contains a ``.``) where a plan's own
stem — ``coordinator-doc-new --type plan``'s output — never is. Left unfiltered,
a directory walk reports NOT-PREPPED for hundreds of files that were never plans,
which is not a defect any author can fix.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from coordinator_core.roadmap.prep_gate import (
    NOT_PREPPED,
    PREPPED,
    REFUSED,
    gate_plan,
)

#: Exit codes, one per verdict plus usage. ``EXIT_REFUSED`` is reserved and
#: currently unreachable (nothing in ``prep_gate.py`` produces ``REFUSED`` — see
#: that module's own docstring), kept named so no future producer's mapping shifts.
EXIT_PREPPED = 0
EXIT_NOT_PREPPED = 1
EXIT_REFUSED = 2
EXIT_USAGE = 3


class GateCLIError(RuntimeError):
    """A fail-loud precondition — ``main()`` prints ``str(exc)`` and exits ``EXIT_USAGE``."""


def _is_plan_sidecar(path: Path) -> bool:
    """Whether ``path`` is a review/coverage sidecar rather than a plan.

    Restated to the letter from DoE-claude ``coordinator/bin/mise-prep-gate.py
    :: _is_plan_sidecar``. A plan's filename is a SINGLE stem —
    ``2026-06-27-foo.md`` — because that is what ``coordinator-doc-new --type
    plan`` emits. A sidecar is named for the plan it annotates plus its own
    kind, so its stem is compound: ``2026-06-27-foo.md.the Director of Engineering-review.md``,
    ``2026-07-01-bar.code-review-A-store.md``, ``2026-06-24-baz.prior-art-
    check.md``.

    Keyed on the compound stem rather than on the base plan still existing,
    because a sidecar outlives its plan. Only applied when EXPANDING A
    DIRECTORY — see ``_targets``.
    """
    stem = path.name[:-3] if path.name.endswith(".md") else path.name
    return "." in stem


def _targets(args: List[str], repo_root: Path) -> List[Path]:
    """Resolve positional targets to a flat list of plan files.

    A directory expands to its immediate ``*.md`` children, sorted, with every
    review/coverage sidecar pruned (``_is_plan_sidecar``); a file named
    explicitly is taken as-is, sidecar or not. Every relative argument
    resolves against ``repo_root``, so a caller running from a subdirectory
    still names the same file the door names.
    """
    out: List[Path] = []
    for arg in args:
        path = Path(arg)
        if not path.is_absolute():
            path = repo_root / path
        if path.is_dir():
            out.extend(
                p for p in sorted(path.glob("*.md")) if not _is_plan_sidecar(p)
            )
        elif path.is_file():
            out.append(path)
        else:
            raise GateCLIError(f"mise-prep-gate: no such plan or directory: {arg}")
    return out


def _tally(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate a report batch into verdict counts and defect-kind counts."""
    counts: Dict[str, int] = {PREPPED: 0, NOT_PREPPED: 0, REFUSED: 0}
    kinds: Dict[str, int] = {}
    for report in reports:
        counts[report["verdict"]] = counts.get(report["verdict"], 0) + 1
        for key, value in report["classes"].items():
            if value["status"] == "PASS":
                continue
            kind = f"{key}/{value['kind']}"
            kinds[kind] = kinds.get(kind, 0) + 1
    return {"verdicts": counts, "defect_kinds": kinds, "total": len(reports)}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mise-prep-gate",
        description="Run the mise-prep authoring bar over a plan. Writes nothing.",
    )
    parser.add_argument(
        "target",
        nargs="*",
        default=["docs/plans"],
        help="plan file(s) or a directory of them (default: docs/plans)",
    )
    parser.add_argument("--json", action="store_true", help="emit the full report as JSON")
    parser.add_argument(
        "--tally",
        action="store_true",
        help="print only the corpus tally — the re-measurement surface",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="repo to resolve relative targets against (default: cwd)",
    )
    return parser


def main(argv: "list[str] | None" = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    try:
        repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd()
        targets = _targets(args.target or ["docs/plans"], repo_root)
    except GateCLIError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE

    reports = [gate_plan(repo_root, target) for target in targets]

    if args.json:
        print(json.dumps({"reports": reports, "tally": _tally(reports)}, indent=2))
    elif args.tally:
        tally = _tally(reports)
        for verdict, count in tally["verdicts"].items():
            share = 100.0 * count / tally["total"] if tally["total"] else 0.0
            print(f"{count:5d}  {share:5.1f}%  {verdict}")
        print(f"{tally['total']:5d}         TOTAL")
        for kind, count in sorted(tally["defect_kinds"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:5d}  {kind}")
    else:
        for report in reports:
            print(report["message"])

    if any(r["verdict"] == REFUSED for r in reports):
        return EXIT_REFUSED
    if any(r["verdict"] == NOT_PREPPED for r in reports):
        return EXIT_NOT_PREPPED
    return EXIT_PREPPED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
