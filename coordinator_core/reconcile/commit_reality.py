"""
coordinator_core.reconcile.commit_reality — DEC-1 commit-reality shipped-ness matcher.

Purpose: given one open handoff's frontmatter (scope pathspecs, title, created date)
and a repo worktree root, decide whether reality (git history + disk state) shows the
handoff's work was actually shipped, even though the handoff itself was never formally
consumed/archived. This is COMPUTE_ONLY (DR-208 classification) — pure read + compute,
NO writes, NO git mutation. `handoff.reconcile_open` (C4) is the only caller authorized
to act on an `auto-ship` verdict, via `handoff.ship_and_archive`.

DEC-1 three-signal rule (verdict=auto-ship iff ALL THREE hold, per handoff):
    (a) SUBJECT MATCH  — `git log --oneline --since=<created> -- <scope-paths>` yields a
        commit whose subject contains at least `three_signal.subject_match_min_tokens`
        (default 2) DISTINCT noun tokens derived from the handoff's scope leaf-file
        basenames / title — directory-only scope entries and structural-vocabulary
        stopwords (`ops`, `core`, `config`, ... — `three_signal.subject_match_extra_
        stopwords`) contribute no tokens — excluding denylisted mechanical-commit-subject
        prefixes (policy `mechanical_commit_denylist`) so a `pickup:`/`memo:`/session-init/
        handoff.transition-family/frontmatter-mutation commit is never treated as
        completion evidence on its own (the Staff Engineer #2 / DoE alignment reply — the OPPOSITE
        risk from the /pickup Step 3 bare-commit-existence false-"still-live" guard,
        `state/lessons/2026-07-09-git-log-liveness-checks-on-the-coordinat-669018b53b04.yaml`).
        (2026-07-20 claude-central-em false-positive memo, Defect 2a: a lone
        incidental path-component token, e.g. "ops", was previously sufficient on
        its own to select a wholly unrelated commit.)
    (b) DELIVERABLE PRESENT — the handoff's named deliverable path(s) (its scope
        pathspecs) exist on disk as a FILE (or a glob with >=1 file hit) when
        `three_signal.deliverable_requires_file` is true (the default) — an existing
        directory alone no longer counts (Defect 2, signal (b) was previously vacuous
        for any handoff scoped to a directory that merely exists).
    (c) SHA REACHABLE — the candidate commit is a valid git object present on
        ANY local branch (`git cat-file -e` + `git branch --contains`) — see
        `_sha_on_any_local_branch`'s own docstring for why this is NOT
        HEAD-specific ancestry.

Cross-handoff attribution guard (the Staff Engineer review, finding index 2): even when (a)+(b)+(c)
all clear, if MORE THAN ONE other open handoff's scope pathspecs overlap the candidate
commit's touched paths, the verdict is DEMOTED to `surface` — attribution is ambiguous,
so this closes the vector where a real commit + deliverable satisfies the three signals
for handoff X but the work actually belongs to a different open handoff Y with
overlapping scope. The count only credits an "other" handoff whose overlap comes from a
FILE-level pathspec (Defect 2b) — a bare-directory scope shared by a dozen open stubs is
non-discriminating and was inflating this count without genuine evidence.

The matcher reads its threshold data (mechanical-commit denylist, three-signal tuning
knobs) from the LOADED POLICY dict passed by the caller (C9 policy_loader) — it encodes
no threshold constant itself beyond documented code-side defaults. Policy shape (C9
grammar pin):
    {three_signal: {subject_match_min_tokens: int, subject_match_extra_stopwords:
       [str], deliverable_requires_file: bool, ...},
     mechanical_commit_denylist: [str], cross_handoff_attribution: bool, dry_run: bool}

Mirrors the /pickup Step 3 heuristic prose (git-log-as-liveness-evidence), inverted:
/pickup guards against false "still-live"; this matcher guards against false "shipped".

Git-subprocess pattern reused from coordinator_core/ops/commit_anchors.py
(_resolve_plan_from_diff / _read_meta_from_staged) — sync `subprocess.run`,
capture_output=True, text=True, check=False, cwd=worktree_root. Read-only git
subprocesses (log/cat-file/branch) are COMPUTE_ONLY-safe (ipc.py:44 carve-out);
this module never runs a git command that mutates repo state.

Spec backlink: pln-claude-klabauter-auto-reconcile-pass-off-425848 § C2 (DEC-1)

Negative-spec:
  - Does NOT write any file, git object, or repo state — pure compute over
    subprocess reads and in-memory frontmatter dicts.
  - Does NOT invoke handoff.ship_and_archive or any mutating op — that is C4's job.
  - Does NOT encode the mechanical-commit denylist or any threshold constant here —
    both come from the caller-supplied policy dict (C9); a DoE policy-YAML edit
    changes matcher behavior with zero claude-klabauter code change.
  - Does NOT treat a mechanical-only commit subject match as evidence for signal (a).
  - Does NOT auto-ship when attribution is ambiguous — cross-handoff overlap always
    demotes to `surface`, never silently drops to `no-match` (there IS real evidence,
    it's just ambiguously attributed).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set

# NOTE: `archive_stamp._commit_session_id` and `ops.session_context.
# resolve_current_session_id` are imported LOCALLY (inside
# `_sha_attributed_to_session` and `_evaluate_explicit_ship_claim`
# respectively), not at module scope. `archive_stamp` imports
# `coordinator_core.ops.plan_status_transition`, and `session_context` is
# itself a submodule of `coordinator_core.ops` -- either import, at THIS
# module's own top level, requires initializing `coordinator_core/ops/
# __init__.py` first, which eager-imports every op module -- including
# `handoff_reconcile.py` and `completion_ops.py`, both of which import THIS
# module at their own module scope. A top-level import here therefore closes a
# real import cycle (commit_reality -> archive_stamp (or ops.session_context)
# -> coordinator_core.ops -> handoff_reconcile -> commit_reality), confirmed by
# `python3 -c "import coordinator_core.reconcile.commit_reality"` failing with
# "cannot import name '_git' from partially initialized module ... (most
# likely due to a
# circular import)". Deferring the import to call time (after all modules have
# finished loading) sidesteps the cycle without a third copy of the trailer-
# extraction logic.

#: The four `shipped_in_kind` values DR-096 § Decision 2 declares
#: (`docs/decisions/DR-096-shipped-in-names-the-ship-commit.md`, DoE-claude).
#: Kept as a code-side mirror of the schema enum (A1's schema edit is the
#: grammar SSOT; this set is the resolver's own discriminant, per AC26 — the
#: resolver must not infer the enum shape from the schema at runtime).
_SHIPPED_IN_KIND_ENUM: frozenset = frozenset({
    "ship-commit", "successor", "scope-derived", "no-commit",
})

#: Kinds whose `shipped_in` value names a real, potentially-dereferenceable
#: ship commit — eligible for the full reachability + (scope-overlap OR
#: session-attribution) evaluation in `_evaluate_explicit_ship_claim` that can
#: reach confidence:high / verdict:auto-ship (DR-096 § Decision 3). Every
#: other kind — `scope-derived` (frequently an unrelated bystander commit by
#: construction, see DR-096), `no-commit` (not a SHA at all), UNTAGGED
#: (`shipped_in_kind` absent), and any value outside `_SHIPPED_IN_KIND_ENUM`
#: — fails closed to sub-clear (`confidence:partial` / `verdict:surface`)
#: without ever being read as CLEAR evidence (AC26,
#: `docs/plans/2026-07-26-gate-resolution-widen-and-migrate.md`).
_SHIPPED_IN_KIND_CLEAR_ELIGIBLE: frozenset = frozenset({"ship-commit", "successor"})

#: Fallback mechanical-commit-subject denylist used only when the policy dict omits
#: `mechanical_commit_denylist` (defensive default — C9 policy_loader normally supplies
#: this from the DoE-owned YAML). Kept in sync with the plan's five prefixes, plus the
#: archival/migration-machinery prefixes `archive_stamp.resolve_source_ship_sha` /
#: `stamp_shipped_in`'s scope-derived walk-back added (2026-08-05): a handoff or plan's
#: most recent toucher is very often the fleet-archive sweep or a corpus-wide vocabulary
#: migration, not the work itself — a false `shipped_in` reads as authoritative to every
#: later reconciler, so this denylist is the single shared exclusion list BOTH the
#: commit-reality matcher and the provenance-writing resolvers consume (see
#: `archive_stamp._mechanical_commit_denylist`, which imports this tuple directly rather
#: than keeping a second, driftable copy).
_DEFAULT_MECHANICAL_DENYLIST: tuple = (
    "pickup:",
    "reclaim(docs)",
    "session-init",
    "memo:",
    "handoff.transition",
    "fleet: archive",
    "archive handoff:",
    "auto-commit:",
    "change_kind:",
    "migrate_handoff_vocabulary",
    "migrate handoff corpus",
)

#: Stopwords excluded from noun-token derivation (too generic to count as evidence).
_STOPWORD_TOKENS: frozenset = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with",
    "handoff", "handoffs", "md", "py", "yaml", "yml",
})

#: Structural-vocabulary stopwords (2026-07-20 claude-central-em false-positive
#: memo, Defect 2a) — path/repo-shape words that are load-bearing directory names
#: nearly every scope entry passes through, and so carry ~zero discriminating
#: signal for "does this commit's subject describe THIS handoff's actual work".
#: Code-side default for the policy-driven `three_signal.subject_match_extra_
#: stopwords` key (see `evaluate_commit_reality`); a DoE policy-YAML edit can
#: extend or override this set with zero claude-klabauter code change.
_STRUCTURAL_STOPWORD_TOKENS: frozenset = frozenset({
    "ops", "core", "config", "plans", "docs", "state", "tests", "lib", "bin",
    "src", "schemas", "contract",
    # Repo/org/product names -- co-occurring on a title is not subject-matter
    # evidence (2026-07-20 EM follow-up: "claude-klabauter" + "doe" both appearing was
    # enough to clear the 2-token bar for a wholly-unrelated tracker commit).
    "claude-klabauter", "doe", "claude", "coordinator",
})

#: Scope pathspecs that name handoff-PROCESS artifacts, never a deliverable. A
#: handoff is not its own evidence of completed work -- 2026-07-20 EM follow-up:
#: a handoff whose scope is `[its own state/handoffs/*.md doc, state/handoff-
#: tracker.md]` previously satisfied both signal (a) (title tokens matched an
#: unrelated tracker-touching commit) and signal (b) (its own doc + the tracker
#: both trivially "exist") on zero real deliverable evidence.
_NON_DELIVERABLE_SCOPE_FILES: frozenset = frozenset({
    "state/handoff-tracker.md",
    "state/doe-handoff-tracker.md",
})
_NON_DELIVERABLE_SCOPE_PREFIX = "state/handoffs/"


def _is_non_deliverable_scope_entry(path_str: str) -> bool:
    """Return True when a scope pathspec names a handoff doc or tracker file
    rather than an actual deliverable (any `state/handoffs/*.md` entry, or one
    of the known tracker files) -- see `_NON_DELIVERABLE_SCOPE_FILES` above."""
    normalized = path_str.strip().replace("\\", "/")
    if normalized in _NON_DELIVERABLE_SCOPE_FILES:
        return True
    return normalized.startswith(_NON_DELIVERABLE_SCOPE_PREFIX)

#: Code-side default for the policy-driven `three_signal.subject_match_min_tokens`
#: key — the minimum number of DISTINCT derived noun tokens that must appear in a
#: candidate commit's subject for signal (a) to count as a match (2026-07-20
#: claude-central-em false-positive memo, Defect 2a: a single incidental
#: path-component token, e.g. "ops", was previously sufficient on its own).
_DEFAULT_SUBJECT_MATCH_MIN_TOKENS = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _is_directory_scope(path_str: str) -> bool:
    """Return True when a scope pathspec names a directory, not a leaf file.

    Convention: a scope entry ending in "/" (this repo's directory-pathspec
    convention, e.g. `"coordinator_core/ops/"`) names a directory. Directory
    pathspecs must not contribute noun tokens (2026-07-20 claude-central-em
    false-positive memo, Defect 2a) — only a leaf FILE basename's stem is
    load-bearing subject-match vocabulary; a bare directory component like
    `coordinator_core` or `ops` is structural, not descriptive of the work.
    """
    return path_str.rstrip().endswith("/")


def _git(worktree_root: Path, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Run a read-only git subcommand from worktree_root and return the CompletedProcess.

    Purpose: single choke point for this module's git subprocess invocations, mirroring
    commit_anchors.py's inline subprocess.run pattern. Never passes a mutating verb
    (log/cat-file/branch only, per this module's callers) — COMPUTE_ONLY-safe.
    """
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(worktree_root),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _derive_noun_tokens(
    scope: Sequence[str],
    title: str,
    extra_stopwords: Sequence[str] = (),
) -> Set[str]:
    """Derive lowercase noun tokens from scope basenames and the handoff title.

    Purpose: signal (a)'s subject-match vocabulary — splits path basenames (stem,
    without extension) and the title into alnum tokens, drops stopwords/short tokens.
    Returns an empty set when nothing derivable (caller then finds no subject match).

    A DIRECTORY scope entry (`_is_directory_scope`) contributes NOTHING — only a
    leaf FILE basename's stem is descriptive vocabulary (2026-07-20 claude-
    central-em false-positive memo, Defect 2a: `coordinator_core/ops/` previously
    contributed the bare-structural token `ops`). Title tokens still contribute
    regardless of source. `extra_stopwords` (policy-driven, see
    `evaluate_commit_reality`) is unioned with the built-in `_STOPWORD_TOKENS`.
    """
    tokens: Set[str] = set()
    for path_str in scope:
        if _is_directory_scope(path_str) or _is_non_deliverable_scope_entry(path_str):
            continue
        stem = Path(path_str).stem
        tokens.update(_TOKEN_RE.findall(stem.lower()))
    tokens.update(_TOKEN_RE.findall((title or "").lower()))
    stopwords = _STOPWORD_TOKENS | set(extra_stopwords)
    tokens = {t for t in tokens if len(t) >= 3 and t not in stopwords}
    return tokens


#: Denylist tokens matched as a SUBSTRING (family marker) rather than a prefix —
#: catches a whole commit-subject family wherever the marker sits (e.g.
#: "chore: handoff.transition: ship <id>", "fix(migrate_handoff_vocabulary): ...").
#: Every other denylist entry is matched as a case-insensitive PREFIX only
#: (Slice-A review Finding 1): a real feature commit whose subject merely CONTAINS
#: a token like "memo:" or "pickup:" mid-subject (not as a prefix) must not be
#: silently denylisted. `migrate_handoff_vocabulary` and `migrate handoff corpus`
#: (2026-08-05) join `handoff.transition` here for the same reason: the DR-084
#: vocabulary-migration commit family is fronted by several different conventional-
#: commit type prefixes (`feat(fleet):`, `fix(migrate_handoff_vocabulary):`,
#: `fix(dr084):`, a bare `migrate_handoff_vocabulary:`) with no single shared prefix,
#: so only a substring match reliably catches the family.
_SUBSTRING_FAMILY_TOKENS: frozenset = frozenset({
    "handoff.transition",
    "migrate_handoff_vocabulary",
    "migrate handoff corpus",
})


def _is_mechanical_subject(subject: str, denylist: Sequence[str]) -> bool:
    """Return True when `subject` starts with a denylisted mechanical-commit
    prefix, or contains one of `_SUBSTRING_FAMILY_TOKENS` anywhere.

    Purpose: signal (a) must not treat a pickup:/memo:/session-init/
    handoff.transition-family/frontmatter-mutation/fleet-archive/vocabulary-
    migration commit as completion evidence (or, for the provenance-writing
    resolvers in `archive_stamp.py`, as `shipped_in` evidence). Case-insensitive
    PREFIX match for every denylist entry, EXCEPT the entries in
    `_SUBSTRING_FAMILY_TOKENS`, which also match as a substring to catch a whole
    subject family regardless of its conventional-commit type prefix — Slice-A
    review Finding 1: substring-everywhere would silently exclude a legitimate
    commit whose subject merely contains a token like "memo:" mid-subject.
    """
    lowered = subject.strip().lower()
    for token in denylist:
        token_l = token.strip().lower()
        if not token_l:
            continue
        if token_l in _SUBSTRING_FAMILY_TOKENS:
            if token_l in lowered:
                return True
        elif lowered.startswith(token_l):
            return True
    return False


def _matched_tokens(subject: str, tokens: Set[str]) -> Set[str]:
    """Return the set of noun tokens that appear as whole words in `subject`.

    Purpose: shared evidence-derivation helper, split out of the boolean predicate
    below so a caller wanting the matched set (e.g. for a richer evidence string)
    doesn't need to re-derive it (2026-07-20 claude-central-em false-positive memo,
    Defect 2a note).
    """
    subject_tokens = set(_TOKEN_RE.findall(subject.lower()))
    return subject_tokens & tokens


def _subject_matches_tokens(
    subject: str,
    tokens: Set[str],
    min_tokens: int = _DEFAULT_SUBJECT_MATCH_MIN_TOKENS,
) -> bool:
    """Return True when at least `min_tokens` distinct noun tokens appear as whole
    words in the commit subject.

    Purpose: signal (a)'s match bar. Raised from "any single token" to a
    policy-driven minimum (2026-07-20 claude-central-em false-positive memo,
    Defect 2a) — a lone incidental token (e.g. a path-component leftover like
    "ops") was previously sufficient to select a candidate commit that had
    nothing to do with the handoff's actual work.
    """
    return len(_matched_tokens(subject, tokens)) >= min_tokens


def _find_candidate_commits(
    worktree_root: Path,
    scope: Sequence[str],
    since: Optional[str],
    tokens: Set[str],
    denylist: Sequence[str],
    evidence: Optional[List[str]] = None,
    min_tokens: int = _DEFAULT_SUBJECT_MATCH_MIN_TOKENS,
) -> List[Dict[str, str]]:
    """Run `git log --oneline --since=<since> -- <scope-paths>` and return candidates.

    Purpose: signal (a). Returns a list of {"sha": str, "subject": str} for every
    commit in the log window whose subject matches a derived noun token AND is NOT
    a denylisted mechanical-commit subject. Empty scope or empty tokens → [].

    `evidence` (optional, appended in-place): when the `git log` invocation
    itself fails AND `since` was supplied, an evidence string distinguishing
    "git log failed (possibly malformed created date)" from "no candidates
    found" is appended (Slice-A review Finding 7) — a data-quality problem in
    a handoff's `created` frontmatter is otherwise indistinguishable from
    "nothing shipped yet" in the caller's returned evidence trail.

    Uses `--pretty=format:%H %s` rather than bare `--oneline` — same one-commit-per-line
    "oneline" shape the plan specifies, but with the FULL 40-char SHA (bare --oneline
    abbreviates the hash, which breaks downstream `git cat-file -e` / `git branch
    --contains` identity checks on short-SHA collision risk).
    """
    if not scope or not tokens:
        return []

    args: List[str] = ["log", "--pretty=format:%H %s", "--no-color"]
    if since:
        args.append(f"--since={since}")
    args.append("--")
    args.extend(scope)

    result = _git(worktree_root, args)
    if result.returncode != 0:
        if since and evidence is not None:
            evidence.append(
                f"git log --since={since!r} failed (possibly malformed created date) "
                f"— treating as no candidates"
            )
        return []

    candidates: List[Dict[str, str]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        sha = parts[0]
        subject = parts[1] if len(parts) > 1 else ""
        if _is_mechanical_subject(subject, denylist):
            continue
        if not _subject_matches_tokens(subject, tokens, min_tokens=min_tokens):
            continue
        candidates.append({"sha": sha, "subject": subject})
    return candidates


def _deliverable_present(
    worktree_root: Path,
    scope: Sequence[str],
    require_file: bool = True,
) -> bool:
    """Signal (b): do any of the handoff's scope pathspecs exist on disk (glob-aware)?

    When `require_file` is True (the policy-driven default, `three_signal.
    deliverable_requires_file`), an existing DIRECTORY does not count — only a
    leaf FILE (or a glob with >=1 file hit) is real deliverable evidence
    (2026-07-20 claude-central-em false-positive memo, Defect 2 signal (b): a
    bare-directory scope like `coordinator_core/` is trivially "present" for
    nearly every open handoff, so it was vacuous evidence). `require_file=False`
    restores the pre-fix directory-tolerant behavior for callers that need it.
    """
    for path_str in scope:
        if not path_str or _is_non_deliverable_scope_entry(path_str):
            continue
        if any(ch in path_str for ch in "*?["):
            try:
                matches = list(worktree_root.glob(path_str))
            except (OSError, ValueError) as exc:
                # A malformed/inaccessible glob pattern degrades to "no match
                # from this scope entry" (fail-closed toward auto-ship — this
                # can only make deliverable-present under-report, never
                # over-report), but surface it since it means one of the
                # handoff's scope entries could not be evaluated at all.
                print(
                    f"commit_reality: glob failed for scope entry {path_str!r} "
                    f"under {worktree_root}: {exc}",
                    file=sys.stderr,
                )
                continue
            if require_file:
                if any(m.is_file() for m in matches):
                    return True
            elif matches:
                return True
        else:
            target = worktree_root / path_str
            if require_file:
                if target.is_file():
                    return True
            elif target.exists():
                return True
    return False


def _sha_on_any_local_branch(worktree_root: Path, sha: str) -> bool:
    """Signal (c): is `sha` a valid git object contained by ANY local branch?

    NOTE the name is deliberately NOT "reachable on HEAD" (a prior name this
    function carried, and a trap for the next reader): after `git cat-file -e`
    this runs `git branch --contains <sha>` and returns True if ANY local
    branch contains it — not specifically HEAD's own ancestry. On a repo with
    per-machine daily `work/{machine}/{date}` branches this accepts
    essentially any commit present in the local clone, including a concurrent
    sibling session's commit on its own branch. Callers that need a stronger
    ownership signal than "this object exists somewhere in my local clone"
    must pair this with a provenance check (see `_sha_attributed_to_session`)
    rather than relying on this predicate alone (sidecar finding 4, review
    `state/subagent-share/eb3895a6-344e-4c49-b965-7e924303ae90/
    coordinatoreng-director-7848035c.md`, 2026-07-26).
    """
    exists = _git(worktree_root, ["cat-file", "-e", sha])
    if exists.returncode != 0:
        return False
    contains = _git(worktree_root, ["branch", "--contains", sha])
    if contains.returncode != 0:
        return False
    return bool(contains.stdout.strip())


def _sha_attributed_to_session(worktree_root: Path, sha: str, session_id: Optional[str]) -> bool:
    """Positive-provenance predicate: does `sha`'s Session-Id trailer match `session_id`?

    Purpose: the provenance half of the top-tier (confidence:high / verdict:
    auto-ship) gate for an explicit `shipped_in` claim (2026-07-26 ruling,
    sidecar finding 4) — an alternative to scope-overlap, not a replacement
    for it. Reuses the trailer-extraction mechanism already built and
    hardened in `archive_stamp._commit_session_id` (itself replicated from
    `coverage.py:1017-1040`) rather than growing a third copy of the same
    UUID-shape-validated trailer read. Returns False (never raises) when
    either side is unresolvable — an unattributable SHA degrades to the
    scope-overlap path, it does not get a free pass.
    """
    if not session_id:
        return False
    from coordinator_core.archive_stamp import _commit_session_id  # deferred: see module-scope NOTE above

    candidate_sid = _commit_session_id(worktree_root, sha)
    return candidate_sid is not None and candidate_sid == session_id


def _touched_paths(worktree_root: Path, sha: str) -> Set[str]:
    """Return the set of repo-relative paths touched by commit `sha` (best-effort)."""
    result = _git(worktree_root, ["show", "--name-only", "--pretty=format:", sha])
    if result.returncode != 0:
        return set()
    return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}


def _pathspec_overlaps(pathspecs: Sequence[str], touched: Set[str]) -> bool:
    """Return True when any of `pathspecs` overlaps (prefix-matches) any touched path."""
    for spec in pathspecs:
        spec_clean = spec.rstrip("*").rstrip("/")
        if not spec_clean:
            continue
        for touched_path in touched:
            if touched_path == spec_clean or touched_path.startswith(spec_clean + "/"):
                return True
            # Review: code-reviewer (F1, P1) — anchor on a path-separator boundary
            # the same way the branch above does. A bare string-prefix match (no
            # "/" boundary check) would report `src/foo.py.bak` as "overlapping"
            # touched path `src/foo.py` — a false subsumption on any filename-
            # suffix collision (.bak/.orig/.pyc/.disabled/...). The `==` case is
            # already covered by the branch above, so only the directory-
            # containment form is needed here.
            if spec_clean.startswith(touched_path + "/"):
                return True
    return False


def _discriminating_pathspecs(pathspecs: Sequence[str]) -> List[str]:
    """Filter a scope pathspec list down to file-level (non-directory) entries.

    Purpose: shared filter for every call site that needs to know whether a
    scope overlap reflects genuine attribution rather than incidental breadth.
    A bare-directory pathspec (e.g. `"coordinator_core/"`) is too broad to
    discriminate one thing from another — a dozen open stubs can share the
    exact same top-level directory verbatim, and a ship commit can touch
    *something* under a wide directory without touching the deliverable a
    handoff actually names. Two call sites rely on this: the cross-handoff
    attribution count in `evaluate_commit_reality` (2026-07-20 claude-central-em
    false-positive memo, Defect 2b — inflated "N open handoffs share scope
    overlap" counts). The explicit-ship-claim self-overlap gate in
    `_evaluate_explicit_ship_claim` (mixed-scope directory-entry defect pinned
    by `TestMixedScopeDirectoryEntryGrantsUnearnedHighConfidence` — a directory
    entry alone must not carry `confidence:high` / `verdict:auto-ship`) uses
    the disk-aware sibling `_discriminating_pathspecs_on_disk` instead, since
    this function's syntactic-only trailing-slash convention misses a real
    directory entry with no trailing slash (e.g. `docs/plans`).
    Deliberately does NOT touch the shared `_pathspec_overlaps` predicate
    itself — `handoff_reconcile.py` imports that for the C6 chain-walk
    subsumption gate (`_scope_subsumed`) and relies on its existing
    directory-tolerant semantics; changing it here would move that gate's bar
    too.
    """
    return [p for p in pathspecs if p and not _is_directory_scope(p)]


def _is_directory_scope_disk_aware(path_str: str, worktree_root: Path) -> bool:
    """Return True when a scope pathspec names a directory, using disk truth as
    a fallback beyond `_is_directory_scope`'s trailing-slash-only convention.

    Purpose: `_is_directory_scope` recognizes only the trailing-"/" convention.
    Real scope entries in this corpus name directories WITHOUT a trailing
    slash (e.g. `docs/plans`, `state/handoffs`, `coordinator/templates/bin`),
    so they fall through as file-shaped and let
    `_evaluate_explicit_ship_claim`'s self-overlap gate grant
    `confidence:high` on a bare directory prefix-match -- exactly the
    non-discriminating evidence `_discriminating_pathspecs` exists to reject.
    A purely syntactic "no file extension => directory" heuristic was
    considered and rejected: extension-less FILES are real in this corpus
    (`coordinator-auto-push` is one), and that heuristic would misclassify
    them as directories, wrongly demoting genuine file-level attribution.
    This helper is disk-aware instead: an entry is directory-shaped when the
    syntactic convention already says so, OR it resolves to an actual
    directory under `worktree_root`. A path that does not exist, or cannot be
    stat'd, falls back to the syntactic answer -- never guessed from the
    absence of an extension.

    Deliberately local to the one call site that has `worktree_root` in hand
    (the self-overlap gate in `_evaluate_explicit_ship_claim`). The
    cross-handoff attribution count call site (`evaluate_commit_reality`, via
    `_discriminating_pathspecs`) has no worktree_root available and keeps the
    syntactic-only answer unchanged -- see that function's own docstring.

    Confinement asymmetry (deliberate, code-review finding, P3): the disk
    probe below is skipped -- falling back to the syntactic answer -- when
    `path_str` is absolute or carries a `..` component, so this helper never
    stats outside `worktree_root`. `_deliverable_present`'s own
    `worktree_root / path_str` join has the identical unconfined shape
    (`Path("/repo") / "/etc"` discards the left operand entirely when the
    right is absolute, and a `..`-bearing relative entry resolves upward) and
    is deliberately NOT touched here -- that is pre-existing precedent, out of
    scope for this fix. Only the NEW helper introduced by this change is
    confined.
    """
    if _is_directory_scope(path_str):
        return True
    stripped = path_str.rstrip()
    candidate = Path(stripped)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    try:
        return (worktree_root / stripped).is_dir()
    except (OSError, ValueError):
        return False


def _discriminating_pathspecs_on_disk(
    pathspecs: Sequence[str], worktree_root: Path
) -> List[str]:
    """Disk-aware sibling of `_discriminating_pathspecs`, for the one call site
    with `worktree_root` in hand -- see `_is_directory_scope_disk_aware`.
    """
    return [
        p
        for p in pathspecs
        if p and not _is_directory_scope_disk_aware(p, worktree_root)
    ]


#: Grammar for the documented cross-repo `scope:` form `<repo-id>:<path>`
#: (e.g. `claude-klabauter:coordinator_core/dag.py`, `example-retrieval-repo:<path>` --
#: whitespace after the colon is OPTIONAL and both `repo-id:path` and
#: `repo-id: path` are accepted) -- reused VERBATIM from
#: `coordinator_core/pickup_assemble/__init__.py`'s own
#: `_SCOPE_SIBLING_PREFIX_RE` (that module's copy is the grammar's SSOT):
#: a repo-id-shaped token (letters, digits, hyphens, underscores, starting
#: with a letter, TWO OR MORE characters total) followed by a colon and
#: optional whitespace.
#:
#: 2026-07-27 fix: the prior pattern made whitespace after the colon
#: MANDATORY (`\s+`), which no real plan/handoff scope entry ever satisfies
#: -- YAML parses `- repo: path` (with a space) as a MAPPING, not the plain
#: string a scope list wants, so every real author writes `- repo:path` (no
#: space). The mandatory-whitespace grammar matched a form nobody could
#: structurally write, silently disabling cross-repo scope recognition
#: entirely. Verified against real plans: `grep -rhoE '^\s+- [a-z0-9-]+:
#: [^ ]+' docs/plans/*.md` in DoE-claude returns only no-space entries like
#: `- claude-klabauter:coordinator_core/ops/plan_tasks_mutate.py`.
#:
#: Windows-drive-letter safety is UNCHANGED by this fix and does not depend
#: on the whitespace requirement at all: the repo-id group requires TWO OR
#: MORE characters (`[A-Za-z][A-Za-z0-9_-]+`, note the trailing `+`, not
#: `*`), and a drive letter is exactly one character before its colon
#: (`C:\Users\...`, `D:/foo`) -- `C` and `D` alone never satisfy
#: `[A-Za-z0-9_-]+`'s "one or more" requirement, so the match fails at the
#: repo-id group regardless of what follows the colon or how much
#: whitespace trails it. See
#: `TestScopeSiblingPrefixDriveLetterSafety` in this module's test file for
#: the pinned regression coverage.
#:
#: URL safety: with whitespace now optional, `https://example.com/x` would
#: otherwise satisfy the repo-id shape (`https` is 5 characters) and read as
#: a cross-repo prefix -- a risk the mandatory-whitespace grammar
#: incidentally avoided only when a URL was never written with a trailing
#: space, not by any principled exclusion. The `(?!//)` negative lookahead
#: sits immediately after the colon, BEFORE the optional `\s*` -- so it
#: rejects a bare `scheme://...` outright while still tolerating the
#: documented `<repo-id>: <path>` form's single space (the lookahead only
#: ever sees `//` right after the colon in the URL case; a real scope
#: entry's `<path>` half never starts with `//`, spaced or not, so nothing
#: legitimate is excluded). Verbatim match with
#: `pickup_assemble._SCOPE_SIBLING_PREFIX_RE` requires this exact
#: lookahead placement -- do not reorder relative to `\s*` without
#: updating both copies together.
_SCOPE_SIBLING_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]+):(?!//)\s*(.+)$")


def _is_cross_repo_scope_entry(path_str: str) -> bool:
    """Return True when a scope pathspec uses the documented cross-repo
    `<repo-id>:<path>` form (see `_SCOPE_SIBLING_PREFIX_RE`) rather than
    naming a path in THIS worktree.

    This module deliberately does NOT resolve the sibling repo or its path --
    doing so would mean reaching outside the worktree, which contradicts this
    module's zero-spawn/sub-10ms hot-path design (see module docstring).
    Recognizing the shape is enough to stop an entry like this from silently
    falling through the local-path predicates as an ordinary pathspec that
    just happens to match nothing (the defect this helper exists to close).
    """
    return bool(_SCOPE_SIBLING_PREFIX_RE.match(path_str.strip()))


def _is_unparseable_scope_prefix_entry(path_str: str) -> bool:
    """Return True when `path_str` matches the `<repo-id>:<rest>` shape
    (`_SCOPE_SIBLING_PREFIX_RE`) but `<rest>` is empty or contains internal
    whitespace -- prose mistakenly shaped like a repo-id prefix, not a real
    cross-repo path (e.g. `"example-retrieval-repo: doctrine-corpus indexing (PHASE 2
    -- gated on de-bashing)"`).

    Mirrors `pickup_assemble.compute_tree_quiescence`'s own discriminator
    (`coordinator_core/pickup_assemble/__init__.py`, the grammar's SSOT):
    that function matches the same regex, then rejects the match into
    `unparseable_scope_entries` when `not rest or " " in rest`, rather than
    treating a matched-then-rejected shape as a genuine sibling-repo path.
    `_is_cross_repo_scope_entry` alone does not replicate that downstream
    rejection -- this helper exists so callers that need to tell "genuine
    cross-repo path" apart from "malformed prefix-plus-prose" for their
    evidence text can do so (code-review F1, `commit_reality.py` review,
    2026-07-27) without changing which entries are excluded from local-path
    evaluation -- a malformed entry never resolved on disk either way, so
    this discriminator is evidence-text-only, not classification for the
    local/cross-repo partition itself.
    """
    match = _SCOPE_SIBLING_PREFIX_RE.match(path_str.strip())
    if match is None:
        return False
    rest = match.group(2).strip()
    return not rest or " " in rest


def _split_cross_repo_scope(
    scope: Sequence[str],
) -> "tuple[List[str], List[str]]":
    """Partition a scope pathspec list into (local_entries, cross_repo_entries)
    using `_is_cross_repo_scope_entry`. Order is preserved within each half;
    falsy entries are dropped from both (never evaluable either way)."""
    local: List[str] = []
    cross_repo: List[str] = []
    for entry in scope:
        if not entry:
            continue
        if _is_cross_repo_scope_entry(entry):
            cross_repo.append(entry)
        else:
            local.append(entry)
    return local, cross_repo


def _find_plan_path_in_scope(scope: Sequence[str]) -> Optional[str]:
    """Resolve the first `docs/plans/*.md` pathspec present in a handoff's scope.

    Purpose: signal (ii)'s plan-lookup step — a handoff's scope may name its own
    authoring plan alongside its deliverable paths; this locates that plan so its
    frontmatter `status` can be read as corroborating (never sole) ship evidence.
    """
    for path_str in scope:
        if path_str.startswith("docs/plans/") and path_str.endswith(".md"):
            return path_str
    return None


def _read_plan_status(worktree_root: Path, plan_path: str) -> Optional[str]:
    """Best-effort read of a plan file's frontmatter `status:` value, or None.

    Purpose: signal (ii)'s corroboration read. Never raises — a missing/malformed
    plan file is treated as "no status available", not an error, since this is
    corroborating evidence only and must never itself gate auto-ship.
    """
    try:
        text = (worktree_root / plan_path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    match = re.search(r"^status:\s*(\S+)\s*$", text[3:end], re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _demote_awaiting_gate_auto_ship(
    result: Dict[str, Any], deployment_state: str
) -> Dict[str, Any]:
    """An `awaiting_gate` handoff must never auto-ship on commit-reality evidence
    alone -- `gate_eval` (C3) is the sole authority for clearing a gate, and its
    verdict is a SEPARATE call this module has no visibility into. Demotes a
    verdict='auto-ship' result to 'surface' with explicit evidence when the
    caller-supplied handoff dict's own `deployment_state` is `awaiting_gate`
    (2026-07-20 claude-central-em false-positive memo follow-up: the caller in
    `handoff_reconcile.py` checks `commit_verdict.get("verdict") == "auto-ship"`
    BEFORE it ever reaches its own `awaiting_gate` branch, so an ungated
    auto-ship verdict here would bypass the gate check entirely).
    """
    if result.get("verdict") == "auto-ship" and deployment_state == "awaiting_gate":
        result = dict(result)
        result["evidence"] = list(result.get("evidence") or []) + [
            "handoff is awaiting_gate -- commit-reality evidence alone must not "
            "auto-ship a gated handoff; gate_eval is the sole clearing authority"
        ]
        result["verdict"] = "surface"
    return result


def _evaluate_explicit_ship_claim(
    handoff: Dict[str, Any],
    worktree_root: Path,
    scope: Sequence[str],
    deliverable_present: bool,
    evidence: List[str],
) -> Optional[Dict[str, Any]]:
    """Alternative ship-evidence path for a baton carrying an explicit ship claim
    but no scope-token-matching commit (DEC-1 signal (a) found nothing).

    Purpose: covers (i) an explicit `shipped_in: <SHA>` frontmatter claim and
    (ii) a linked `docs/plans/*.md` stamped `status: implemented` — corroborating
    only, never sole evidence. Both sub-signals require reachability
    (`_sha_on_any_local_branch`) and deliverable-presence to clear at all; the
    top tier (confidence:high / verdict:auto-ship) additionally requires EITHER
    self-scope-overlap (`_pathspec_overlaps` over `_touched_paths` — unchanged,
    still the strongest signal) OR positive provenance attribution of the SHA
    to the CALLING session (`_sha_attributed_to_session` — the caller-supplied
    ship SHA legitimised by the 2026-07-26 ruling will often touch none of the
    baton's scope, so scope-overlap alone can no longer be the sole top-tier
    gate; see sidecar finding 4). Neither signal clearing degrades to
    `confidence:partial / verdict:surface` — a REPORT, not a failure; this is
    distinct from, and in ADDITION to, the existing cross-handoff attribution
    guard. Returns None when no explicit-claim evidence applies at all (caller
    falls through to the pre-existing no-match verdict); otherwise a full
    verdict dict, always `auto-ship` or `surface` — never a bare claim promoted
    without at least one of reachable+scope-overlap or reachable+provenance.

    A scope entry using the documented cross-repo `<repo-id>:<path>` form
    (`_is_cross_repo_scope_entry`) names a path in a SIBLING worktree, not
    this one -- it is excluded from every local-path predicate this function
    drives (the linked-plan lookup, and the self-scope-overlap check below)
    rather than being left in to silently fail every match by accident, and
    an evidence string names the excluded entries explicitly so the operator
    can see evidence was skipped and why, instead of reading plain silence.
    This does not change verdict/confidence semantics versus today: a
    cross-repo entry never resolved on disk or prefix-matched a touched path
    in this worktree either way -- only the evidence trail changes.

    DR-096 discrimination (AC26): a `shipped_in` value is only eligible for
    this function's git-dereferencing (reachability, deliverable-gating,
    scope-overlap, session-attribution) path when its paired
    `shipped_in_kind` is `ship-commit` or `successor`
    (`_SHIPPED_IN_KIND_CLEAR_ELIGIBLE`). `scope-derived`, `no-commit`,
    UNTAGGED, and any unrecognized kind value are demoted to
    `confidence:partial` / `verdict:surface` immediately, before any git
    subprocess runs — none of those shapes may read as CLEAR evidence
    regardless of what the raw string looks like.
    """
    scope, cross_repo_scope_entries = _split_cross_repo_scope(scope)
    if cross_repo_scope_entries:
        genuine_cross_repo_entries = [
            e for e in cross_repo_scope_entries
            if not _is_unparseable_scope_prefix_entry(e)
        ]
        malformed_scope_prefix_entries = [
            e for e in cross_repo_scope_entries
            if _is_unparseable_scope_prefix_entry(e)
        ]
        if genuine_cross_repo_entries:
            evidence.append(
                f"{len(genuine_cross_repo_entries)} scope entries name another repo "
                f"and cannot be evaluated in this worktree: {genuine_cross_repo_entries!r}"
            )
        if malformed_scope_prefix_entries:
            evidence.append(
                f"{len(malformed_scope_prefix_entries)} scope entries match the "
                f"repo-id prefix shape but are malformed (prefix followed by "
                f"prose, not a path) -- not genuine cross-repo entries, per "
                f"pickup_assemble's own matched-then-rejected discriminator: "
                f"{malformed_scope_prefix_entries!r}"
            )

    shipped_in = handoff.get("shipped_in")
    plan_path = _find_plan_path_in_scope(scope)
    plan_status = _read_plan_status(worktree_root, plan_path) if plan_path else None
    plan_implemented = plan_status == "implemented"
    # `landed` is a distinct, WEAKER corroboration tier than `implemented` — never
    # widen `plan_implemented` to include it (see this module's plan, C2: a
    # `landed` plan corroborates "code is in", not "shipped", and `landed` is
    # explicitly non-terminal per `lifecycle_constants.py`'s negative-spec /
    # DoE ruling `80b0b29fb`). Kept as its own flag with its own evidence string
    # so the two tiers stay distinguishable rather than merging into one path.
    plan_landed = plan_status == "landed"
    if plan_path and plan_implemented:
        evidence.append(f"linked plan {plan_path} stamped status:implemented (corroborating)")
    elif plan_path and plan_landed:
        evidence.append(
            f"linked plan {plan_path} stamped status:landed (weaker corroborating "
            "evidence — code is in, not confirmed shipped)"
        )

    if not shipped_in:
        if plan_implemented:
            evidence.append(
                "no reachable self-scope-overlapping SHA available to gate the "
                "linked plan's status:implemented stamp"
            )
            return {
                "handoff_id": handoff.get("id") or handoff.get("title") or "",
                "candidate_sha": None,
                "confidence": "partial",
                "evidence": evidence,
                "verdict": "surface",
            }
        if plan_landed:
            evidence.append(
                "no reachable self-scope-overlapping SHA available to gate the "
                "linked plan's status:landed stamp — surfacing landed-but-unshipped "
                "for reconciliation rather than dropping silently"
            )
            return {
                "handoff_id": handoff.get("id") or handoff.get("title") or "",
                "candidate_sha": None,
                "confidence": "partial",
                "evidence": evidence,
                "verdict": "surface",
            }
        return None

    handoff_id = handoff.get("id") or handoff.get("title") or ""
    candidate_sha = str(shipped_in).strip()

    raw_kind = handoff.get("shipped_in_kind")
    # Review: code-reviewer (Finding 6) -- exact-case, exact-string match
    # against the schema enum (no case-folding, no whitespace-trimming). The
    # schema's `shipped_in_kind` enum is exact-case ("ship-commit", not
    # "Ship-Commit") with no tolerance for surrounding whitespace; a
    # case/whitespace variant is itself a malformed writer output the
    # resolver must surface as unrecognized (kind_normalized not in
    # _SHIPPED_IN_KIND_ENUM below), never silently accepted as if it were
    # the canonical form -- .strip().lower() previously widened the
    # resolver's acceptance beyond what the grammar SSOT permits, masking a
    # genuinely malformed writer as a cosmetic issue.
    kind_normalized = raw_kind if isinstance(raw_kind, str) and raw_kind else None

    if kind_normalized not in _SHIPPED_IN_KIND_CLEAR_ELIGIBLE:
        # AC26 discriminator — fail closed before any git dereferencing. An
        # untagged, scope-derived, no-commit, or unrecognized kind must never
        # be read as CLEAR evidence here, no matter how plausible the raw
        # `shipped_in` string looks.
        if kind_normalized is None:
            evidence.append(
                f"shipped_in {candidate_sha!r} carries no shipped_in_kind tag — "
                "an untagged value is never CLEAR evidence (DR-096 AC26)"
            )
        elif kind_normalized not in _SHIPPED_IN_KIND_ENUM:
            evidence.append(
                f"shipped_in_kind {raw_kind!r} is not a recognized DR-096 enum "
                "value — failing closed rather than treating it as CLEAR"
            )
        elif kind_normalized == "scope-derived":
            evidence.append(
                f"shipped_in {candidate_sha!r} is shipped_in_kind:scope-derived — "
                "derived from `git log -n1 -- <scope>` at write time, which lands "
                "in the same commit as the stamp write and so can never resolve "
                "to the actual ship commit; not CLEAR evidence (DR-096 AC26)"
            )
        else:  # kind_normalized == "no-commit"
            evidence.append(
                f"shipped_in {candidate_sha!r} is shipped_in_kind:no-commit — "
                "the sanctioned substantively-shipped-no-commit token, not a "
                "SHA; not CLEAR evidence and not git-dereferenceable"
            )
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": "partial",
            "evidence": evidence,
            "verdict": "surface",
        }

    if not _sha_on_any_local_branch(worktree_root, candidate_sha):
        evidence.append(f"shipped_in SHA {candidate_sha} not reachable on HEAD")
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": "partial",
            "evidence": evidence,
            "verdict": "surface",
        }

    if not deliverable_present:
        evidence.append(
            f"shipped_in SHA {candidate_sha} reachable but deliverable absent on disk"
        )
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": "partial",
            "evidence": evidence,
            "verdict": "surface",
        }

    touched = _touched_paths(worktree_root, candidate_sha)
    # A directory-shaped scope entry (:190 `_is_directory_scope`) is satisfied
    # by ANY commit touching anything beneath it — non-discriminating evidence
    # that must not carry the top tier on its own (pinned by
    # `TestMixedScopeDirectoryEntryGrantsUnearnedHighConfidence`). Only a
    # file-level pathspec overlap is strong enough for confidence:high here;
    # `_pathspec_overlaps` itself is left untouched — other callers depend on
    # its directory-tolerant semantics (see `_discriminating_pathspecs`'s
    # docstring). Uses the disk-aware filter, not `_discriminating_pathspecs`
    # itself: a directory scope entry without a trailing slash (e.g.
    # `docs/plans`) is real in this corpus and must not slip through the
    # syntactic-only check (see `_is_directory_scope_disk_aware`).
    discriminating_scope = _discriminating_pathspecs_on_disk(scope, worktree_root)
    scope_overlaps = bool(discriminating_scope) and _pathspec_overlaps(discriminating_scope, touched)
    if scope_overlaps:
        evidence.append("explicit shipped_in SHA verified reachable and touches own scope")
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": "high",
            "evidence": evidence,
            "verdict": "auto-ship",
        }

    directory_only_overlap = _pathspec_overlaps(scope, touched)

    from coordinator_core.ops.session_context import resolve_current_session_id  # deferred: see module-scope NOTE above

    caller_session_id = resolve_current_session_id(worktree_root)
    if _sha_attributed_to_session(worktree_root, candidate_sha, caller_session_id):
        evidence.append(
            f"shipped_in SHA {candidate_sha} does not touch this handoff's scope "
            f"but is positively attributed to the stamping session (Session-Id "
            f"trailer match) — treated as verified ship evidence"
        )
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": "high",
            "evidence": evidence,
            "verdict": "auto-ship",
        }

    if directory_only_overlap:
        evidence.append(
            f"shipped_in SHA {candidate_sha} only overlaps this handoff's scope "
            f"through a directory-shaped scope entry — non-discriminating "
            f"evidence, not attribution — and is not attributable to the "
            f"stamping session"
        )
    else:
        evidence.append(
            f"shipped_in SHA {candidate_sha} reachable but touches nothing in this "
            f"handoff's scope and is not attributable to the stamping session "
            f"(no scope-overlap, no matching Session-Id provenance)"
        )
    return {
        "handoff_id": handoff_id,
        "candidate_sha": candidate_sha,
        "confidence": "partial",
        "evidence": evidence,
        "verdict": "surface",
    }


def evaluate_commit_reality(
    handoff: Dict[str, Any],
    worktree_root: Path,
    policy: Dict[str, Any],
    other_open_handoffs: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute the DEC-1 three-signal shipped-ness verdict for one open handoff.

    Params:
        handoff: parsed frontmatter dict for the handoff under evaluation. Recognized
            keys: "id" (str), "scope" (list[str] pathspecs), "title" (str),
            "created" (str, optional — passed to --since).
        worktree_root: repo worktree root (Path) — git subprocesses run with this cwd.
        policy: loaded policy dict (C9 shape). `mechanical_commit_denylist` (list[str])
            is read from here; falls back to _DEFAULT_MECHANICAL_DENYLIST when absent.
        other_open_handoffs: parsed frontmatter dicts for every OTHER currently-open
            handoff (same shape as `handoff`), used for the cross-handoff attribution
            guard. The handoff under evaluation must NOT be included in this sequence.

    Returns:
        {handoff_id, candidate_sha, confidence: 'high'|'partial'|'none',
         evidence: [str], verdict: 'auto-ship'|'surface'|'no-match'}

    verdict=='auto-ship' ONLY when signals (a)+(b)+(c) all hold AND the cross-handoff
    attribution guard does not demote.
    """
    handoff_id = handoff.get("id") or handoff.get("title") or ""
    scope: List[str] = list(handoff.get("scope") or [])
    title = handoff.get("title") or ""
    since = handoff.get("created")
    deployment_state = (handoff.get("deployment_state") or "").strip().lower()

    denylist = policy.get("mechanical_commit_denylist")
    if denylist is None:
        denylist = list(_DEFAULT_MECHANICAL_DENYLIST)
    attribution_guard_enabled = policy.get("cross_handoff_attribution", True)

    # Policy-driven tunables (2026-07-20 claude-central-em false-positive memo,
    # Defect 2) — nested under `three_signal`, per the grammar doc's own note that
    # this mapping is "reserved for future threshold tuning... without a matcher
    # code change". Code-side defaults apply when the key/mapping is absent, so a
    # DoE policy-YAML edit is the only thing needed to retune these, same as
    # `mechanical_commit_denylist` above.
    three_signal_policy = policy.get("three_signal") or {}
    subject_match_min_tokens = three_signal_policy.get(
        "subject_match_min_tokens", _DEFAULT_SUBJECT_MATCH_MIN_TOKENS
    )
    if not isinstance(subject_match_min_tokens, int) or subject_match_min_tokens < 1:
        subject_match_min_tokens = _DEFAULT_SUBJECT_MATCH_MIN_TOKENS
    subject_match_extra_stopwords = three_signal_policy.get("subject_match_extra_stopwords")
    if subject_match_extra_stopwords is None:
        subject_match_extra_stopwords = _STRUCTURAL_STOPWORD_TOKENS
    deliverable_requires_file = three_signal_policy.get("deliverable_requires_file", True)
    if not isinstance(deliverable_requires_file, bool):
        deliverable_requires_file = True

    evidence: List[str] = []

    tokens = _derive_noun_tokens(scope, title, extra_stopwords=subject_match_extra_stopwords)
    candidates = _find_candidate_commits(
        worktree_root, scope, since, tokens, denylist, evidence,
        min_tokens=subject_match_min_tokens,
    )

    deliverable_present = _deliverable_present(
        worktree_root, scope, require_file=deliverable_requires_file
    )
    if deliverable_present:
        evidence.append("deliverable present on disk")
    else:
        evidence.append("deliverable absent on disk")

    if not candidates:
        evidence.append("no non-mechanical commit subject matches scope/title tokens")
        explicit_claim_verdict = _evaluate_explicit_ship_claim(
            handoff, worktree_root, scope, deliverable_present, evidence
        )
        if explicit_claim_verdict is not None:
            return _demote_awaiting_gate_auto_ship(explicit_claim_verdict, deployment_state)
        return {
            "handoff_id": handoff_id,
            "candidate_sha": None,
            "confidence": "none",
            "evidence": evidence,
            "verdict": "no-match",
        }

    # Prefer the most recent candidate (git log default order is newest-first).
    candidate = candidates[0]
    candidate_sha = candidate["sha"]
    evidence.append(f"commit {candidate_sha} subject matched: {candidate['subject']!r}")

    reachable = _sha_on_any_local_branch(worktree_root, candidate_sha)
    if reachable:
        evidence.append(f"{candidate_sha} is git-reachable on HEAD")
    else:
        evidence.append(f"{candidate_sha} is NOT git-reachable on HEAD")

    signals_hold = deliverable_present and reachable

    if not signals_hold:
        confidence = "partial" if (deliverable_present or reachable) else "none"
        return {
            "handoff_id": handoff_id,
            "candidate_sha": candidate_sha,
            "confidence": confidence,
            "evidence": evidence,
            "verdict": "surface",
        }

    # All three signals hold — check the cross-handoff attribution guard before
    # confirming auto-ship.
    if attribution_guard_enabled:
        touched = _touched_paths(worktree_root, candidate_sha)
        # Count includes the handoff under evaluation itself (it overlaps by
        # construction — its own scope is what produced this candidate) plus every
        # OTHER open handoff whose scope also overlaps the commit's touched paths.
        # >1 total overlapping handoffs means attribution is ambiguous.
        overlapping = 1
        for other in other_open_handoffs:
            # 2026-07-20 claude-central-em false-positive memo, Defect 2b: count
            # an "other" handoff as overlapping ONLY when the overlap comes from
            # a file-level (non-directory) pathspec — a bare-directory scope
            # entry is non-discriminating (see `_discriminating_pathspecs`).
            other_scope = _discriminating_pathspecs(other.get("scope") or [])
            if other_scope and _pathspec_overlaps(other_scope, touched):
                overlapping += 1
        if overlapping > 1:
            evidence.append(
                f"ambiguous attribution — {overlapping} open handoffs share scope "
                f"overlap with this commit"
            )
            return {
                "handoff_id": handoff_id,
                "candidate_sha": candidate_sha,
                "confidence": "partial",
                "evidence": evidence,
                "verdict": "surface",
            }

    evidence.append("three-signal DEC-1 bar cleared")
    return _demote_awaiting_gate_auto_ship({
        "handoff_id": handoff_id,
        "candidate_sha": candidate_sha,
        "confidence": "high",
        "evidence": evidence,
        "verdict": "auto-ship",
    }, deployment_state)
