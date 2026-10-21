"""
coordinator_core.execute_plan_assemble.goal_gate -- the goal-falsifier
stamp-decision gate and the AC-table desync finding, as a light leaf.

Purpose: `plan_status_transition._stamp_implemented` (the implemented flip
`dispatch.land_run` runs) needs `_evaluate_goal_falsifier_gate`,
`_goal_refusal_next_move` and `_ac_table_desync_finding`, but
`close_out_and_stamp` imports `plan_status_transition` at module scope and
carries a heavy closure (git.commit, ceremony.*, the bin/lib bootstrap).
These functions live here so the flip imports only this module;
`close_out_and_stamp` re-exports every name under its existing spelling, so
there is exactly one definition.

Negative-spec:
    - Never imports `close_out_and_stamp` or `plan_status_transition`: that
      cycle is why the flip's import was function-local.
    - Does NOT skip or pre-decide the gate: every arm of
      `_evaluate_goal_falsifier_gate` runs exactly as it did in
      `close_out_and_stamp`.
    - A test that monkeypatches one of these names patches THIS module: the
      functions resolve their helpers through this module's globals, so a
      patch on the `close_out_and_stamp` re-export does not reach them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

import yaml

from coordinator_core.execute_plan_assemble.falsifier_shape import (
    _BASELINE_REF_CROSS_REPO_RE,
    _DISPOSITION_REF_QUALIFIED_RE,
    _DISPOSITION_REF_SHA_RE,
    GOAL_REFUSAL_FALSIFIER_ABSENT,
    GOAL_REFUSAL_FALSIFIER_MISNESTED,
    GOAL_REFUSAL_PRIME_ABSENT,
    GRANDFATHER_DATE,
    _falsifier_block,
    _falsifier_misnested,
    goal_falsifier_defect,
)
from coordinator_core.execute_plan_assemble.row_spans import _run_git
from coordinator_core.frontmatter.primitives import (
    canonical_body_sha,
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops._sizing_citation import resolve_sizing_citation
from coordinator_core.session.record_homes import home_dir

#: Rejection reasons `_verify_disposition_ref` / `_verify_baseline_ref`
#: return. `INDETERMINATE` is a statement about the probe (a git timeout),
#: never folded into `UNRESOLVABLE`/`NOT_ANCESTOR`, which are verdicts about
#: the sha.
DISPOSITION_REF_ABSENT = "absent"
DISPOSITION_REF_MALFORMED = "malformed"
DISPOSITION_REF_UNRESOLVABLE = "unresolvable"
DISPOSITION_REF_NOT_ANCESTOR = "non-ancestor"
DISPOSITION_REF_INDETERMINATE = "git-timed-out"


def _registered_repo_root(repo_key: str) -> Optional[Path]:
    """`repos.<repo_key>` from the machine-local registry as an existing
    directory, else `None` (unregistered key, or a path absent on this
    machine). Zero spawns."""
    raw = registry_get(f"repos.{repo_key}")
    if not raw:
        return None
    root = Path(raw)
    return root if root.is_dir() else None


def _verify_disposition_ref(
    repo_root: Path, ref: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Verifies a single row's `disposition_ref` value as commit-required
    evidence.

    Returns `(sha, reason)`: `sha` is the ref's own full, resolved commit sha
    when -- and ONLY when -- it names a real commit object in `repo_root`'s
    history that `git merge-base --is-ancestor` proves is reachable from
    `HEAD`. Otherwise `sha` is `None` and `reason` is exactly one of
    `DISPOSITION_REF_ABSENT` (not a non-blank string), `DISPOSITION_REF_
    MALFORMED` (neither a bare hex sha nor a `<repo_key>:<sha>` shape --
    checked before ever reaching git, so a `-`-leading token or a symbolic
    ref like `HEAD~3` never resolves to something the author did not pin),
    `DISPOSITION_REF_UNRESOLVABLE` (well-shaped, but `git rev-parse --verify`
    cannot resolve it to a commit object in the named repo; for the
    qualified form also an unregistered `repos.<repo_key>` or a registered
    path that is not a directory), `DISPOSITION_REF_NOT_ANCESTOR` (a real
    commit the named repo's `HEAD` never reached), or
    `DISPOSITION_REF_INDETERMINATE` (a git probe timed out -- neither proven
    nor disproven). Never raises."""
    if not isinstance(ref, str) or not ref.strip():
        return None, DISPOSITION_REF_ABSENT
    ref = ref.strip()
    qualified = _DISPOSITION_REF_QUALIFIED_RE.match(ref)
    if qualified is not None:
        verify_root = _registered_repo_root(qualified.group(1))
        if verify_root is None:
            return None, DISPOSITION_REF_UNRESOLVABLE
        ref = qualified.group(2)
    elif _DISPOSITION_REF_SHA_RE.match(ref):
        verify_root = repo_root
    else:
        return None, DISPOSITION_REF_MALFORMED

    resolve_result = _run_git(["rev-parse", "--verify", f"{ref}^{{commit}}"], verify_root)
    if resolve_result.timed_out:
        return None, DISPOSITION_REF_INDETERMINATE
    sha = (resolve_result.stdout or "").strip()
    if resolve_result.returncode != 0 or not sha:
        return None, DISPOSITION_REF_UNRESOLVABLE

    ancestor_result = _run_git(["merge-base", "--is-ancestor", sha, "HEAD"], verify_root)
    if ancestor_result.timed_out:
        return None, DISPOSITION_REF_INDETERMINATE
    if ancestor_result.returncode != 0:
        return None, DISPOSITION_REF_NOT_ANCESTOR

    return sha, None


def _verify_baseline_ref(
    repo_root: Path, ref: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Verifies `falsifier.baseline_ref` (AC6) -- a strict superset of
    `_verify_disposition_ref`'s own bare-hex-sha, same-repo, ancestor-of-
    HEAD form, plus a `<repo>:<sha>` cross-repo form for a publisher-side
    plan whose baseline necessarily lives in the mirror repo. A bare hex sha
    is delegated to `_verify_disposition_ref` unchanged.

    The cross-repo form resolves `<repo>` through the machine-local registry
    and verifies `<sha>` names a real commit object IN THAT REPO's OWN
    history; `merge-base --is-ancestor HEAD` is deliberately never run for
    it, since the sha belongs to a different repo's commit graph.

    Returns the same `(sha, reason)` shape as `_verify_disposition_ref`.
    Never raises."""
    if not isinstance(ref, str) or not ref.strip():
        return None, DISPOSITION_REF_ABSENT
    stripped = ref.strip()
    qualified = _BASELINE_REF_CROSS_REPO_RE.match(stripped)
    if qualified is None:
        return _verify_disposition_ref(repo_root, stripped)

    repo_key, sha_token = qualified.group(1), qualified.group(2)
    sibling_root = _registered_repo_root(repo_key)
    if sibling_root is None:
        return None, DISPOSITION_REF_UNRESOLVABLE

    resolve_result = _run_git(["rev-parse", "--verify", f"{sha_token}^{{commit}}"], sibling_root)
    if resolve_result.timed_out:
        return None, DISPOSITION_REF_INDETERMINATE
    sha = (resolve_result.stdout or "").strip()
    if resolve_result.returncode != 0 or not sha:
        return None, DISPOSITION_REF_UNRESOLVABLE
    return sha, None


def _read_status_override(plan_text: str) -> Optional[dict[str, str]]:
    """The sanctioned escape for arms 2-5 (AC19): an existing
    `status_override_by`/`_reason`/`_at` attestation (the same trio
    `plan_status_transition._stamp_implemented` already writes for the
    frozen-status override), bound to the CURRENT plan-body hash
    (`canonical_body_sha`) by requiring that hash appear, verbatim hex,
    inside `status_override_reason` -- an override attested against an
    earlier body state does not silently keep suppressing the falsifier
    refusal after the body has since been re-authored underneath it. All
    three fields must be present and non-blank, and the plan's own body
    must hash at all (a plan whose frontmatter cannot be split has no body
    to bind against). Returns the three raw field values plus the bound
    hash on a valid, current attestation, or `None` on anything else --
    never raises."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    by = read_fm_field_unquoted(split.fm_text, "status_override_by")
    reason = read_fm_field_unquoted(split.fm_text, "status_override_reason")
    at = read_fm_field_unquoted(split.fm_text, "status_override_at")
    if not by or not by.strip():
        return None
    if not reason or not reason.strip():
        return None
    if not at or not at.strip():
        return None
    body_sha = canonical_body_sha(plan_text)
    if not body_sha or body_sha not in reason:
        return None
    return {"by": by.strip(), "reason": reason.strip(), "at": at.strip(), "body_sha": body_sha}


def _sibling_sizing_resolves(derived_from: str, root: Path) -> bool:
    """A multi-repo slate's plans derive from one sizing living in a sibling
    repo: on a miss in the plan's own repo, probe every `repos.*` registry
    entry other than `root`. Zero spawns -- `registry_get` reads the TOML
    directly."""
    try:
        from coordinator_core.machine_resolver import registry_dir, load_flat_registry_file
    except ImportError:
        return False
    keys: set[str] = set()
    for fname in ("registry.local.toml", "registry.toml"):
        try:
            flat = load_flat_registry_file(registry_dir() / fname)
        except Exception:
            continue
        keys.update(k for k in flat if k.startswith("repos.") and not k.endswith("_url"))

    own = str(root.resolve())
    for key in sorted(keys):
        value = registry_get(key)
        if not value or not Path(value).is_dir() or str(Path(value).resolve()) == own:
            continue
        if resolve_sizing_citation(value, derived_from) is not None:
            return True
    return False


def _resolve_derived_from(derived_from: str, root: Path) -> Optional[str]:
    """Resolves `prime_exit_criterion.derived_from` against the plan's own
    repo (AC20) -- a link a reader can open and compare, never a
    self-declared provenance flag. The schema's own `pattern` already
    narrows the string to one of two shapes:

      - `state/sizings/<id>.yaml` -- must resolve either at the literal
        path or, on a miss, under `archive/sizings/**` by same basename
        (`resolve_sizing_citation`, DR-293).
      - `<goal_id>#kr-<kr-id>` -- `goal_id` must name a goal whose own `id`
        field is found among `state/goals/*.yaml`, AND that goal's
        `key_results[]` must carry an entry whose `id` equals the FULL
        `kr-<kr-id>` token after the `#` -- an id, never an array index.

    Returns `None` on a resolved link, otherwise a short message naming
    WHICH half failed. Zero git spawns. A malformed/unreadable
    `state/goals/*.yaml` file is skipped, never fatal."""
    if "#" not in derived_from:
        if resolve_sizing_citation(root, derived_from) is None and not _sibling_sizing_resolves(
            derived_from, root
        ):
            return f"sizing object not found: {derived_from}"
        return None

    goal_id, _, kr_id = derived_from.partition("#")
    goals_dir = Path(home_dir(str(root), "goals"))
    if not goals_dir.is_dir():
        return f"goal id not found (no state/goals/ directory): {goal_id}"
    for goal_path in sorted(goals_dir.glob("*.yaml")):
        try:
            doc = yaml.safe_load(goal_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(doc, dict) or doc.get("id") != goal_id:
            continue
        key_results = doc.get("key_results")
        if isinstance(key_results, list):
            for kr in key_results:
                if isinstance(kr, dict) and kr.get("id") == kr_id:
                    return None
        return f"key result id not found: {kr_id} in goal {goal_id}"
    return f"goal id not found: {goal_id}"


GOAL_REFUSAL_EXIT_CRITERION_ABSENT = "exit_criterion_met_absent"
GOAL_REFUSAL_NOT_ASSERTED = "exit_criterion_not_asserted"
GOAL_REFUSAL_BASELINE_REF_PREFIX = "baseline_ref_"
GOAL_REFUSAL_VERDICT_NOT_PASS = "falsifier_verdict_not_pass"
GOAL_REFUSAL_DERIVED_FROM_UNRESOLVABLE = "derived_from_unresolvable"

#: Arm 0's own next move: this plan has no observation to re-run, so the
#: generic `_GOAL_REFUSAL_NEXT_MOVE` would misdirect.
_PRIME_ABSENT_NEXT_MOVE = (
    "Author prime_exit_criterion (statement + derived_from, plus a falsifier "
    "or a named falsifier_exemption) in this plan's frontmatter, then re-run "
    "the close-out."
)

#: The absent-falsifier arm's own next move: the plan has a criterion and no
#: instrument, so it is told to author the falsifier, not the statement.
_FALSIFIER_ABSENT_NEXT_MOVE = (
    "Author prime_exit_criterion.falsifier (how + baseline_output + "
    "baseline_ref + expected_when_true, captured against a baseline that "
    "reads FALSE), or a named falsifier_exemption if this criterion is "
    "genuinely un-falsifiable by observation, then re-run the close-out."
)

#: The misnested-falsifier arm's own next move (gh-klabauter#63): the field
#: is authored, just one level too shallow.
_FALSIFIER_MISNESTED_NEXT_MOVE = (
    "Move the top-level falsifier: block under prime_exit_criterion.falsifier "
    "(it must nest there, not sit as a sibling key), then re-run the close-out."
)

#: Shared tail of the refusal message: "the observation this refusal is about
#: was never recorded", which `plan-status-transition stamp-implemented`
#: records.
_GOAL_REFUSAL_NEXT_MOVE = (
    "Record the verdict: plan-status-transition stamp-implemented --plan <p> "
    "--falsifier-verdict pass --falsifier-output <raw> --prose <line>. "
    "This engine reads exit_criterion_met and never re-runs the falsifier."
)


def _goal_refusal_next_move(reason: Optional[str]) -> str:
    """The one next move for a given `goal_gate["reason"]` value (P129-C1),
    shared by `close_out_and_stamp` and `plan_status_transition.
    _stamp_implemented` so the two doors print the SAME wording.
    `_GOAL_REFUSAL_NEXT_MOVE` is the default tail; each of the three named
    reasons overrides it with its own move."""
    if reason == GOAL_REFUSAL_PRIME_ABSENT:
        return _PRIME_ABSENT_NEXT_MOVE
    if reason == GOAL_REFUSAL_FALSIFIER_ABSENT:
        return _FALSIFIER_ABSENT_NEXT_MOVE
    if reason == GOAL_REFUSAL_FALSIFIER_MISNESTED:
        return _FALSIFIER_MISNESTED_NEXT_MOVE
    return _GOAL_REFUSAL_NEXT_MOVE


def _evaluate_goal_falsifier_gate(
    plan_text: str, root: Path
) -> Optional[dict[str, Any]]:
    """The third refusal class of the close-out gate (C2): evaluated ONLY at
    the point `close_out_and_stamp` would otherwise set `status_target =
    "implemented"`, and by `plan_status_transition._stamp_implemented`.

    Predicate, in order:
      0. No `prime_exit_criterion` at all -> grandfathered to `None` UNLESS
         the plan is both M+ (per its linked sizing object) and `created` on
         or after `GRANDFATHER_DATE`, in which case it refuses
         (`GOAL_REFUSAL_PRIME_ABSENT`), same `status_override_*` exception as
         the arms below.
      5. `derived_from` resolution (AC20) -- independent of falsifier
         presence; an unresolvable link refuses (same override exception).
      1(cont). No usable `falsifier` block (`_falsifier_block` -- TOTAL,
         never-raising detection) -> refuses (`GOAL_REFUSAL_FALSIFIER_ABSENT`,
         or `GOAL_REFUSAL_FALSIFIER_MISNESTED` when a non-empty top-level
         `falsifier:` sibling key explains the absence) on the SAME two
         bounds arm 0 uses, M+ and created on or after `GRANDFATHER_DATE`,
         and only when no well-formed `falsifier_exemption` was taken.
      2. `exit_criterion_met` absent, or present with `asserted: false` ->
         refuse (AC4), unless a current `status_override_*` attestation
         is present (AC19).
      3. `falsifier.baseline_ref` fails `_verify_baseline_ref` -> refuse,
         naming which of its reasons (AC6), same override exception.
      4. `exit_criterion_met.falsifier_verdict != "pass"` -> refuse (AC5) --
         one enum read, never a comparison of `falsifier_output` against
         `baseline_output`.

    Returns `None` only for step 0's grandfathered case; every other path
    returns a dict:
    `{"refused": bool, "reason": Optional[str], "detail": Optional[str],
    "override": bool, "reasons": list[str], "details": list[str]}`.
    `override` is `True` whenever a current `status_override_*` attestation
    suppressed what would otherwise have been a refusal -- callers must
    report that as an override, never as a clean stamp. Never raises.

    F23: every applicable arm is evaluated -- none short-circuits the others
    -- and every refusal found is collected into `reasons`/`details`, in arm
    order. `reason`/`detail` stay singular (the FIRST collected refusal)."""
    split = split_frontmatter(plan_text)
    if split is None:
        return None
    try:
        fm = yaml.safe_load(split.fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(fm, dict):
        return None
    prime = fm.get("prime_exit_criterion")
    if prime is None:
        # Arm 0 (the grandfather-date pin). Absence was once the gate's only
        # unconditional exit, which made the whole gate opt-out by omission.
        # Bounded on BOTH axes (created on or after GRANDFATHER_DATE, M+ per
        # the linked sizing); either predicate failing to read cleanly
        # declines toward grandfathering.
        if goal_falsifier_defect(fm, root) != GOAL_REFUSAL_PRIME_ABSENT:
            return None
        if _read_status_override(plan_text) is not None:
            return {
                "refused": False,
                "reason": None,
                "detail": None,
                "override": True,
            }
        return {
            "refused": True,
            "reason": GOAL_REFUSAL_PRIME_ABSENT,
            "detail": (
                "plan declares no prime_exit_criterion, and is sized M+ with "
                f"created on or after {GRANDFATHER_DATE} (plans created before "
                "that date are grandfathered and skip this check)"
            ),
            "override": False,
        }

    result: dict[str, Any] = {
        "refused": False,
        "reason": None,
        "detail": None,
        "override": False,
        "reasons": [],
        "details": [],
    }
    override = _read_status_override(plan_text)

    def _report(reason: str, detail: str) -> None:
        """Records ONE applicable refusal (F23). `override`, when set,
        suppresses the refusal but still marks the result overridden."""
        if override is not None:
            result["override"] = True
            return
        result["refused"] = True
        result["reasons"].append(reason)
        result["details"].append(detail)
        if result["reason"] is None:
            result["reason"] = reason
            result["detail"] = detail

    derived_from = prime.get("derived_from") if isinstance(prime, dict) else None
    if isinstance(derived_from, str) and derived_from.strip():
        failure = _resolve_derived_from(derived_from.strip(), root)
        if failure is not None:
            _report(GOAL_REFUSAL_DERIVED_FROM_UNRESOLVABLE, failure)

    # Arm 1(cont), size-gated: grandfather date first, then M+, each failing
    # toward NOT refusing. A named `falsifier_exemption` is checked ahead of
    # both (inside `goal_falsifier_defect`) -- it is the schema's own escape
    # hatch and refusing a plan that took it would punish the discipline.
    falsifier = _falsifier_block(prime)
    misnested_top_level = fm.get("falsifier") if _falsifier_misnested(fm) else None
    falsifier_defect = goal_falsifier_defect(fm, root)
    if falsifier is None and falsifier_defect is not None:
        if misnested_top_level is not None:
            # Distinct from GOAL_REFUSAL_FALSIFIER_ABSENT (gh-klabauter#63):
            # the field is present, one level too shallow.
            _report(
                GOAL_REFUSAL_FALSIFIER_MISNESTED,
                "plan declares a top-level falsifier: key as a sibling of "
                "prime_exit_criterion instead of nesting it under "
                "prime_exit_criterion.falsifier, and is sized M+ with created "
                f"on or after {GRANDFATHER_DATE}",
            )
        else:
            _report(
                GOAL_REFUSAL_FALSIFIER_ABSENT,
                "plan declares prime_exit_criterion with no usable falsifier, and "
                f"is sized M+ with created on or after {GRANDFATHER_DATE} (an M+ "
                "criterion genuinely un-falsifiable by observation takes a named "
                "falsifier_exemption instead; S and XS carry no falsifier at all)",
            )

    # F23: a misnested top-level `falsifier:` dict carries the same fields
    # (`baseline_ref`, ...) as the nested one; fall back to it only when the
    # correctly nested block is absent, so its other defects are reported in
    # the same pass.
    effective_falsifier = falsifier if falsifier is not None else misnested_top_level

    exit_criterion_met = fm.get("exit_criterion_met")
    exit_criterion_ok = False
    if effective_falsifier is not None:
        if not isinstance(exit_criterion_met, dict) or "asserted" not in exit_criterion_met:
            _report(
                GOAL_REFUSAL_EXIT_CRITERION_ABSENT,
                "exit_criterion_met is absent or not an object carrying 'asserted'",
            )
        elif exit_criterion_met.get("asserted") is False:
            _report(
                GOAL_REFUSAL_NOT_ASSERTED,
                "exit_criterion_met.asserted is false: "
                f"{exit_criterion_met.get('reason') or 'no reason recorded'}",
            )
        else:
            exit_criterion_ok = True

        baseline_ref = effective_falsifier.get("baseline_ref") if isinstance(
            effective_falsifier, dict
        ) else None
        sha, ref_reason = _verify_baseline_ref(root, baseline_ref)
        if sha is None:
            _report(
                f"{GOAL_REFUSAL_BASELINE_REF_PREFIX}{ref_reason}",
                f"prime_exit_criterion.falsifier.baseline_ref did not verify: {ref_reason}",
            )

        if exit_criterion_ok:
            verdict = exit_criterion_met.get("falsifier_verdict")
            if verdict != "pass":
                _report(
                    GOAL_REFUSAL_VERDICT_NOT_PASS,
                    f"exit_criterion_met.falsifier_verdict is {verdict!r}, not 'pass'",
                )

    return result


_AC_HEADING_RE = re.compile(r"^## Acceptance Criteria\s*$", re.MULTILINE | re.IGNORECASE)
"""Anchors the plan's `## Acceptance Criteria` section. Case-insensitive:
real plans vary the heading's casing, and a case-sensitive match would read
those as "no AC heading" and go quiet on exactly the plans it watches."""

_AC_NEXT_HEADING_RE = re.compile(r"^## ", re.MULTILINE)

_AC_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_AC_TABLE_SEPARATOR_RE = re.compile(r"^[\s|:-]+$")

_AC_UNRESOLVED_STATUS_RE = re.compile(r"^(?:$|(?:pending|todo|tbd|open)\b)", re.IGNORECASE)
"""Matches an EMPTY `Status` cell, or one whose vocabulary word OPENS the cell.

Word-bounded rather than whole-cell anchored: the anchored form read
`open -- owned by X` as resolved, because the trailing reason defeated the
`$`. The empty-cell alternative is kept explicit (a bare `\\b` after an
optional group would match every cell, resolved ones included)."""
_AC_STRIKETHROUGH_CELL_RE = re.compile(r"^~~(.+)~~$", re.DOTALL)
"""Matches a `Status` cell whose ENTIRE (trimmed) content is struck through,
e.g. `~~pending~~`. Checked BEFORE `strip("*_~")` runs: stripping first
collapses `~~pending~~` to `pending` and defeats the rule that struck-through
text reads as author-resolved. Only a WHOLE-cell strikethrough counts."""
_AC_UNRESOLVED_CHECKBOX_GLYPHS = frozenset({"☐"})
"""The narrow, corpus-derived set of AC `Status` values read as UNRESOLVED
(D1/C1): a blank cell, `pending`, `todo`, `tbd`, `open`, or an empty
checkbox glyph. Everything else (`✅`, `green (<sha>)`, `superseded`,
struck-through text, any free-form terminal-looking value) is RESOLVED, on
purpose: a check with a high false-positive rate is worse than none."""


def _ac_section_text(plan_text: str) -> Optional[str]:
    """The plan body slice between its own `## Acceptance Criteria` heading
    and the next `## ` heading (or end of document) -- `None` when the plan
    carries no such heading at all."""
    match = _AC_HEADING_RE.search(plan_text)
    if match is None:
        return None
    start = match.end()
    next_heading = _AC_NEXT_HEADING_RE.search(plan_text, start)
    end = next_heading.start() if next_heading else len(plan_text)
    return plan_text[start:end]


def _parse_ac_table_rows(section_text: str) -> Optional[list[tuple[str, str]]]:
    """Parses a markdown pipe-table's rows out of an `## Acceptance
    Criteria` section slice, returning `[(ac_id, status_cell), ...]` in
    document order, or `None` when no recognizable table is present (no
    pipe-rows at all, fewer than a header+separator+one data row, or a
    header row with no cells to key off of) -- callers treat `None` as "no
    finding", never as an error (C3: degrade quietly, never raise).

    The `Status` column is located BY HEADER NAME (case-insensitive
    substring match), never a fixed index, since column order and count
    vary across plans; falls back to the LAST column when no header cell
    mentions "status". A row whose cell count doesn't match the header's is
    skipped rather than guessed at."""
    raw_rows: list[list[str]] = []
    for line in section_text.splitlines():
        match = _AC_TABLE_ROW_RE.match(line)
        if match is None:
            continue
        raw_rows.append([cell.strip() for cell in match.group(1).split("|")])
    if len(raw_rows) < 2:
        return None

    header = raw_rows[0]
    if not header or not any(header):
        return None

    status_idx: Optional[int] = None
    for idx, cell in enumerate(header):
        if "status" in cell.lower():
            status_idx = idx
            break
    if status_idx is None:
        status_idx = len(header) - 1

    body_rows = [
        row for row in raw_rows[1:] if not _AC_TABLE_SEPARATOR_RE.match("|".join(row))
    ]
    if not body_rows:
        return None

    parsed: list[tuple[str, str]] = []
    for row in body_rows:
        if len(row) != len(header) or status_idx >= len(row):
            continue
        ac_id = row[0] if row[0] else "?"
        parsed.append((ac_id, row[status_idx]))
    if not parsed:
        return None
    return parsed


def _ac_status_is_unresolved(status_cell: str) -> bool:
    """True iff an AC table `Status` cell value reads as still-unresolved per
    `_AC_UNRESOLVED_CHECKBOX_GLYPHS`'s vocabulary. Whole-cell strikethrough
    short-circuits to RESOLVED BEFORE the `*_~` delimiter strip runs (see
    `_AC_STRIKETHROUGH_CELL_RE`)."""
    trimmed = status_cell.strip()
    if _AC_STRIKETHROUGH_CELL_RE.match(trimmed):
        return False
    cleaned = trimmed.strip("*_~").strip()
    if cleaned in _AC_UNRESOLVED_CHECKBOX_GLYPHS:
        return True
    return bool(_AC_UNRESOLVED_STATUS_RE.match(cleaned.lower()))


def _ac_table_desync_finding(
    plan_text: str, spine_fully_resolved: bool
) -> Optional[dict[str, Any]]:
    """Advisory-only desync detector (C1/C2): fires when every
    commit-required `## Tasks` spine row has already reached a terminal,
    verified disposition (`spine_fully_resolved`, computed by the caller
    from the SAME verdict it derives for its own stamp decision -- no second
    completeness oracle here) while the plan's own `## Acceptance Criteria`
    table still carries at least one row this check reads as unresolved.

    Returns `None` -- "no finding" -- whenever `spine_fully_resolved` is
    `False`, the plan has no `## Acceptance Criteria` heading, its table is
    unparseable/absent, or every parsed row reads as resolved. Never raises:
    the computation is wrapped in a broad `except Exception` so a corpus
    shape the parser did not anticipate can never turn an advisory check
    into a stamp-path failure. The result never touches the stamp decision."""
    if not spine_fully_resolved:
        return None
    try:
        section = _ac_section_text(plan_text)
        if section is None:
            return None
        rows = _parse_ac_table_rows(section)
        if rows is None:
            return None
        unresolved_ac_ids = [
            ac_id for ac_id, status_cell in rows if _ac_status_is_unresolved(status_cell)
        ]
        if not unresolved_ac_ids:
            return None
        return {
            "unresolved_ac_ids": unresolved_ac_ids,
            "total_ac_rows": len(rows),
        }
    except Exception:
        return None


def _ac_advisory_text(finding: dict[str, Any]) -> str:
    """Render `_ac_table_desync_finding`'s dict into the one binding
    advisory sentence both doors emit (P129-C2), so close-out and
    `plan_status_transition._ac_open_rows_warning` cannot drift apart.

    This stays advisory-only forever (`state/cross-repo/archive/2026-08-27-
    coordinator-content-repo-em-ac-table-disposition.md`): "It reports and never
    blocks". No caller of this helper may gate on its return value."""
    return (
        " -- ADVISORY: the '## Tasks' spine is fully resolved but the "
        f"plan's own '## Acceptance Criteria' table still has "
        f"{len(finding['unresolved_ac_ids'])} of "
        f"{finding['total_ac_rows']} advisory AC cell(s) unticked "
        f"({', '.join(finding['unresolved_ac_ids'])}) -- not a "
        "gate; the mechanical oracle over this table is retired, an "
        "unticked cell carries no information in either direction, and "
        "delivery is the falsifier delta "
        "(prime_exit_criterion.exit_criterion_met.falsifier_verdict "
        "against its recorded baseline)."
    )
