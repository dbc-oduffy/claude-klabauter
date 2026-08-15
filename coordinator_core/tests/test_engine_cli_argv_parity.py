"""Pins `argv_parity_report`'s live result against this repo at HEAD to a
committed baseline (`engine_cli_argv_parity_baseline.json`), enforcing that
skew can only shrink, never grow, from here.

Spec backlink: C2 of
`docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md`.

PRIOR ART: this committed-JSON-baseline shape has no precedent in this
repo -- it is distinct from the `pending_fix`/`designed_red` marker system
(`coordinator_core/tests/test_tiers.py` and friends), which EXCLUDES a whole
test from a tier rather than pinning a per-item data baseline inside a
passing test. That marker system was the considered-and-not-chosen
alternative here: a `pending_fix` mark would hide the whole file from the
fast/full split rather than let a reader see the enumerated red set shrink
line by line, which is the point of this test. Recorded so a reviewer can
challenge the choice rather than assume no alternative was weighed.

Negative-spec:
    - Does NOT assert the baseline is empty. The baseline is non-empty at
      authoring time (67 pre-existing flagged pairings) and this test is
      enforcing from the start regardless -- it fails ONLY on a pairing's
      flags growing past what the baseline already records, never on the
      baseline's own non-empty starting content.
    - Does NOT regenerate the baseline as a side effect of running the
      test, under any flag, environment variable, or fixture. Regeneration
      is `_regenerate_baseline` below, invoked only via
      `python -m coordinator_core.tests.test_engine_cli_argv_parity
      --write-baseline`, a deliberate operator action, never part of a
      pytest collection or run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from coordinator_core.directive_cli_arity import argv_parity_report

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PATH = Path(__file__).resolve().with_name("engine_cli_argv_parity_baseline.json")


def _flag_keys(pairing) -> set[tuple]:
    """One tuple key per flagged aspect of `pairing` -- an unresolved
    pairing contributes a single `("unresolved", ...)` key; a resolved
    pairing contributes one `("unaccepted", ..., token)` or
    `("undeclared_required", ..., token)` key per offending token. A clean,
    resolved pairing with no tokens in either set contributes nothing --
    exactly the population the subset assertion below must not regress."""
    base = (pairing.module, pairing.directive_id, pairing.cli)
    if pairing.unresolved:
        return {("unresolved", *base)}
    keys: set[tuple] = set()
    for token in pairing.unaccepted:
        keys.add(("unaccepted", *base, token))
    for token in pairing.undeclared_required:
        keys.add(("undeclared_required", *base, token))
    return keys


def _load_baseline_keys() -> set[tuple]:
    doc = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    keys: set[tuple] = set()
    for entry in doc["entries"]:
        base = (entry["module"], entry["directive_id"], entry["cli"])
        if entry["unresolved"]:
            keys.add(("unresolved", *base))
            continue
        for token in entry["unaccepted"]:
            keys.add(("unaccepted", *base, token))
        for token in entry["undeclared_required"]:
            keys.add(("undeclared_required", *base, token))
    return keys


def _format_new_keys(new_keys: set[tuple]) -> str:
    lines = []
    for kind, module, directive_id, cli, *token in sorted(new_keys):
        token_txt = f" token={token[0]!r}" if token else ""
        lines.append(f"  [{kind}] module={module} directive_id={directive_id!r} cli={cli!r}{token_txt}")
    return "\n".join(lines)


def test_live_argv_parity_report_is_subset_of_baseline():
    live_report = argv_parity_report(_REPO_ROOT)
    live_keys: set[tuple] = set()
    for pairing in live_report.pairings:
        live_keys |= _flag_keys(pairing)

    baseline_keys = _load_baseline_keys()
    new_keys = live_keys - baseline_keys

    assert not new_keys, (
        "argv parity regressed: the following pairing(s) are flagged now but "
        "are NOT in the committed baseline "
        f"({_BASELINE_PATH.name}):\n{_format_new_keys(new_keys)}\n"
        "If this is a genuine new skew, fix the emitting module or the target "
        "CLI's parser. If it is a deliberate, reviewed baseline update, "
        "regenerate the file with: python -m "
        "coordinator_core.tests.test_engine_cli_argv_parity --write-baseline"
    )


def _regenerate_baseline() -> None:
    """Deliberate-only regeneration path -- never invoked by pytest. Rewrites
    `_BASELINE_PATH` from the live `argv_parity_report` result, one line per
    flagged pairing, preserving the schema/note shape authored at C2."""
    report = argv_parity_report(_REPO_ROOT)
    entries = []
    for pairing in report.pairings:
        if pairing.unresolved:
            entries.append(
                {
                    "module": pairing.module,
                    "directive_id": pairing.directive_id,
                    "cli": pairing.cli,
                    "unresolved": True,
                    "unaccepted": [],
                    "undeclared_required": [],
                }
            )
        elif pairing.unaccepted or pairing.undeclared_required:
            entries.append(
                {
                    "module": pairing.module,
                    "directive_id": pairing.directive_id,
                    "cli": pairing.cli,
                    "unresolved": False,
                    "unaccepted": sorted(pairing.unaccepted),
                    "undeclared_required": sorted(pairing.undeclared_required),
                }
            )
    entries.sort(key=lambda e: (e["module"], e["directive_id"], e["cli"]))
    doc = {
        "schema": "engine-cli-argv-parity-baseline/v1",
        "note": (
            "One line per pre-existing flagged pairing from argv_parity_report at "
            "repo HEAD. A subset assertion pins this: a NEW flag (unaccepted "
            "token, undeclared_required token, or a pairing newly unresolved) "
            "fails the test; clearing an existing flag passes and should shrink "
            "this file. This is not a count -- every entry names the module, "
            "directive_id, cli, and the exact tokens so the red set is visible "
            "and its shrinkage is auditable."
        ),
        "entries": entries,
    }
    _BASELINE_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(entries)} entries to {_BASELINE_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-baseline", action="store_true", required=True)
    parser.parse_args()
    _regenerate_baseline()
