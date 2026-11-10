#!/usr/bin/env python3
"""Record a binary's Ghidra program facts (functions, xrefs, imports, strings) in the REA ledger.

Runs Ghidra's headless analyzer over a copy of the binary (never executing it) and writes the
facts through ``rea_ledger.write``. Needs Ghidra 11.x: GHIDRA_INSTALL_DIR or ``--ghidra``.
The Ghidra project persists under scratch/ghidra_ingest/projects unless ``--project-dir`` says
otherwise; ``--confine`` opts into process confinement (Windows Job Object / POSIX new session).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from coordinator_core.rea_ingest import ghidra_runner  # noqa: E402
from coordinator_core.rea_ingest.program_facts import ProgramFactsWriteError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("binary", type=Path, help="caller-held binary to analyse (copied, never executed)")
    ap.add_argument("--ghidra", help="Ghidra install dir (default: GHIDRA_INSTALL_DIR)")
    ap.add_argument("--project-dir", type=Path, help="persistent Ghidra project dir")
    ap.add_argument("--confine", action="store_true", help="confine the analyzer process tree")
    ap.add_argument("--timeout", type=float, default=ghidra_runner.DEFAULT_TIMEOUT_S, help="seconds")
    ap.add_argument("--observed-at", help="RFC 3339 stamp for the records (default: fixed, keeps re-runs idempotent)")
    args = ap.parse_args(argv)
    try:
        report = ghidra_runner.ingest(
            args.binary, ghidra=args.ghidra, project_dir=args.project_dir, confine=args.confine,
            timeout=args.timeout, observed_at=args.observed_at,
        )
    except (ghidra_runner.GhidraRefusal, ProgramFactsWriteError) as exc:
        print(f"ghidra_ingest: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
