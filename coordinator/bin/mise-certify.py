#!/usr/bin/env python3
"""mise-certify -- Phase 0 fire-time certification, both legs, one command. Writes nothing.

Per plan, in the order `coordinator-content-repo coordinator/docs/wiki/lesson-triage/mise-prepped-attest.md`
§ "Fire-time revalidation is ONE step, not two" fixes:

  1. sha leg     `prep_gate.read_stamp` -- CERTIFIED / STALE / UNSTAMPED / MALFORMED, from the
                 recomputed `canonical_body_sha` against `mise_prepped_sha`. Spawn-free.
  2. census leg  `mise-census-revalidate`'s `revalidate`, run ONLY on a CERTIFIED plan: it spawns
                 per entry, and re-measuring the premises of a document that is no longer the
                 certified one spends the box on an answer nobody can use.

Exit status:
  0  nothing below fired (UNSTAMPED, UNDECIDABLE, REFUSED and UNRUNNABLE are reported, not failed)
  1  at least one STALE, MALFORMED or census DRIFT
  3  usage
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 3

#: Sha-leg states that fail the run. UNSTAMPED is not here: an unstamped plan was never claimed
#: certified, so it is a routing fact for the caller, not a broken certification.
_FAILING_SHA = frozenset({"STALE", "MALFORMED"})


def _prep_gate():
    own_dir = str(Path(__file__).resolve().parent)
    if own_dir not in sys.path:
        sys.path.insert(0, own_dir)
    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    import cc_invoke

    cc_invoke.require_engine_on_path(__file__)
    from coordinator_core.roadmap import prep_gate

    return prep_gate


def _revalidator():
    """The census leg, loaded from its sibling file so the two commands share one executor."""
    path = Path(__file__).resolve().parent / "mise-census-revalidate.py"
    spec = importlib.util.spec_from_file_location("mise_census_revalidate", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def certify(plan: Path, repo_root: Path, timeout: int) -> dict:
    try:
        text = plan.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"plan": str(plan), "sha": "MALFORMED", "sha_detail": str(exc), "census": None}
    stamp = _prep_gate().read_stamp(text)
    row = {"plan": str(plan), "sha": stamp["state"], "census": None}
    if stamp["state"] == "STALE":
        row["sha_detail"] = f"recorded {stamp['recorded_sha']}, body {stamp['body_sha']}"
    elif stamp["state"] == "MALFORMED":
        row["sha_detail"] = f"stamp fields missing: {', '.join(stamp.get('missing') or [])}"
    if stamp["state"] == "CERTIFIED":
        row["census"] = _revalidator().revalidate(plan, repo_root, timeout)
    return row


def _print(rows: list[dict]) -> None:
    for row in rows:
        print(Path(row["plan"]).name)
        print(f"  sha     {row['sha']}" + (f" -- {row['sha_detail']}" if row.get("sha_detail") else ""))
        census = row["census"]
        if census is None:
            print(f"  census  not run (sha leg {row['sha']})")
            continue
        print(f"  census  {census['state']}" + (f" -- {census['detail']}" if census.get("detail") else ""))
        for e in census["entries"]:
            if e["state"] == "MATCH":
                continue
            print(f"    {e['state']:12s} {e['command'][:110]}")
            if e.get("basis"):
                print(f"      basis    {e['basis']}")
            if e.get("detail"):
                print(f"      detail   {e['detail']}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mise-certify")
    ap.add_argument("plans", nargs="+", help="plan paths, repo-relative or absolute")
    ap.add_argument("--repo-root", default=".", help="the repo the census commands run in (default: cwd)")
    ap.add_argument("--timeout", type=int, default=30, help="per census command, seconds")
    ap.add_argument("--json", action="store_true")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK

    root = Path(args.repo_root).resolve()
    rows = [certify(Path(p) if Path(p).is_absolute() else root / p, root, args.timeout)
            for p in args.plans]
    if args.json:
        json.dump(rows, sys.stdout, indent=1)
        print()
    else:
        _print(rows)

    failed = any(r["sha"] in _FAILING_SHA or (r["census"] or {}).get("state") == "DRIFT"
                 for r in rows)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
