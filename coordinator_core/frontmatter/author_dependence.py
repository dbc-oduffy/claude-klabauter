"""
coordinator_core.frontmatter.author_dependence

A checker: given a shaped artifact (frontmatter + body text), returns a verdict
plus named reasons about whether that artifact can execute without its author —
i.e. whether it was scoped in conversation in a way that silently depends on the
conversation surviving into an unattended run.

Spec: docs/plans/2026-09-06-the-artifact-that-still-needs-its-author.md, chunk C1.
Baton: state/handoffs/2026-09-06_180400_roadmap-cloud-em-05.md § Acceptance criteria.

WHY NO STALL DETECTOR (AC-7). Frontier models misframe 39-63% of ambiguous tasks
*without flagging the error* (arXiv 2505, task-framing ambiguity / Ambig-DS);
38.3% of SWE-bench samples are flagged underspecified, 68.3% once unfair tests
are also counted (arXiv 2507.21504). The failure mode this plan defends against
is confident silence, not a hang or a question — a detector that waits for a
stall, an error, or a request for clarification will never fire, because the
run that needed its author does not know it needed one. The only place a signal
still exists is the artifact itself, before dispatch. This module is therefore a
pre-dispatch property checker over the artifact text, not a runtime watchdog.

THE VERDICT PATH IS A PURE LIBRARY (AC-6). `check_artifact` and everything it
calls does no interpreter start, no subprocess, no git call, and raises nothing
on a failing property — see § Structural constraints below. That is a hard,
load-bearing property: git history access is confined to the offline labelling
harness in this module (`build_labels`, § Labelling harness), which is a
one-off corpus-analysis step, never invoked by `check_artifact`. Per-item git
spawning is guarded against fleet-wide by
`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`; the labelling
harness satisfies that guard by issuing exactly two batched `git log`
invocations over the whole corpus, never one per artifact.

AGGREGATION RULE: DISJUNCTION, k >= 1. The verdict is "flagged" iff at least one
surviving property fires; the reasons are exactly the names of the properties
that fired. One hard-coded exception: an artifact whose frontmatter cannot be
parsed at all is always flagged with `reasons=["unparseable_frontmatter"]`,
never scored against `PROPERTY_CHECKS` — see `check_artifact`'s own docstring.
This is the only aggregation rule that adds no free parameter —
a weighted sum, a majority vote, or a severity-ranked escalation is a scorer,
and the anti-scope names a scorer as the exact target the false-positive
evidence (see § Structural constraints) argues against building. Do not add
one here without going back to the plan.

THE REPORTED FLAG RATE IS NOT A TRANSPORTABLE CONSTANT. Because the rule is a
disjunction, the flag rate over any corpus is non-decreasing in the number of
surviving properties (`PROPERTY_CHECKS` below) — adding a property can only add
positives, never remove one. Any rate quoted from this module (in C2/C3, in a
report, in a future plan) is conditional on the *exact* `PROPERTY_CHECKS` list
at the time it was measured; quote the property list beside the rate, and treat
a later change to that list as invalidating a previously-quoted figure rather
than as a refinement of it. This is a distinct claim from a property's own
raw firing count, which is a corpus count, not a weighting input into the
verdict — the disjunction never inspects "how many" fired, only "did any".

STRUCTURAL CONSTRAINTS (AC-5, tested by absence in
`tests/test_author_dependence.py`):
  - No `sys.exit` anywhere in this module.
  - No `raise` inside a property check or inside `check_artifact` on a failing
    or unparseable property — an artifact this module cannot parse is a named,
    reported outcome (`"unparseable_frontmatter"`), never a silent pass and
    never a raised exception. 14 of 675 artifacts measured in an earlier survey
    (95c38a2cb9) did not parse; scoring them clean, or blowing up the caller,
    are both the exact silent-failure shape this plan exists to catch.
  - No severity ranking, no threshold, no numeric score. `Verdict.flagged` is a
    bool; `Verdict.reasons` is an unordered list of property names.
  - No `try`/`except` anywhere in the verdict path that swallows a parse
    failure into a default answer ("assume it passes" is exactly what this
    module is not allowed to do).

WHAT THIS MODULE DOES NOT DECIDE. Whether a `flagged=True` verdict blocks a
dispatch, warns, or is silently logged is a call-site decision — this module
ships no call site, no engine op, no hook. See the plan's Out-of-scope section.

THE LABEL PROXY (used only by the offline labelling harness, never by the
verdict path): two proxies for "needed its author mid-flight", which do not
measure the same thing and are both reported, never merged into one figure.

  Proxy A (continuation) — `archive/handoffs/` and `state/handoffs/` only.
  `docs/plans/` carries `predecessor_handoff`, a scaffolder-emitted provenance
  FK rather than a continuation edge, and is excluded from Proxy A entirely.
  Fires when `predecessor` is a non-`none` value that is STRING-SHAPE a
  repo-relative path ending `.md` and is NOT a bare 7-40 hex SHA (a SHA is a
  crash-time marker, not a continuation edge — the corpus carries commented
  rows saying so explicitly), or when a `continued_into` frontmatter field is
  present. This is a string-shape test, never a filesystem-resolution test:
  archiving moves the files a `predecessor` edge points at, so "does it still
  resolve on disk" would measure archive hygiene, not continuation.

  Proxy B (in-flight correction) — computed over all three corpora, and
  necessarily from git history (`build_labels`) rather than a static read,
  because the two field-level signals it depends on carry no timestamp of
  their own: `blocking_notes` is a single unversioned string scalar, and
  `gate_dependency` retirement strips the field with no marker of when it
  happened. Fires when EITHER (i) the artifact's current body contains an
  `AMENDED`/`corrected` in-flight marker (this leg alone is static — no git
  needed), OR (ii) `blocking_notes` carries two or more distinct values across
  the artifact's commit history (a temporal, git-derived signal), OR (iii) a
  `gate_dependency` field was present in some earlier revision at the
  artifact's current path and is absent from the artifact today (a temporal,
  git-derived signal).

  B-BLIND SUBSET. An artifact with exactly one commit at its current path is
  structurally invisible to Proxy B's temporal legs (ii)/(iii) — there is no
  earlier revision to diff against. That is not evidence the artifact was
  never corrected; it is a measurement gap, counted and reported, never
  silently folded into "Proxy B says no". A rename-lineage reconstruction was
  considered and rejected: `git log --name-status -M --diff-filter=R --
  archive/handoffs` returns zero rename rows on this repo, so there is no
  lineage to reconstruct.

  THE SPLIT PROTECTS AGAINST LABEL-FITTED SELECTION, NOT TEMPLATE OVERFITTING.
  The holdout split (`split_holdout`) is a stable hash of each artifact's
  repo-relative path, ~70/30 train/holdout. It bounds analyst-selection
  optimism during property choice (C1(b) reads the train arm only); it does
  NOT remove template/session clustering, since a path hash scatters template
  families evenly across both arms rather than grouping them. A future
  grouped split, if wanted, should key on (artifact class, ISO week).

Public surface:
  Verdict                         NamedTuple(flagged: bool, reasons: list[str])
  PROPERTY_CHECKS                 tuple[(name, fn), ...] — the surviving properties
  DISCARDED_PROPERTIES            tuple[dict, ...] — candidates rejected, with why
  has_unresolved_placeholder(body: str) -> bool
  has_missing_chunk_file_scope(body: str) -> bool
  check_artifact(file_text: str) -> Verdict
  split_holdout(rel_path: str) -> str        "train" | "holdout"
  proxy_a_label(fm: str) -> str | None       "positive" | "sha_excluded" | None
  build_labels(repo_root: Path | None = None) -> dict   offline harness, USES GIT
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple, Optional

from coordinator_core.frontmatter.body_blocks import LocateStatus
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

CORPUS_DIRS = ("archive/handoffs", "state/handoffs", "docs/plans")

# ---------------------------------------------------------------------------
# Verdict path — pure, no git, no subprocess (AC-6).
# ---------------------------------------------------------------------------


class Verdict(NamedTuple):
    """A checker verdict. No severity, no score — see module docstring § Structural constraints."""

    flagged: bool
    reasons: list[str]


_PLACEHOLDER_MARKERS = ("<REPLACE", "PLACEHOLDER", "TBD", "TODO:")


def has_unresolved_placeholder(body: str) -> bool:
    """Mechanically checkable, class-agnostic: an earlier survey (95c38a2cb9)
    measured 31/377 `docs/plans/` artifacts carrying one of these markers —
    the property VARIES and is kept. Substring match only, never a judgment of
    whether surrounding prose is "clear"."""
    return any(marker in body for marker in _PLACEHOLDER_MARKERS)


def has_missing_chunk_file_scope(body: str) -> bool:
    """A `docs/plans/` chunk (a row in the ` ```yaml plan-tasks ` fenced
    block) whose `change_kind` is `code-edit` and whose `writes:` is absent or
    empty. Vacuously False for any artifact carrying no `plan-tasks` block
    (every `archive/handoffs/`, `state/handoffs/` artifact, and any plan with
    no task spine yet) — this property is a structural check of the spine
    when one exists, not a class gate maintained by this function.

    Reuses `plan_tasks_render.load_rows` (the one true door onto the
    plan-tasks spine — see that module's own docstring) rather than a
    second hand-copy of `locate_fenced_block` -> `yaml.safe_load` ->
    isinstance ladder. That duplication was flagged by
    `coordinator:overengineering-reviewer`
    (docs/plans/2026-09-06-the-artifact-that-still-needs-its-author.md):
    this function's own docstring argues against a bespoke fence scan
    while a prior version committed the equivalent duplication one layer
    up. `not writes` here treats an absent `writes:` key the same as a
    declared-empty `writes: []` — both spellings are, for THIS property,
    "a code-edit row with no declared file scope", which is exactly what
    "absent or empty" above means; it is not the same question as
    `dispatch_emit/spine_read.UNDECLARED`, which distinguishes the two
    spellings for wave-collision purposes downstream. Composing
    `load_rows` changes no label: it returns the same rows this function
    used to parse by hand, `NOT_FOUND`/`MALFORMED` still yield False, and
    the `if not writes` test is untouched.
    """
    result = load_rows(body)
    if result.status is not LocateStatus.LOCATED:
        return False
    for row in result.rows:
        if row.get("change_kind") != "code-edit":
            continue
        writes = row.get("writes")
        if not writes:
            return True
    return False


PROPERTY_CHECKS: tuple[tuple[str, Callable[[str], bool]], ...] = (
    ("unresolved_placeholder", has_unresolved_placeholder),
    ("missing_chunk_file_scope", has_missing_chunk_file_scope),
)
"""The properties that survived C1(b) — kept because they are BOTH mechanically
checkable AND actually varying across the train arm (see `build_labels`'s
`property_variance` output). See `DISCARDED_PROPERTIES` for what was
considered and rejected, and why."""


DISCARDED_PROPERTIES: tuple[dict, ...] = (
    {
        "name": "acceptance_criterion_no_checkbox",
        "reason": (
            "70/377 docs/plans artifacts carry an AC/exit-criteria heading with "
            "no checkbox, but plans use numbered exit criteria by design — this "
            "is a BATON-class property (state/handoffs, archive/handoffs), not "
            "a plan-class one. Scoping it per artifact class would add a second "
            "dimension (class x property) to a module whose whole point is one "
            "corpus walk producing one flat property list; discarded rather "
            "than scoped."
        ),
    },
    {
        "name": "spec_cites_conversation_not_artifact",
        "reason": (
            "0/299 state/handoffs batons fire on prose heuristics for this, and "
            "it requires judging prose to detect at all — discarded on both "
            "grounds. This was the anti-scope's own named example of what not "
            "to build (an ambiguity/quality judgment), and the corpus "
            "independently agrees it would never fire even if built."
        ),
    },
)


def check_artifact(file_text: str) -> Verdict:
    """Given a whole artifact's file text, return a Verdict.

    An artifact whose frontmatter cannot be parsed is reported as
    `flagged=True, reasons=["unparseable_frontmatter"]` — never scored clean
    and never raised. This is the single named exception to "the verdict is
    the disjunction over PROPERTY_CHECKS": an unparseable artifact cannot even
    be evaluated for those properties, so it is a distinct, explicit reason,
    not a silent absence of reasons standing in for "fine".
    """
    split = split_frontmatter(file_text)
    if split is None:
        return Verdict(flagged=True, reasons=["unparseable_frontmatter"])
    body = split.body_with_leading_newline
    reasons = [name for name, fn in PROPERTY_CHECKS if fn(body)]
    return Verdict(flagged=bool(reasons), reasons=reasons)


# ---------------------------------------------------------------------------
# Holdout split — pure, deterministic, no git.
# ---------------------------------------------------------------------------

_HOLDOUT_FRACTION = 0.30


def split_holdout(rel_path: str) -> str:
    """Stable hash of `rel_path` into "train" or "holdout", ~70/30. Recorded
    in the goldens rather than re-drawn — the split is a property of the path
    string, not of when this function runs.
    (Review: coordinator:code-reviewer — the divisor was `0xFFFFFFFF`
    (2**32 - 1), which makes the max representable bucket exactly `1.0`
    instead of `~0.99999999977` and biases the split by one part in four
    billion. Corrected to the conventional `0x100000000` (2**32). This was
    first held back as "changing it would move published figures", then
    MEASURED rather than assumed: across all 1657 corpus artifacts, ZERO
    change arm under the corrected divisor — the boundary moves by 7.0e-11
    while the nearest artifact sits 1.1e-5 away, five orders of magnitude of
    margin. No label moves and no published figure moves, so the deviation
    was never worth carrying.)"""
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0x100000000
    return "holdout" if bucket < _HOLDOUT_FRACTION else "train"


# ---------------------------------------------------------------------------
# Proxy A (continuation) — string-shape, no filesystem resolution, no git.
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_PATH_LIKE_RE = re.compile(r"^[\w./\-]+\.md$")


def _predecessor_is_continuation_path(value: str) -> bool:
    """String-shape predicate only (§ Proxy A in the module docstring): a
    repo-relative path ending `.md` that is NOT a bare 7-40 hex SHA. Never
    resolves against the filesystem — archiving relocates the files a
    `predecessor` edge names, so disk resolution would measure archive
    hygiene rather than continuation."""
    value = value.strip()
    if not value or value.lower() == "none":
        return False
    if _SHA_RE.match(value):
        return False
    return bool(_PATH_LIKE_RE.match(value))


def proxy_a_label(fm: str) -> Optional[str]:
    """Proxy A over one artifact's frontmatter text. Returns:
      "positive"     — continuation edge present (predecessor path or continued_into)
      "sha_excluded" — predecessor present but SHA-shaped (excluded, counted separately)
      None           — no continuation signal

    Caller restricts this to `archive/handoffs/` and `state/handoffs/` only —
    this function does not know its own corpus, by design (it is a pure
    field-shape predicate, reusable regardless of which directory calls it).
    """
    predecessor = read_fm_field_unquoted(fm, "predecessor")
    continued_into = read_fm_field_unquoted(fm, "continued_into")
    if continued_into and continued_into.strip().lower() != "none":
        return "positive"
    if predecessor and predecessor.strip().lower() != "none":
        if _predecessor_is_continuation_path(predecessor):
            return "positive"
        if _SHA_RE.match(predecessor.strip()):
            return "sha_excluded"
    return None


# ---------------------------------------------------------------------------
# Proxy B (in-flight correction) — the AMENDED/corrected leg is static; the
# blocking_notes-churn and gate_dependency-retirement legs need git history
# and live only in the labelling harness below.
# ---------------------------------------------------------------------------

_INFLIGHT_MARKER_RE = re.compile(r"\bAMENDED\b|\bcorrected\b", re.IGNORECASE)


def has_inflight_marker(body: str) -> bool:
    """The one static leg of Proxy B — no git needed. Kept separate from
    `PROPERTY_CHECKS` deliberately: Proxy B is a LABEL for measuring this
    checker against, not a property this checker fires on itself (an
    artifact correcting itself in-flight is evidence for the label, not an
    author-dependence defect in the artifact)."""
    return bool(_INFLIGHT_MARKER_RE.search(body))


# ---------------------------------------------------------------------------
# Labelling harness (offline, one-off corpus analysis). USES GIT.
#
# This is the sole carve-out from the no-git constraint that governs the
# verdict path above — see module docstring. Exactly two batched `git log`
# invocations cover the entire ~1650-artifact corpus; there is no per-item
# git spawn anywhere in this function or its helpers.
# ---------------------------------------------------------------------------

_COMMIT_MARK = "C"
_BN_RE = re.compile(r"^blocking_notes:\s*(.*)$")
_GD_RE = re.compile(r"^gate_dependency:")
_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _corpus_files(repo_root: Path) -> list[str]:
    files: list[str] = []
    for corpus in CORPUS_DIRS:
        for p in sorted((repo_root / corpus).rglob("*.md")):
            files.append(str(p.relative_to(repo_root)).replace("\\", "/"))
    return files


def _git_commit_counts(repo_root: Path, corpus_files: set) -> dict[str, int]:
    """One batched `git log --name-only` invocation; counts commits per path,
    restricted to paths currently in the corpus."""
    proc = subprocess.run(
        ["git", "log", f"--format={_COMMIT_MARK}%n%H", "--name-only", "--"]
        + list(CORPUS_DIRS),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    lines = proc.stdout.splitlines()
    counts: dict[str, set] = {}
    i = 0
    cur_commit = None
    while i < len(lines):
        line = lines[i]
        if line == _COMMIT_MARK:
            cur_commit = lines[i + 1] if i + 1 < len(lines) else None
            i += 2
            continue
        stripped = line.strip()
        if stripped and cur_commit and stripped in corpus_files:
            counts.setdefault(stripped, set()).add(cur_commit)
        i += 1
    return {path: len(commits) for path, commits in counts.items()}


def _git_temporal_signals(
    repo_root: Path, corpus_files: set
) -> tuple[dict[str, set], set]:
    """One batched `git log -p` invocation; returns (blocking_notes value
    sets per path, paths that ever had a gate_dependency line removed),
    restricted to paths currently in the corpus."""
    proc = subprocess.run(
        ["git", "log", "-p", f"--format={_COMMIT_MARK}%n%H", "--"]
        + list(CORPUS_DIRS),
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    blocking_notes_values: dict[str, set] = {}
    gate_dep_removed_ever: set = set()
    cur_path = None
    in_corpus = False
    for line in proc.stdout.splitlines():
        if line.startswith("diff --git"):
            m = _DIFF_GIT_RE.match(line)
            cur_path = m.group(2) if m else None
            in_corpus = cur_path in corpus_files if cur_path else False
            continue
        if not in_corpus:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            content = line[1:].strip()
            bm = _BN_RE.match(content)
            if bm:
                blocking_notes_values.setdefault(cur_path, set()).add(
                    bm.group(1).strip()
                )
            if line.startswith("-") and _GD_RE.match(content):
                gate_dep_removed_ever.add(cur_path)
    return blocking_notes_values, gate_dep_removed_ever


def build_labels(repo_root: Optional[Path] = None) -> dict:
    """The offline labelling pass (C1(a)/(c)). Walks the corpus once, computes
    Proxy A, Proxy B (via two batched git log calls — see module docstring),
    the holdout split, the A-vs-B contingency table, the B-blind subset, and
    per-property firing counts over the train arm. Returns a plain dict —
    this is the shape written to `tests/_goldens/author_dependence_labels.json`.

    Never called by `check_artifact` or any verdict-path function — this is
    the git carve-out, confined to this one function and its two private
    helpers above.

    THE CORPUS IS LIVE; THE GOLDEN IS A SNAPSHOT, NOT A PIN. `CORPUS_DIRS`
    are three directories that grow every session (`docs/plans` alone moved
    378 -> 380 within one session of the golden being committed). Nothing
    in this repo re-derives `tests/_goldens/author_dependence_labels.json`
    automatically; `python -m coordinator_core.frontmatter.author_dependence
    --write` is the one committed producer (§ `if __name__ == "__main__"`
    below), and running it re-reads the corpus at whatever commit is
    checked out, not at the ref the golden was last written from. Treat the
    golden as "the corpus as of the commit that last ran --write", never as
    a stable regression baseline that a passing test suite implies still
    matches disk.
    """
    root = repo_root or _REPO_ROOT
    files = _corpus_files(root)
    files_set = set(files)

    commit_counts = _git_commit_counts(root, files_set)
    blocking_notes_values, gate_dep_removed_ever = _git_temporal_signals(
        root, files_set
    )

    per_artifact: dict[str, dict] = {}
    sha_excluded_by_corpus = {"archive/handoffs": 0, "state/handoffs": 0}
    unparseable: list[str] = []

    for rel_path in files:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
        split = split_frontmatter(text)
        corpus = next(c for c in CORPUS_DIRS if rel_path.startswith(c + "/"))
        entry: dict = {
            "corpus": corpus,
            "split": split_holdout(rel_path),
            "b_blind": commit_counts.get(rel_path, 0) <= 1,
        }
        if split is None:
            unparseable.append(rel_path)
            entry["proxy_a"] = None
            entry["proxy_b"] = None
            per_artifact[rel_path] = entry
            continue

        fm = split.fm_text
        body = split.body_with_leading_newline

        # Tri-valued on purpose. Collapsing "sha_excluded" into the same
        # None as "no signal" loses the only per-artifact record of WHICH
        # rows the sha exclusion removed -- the count survives solely as the
        # `sha_excluded_by_corpus` aggregate, which is not split by
        # train/holdout. Downstream that made Proxy A's negative denominator
        # (and therefore its flag rate and lift, the doc's headline figures)
        # unreproducible from this goldens file: a recomputation overcounts
        # negatives by exactly the exclusion total with no way to identify
        # the rows. Readers must test `is True` / `== "sha_excluded"`, never
        # truthiness -- the string is truthy and a bool() coercion silently
        # counts an excluded row as a Proxy A positive.
        proxy_a: object = None
        if corpus in ("archive/handoffs", "state/handoffs"):
            label = proxy_a_label(fm)
            if label == "sha_excluded":
                sha_excluded_by_corpus[corpus] += 1
                proxy_a = "sha_excluded"
            elif label == "positive":
                proxy_a = True
        entry["proxy_a"] = proxy_a

        gd_retired = rel_path in gate_dep_removed_ever and not read_fm_field_unquoted(
            fm, "gate_dependency"
        )
        bn_churn = len(blocking_notes_values.get(rel_path, ())) >= 2
        proxy_b = has_inflight_marker(body) or bn_churn or gd_retired
        entry["proxy_b"] = proxy_b

        verdict = check_artifact(text)
        entry["verdict_flagged"] = verdict.flagged
        entry["verdict_reasons"] = verdict.reasons

        per_artifact[rel_path] = entry

    # Contingency table + per-property variance, computed on the
    # B-observable subset (excludes b_blind, per the plan's pre-registration).
    observable = {p: e for p, e in per_artifact.items() if not e["b_blind"]}

    a_only = b_only = a_and_b = neither = 0
    a_only_verdict_diff = b_only_verdict_diff = 0
    for e in observable.values():
        # `is True`, never bool(): "sha_excluded" is a truthy string and a
        # coercion here would count an excluded row as a Proxy A positive.
        a = e["proxy_a"] is True
        b = bool(e["proxy_b"])
        if a and b:
            a_and_b += 1
        elif a and not b:
            a_only += 1
        elif b and not a:
            b_only += 1
        else:
            neither += 1

    contingency = {
        "a_only": a_only,
        "b_only": b_only,
        "a_and_b": a_and_b,
        "neither": neither,
        "b_observable_total": len(observable),
        "b_blind_total": sum(1 for e in per_artifact.values() if e["b_blind"]),
    }

    train_files = [p for p, e in per_artifact.items() if e["split"] == "train"]
    property_variance = {}
    for name, _fn in PROPERTY_CHECKS:
        fired = sum(
            1 for p in train_files if name in per_artifact[p].get("verdict_reasons", [])
        )
        property_variance[name] = {"fired": fired, "of": len(train_files)}

    return {
        "corpus_totals": {
            "archive/handoffs": sum(1 for f in files if f.startswith("archive/handoffs/")),
            "state/handoffs": sum(1 for f in files if f.startswith("state/handoffs/")),
            "docs/plans": sum(1 for f in files if f.startswith("docs/plans/")),
            "total": len(files),
        },
        "unparseable": sorted(unparseable),
        "unparseable_count": len(unparseable),
        "sha_excluded_by_corpus": sha_excluded_by_corpus,
        "single_commit_at_current_path": sum(
            1 for e in per_artifact.values() if e["b_blind"]
        ),
        "contingency": contingency,
        "property_variance": property_variance,
        "discarded_properties": [d["name"] for d in DISCARDED_PROPERTIES],
        "holdout_fraction": _HOLDOUT_FRACTION,
        "per_artifact": per_artifact,
    }


if __name__ == "__main__":
    # Review: overengineering-reviewer — `out_path` used to be computed and
    # never written, so the golden below had no committed regeneration path
    # at all (a hand gesture in an ended session produced it). `--write` is
    # the closed gap; the default stays a dry summary print so running this
    # module never mutates a committed fixture by accident.
    #
    # CORPUS IS LIVE, GOLDEN IS NOT A PIN. `CORPUS_DIRS` are three growing
    # repo directories, not a frozen input — the golden this writes is a
    # snapshot at whatever commit `--write` was last run against, not a
    # stable regression baseline. It has already drifted under itself
    # within a single session (docs/plans: 378 at golden-write time, 380
    # on disk shortly after). Running `--write` again picks up that drift
    # and moves the numbers `docs/reference/author-dependence-check.md`
    # quotes — do that deliberately, on its own commit, never as a side
    # effect of an unrelated change.
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description=(
            "Author-dependence label builder. Default: print a summary "
            "(no per_artifact rows) and exit. --write: also write the full "
            "labels, including per_artifact, to the golden path below."
        )
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write the full labels to the golden JSON path (mutates a committed fixture).",
    )
    args = parser.parse_args()

    labels = build_labels()
    out_path = (
        _REPO_ROOT
        / "coordinator_core"
        / "frontmatter"
        / "tests"
        / "_goldens"
        / "author_dependence_labels.json"
    )
    summary = {k: v for k, v in labels.items() if k != "per_artifact"}
    print(json.dumps(summary, indent=2))

    if args.write:
        out_path.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {out_path}", flush=True)
