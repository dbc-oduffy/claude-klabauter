"""
coordinator_core.ops.mint_deliverable_id — deliverable_id minting for the fleet
artifact spine.

Purpose: shared minting helper for all four authoring surfaces
(coordinator-doc-new, roadmap-planning, handoff, spinoff) so identity is
minted identically everywhere. Three mutually-exclusive paths:
  carry — existing deliverable_id supplied -> echo it unchanged (never re-mint)
  stub  — stub_id supplied -> emit "dlv-<stub_id>" (D1: reuse stub identity)
  slug  — slug supplied -> mint "dlv-<slug>-<6hex>" (uniqueness, not crypto)

Logs which path was taken to stderr ("carry" / "mint-from-stub" /
"mint-from-slug") — callers/tests grep this.

Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § D1, C3a
Port of: mint-deliverable-id.sh (coordinator-claude b5a4192c, 2026-07-20)

Negative-spec:
    (shell-doc-ok: the recipe below quotes the bash oracle's own parameter
    expansion verbatim, so the shell spelling is the accurate one.)

    - The slug path's 6-hex suffix is derived from slug + wall-clock seconds +
      pid + a random component, mirroring the bash oracle's
      `${SLUG}|$(date +%s)|$$|${RANDOM}` -> sha1 -> first 6 hex chars. This is
      NOT cryptographically unique — matching the bash script's own documented
      caveat ("Uniqueness is the goal; this is not cryptographic.") — do not
      "fix" this into a stronger uniqueness guarantee mid-port.
    - Exactly one of deliverable_id / stub_id / slug must be non-empty;
      zero or more-than-one is a usage error (exit 1 in the CLI form / raises
      ValueError from the library form), mirroring the bash script's
      _MODE_COUNT arithmetic.
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
import time
from typing import List, Optional, Tuple


def _sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def mint(
    deliverable_id: Optional[str] = None,
    stub_id: Optional[str] = None,
    slug: Optional[str] = None,
) -> Tuple[str, str]:
    """Mint or carry a deliverable_id. Returns (result, path_label).

    path_label is one of "carry" / "mint-from-stub" / "mint-from-slug" —
    matches the bash oracle's stderr-logged path names verbatim.

    Raises ValueError if zero or more than one of the three arguments is a
    non-empty string (mirrors the bash script's mutual-exclusivity check).
    """
    selected = [v for v in (deliverable_id, stub_id, slug) if v]
    if len(selected) == 0:
        raise ValueError("one of deliverable_id, stub_id, or slug is required")
    if len(selected) > 1:
        raise ValueError("deliverable_id, stub_id, and slug are mutually exclusive")

    if deliverable_id:
        return deliverable_id, "carry"

    if stub_id:
        return f"dlv-{stub_id}", "mint-from-stub"

    hash_input = f"{slug}|{int(time.time())}|{os.getpid()}|{random.randint(0, 32767)}"
    six_hex = _sha1_hex(hash_input)[:6]
    return f"dlv-{slug}-{six_hex}", "mint-from-slug"


_HELP_TEXT = """\
mint-deliverable-id.sh — Mint or carry a deliverable_id for the fleet artifact spine.
Purpose: Shared minting helper for all four authoring surfaces (coordinator-doc-new,
         roadmap-planning, handoff, spinoff) so identity is minted identically everywhere.
         Three paths:
           carry      — --deliverable-id supplied → echo it unchanged (never re-mint)
           stub       — --stub-id supplied → emit "dlv-<stub_id>" (D1: reuse stub identity)
           slug       — --slug supplied → mint "dlv-<slug>-<6hex>" (uniqueness not crypto)
         Logs which path was taken to stderr ("carry" / "mint-from-stub" / "mint-from-slug").
Spec backlink: pln-fleet-deliverable-spine-identity-and-facets-2b331c § D1, C3a
Usage:
  mint-deliverable-id.sh --deliverable-id <existing>   # carry (idempotent)
  mint-deliverable-id.sh --stub-id <stub_id>           # reuse stub identity
  mint-deliverable-id.sh --slug <slug>                 # mint new with time hash
Output: the deliverable_id on stdout; carry/mint path on stderr.
Exit:   0 on success, 1 on usage error.\
"""


def main(argv: List[str]) -> int:
    """CLI entry: mirrors the bash script's --deliverable-id/--stub-id/--slug/--help flags."""
    deliverable_id = ""
    stub_id = ""
    slug = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--deliverable-id":
            if i + 1 >= len(argv):
                print("ERROR: --deliverable-id requires a value", file=sys.stderr)
                return 1
            deliverable_id = argv[i + 1]
            i += 2
        elif arg == "--stub-id":
            if i + 1 >= len(argv):
                print("ERROR: --stub-id requires a value", file=sys.stderr)
                return 1
            stub_id = argv[i + 1]
            i += 2
        elif arg == "--slug":
            if i + 1 >= len(argv):
                print("ERROR: --slug requires a value", file=sys.stderr)
                return 1
            slug = argv[i + 1]
            i += 2
        elif arg in ("-h", "--help"):
            print(_HELP_TEXT)
            return 0
        else:
            print(f"ERROR: Unknown argument: {arg}", file=sys.stderr)
            return 1

    try:
        result, path_label = mint(
            deliverable_id=deliverable_id or None,
            stub_id=stub_id or None,
            slug=slug or None,
        )
    except ValueError as exc:
        msg = str(exc)
        print(f"ERROR: {msg}", file=sys.stderr)
        if "required" in msg:
            print(
                "Usage: mint-deliverable-id.sh --deliverable-id <id> | --stub-id <id> | --slug <slug>",
                file=sys.stderr,
            )
        return 1

    label_text = {
        "carry": f"carry path — using existing id: {result}",
        "mint-from-stub": f"mint-from-stub path — minted: {result}",
        "mint-from-slug": f"mint-from-slug path — minted: {result}",
    }[path_label]
    print(f"mint-deliverable-id: {label_text}", file=sys.stderr)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
