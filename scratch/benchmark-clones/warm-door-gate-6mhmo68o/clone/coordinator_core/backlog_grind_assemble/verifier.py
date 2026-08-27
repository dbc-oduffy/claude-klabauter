"""coordinator_core.backlog_grind_assemble.verifier — the one Haiku-verifier
dispatch primitive shared by bug-blitz's (`readers_blitz.py`) and
mise-en-place's (`readers_mise.py`) per-item verify step.

Both call sites dispatch a Haiku agent, `run_in_background: true`, that
reads a DONE summary plus a diff and writes an on-disk verdict — that
shared shape is `build_haiku_verifier_dispatch` below, returning a plain
directive dict consumed by `apply.py` (C4) through its own closed `cli`
dispatch table, exactly as every other builder in this package
(`directives.py`) does. This module builds DICTS only — it never dispatches
an agent, never shells out, and never imports `subprocess`.

The two sites diverge on everything past the shared shape, and this module
does NOT paper over that divergence:

  - **bug-blitz** (`state/bug-backlog/` items): evidence is the DONE
    summary plus the unstaged diff for the item's `files` plus the cited
    code; verdict is one of `BUG_BLITZ_VERIFIER_ENUM`
    (`PASS` / `PATTERN-STILL-PRESENT` / `FOOTPRINT-VIOLATION` /
    `REGRESSION`); output lands at
    `state/scratch/bug-blitz/{run-id}/{item-id}.verify.md`.
  - **mise-en-place** (readiness-gated backlog items): evidence is the
    DONE summary plus the spec plus the item's still-uncommitted
    working-tree diff plus any deferred-verification note the executor
    left; verdict is one of `MISE_VERIFIER_ENUM`
    (`PASS` / `FOOTPRINT-VIOLATION` / `AC-MISS` /
    `VERIFICATION-CMD-FAILED` / `NEEDS-EM`); output lands at
    `tasks/mise-verify/<item-id>.md`.

Only `PASS` and `FOOTPRINT-VIOLATION` are common to both sets — every other
value is call-site-specific and would be meaningless at the other site
(e.g. `AC-MISS` presupposes a spec that declares an acceptance surface at
all, which a bug-backlog item never carries).

`AC-MISS`'s subject was re-anchored on 2026-08-27 and the name outlived it:
it asserts that the item's spec-declared acceptance surface went
undischarged, and that surface is now the spec's `## Tasks` spine row for
the item plus its `prime_exit_criterion` — NOT a `## Acceptance Criteria`
section, which the plan scaffold no longer emits (forward-only PM ruling;
DoE-claude `pln-collapse-the-ac-checkbox-table-c53bbc`). The constant is
deliberately NOT renamed to `SPINE-MISS`: this literal is the wire value a
Haiku verdict file ends with, and DoE-claude's
`coordinator/pipelines/mise-en-place/PIPELINE.md` § Phase 5 names the same
four strings — renaming it is a two-repo coordinated change with no reader
it would make more correct. `build_haiku_verifier_dispatch`
therefore takes `enum_set` as a caller-supplied parameter and enforces only
that the two shared values are present in whatever set is passed — it does
NOT merge, widen, or offer a combined enum. See the plan's own instruction:
"Parameterize; do NOT unify the enums."

Contract: DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b7-backlog-grind-cluster-compu-bebb7c,
chunk C6.

Negative-spec:
    - Do NOT add a third enum constant that merges `BUG_BLITZ_VERIFIER_ENUM`
      and `MISE_VERIFIER_ENUM` — a merged set would let a bug-blitz verdict
      return `AC-MISS`, which is meaningless there (and symmetrically for
      an `mise` verdict returning `PATTERN-STILL-PRESENT`/`REGRESSION`).
    - Do NOT call `subprocess`, dispatch an agent, or write to disk from
      this module — every function here returns a plain dict; dispatch and
      execution are entirely the caller's (`apply.py`, C4).
    - Do NOT hardcode either call site's `output_path` pattern here — both
      `state/scratch/bug-blitz/{run-id}/{item-id}.verify.md` and
      `tasks/mise-verify/<item-id>.md` are the CALLER's computed strings
      (the reader modules, C3a/C3b), passed straight through.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "PASS",
    "FOOTPRINT_VIOLATION",
    "BUG_BLITZ_VERIFIER_ENUM",
    "MISE_VERIFIER_ENUM",
    "HAIKU_VERIFIER_CLI",
    "build_haiku_verifier_dispatch",
]

#: values common to both call sites — every `enum_set` passed to
#: `build_haiku_verifier_dispatch` must carry both.
PASS = "PASS"
FOOTPRINT_VIOLATION = "FOOTPRINT-VIOLATION"

#: bug-blitz's verdict set (`bug-blitz.md` Phase 3 step 3) — diff-plus-
#: cited-code evidence, `state/scratch/bug-blitz/{run-id}/{item-id}.verify.md`.
BUG_BLITZ_VERIFIER_ENUM: tuple[str, ...] = (
    PASS,
    "PATTERN-STILL-PRESENT",
    FOOTPRINT_VIOLATION,
    "REGRESSION",
)

#: mise-en-place's verdict set (`mise-en-place.md` Phase 5 step 2) — diff-
#: plus-spec-plus-deferred-verification-audit evidence,
#: `tasks/mise-verify/<item-id>.md`.
MISE_VERIFIER_ENUM: tuple[str, ...] = (
    PASS,
    FOOTPRINT_VIOLATION,
    "AC-MISS",
    "VERIFICATION-CMD-FAILED",
    "NEEDS-EM",
)

#: `cli` verb this module's directives carry — C4's own closed dispatch-
#: table entry (`apply.py`, not this module), never invoked here. Public
#: (not underscore-private) because `apply.py` imports it directly rather
#: than hand-maintaining its own verbatim copy — one string, one owner.
HAIKU_VERIFIER_CLI = "dispatch-haiku-verifier"

_SHARED_ENUM_VALUES = (PASS, FOOTPRINT_VIOLATION)


def build_haiku_verifier_dispatch(
    *,
    id: str,
    evidence_source: Sequence[str],
    enum_set: Sequence[str],
    output_path: str,
    depends_on: Optional[str] = None,
    extra_fields: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Build ONE `directives[]` entry for a Haiku-verifier dispatch —
    `model: haiku`, `run_in_background: true`, DONE-summary-plus-diff
    evidence, an on-disk verdict — parameterized by the three axes the two
    call sites diverge on:

      - `evidence_source`: the ordered list of evidence the verifier reads
        before writing a verdict (e.g. `["done-summary", "diff:files",
        "cited-code"]` for bug-blitz, or `["done-summary", "spec",
        "diff:footprint", "deferred-verification-audit"]` for
        mise-en-place). Passed through verbatim — this module does not
        interpret or validate individual entries beyond requiring the
        sequence be non-empty.
      - `enum_set`: the caller's own verdict vocabulary — pass
        `BUG_BLITZ_VERIFIER_ENUM` or `MISE_VERIFIER_ENUM` (or a future
        third call site's own tuple). Must include both `PASS` and
        `FOOTPRINT_VIOLATION` (see module docstring) — anything else is
        the caller's choice and is NOT validated further here; unifying
        the two known sets or inventing a merged vocabulary is exactly
        what this function refuses to do.
      - `output_path`: the caller's own computed on-disk verdict path
        (`state/scratch/bug-blitz/{run-id}/{item-id}.verify.md` or
        `tasks/mise-verify/<item-id>.md`) — a plain string this module
        never parses or reshapes.

    `depends_on` is optional and, when supplied, is a single judgment-point
    id (a bare `str`), matching every other builder in this package (see
    `directives.py`'s AC28 note) — a verifier dispatch has no structural
    need for the array (AND-semantics) form.

    `extra_fields`, when supplied, is merged into the returned directive's
    top level (e.g. a call site's own item-id/run-id bookkeeping fields the
    caller wants carried alongside the dispatch, not reinterpreted here).
    Reserved keys (`id`, `cli`, `args`, `model`, `run_in_background`,
    `evidence_source`, `enum_set`, `output_path`, `depends_on`) are never
    overridden by `extra_fields` — a collision raises rather than silently
    clobbering the primitive's own fields.
    """
    if not id:
        raise ValueError("build_haiku_verifier_dispatch: id must be non-empty")
    if not evidence_source:
        raise ValueError(
            "build_haiku_verifier_dispatch: evidence_source must be non-empty"
        )
    if not enum_set:
        raise ValueError("build_haiku_verifier_dispatch: enum_set must be non-empty")
    missing_shared = [v for v in _SHARED_ENUM_VALUES if v not in enum_set]
    if missing_shared:
        raise ValueError(
            "build_haiku_verifier_dispatch: enum_set must include the shared "
            f"verdict values {_SHARED_ENUM_VALUES}; missing {missing_shared!r}"
        )
    if not output_path:
        raise ValueError("build_haiku_verifier_dispatch: output_path must be non-empty")

    directive: dict[str, Any] = {
        "id": id,
        "cli": HAIKU_VERIFIER_CLI,
        "args": [],
        "model": "haiku",
        "run_in_background": True,
        "evidence_source": list(evidence_source),
        "enum_set": list(enum_set),
        "output_path": output_path,
        "depends_on": depends_on,
    }

    if extra_fields:
        collisions = sorted(set(extra_fields) & set(directive))
        if collisions:
            raise ValueError(
                "build_haiku_verifier_dispatch: extra_fields collides with "
                f"reserved directive keys: {collisions!r}"
            )
        directive.update(extra_fields)

    return directive
