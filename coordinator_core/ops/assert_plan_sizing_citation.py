"""
coordinator_core.ops.assert_plan_sizing_citation — read-only gate asserting
(a) every plan's frontmatter `sizing_object` citation resolves on disk, and
(b) every plan created on/after the sizing-citation convention's cutoff
declares one at all — an explicit path or an explicit `null`.

Purpose: `sizing_object:` is a hand-written plan frontmatter key naming a
`state/sizings/<id>.yaml` record. Two failure modes exist and are checked
independently. DANGLING: a citation is present but the path does not
resolve — the plan-authoring doctrine this key belongs to describes the
gate as "a wall, not a courtesy", and a plan shipped citing a sizing object
that did not exist. MISSING: no citation is present at all — the ordinary
failure, since a plan with no key was previously invisible to this gate
entirely (see docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md
§ Problem). Both legs walk `docs/plans/*.md`, parse each file's YAML
frontmatter, and read `sizing_object` ONLY from the parsed frontmatter
mapping.

AC6/AC9 (the load-bearing negative — see
docs/plans/2026-08-06-plan-sizing-citation-gate.md, inherited verbatim by
docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md): this op MUST
NEVER regex over the file's BODY text for `state/sizings/...`, and MUST
NEVER compare a citation (or its absence) against the plan's problem
restatement. Body prose legitimately cites a nonexistent sizing object when
documenting its absence — e.g.
docs/plans/2026-08-06-windows-hot-path-less-work-per-interpreter.md cites
`state/sizings/2026-08-06-ten-interpreters-per-edit.yaml` as the evidence
that no sizing object was ever written for its predecessor ask. A
text-scanning implementation fires on that plan and makes it unwriteable —
true for the dangling leg, and equally true for the absence leg: an
explicit `sizing_object: null` in frontmatter satisfies the check
regardless of what the body says. This op is structurally incapable of
that failure: it reads the value via
`coordinator_core.frontmatter.primitives.split_frontmatter` +
`read_fm_field_unquoted`, which only ever sees the text between the two
`---` delimiters — the body is never passed to either function.

The absence leg is date-scoped by `_CUTOFF` and file-scoped by
`_PLAN_BASENAME` — see those constants' own comments for why. It never
touches the 292 pre-convention plans the schema explicitly exempts from
backfill.

Exit codes:
  0 -- zero dangling citations and zero missing declarations (or no plans found)
  1 -- >=1 dangling citation and/or >=1 missing declaration, each listed
       with its owning plan and its own finding block

Read-only: no `--fix` mode. Unlike the backlinks sibling
(assert_no_dangling_plan_backlinks), there is no move-map to repoint a
dangling citation against — a dangling `sizing_object` means the record was
never written, not that it moved. Likewise a missing declaration means the
decision to cite was never made; the anti-scope forbids guessing one.

Spec backlink: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C3 / AC4 / AC6
Spec backlink: docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md § C2 / AC5-AC10
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)

# The day the sizing-citation convention landed (see the absence-plan's
# census: of 13 plans created this day, 5 carried the key and 8 did not).
# Plans created before this date are the 292-plan pre-convention corpus
# plan.schema.json explicitly exempts from backfill; only on/after this
# date does a missing `sizing_object` become a finding.
_CUTOFF = "2026-08-06"

# `docs/plans/*.md` is not a directory of plans — it also holds 164
# non-plan sidecars (`.prior-art-check.md`, `.plan-coverage-check.md`,
# `.sonnet-review.md`, `.docs-check.md`) plus `INDEX.md`/`README.md`. This
# basename shape, combined with the frontmatter and `kind` checks below,
# was verified over the live 334-file corpus to select exactly the 170 real
# plans and zero sidecars — see the absence-plan's substrate-finding table;
# neither leg of the conjunction alone was sufficient.
_PLAN_BASENAME = re.compile(r"^\d{4}-\d{2}-\d{2}-[^.]+\.md$")


def _resolve_root(explicit_root: Optional[str]) -> Optional[str]:
    if explicit_root:
        return explicit_root
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _scan_plans(
    root: str,
) -> Tuple[List[Tuple[str, str]], List[str], List[str]]:
    """Returns (dangling, missing, unreadable).

    `dangling` is a list of (rel_plan_path, cited_path) for every plan whose
    frontmatter `sizing_object` does not resolve on disk. `missing` is a
    list of rel_plan_path for every plan created on/after `_CUTOFF` whose
    frontmatter carries no `sizing_object` at all (an explicit `null`
    satisfies the check and is never in this list). `unreadable` lists
    plans this scan could not open, for a fail-closed caller decision.

    A file is a PLAN for absence purposes iff ALL THREE hold — the
    conjunction is load-bearing, each leg alone was tested against the live
    corpus and failed (see module docstring / `_PLAN_BASENAME`):
      1. basename matches `_PLAN_BASENAME`
      2. `split_frontmatter` returns non-None
      3. `kind` is falsy or equals "plan"
    The dangling leg does not need this filter — a sidecar carries no
    `sizing_object` key, so it is skipped by the dangling leg regardless.

    Reads ONLY the frontmatter mapping (`split_frontmatter` +
    `read_fm_field_unquoted`) — never the body text. See module docstring,
    AC6/AC9.
    """
    dangling: List[Tuple[str, str]] = []
    missing: List[str] = []
    unreadable: List[str] = []
    plans_dir = os.path.join(root, "docs", "plans")
    for full_path in sorted(glob.glob(os.path.join(plans_dir, "*.md"))):
        rel_path = os.path.relpath(full_path, root).replace(os.sep, "/")
        basename = os.path.basename(full_path)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            print(
                f"WARNING: unreadable plan excluded from sizing-citation scan: {full_path}: {exc}",
                file=sys.stderr,
            )
            unreadable.append(rel_path)
            continue

        split = split_frontmatter(text)
        if split is None:
            continue

        cited = read_fm_field_unquoted(split.fm_text, "sizing_object")

        if cited and cited != "null":
            if not os.path.exists(os.path.join(root, cited)):
                dangling.append((rel_path, cited))
            continue

        # cited is absent or explicit null past this point — absence leg.
        if not _PLAN_BASENAME.match(basename):
            continue
        kind = read_fm_field_unquoted(split.fm_text, "kind")
        if kind and kind != "plan":
            continue
        if cited == "null":
            continue  # explicit null is a satisfied declaration, not a finding
        created = read_fm_field_unquoted(split.fm_text, "created")
        if not created or created[:10] < _CUTOFF:
            continue
        missing.append(rel_path)

    return dangling, missing, unreadable


def main(argv: List[str]) -> int:
    root_arg: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else None
            i += 2
            continue
        i += 1

    root = _resolve_root(root_arg)
    if not root:
        return 0

    if not os.path.isdir(os.path.join(root, "docs", "plans")):
        print("OK: no docs/plans/ — nothing to check")
        return 0

    dangling, missing, unreadable = _scan_plans(root)

    if unreadable:
        for rel_plan in unreadable:
            print(f"UNSCANNABLE: {rel_plan}", file=sys.stderr)
        print(
            f"FAIL: {len(unreadable)} plan(s) could not be scanned for a sizing_object "
            "citation — assertion cannot be made (fail-closed)",
            file=sys.stderr,
        )
        return 1

    if missing:
        for rel_plan in missing:
            print(f"MISSING sizing_object in: {rel_plan}", file=sys.stderr)
            print(
                f"  created on/after {_CUTOFF} but declares no sizing_object "
                "(add a resolving path, or `sizing_object: null` to declare none)",
                file=sys.stderr,
            )
        print(
            f"FAIL: {len(missing)} plan(s) missing a sizing_object declaration",
            file=sys.stderr,
        )

    if dangling:
        for rel_plan, cited in dangling:
            print(f"DANGLING sizing_object in: {rel_plan}", file=sys.stderr)
            print(f"  {cited} does not resolve on disk", file=sys.stderr)
        print(
            f"FAIL: {len(dangling)} dangling plan sizing_object citation(s)",
            file=sys.stderr,
        )

    if missing or dangling:
        return 1

    print("OK: no dangling or missing plan sizing_object citations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
