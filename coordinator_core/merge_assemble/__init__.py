"""coordinator_core.merge_assemble — the compute-only READ-ONLY half of the
`merge-assemble` computed skill: `brief()` returns the ORDERED directive list
over the existing merge CLIs `merging-to-main/SKILL.md` currently hand-
sequences (`merge-recovery-and-tag-cut` resolve-tag-prefix/cut-tag,
`merge-gate-and-pr` coverage-gate/pr-body, `check-no-illegal-paths`,
`merge-release-notes-derive` flip-tags, `orphan-branch-sweep`, plus the node
ceremony hard-gate) alongside the computed decision fields this module
derives itself: `branch_state` (the clean/needs-recovery/diverged
trichotomy), a version-bump PROPOSAL, and a `gate_verdicts` scaffold `apply()`
fills in as each gate actually runs.

`brief()` never mutates — every action is returned as a `directives[]` entry
naming an existing atomic CLI; `merge_assemble.apply` (the mutating half)
recomputes this brief in-process and dispatches through
`coordinator_core.contract.apply_base`'s shared directive-execution engine.

Contract (frozen, reviewed): DoE-claude coordinator/docs/wiki/computed-skills.md
Spec backlink: DoE-claude:pln-b4-baton-branch-lifecycle-comp-780d48, chunk C6

`brief()` routes every construction through the shipped
`coordinator_core.contract.decision_object.envelope.build_envelope` /
`.judgment.build_judgment_point`/`build_disposition` constructors — matching
`baton_assemble`'s and `orient_assemble`'s shape (Review: code-reviewer —
Finding 1; this module used to hand-roll a divergent 6-key decision object
missing `artifact`/`preflight`/`decisions`/`narration`/`next_move`
outright). The four ad-hoc top-level keys the old shape carried now live in
their canonical envelope homes: `branch_state`/`release_tag_cut`/
`version_bump` (the merge target's computed state) moved into `artifact`;
`gate_verdicts` moved into `gates` — a relocation, not a redesign; every
value is preserved verbatim.

Negative-spec:
    - Do NOT re-derive the directive-dependency ordering, judgment-gating,
      session-identity propagation, or scoped-commit machinery here — that
      is `coordinator_core.contract.apply_base`'s job; this module only
      COMPUTES the decision object `apply_base`'s generic engine consumes.
    - Do NOT special-case merge's real divergence from `pickup_assemble`'s
      shape at this module's own call site (the DR-092 anti-pattern) — feed
      it back into `apply_base`'s own parameters instead (see that module's
      docstring, "PROVISIONAL through W3").
    - Do NOT hand-roll a parallel 8-key envelope or judgment-point dict
      literal — route every construction through
      `decision_object.envelope.build_envelope` / `.judgment.
      build_judgment_point`/`build_disposition`, matching every sibling
      assembler in this baton.
"""
from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import Any, Optional

from coordinator_core.contract.decision_object.envelope import build_envelope
from coordinator_core.contract.decision_object.judgment import (
    build_disposition,
    build_judgment_point,
)
from coordinator_core.git.repo_root import show_toplevel

# ---------------------------------------------------------------------------
# Exit-code contract — locally scoped to this compute-only half, NOT shared
# with `apply_base`'s mutating-half contract (per computed-skills.md
# § Exit-code contract, every compute/apply pair defines its own).
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1
EXIT_USAGE = 2
EXIT_TRANSPORT_FAIL = 3

#: Branch-state trichotomy (AC — "branch-state trichotomy").
BRANCH_STATE_CLEAN = "clean"
BRANCH_STATE_NEEDS_RECOVERY = "needs-recovery"
BRANCH_STATE_DIVERGED = "diverged"

#: Repo-root-relative path to the node ceremony-gate test entrypoint this
#: module's `d0` directive preserves as the first hard-gate (chunk C6 AC).
NODE_CEREMONY_TEST_RELPATH = ("coordinator", "tests", "plugin-ecosystem", "run.js")


def node_ceremony_gate_entrypoint(repo_root: Path) -> Path:
    """Resolves `NODE_CEREMONY_TEST_RELPATH` against `repo_root`. The suite it
    names is coordinator-claude's plugin-ecosystem contract suite, which only
    exists in a repo that owns discovery surfaces — so this path is present in
    the coordinator clone and absent in every consumer repo."""
    return repo_root.joinpath(*NODE_CEREMONY_TEST_RELPATH)


class _TransportFailure(Exception):
    """Raised when `brief()` cannot resolve a git worktree root or the git
    plumbing it reads fails outright — distinct from a business-fail (a
    resolvable repo whose branch state is merely `diverged`)."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def resolve_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """Resolves the enclosing git worktree root from `start` (defaults to
    cwd) via `coordinator_core.git.repo_root.show_toplevel` — the shared
    cwd-keyed memoized resolution seam. Returns `None` on any failure (not
    inside a worktree, git unavailable) rather than raising."""
    cwd = start or Path.cwd()
    top = show_toplevel(str(cwd))
    return Path(top) if top else None


def compute_branch_state(repo_root: Path) -> str:
    """Computes the clean/needs-recovery/diverged trichotomy from the local
    ahead/behind count against `origin/main` (`git rev-list --left-right
    --count origin/main...HEAD`, read-only — no fetch performed here; the
    skill's own Step 1.5 fetch is what keeps `origin/main` current, exactly
    as `merge-gate-and-pr coverage-gate`'s own dependency comment already
    documents for `origin/main..HEAD`).

    - 0 behind, 0 ahead -> `clean` (nothing to merge).
    - behind > 0 -> `needs-recovery` (local main is stale; the skill's
      Step 0's recovery-branch dance applies).
    - behind == 0 and ahead > 0 -> `diverged` (this branch carries unmerged
      work against a current main — the normal merge-in-flight state).

    Falls back to `diverged` (the safest — most-checked — assumption) on
    any git-plumbing failure the caller has not already turned into a
    `_TransportFailure` (e.g. no `origin/main` ref resolvable locally)."""
    try:
        proc = _run_git(["rev-list", "--left-right", "--count", "origin/main...HEAD"], repo_root)
    except (OSError, subprocess.SubprocessError):
        return BRANCH_STATE_DIVERGED
    if proc.returncode != 0:
        return BRANCH_STATE_DIVERGED
    parts = proc.stdout.split()
    if len(parts) != 2:
        return BRANCH_STATE_DIVERGED
    try:
        behind, ahead = int(parts[0]), int(parts[1])
    except ValueError:
        return BRANCH_STATE_DIVERGED
    if behind == 0 and ahead == 0:
        return BRANCH_STATE_CLEAN
    if behind > 0:
        return BRANCH_STATE_NEEDS_RECOVERY
    return BRANCH_STATE_DIVERGED


def compute_version_bump_proposal(repo_root: Path, *, tag_prefix: str = "v") -> dict[str, Any]:
    """Computes a PROPOSAL only (judgment residue `version_bump_final` is
    what the EM/PM confirms the final number on — see negative-spec in the
    module docstring: this function never claims to pick the real number).
    Reads the latest local `<tag_prefix>X.Y.Z` tag (`git tag --list
    '<prefix>*' --sort=-v:refname`, first line) and proposes a patch bump;
    `current`/`proposed` are both `None` when no matching tag exists yet
    (first release)."""
    try:
        proc = _run_git(["tag", "--list", f"{tag_prefix}*", "--sort=-v:refname"], repo_root)
    except (OSError, subprocess.SubprocessError):
        return {"current": None, "proposed": None, "bump": "patch"}
    if proc.returncode != 0:
        return {"current": None, "proposed": None, "bump": "patch"}
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        return {"current": None, "proposed": None, "bump": "patch"}
    current = lines[0]
    body = current[len(tag_prefix):] if current.startswith(tag_prefix) else current
    segments = body.split(".")
    if len(segments) != 3 or not all(seg.isdigit() for seg in segments):
        return {"current": current, "proposed": None, "bump": "patch"}
    major, minor, patch = (int(seg) for seg in segments)
    proposed = f"{tag_prefix}{major}.{minor}.{patch + 1}"
    return {"current": current, "proposed": proposed, "bump": "patch"}


def _parse_version_tag(value: str, tag_prefix: str) -> Optional[tuple[int, int, int]]:
    """Parses `value` against the exact `<tag_prefix>MAJOR.MINOR.PATCH`
    shape `compute_version_bump_proposal` itself parses (three dot-
    separated all-ASCII-digit segments after `tag_prefix`) — returns `None`
    on any mismatch rather than raising, so callers can turn a bad override
    into a loud business-fail instead of a stack trace.

    Segments are checked `seg.isascii() and seg.isdigit()`, not bare
    `seg.isdigit()` — Python's `str.isdigit()` is True for non-ASCII decimal
    digits too (e.g. Arabic-Indic `٣`), which `int()` also parses, so a
    bare `isdigit()` check alone would let a non-ASCII-digit override
    through validation. (Review: code-reviewer — Finding: `_parse_version_
    tag`'s `seg.isdigit()` accepts non-ASCII digits.)"""
    if not value or not value.startswith(tag_prefix):
        return None
    body = value[len(tag_prefix):]
    segments = body.split(".")
    if len(segments) != 3 or not all(seg.isascii() and seg.isdigit() for seg in segments):
        return None
    return tuple(int(seg) for seg in segments)  # type: ignore[return-value]


def normalize_decisions(decisions: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Normalizes a bare-string `version_bump_final` decisions entry (the
    sender's shape 2, e.g. `{"version_bump_final": "v0.16.0"}`) into the
    mapping shape `disposition_resolves_directive` requires
    (`{"disposition": "override", "value": "v0.16.0"}`) — done in exactly
    ONE place so `brief()` and `apply()` can never disagree about what a
    decisions map means: `apply()` recomputes `brief()` in-process and
    then hands the SAME normalized map to `execute_directives`, so both
    halves must read this function's output, never re-derive their own.
    Idempotent — an already-mapping entry (or any other entry shape) is
    returned unchanged. `None`/empty input normalizes to `{}`.

    NOT a uniform copy contract: the string-entry branch returns a shallow
    copy (`dict(decisions)`), but the already-normalized-dict branch
    returns the SAME object reference as `decisions` — callers must not
    rely on the return value being a distinct object from the input.
    (Review: code-reviewer — Finding: aliasing asymmetry between branches.)
    """
    if not decisions:
        return {}
    entry = decisions.get("version_bump_final")
    if isinstance(entry, str):
        normalized = dict(decisions)
        normalized["version_bump_final"] = {"disposition": "override", "value": entry}
        return normalized
    return decisions


def _resolve_version_override(
    decisions: dict[str, Any], tag_prefix: str
) -> tuple[Optional[str], Optional[str]]:
    """Reads an already-`normalize_decisions`-normalized `decisions` map
    for a `version_bump_final` override. Returns `(override_tag, None)`
    when a well-formed override is present, `(None, None)` when no
    override disposition is set at all (the `confirmed`/default path),
    and `(None, error_message)` when an override IS present but fails
    `_parse_version_tag` — never a silent fallback to the proposal."""
    entry = (decisions or {}).get("version_bump_final")
    if not isinstance(entry, dict) or entry.get("disposition") != "override":
        return None, None
    value = entry.get("value")
    parsed = _parse_version_tag(value, tag_prefix) if isinstance(value, str) else None
    if parsed is None:
        return None, (
            f"version_bump_final override {value!r} does not match "
            f"the required {tag_prefix}MAJOR.MINOR.PATCH shape"
        )
    # Reconstructed from the parsed (int, int, int) tuple, NOT the raw
    # input string — the raw string is what reaches `git tag` verbatim
    # (`cut_tag_input`/`d2`'s `cut-tag` arg), so this also normalizes away
    # a leading-zeros shape (e.g. "v01.02.03") that `_parse_version_tag`
    # accepts but is atypical/non-canonical semver. (Review: code-reviewer
    # — Findings: raw-string-verbatim-to-git-tag risk, leading-zeros gap.)
    major, minor, patch = parsed
    return f"{tag_prefix}{major}.{minor}.{patch}", None


#: Maps a `gate_verdicts` scaffold key to the directive id
#: `merge_assemble.apply.apply` actually dispatches for it (C3 AC —
#: `apply()` reads this from `execute_directives`' already-in-hand
#: `report["results"]`/`report["failed_directive"]`, never re-derives
#: state execute_directives has already discarded). There is no
#: `active_branch_guard` directive anywhere in `build_directives` — no
#: active-branch gate exists in this ceremony, so that key was removed
#: from the scaffold below rather than left dangling with no CLI to fill
#: it in (C3 exit (b) judgment for this one key; exit (a) taken for the
#: three gates that DO have a directive — see this module's C3 audit
#: entry in state/audits/2026-08-08-judgment-point-evidence-merge-assemble.md).
GATE_DIRECTIVE_IDS: dict[str, str] = {
    "coverage_gate": "d3",
    "portability_sweep": "d5",
    "check_no_illegal_paths": "d6",
}


def build_gate_verdicts_scaffold() -> dict[str, str]:
    """The `gate_verdicts` scaffold `brief()` returns — every gate this
    module's directives run is `"pending"` at compute time. `apply()` (the
    mutating half) fills in the real verdict for each key named in
    `GATE_DIRECTIVE_IDS` from `execute_directives`' own
    `report["results"]`/`report["failed_directive"]` once dispatch has run
    (C3 fix — previously documented but never implemented: no `gates` key
    was ever written to `apply()`'s report). `active_branch_guard` is
    deliberately absent — `build_directives` emits no directive for it, so
    there is no CLI result that could ever fill it in; see
    `GATE_DIRECTIVE_IDS`'s docstring."""
    return {
        "coverage_gate": "pending",
        "portability_sweep": "pending",
        "check_no_illegal_paths": "pending",
    }


def build_judgment_points(
    *, portability_sweep_result: Optional[str] = None
) -> list[dict[str, Any]]:
    """The judgment residue this chunk's AC names verbatim: ship-verdict,
    CI-failure interpretation, version-bump final number (PM-confirmed),
    portability disposition, merge-conflict resolution. Every entry pins
    `recommendation: None` (AC5c/AC5d discipline shared with `pickup_
    assemble` and `apply_base`'s own negative-spec: `recommendation` is
    advisory content for a human/EM reader, never a control-flow input) and
    is built via the shipped `build_judgment_point`/`build_disposition`
    constructors (Review: code-reviewer — Finding 1) rather than a hand-
    rolled dict literal; every `id`/`dispositions`/`recommendation` value
    is unchanged from the prior hand-rolled shape.

    `portability_sweep_result` is the two-invocation seam: d5 now runs
    UNGATED (its `depends_on` no longer names `portability_disposition`) and
    FEEDS this judgment point rather than being gated behind it. On the
    first pass no sweep result exists yet, so the `portability_disposition`
    evidence honestly names that (compute-time-scoped, not a permanent
    claim — d5 already ran, or is about to, in this same apply() pass). On
    the re-run, the caller threads the sweep's real finding — surfaced in
    the halted pass's own report — back in as `decisions["portability_sweep_
    result"]`; `brief()` passes it through here so the disposition is
    offered against that REAL result, not the point's own absence."""
    return [
        build_judgment_point(
            None,
            id="ship_verdict",
            question="Every gate has reported — ship this merge, or hold?",
            dispositions=[
                build_disposition("ship", resolves=["d2", "d4"]),
                build_disposition("hold"),
            ],
            evidence=(
                "gate_verdicts scaffold is all-pending at compute time — "
                "apply() fills in the real verdict for each gate as it runs"
            ),
            reason="insufficient-evidence",
        ),
        build_judgment_point(
            None,
            id="version_bump_final",
            question=(
                "Confirm the proposed version bump is the number to cut, "
                "or override it by supplying "
                "decisions[\"version_bump_final\"] = "
                "{\"disposition\": \"override\", \"value\": \"<tag_prefix>MAJOR.MINOR.PATCH\"} "
                "(a bare string of that shape is also accepted and normalized "
                "the same way)"
            ),
            dispositions=[
                build_disposition("confirmed", resolves=["d2"]),
                build_disposition("override", resolves=["d2"]),
            ],
            evidence=(
                "version_bump is a PROPOSAL only (patch-bump default, "
                "compute_version_bump_proposal) — the PM/EM confirms the "
                "final released number, or overrides it outright via this "
                "judgment point's own decisions entry; d2's cut-tag args "
                "are frozen from whichever number wins here at brief-"
                "compute time"
            ),
            reason="insufficient-evidence",
        ),
        build_judgment_point(
            None,
            id="portability_disposition",
            question="Proceed with the portability sweep finding, or defer it?",
            dispositions=[
                build_disposition("proceed"),
                build_disposition("defer"),
            ],
            evidence=(
                f"portability-sweep (d5) real finding: {portability_sweep_result}"
                if portability_sweep_result
                else (
                    "portability-sweep (d5) runs UNGATED in this same pass and "
                    "feeds this judgment point — no result has landed in this "
                    "compute yet; re-run with decisions[\"portability_sweep_"
                    "result\"] set to d5's reported finding once the halted "
                    "pass's report has it"
                )
            ),
            reason="insufficient-evidence",
        ),
        build_judgment_point(
            None,
            id="ci_failure_interpretation",
            question="A CI gate failed — flaky retry, or a real failure?",
            dispositions=[
                build_disposition("flaky-retry"),
                build_disposition("real-failure"),
            ],
            evidence="no CI failure has occurred yet at compute time",
            reason="insufficient-evidence",
        ),
        build_judgment_point(
            None,
            id="merge_conflict_resolution",
            question="A merge conflict occurred during d2's cut — has it been resolved?",
            dispositions=[build_disposition("resolved")],
            evidence="no merge conflict has occurred yet at compute time",
            reason="insufficient-evidence",
        ),
    ]


def build_directives(
    repo_root: Path,
    *,
    tag_prefix: str,
    proposed_tag: Optional[str],
) -> list[dict[str, Any]]:
    """The ORDERED directive list over the existing merge CLIs
    `merging-to-main/SKILL.md` currently hand-sequences. `d0` is the node
    ceremony hard-gate — always first, `depends_on: None` so it auto-fires
    unless `apply()`'s `--force` bypass marks it `already_satisfied`
    (chunk C6 AC: "preserve the node ceremony gate ... + its --force
    bypass"). Every other directive's `cli` name resolves through
    `merge_assemble.apply`'s own closed `_CLI_DISPATCH` table — never a
    brief-derived import.

    **`d0` is presence-conditional on its own entrypoint.** The suite it runs
    is coordinator-claude's plugin-ecosystem contract suite; in a repo that
    does not carry that suite the dispatch is a `MODULE_NOT_FOUND` abort on
    the FIRST hard gate, which wedges the whole ceremony over a subject the
    repo does not own. Absence therefore lands `d0` as `already_satisfied`
    with `skipped_reason` naming the missing path — a narrated no-op, not a
    silent pass. Presence still gates hard: a suite that exists and fails
    aborts the run exactly as before."""
    cut_tag = proposed_tag or f"{tag_prefix}0.0.0"
    gate_entrypoint = node_ceremony_gate_entrypoint(repo_root)
    gate_absent = not gate_entrypoint.is_file()
    node_gate: dict[str, Any] = {
        "id": "d0",
        "cli": "node-ceremony-gate",
        "args": [],
        "depends_on": None,
        "already_satisfied": gate_absent,
    }
    if gate_absent:
        node_gate["skipped_reason"] = (
            f"no node ceremony suite in this repo ({'/'.join(NODE_CEREMONY_TEST_RELPATH)} "
            "absent) — nothing to gate"
        )
    return [
        node_gate,
        {
            "id": "d1",
            "cli": "merge-recovery-and-tag-cut",
            "args": ["resolve-tag-prefix", "--config", "coordinator.local.md"],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d2",
            "cli": "merge-recovery-and-tag-cut",
            "args": ["cut-tag", cut_tag],
            "depends_on": ["version_bump_final", "ship_verdict"],
            "already_satisfied": False,
        },
        {
            "id": "d3",
            "cli": "merge-gate-and-pr",
            "args": ["coverage-gate", "origin/main..HEAD"],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d4",
            "cli": "merge-gate-and-pr",
            "args": ["pr-body", "--commit-range", "main..HEAD"],
            "depends_on": ["ship_verdict"],
            "already_satisfied": False,
        },
        {
            "id": "d5",
            "cli": "portability-sweep",
            "args": [str(repo_root), "--diff-only", "origin/main..HEAD", "--report-format", "md"],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d6",
            "cli": "check-no-illegal-paths",
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
        },
        {
            "id": "d7",
            "cli": "merge-release-notes-derive",
            "args": ["flip-tags", cut_tag],
            "depends_on": ["d2"],
            "already_satisfied": False,
        },
        {
            "id": "d8",
            "cli": "orphan-branch-sweep",
            "args": ["--format", "text", "--severity-min", "warning"],
            "depends_on": None,
            "already_satisfied": False,
        },
    ]


def _assert_resolves_depends_on_invariant(
    directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]
) -> None:
    """Structural assertion (C2 AC): every `resolves` id named by any
    disposition across `judgment_points` must name a directive whose own
    `depends_on` contains that judgment point's `id`. A dangling `resolves`
    edge — a judgment point claiming to unblock a directive that no longer
    (or never did) gate on it — is exactly the defect class this chunk
    fixes for `portability_disposition`/`d5`; this assertion is what keeps
    a future edit from reintroducing it silently, here or at any other
    judgment point/directive pair in this module."""
    depends_on_by_directive_id = {
        directive["id"]: set(directive.get("depends_on") or [])
        for directive in directives
    }
    for point in judgment_points:
        point_id = point["id"]
        for disposition in point.get("dispositions", []):
            for directive_id in disposition.get("resolves") or []:
                gating = depends_on_by_directive_id.get(directive_id, set())
                if point_id not in gating:
                    # A bare `assert` is stripped under `python -O`, degrading
                    # this guard to silence on the exact defect it exists to
                    # catch (precedent: c90b638a5, and claude_klabauter_root.py's
                    # shim-spec check).
                    raise RuntimeError(
                        f"judgment point {point_id!r} resolves directive "
                        f"{directive_id!r}, but that directive's depends_on "
                        f"{sorted(gating)!r} does not name {point_id!r} — "
                        "dangling resolves edge"
                    )


@dataclasses.dataclass(frozen=True)
class BriefResult:
    decision_object: dict[str, Any]
    exit_code: int


def brief(
    *,
    decisions: Optional[dict[str, Any]] = None,
    repo_root: Optional[Path] = None,
    tag_prefix: str = "v",
) -> BriefResult:
    """Computes and returns the merge decision object: `branch_state`,
    `version_bump` (the PROPOSAL, plus an `override` key when one is in
    play), `gate_verdicts` (scaffold — `apply()` fills real verdicts in),
    `directives[]` (the ordered CLI list), and `judgment_points[]` (the
    five-item judgment residue). `decisions` DOES feed control flow here
    now: a `version_bump_final` entry resolved to `override` (mapping
    shape `{"disposition": "override", "value": "<tag>"}`, or a bare
    string normalized into that shape by `normalize_decisions`) is what
    `build_directives` freezes into `d2`'s `cut-tag` args instead of the
    patch-bump proposal — `compute_version_bump_proposal` only ever
    proposes a patch bump, so the override channel is the only way a
    PM-chosen minor/major number reaches the cut. A malformed override
    fails loud through this function's own `EXIT_BUSINESS_FAIL` error
    channel rather than silently falling back to the proposal."""
    root = repo_root or resolve_repo_root()
    if root is None:
        return BriefResult(
            decision_object={
                "error": "could not resolve a git worktree root",
                "narration": "merge-assemble brief: no enclosing git worktree found.",
                "next_move": "Run from inside a git working tree, or pass --repo-root.",
            },
            exit_code=EXIT_TRANSPORT_FAIL,
        )

    effective_decisions = normalize_decisions(decisions)

    branch_state = compute_branch_state(root)
    version_bump = compute_version_bump_proposal(root, tag_prefix=tag_prefix)

    override_tag, override_error = _resolve_version_override(effective_decisions, tag_prefix)
    if override_error:
        return BriefResult(
            decision_object={
                "error": override_error,
                "narration": f"merge-assemble brief: {override_error}",
                "next_move": (
                    "Supply decisions[\"version_bump_final\"] as "
                    "{\"disposition\": \"override\", \"value\": "
                    f"\"{tag_prefix}MAJOR.MINOR.PATCH\"}}"
                    " (or a bare string of that shape) and re-run."
                ),
            },
            exit_code=EXIT_BUSINESS_FAIL,
        )

    cut_tag_input = override_tag if override_tag is not None else version_bump.get("proposed")
    if override_tag is not None:
        version_bump = {**version_bump, "override": override_tag}

    gate_verdicts = build_gate_verdicts_scaffold()
    directives = build_directives(root, tag_prefix=tag_prefix, proposed_tag=cut_tag_input)
    judgment_points = build_judgment_points(
        portability_sweep_result=effective_decisions.get("portability_sweep_result")
    )
    _assert_resolves_depends_on_invariant(directives, judgment_points)

    narration = (
        f"merge-assemble brief: branch_state={branch_state!r}, "
        f"{len(directives)} directive(s), {len(judgment_points)} judgment point(s)."
    )
    if override_tag is not None:
        narration += (
            f" version-bump OVERRIDE in effect: cutting {override_tag!r} "
            f"(proposal was {version_bump.get('proposed')!r})."
        )

    envelope = build_envelope(
        artifact={
            "branch_state": branch_state,
            "release_tag_cut": cut_tag_input,
            "version_bump": version_bump,
        },
        preflight={},
        gates=gate_verdicts,
        directives=directives,
        judgment_points=judgment_points,
        decisions=effective_decisions,
        narration=narration,
        next_move="Resolve the open judgment point(s), then re-run apply.",
    )
    return BriefResult(decision_object=envelope, exit_code=EXIT_OK)


def _usage(prog: str) -> int:
    import sys

    print(f"usage: {prog} brief [--tag-prefix <prefix>]", file=sys.stderr)
    print(f"       {prog} apply [--session-id <id>] [--force] [--decisions <json>]", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str]) -> int:
    import json
    import sys

    if not argv:
        return _usage("merge-assemble")

    if argv[0] in ("--help", "-h"):
        print("usage: merge-assemble brief [--tag-prefix <prefix>]")
        print("       merge-assemble apply [--session-id <id>] [--force] [--decisions <json>]")
        return EXIT_OK

    subcmd, rest = argv[0], argv[1:]

    if subcmd == "apply":
        from coordinator_core.merge_assemble.apply import main_apply

        return main_apply(rest)

    if subcmd != "brief":
        print(f"merge-assemble: unknown subcommand {subcmd!r}", file=sys.stderr)
        return _usage("merge-assemble")

    tag_prefix = "v"
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--tag-prefix":
            if i + 1 >= len(rest):
                return _usage("merge-assemble")
            tag_prefix = rest[i + 1]
            i += 2
        else:
            print(f"merge-assemble: unrecognized argument {tok!r}", file=sys.stderr)
            return _usage("merge-assemble")

    result = brief(tag_prefix=tag_prefix)
    print(json.dumps(result.decision_object, indent=2, sort_keys=True))
    return result.exit_code
