"""
coordinator_core.ops.emit.context — EmitContext + provenance envelope builder.

Purpose: capture git / host / repo state ONCE at the top of an emission run and pass
it to every section porter, exactly as the bash oracle collected GIT_BRANCH / GIT_SHA /
OBSERVED_AT / HOSTNAME_VAL / REPO_NAME once and threaded them through provenance_json().

Attribution invariant (REVERSED 2026-07-07 per-repo-emission-cutover): repo_name is the
EMITTING REPO's own slug, resolved from *repo_root*'s git remote.  Each working repo
emits under its own identity — ``~/.claude`` reaches slug ``dbc-oduffy/.example-doctrine-mirror-repo``
via the NORMAL remote-resolution path (its own ``origin`` remote), not via any fallback.
There is NO universal meta-repo default.  When the remote IS unresolvable but ``repo_root``
IS a valid directory, the slug is ``local/<basename>`` (air-gapped / local-only repos must
stay observable).  Only an UNDERIVABLE ``repo_root`` (None or non-existent directory)
raises — that is the corruption/aliasing case.  ``coordinator_root`` is irrelevant for
attribution — it resolves via ``resolve_coordinator_root()`` to the LIVE post-W4.2-cutover
coordinator script/lib clone (``<claude-klabauter-root>/coordinator`` on a current install; the
DoE-claude clone's ``coordinator/bin`` is empty post-migration and is never consulted, see
``resolvers.resolve_coordinator_root``'s docstring).

Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § C1
Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19)
"""

from __future__ import annotations

import re
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.git.git_state import head_sha as _git_state_head_sha
from coordinator_core.win_portability import no_console_creationflags

# TEST/DOC-ORACLE CONSTANT ONLY (post-2026-07-07 per-repo-emission-cutover).
# This is the correct slug that ~/.claude reaches via its own normal remote-resolution path.
# It is NEVER a runtime fallback for unresolvable remotes — an absent/unparseable remote on
# a valid repo_root yields ``local/<basename>``; only an underivable repo_root raises.
META_REPO_NAME_FALLBACK = "dbc-oduffy/.example-doctrine-mirror-repo"

# Provenance source_kinds that are git-backed and therefore carry a non-null ref
# (matches cockpit-contract/src/provenance.ts SourceKind + the D9 bidirectional invariant).
# Review: code-reviewer
# — Finding 5 (2026-07-14 entity_anchor slice review) — git_commit is in the vendored
# isGitBacked set (provenance.ts) but was missing here; dormant today (no in-repo caller
# passes source_kind="git_commit"), added for parity so a future caller auto-populating
# ref gets the correct behavior.
_GIT_BACKED_SOURCE_KINDS = frozenset({"github_graphql", "github_rest", "git_commit"})


def _posix_path(path: str) -> str:
    """Render a filesystem path as forward-slash POSIX, regardless of the host OS.

    ``provenance.path`` is a byte-contract field consumed cross-platform by DoE/rag
    (same os.sep-at-wire-boundary rule already applied to ``provenance.path``-adjacent
    fields elsewhere in this repo — e.g. ``list_review_trail_records.py``'s
    ``os.path.normpath`` note). On Windows, section porters that build ``path`` via
    ``str(Path(...))`` or an ``os.path``-joined value pick up ``os.sep`` ("\\\\"), which
    the (POSIX-captured) golden fixture never contains — normalize once, here, at the
    single shared provenance constructor, rather than in every section porter.

    A value that is ALREADY forward-slash (the common case — most section porters pass a
    repo-relative POSIX literal like ``"archive/completed"``) round-trips unchanged;
    ``Path(...).as_posix()`` is idempotent for forward-slash input on every platform.
    """
    return Path(path).as_posix()


def _run_git(repo_root: Path, *args: str) -> Optional[str]:
    """Run ``git -C <repo_root> <args>`` and return stripped stdout, or None on failure.

    Mirrors the bash ``… 2>/dev/null || echo "unknown"`` posture but returns None so the
    caller decides the sentinel value (the bash uses "unknown"; provenance uses null ref).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        # OSError: executable not found or OS rejects the call.
        # ValueError: raised by subprocess.run if capture_output=True is combined with explicit
        # stdout/stderr overrides; that combination isn't used here, but caught defensively
        # to match the pattern used for similar subprocess calls throughout resolvers.py.
        # Review: code-reviewer (F7) — rationale documented; no live ValueError path for these kwargs.
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _resolve_git_branch(repo_root: Path) -> str:
    """Mirror ``git rev-parse --abbrev-ref HEAD`` without spawning ``git``.

    Reads ``resolve_git_dir(repo_root)/HEAD`` directly -- the same file
    ``git_state.head_sha`` reads, but stopping at the FIRST line rather than
    following the one ref hop to a sha. ``HEAD`` on an attached branch is
    ``ref: refs/heads/<name>\\n``; the ``refs/heads/`` prefix is stripped to
    return ``<name>``, matching ``--abbrev-ref``'s output. A detached HEAD
    (no ``ref:`` prefix -- the file holds a bare sha) returns the literal
    string ``"HEAD"``, matching git's own ``--abbrev-ref`` behaviour there.
    Any read failure (missing ``.git``, unreadable ``HEAD``) returns
    ``"unknown"``, the same sentinel the spawn-based caller used on failure.
    """
    gitdir = resolve_git_dir(repo_root)
    try:
        content = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if not content:
        return "unknown"
    if content.startswith("ref:"):
        ref = content[len("ref:"):].strip()
        prefix = "refs/heads/"
        if ref.startswith(prefix):
            return ref[len(prefix):]
        return ref
    # Bare sha with no `ref:` prefix -- detached HEAD.
    return "HEAD"


def _remote_url_to_slug(url: str) -> Optional[str]:
    """Convert a git remote URL to an ``owner/repo`` slug, or None if unparseable.

    Handles the common SSH and HTTPS forms:
        git@github.com:dbc-oduffy/.example-doctrine-mirror-repo.git  -> dbc-oduffy/.example-doctrine-mirror-repo
        https://github.com/dbc-oduffy/.example-doctrine-mirror-repo  -> dbc-oduffy/.example-doctrine-mirror-repo
    """
    url = url.strip()
    if not url:
        return None
    # Strip a trailing .git suffix.
    url = re.sub(r"\.git$", "", url)
    # SSH scp-like form: git@host:owner/repo
    m = re.match(r"^[^@]+@[^:]+:(?P<slug>.+)$", url)
    if m:
        return m.group("slug").strip("/") or None
    # URL form: scheme://host/owner/repo (take the last two path segments)
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+/(?P<path>.+)$", url)
    if m:
        parts = [p for p in m.group("path").split("/") if p]
        if len(parts) >= 2:
            # NOTE: only the last two segments are used — a GitLab-style subgroup path
            # (group/subgroup/repo) is truncated to subgroup/repo, which is plausible but
            # not uniquely identifying.  Self-hosted multi-segment paths are out of scope;
            # the caller's coordinator_root_path field (AC12) is the disambiguation anchor.
            # Review: code-reviewer (Slice-1 F4) — documents GitLab truncation rather than silently wrong.
            return "/".join(parts[-2:])
        # Single-segment URL (e.g. https://host/myrepo — no owner prefix) — not a valid
        # owner/repo slug; return None so the caller falls through to local/<basename>.
        # Review: code-reviewer (Slice-1 F3) — was returning bare name, now returns None.
        return None
    return None


def resolve_repo_name(repo_root: Optional[Path]) -> str:  # Review: code-reviewer (Slice-1 F2) — body guards None; annotation must match
    """Resolve the emitting-repo slug from *repo_root*'s own git remote.

    Attribution invariant (Q-B hybrid, 2026-07-07 per-repo-emission-cutover AC5):
    ``repo_name`` is the EMITTING REPO's slug.  Resolves ``git -C <repo_root> remote
    get-url origin`` and parses an ``owner/repo`` slug via ``_remote_url_to_slug``.

    Fail-loud / local-slug rules:
      - ``repo_root`` is None or not an existing directory (UNDERIVABLE KEY) → raises
        ``RuntimeError``.  This is the corruption/aliasing case.  In practice callers
        guard before reaching here (artifact_emit / goal_append / recorder None-checks),
        so this branch is defensive.
      - ``repo_root`` IS a valid existing directory BUT has no parseable ``origin`` remote
        (local-only / air-gapped repo) → returns ``local/<basename>`` where ``<basename>``
        is ``Path(repo_root).name``.  Air-gapped repos must stay observable.
        Note: the top-level ``coordinator_root_path`` scalar (AC12) this comment used to
        name as the consumer-side disambiguator for cross-repo ``local/<basename>``
        collisions was removed (24ed2b31, zero readers on this pipeline). No replacement
        mechanism exists on the SnapshotEnvelope side — the per-record ``coordinator_root_path``
        field is always the literal ``"."`` (see ``sections/coordinator_roots.py``) and cannot
        disambiguate anything. ``local/<basename>`` collisions across sibling repos are
        currently unresolved for this feed.
      - Remote IS parseable → returns ``owner/repo`` slug (normal path, unchanged).

    ``META_REPO_NAME_FALLBACK`` is a test/doc-oracle constant only; it is NEVER used as a
    runtime fallback here.  The ``~/.claude`` emission reaches ``META_REPO_NAME_FALLBACK``'s
    value through the NORMAL resolution path (its own ``origin`` remote), not via any catch.

    ``repo_root`` MUST be the root of the emitting working tree.  Do NOT pass
    ``coordinator_root`` (the resolved coordinator script/lib clone — see
    ``resolve_coordinator_root()``, NOT the DoE-claude clone on a current install) — it
    would attribute the wrong tree.

    Spec backlink: pln-per-repo-emission-cutover-un-h-03f05e § C1 / AC5 / Q-B
    """
    # Underivable key guard — raises only when repo_root itself is unresolvable.
    if repo_root is None or not Path(repo_root).is_dir():
        raise RuntimeError(
            f"Cannot resolve repo slug: repo_root is underivable (got {repo_root!r}).  "
            "repo_root must be a valid existing directory "
            "(per-repo-emission-cutover AC5)."
        )
    url = _run_git(repo_root, "remote", "get-url", "origin")
    if url:
        slug = _remote_url_to_slug(url)
        if slug:
            return slug
    # No parseable remote on a valid directory — local-only / air-gapped repo.
    return f"local/{Path(repo_root).name}"


@dataclass
class EmitContext:
    """Immutable-by-convention run context, captured once and threaded to every section.

    Fields (parity with bash:117-122 one-time capture):
        repo_root          — the EMITTING REPO's main-worktree root; git -C target for
                             attribution and state reads.  Derived from the dispatch-provided
                             ``_origin_worktree`` key via ``main_worktree_root(common_dir)``
                             so linked-worktree emissions root at the shared main worktree.
        coordinator_root   — the live post-W4.2-cutover coordinator script/lib clone,
                             resolved via ``resolve_coordinator_root()`` (rung 2:
                             ``<claude-klabauter-root>/coordinator`` on a current install; the
                             DoE-claude clone's ``coordinator/bin`` is empty post-migration
                             and is never consulted — see that function's docstring).
                             NOT the emitting repo; do NOT use for slug attribution.
        central_state_root — per-repo state root: ``<repo_root>/state``; output/sentinel dir.
                             # NOTE: holds a PER-REPO root post-2026-07-07 cutover, NOT central.
        git_branch         — ``git rev-parse --abbrev-ref HEAD`` ("unknown" on failure);
                             reflects the main worktree HEAD (intended — emit reads main-
                             worktree-rooted ``state/``).
        git_sha            — ``git rev-parse HEAD`` ("unknown" on failure); same provenance.
        git_sha_short      — first 8 chars of git_sha (bash: ${GIT_SHA:0:8}).
        observed_at        — ISO-8601 UTC wall-clock, ``date -u +%FT%TZ`` equivalent.
        hostname           — ``hostname`` ("unknown" on failure).
        repo_name          — EMITTING REPO's own slug (e.g. ``dbc-oduffy/claude-klabauter``);
                             resolved from ``repo_root``'s git remote.  This is the ``repo``
                             field on every provenance row — the rag/cockpit ingest key.
        subprocess_root    — optional override for subprocess record-root resolution. When
                             set, sections pass ``--root subprocess_root`` to query-records.js
                             and set COORDINATOR_ROOT env for the review-trail record query.
                             Test-isolation hook: lets the parity harness redirect all
                             filesystem reads to a frozen fixture tree (frozen-fixture doctrine).
                             Production callers leave this None (default) so subprocess calls
                             inherit the process cwd, resolving via ``git rev-parse --show-toplevel``.
        full_enrichment    — cadence tier for this emission. THE single cadence signal; both
                             gated enrichment stages read this one field and nothing else (no
                             second flag, no env-var side channel).
                               True (DEFAULT) — full tier: every enrichment computes for real;
                                 output is byte-identical to the pre-gate emitter.
                               False — cheap tier: the two most expensive enrichments
                                 (``envelope._stamp_docs_staleness`` and the
                                 ``file_attribution`` section) are skipped and satisfied from
                                 last-known values per ``skipped_stage``'s one invariant.
                             DEFAULT-FULL IS THE SAFE DEFAULT, AND IT IS A DELIBERATE
                             REVERSAL (2026-07-29, PM decision). This field shipped hours
                             earlier defaulting to False, on the reasoning that a new or
                             unknown caller should inherit the FAST emit. That reasoning was
                             wrong, and the reason is worth stating so nobody re-derives it:
                             both real ``envelope.emit`` callers are on the full tier, so a
                             False default buys no measured saving at all and functions purely
                             as a trap — a future caller that resolves a context and emits
                             would silently get reused/stale values with NO signal. That is
                             precisely the defect class this work's most serious confirmed
                             review finding was (``artifact.emit`` silently demoted to the
                             cheap tier by the old default). Cost is recoverable by anyone who
                             notices it; silently stale data in a rag/cockpit join key is not.
                             So the cheap tier is an explicit opt-in, taken only by a caller
                             that can reason about staleness — and forgetting the flag now
                             costs time rather than truth.
        _dag_cache         — INSTANCE-level assembler memo keyed by roadmap_id. Sanctioned
                             mutable exception to this class's immutable-by-convention posture
                             (D2 — plan 2026-07-06-roadmap-dag-emit-switch). Auto-isolated per
                             run because EmitContext is created fresh each time: NO module-level
                             cache, NO clear_ctx_cache() seam. Do NOT set this field directly —
                             use the ``assembler_dag()`` accessor.
    """

    repo_root: Path
    coordinator_root: Path
    central_state_root: Path
    git_branch: str
    git_sha: str
    git_sha_short: str
    observed_at: str
    hostname: str
    repo_name: str
    subprocess_root: Optional[Path] = None
    full_enrichment: bool = True
    _dag_cache: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)

    def provenance(
        self,
        source_kind: str,
        path: Optional[str] = None,
        derivation: str = "parsed",
        ref: Optional[dict] = None,
    ) -> dict:
        """Build a ProvenanceEnvelope dict (parity: bash provenance_json:128-148).

        Mirrors the bash filter exactly:
            {
              source_kind, repo, ref, path, observed_at, derivation
            }
        where ``ref`` is ``{branch, sha}`` for git-backed source_kinds
        (github_graphql / github_rest) and ``null`` otherwise — the D9 bidirectional
        invariant (cockpit-contract/src/provenance.ts superRefine). ``path`` is the empty
        string when omitted (bash passes "" for computed / rolled-up entities). An explicit
        ``ref`` argument overrides the source_kind-derived default (used by section porters
        that carry a specific ref).

        ``entity_anchor`` (cockpit-contract v2.15.0, ProvenanceEnvelope.entity_anchor):
        always present-as-``None`` here. Every caller of this method emits a per-repo
        record (``repo`` is always non-empty), so the schema's entity-first case
        (``repo === ''`` requiring a non-null ``{kind, value}`` entity_anchor) never
        applies via this constructor. That case is reserved for the two market-intel
        arrays (``competitor_summaries`` / ``intelligence_signals``), which stay
        empty-by-design (see resolvers.py). Do not compute a non-null entity_anchor in
        this method.

        Review: code-reviewer — Finding 4 (2026-07-14 entity_anchor slice review) — this
        method is NOT the sole provenance constructor in claude-klabauter; a future re-vendor
        agent auditing entity_anchor conformance must ALSO check:
          - ``sections/coordinator_roots.py``'s hand-rolled ``_local_fs_provenance`` dict
            (the section's sole provenance constructor since the PM-ruled 2026-07-29
            removal of its ``gh``-backed sibling), which does not call ``ctx.provenance()``.
          - ``sections/file_attribution.py``, which delegates provenance construction to
            the external producer ``bin/derive-file-attribution.py`` (resolved via
            ``coordinator_root`` — see ``resolve_coordinator_root()`` — not this repo).
          - ``sections/lessons.py``, which delegates to the external producer
            ``bin/lib/emit-lesson-summaries.py`` (same external-root caveat).
        """
        if ref is None and source_kind in _GIT_BACKED_SOURCE_KINDS:
            ref = {"branch": self.git_branch, "sha": self.git_sha}
        return {
            "source_kind": source_kind,
            "repo": self.repo_name,
            "ref": ref,
            "path": _posix_path(path) if path else (path if path is not None else ""),
            "observed_at": self.observed_at,
            "derivation": derivation,
            "entity_anchor": None,
        }

    def assembler_dag(self, roadmap_id: str) -> Dict[str, Any]:
        """Return the assembled DAG for *roadmap_id*, memoizing within this run.

        On the first call for a given roadmap_id, delegates to
        ``assemble_roadmap_dag(roadmap_id, worktree_root=self.central_state_root.parent)``
        and stores the result in ``_dag_cache``.  Subsequent calls for the same id return the
        cached dict without recomputing.  Distinct roadmap_ids each compute independently.

        worktree_root derivation (F0 — CRITICAL):
            ``self.central_state_root.parent`` is the emitting working tree — the repo that
            holds roadmap stub handoffs.  This mirrors the ``cwd=ctx.central_state_root.parent``
            pattern that ``sections/roadmaps.py`` uses for its subprocess calls and matches the
            assembler docstring's ``main_worktree_root(common_dir)`` intent for linked-worktree
            safety (common_dir.parent == central_state_root.parent in the standard layout).
            Post-2026-07-07 cutover, the emitting working tree is ``repo_root`` and
            ``state_root.parent == repo_root``; use ``state_root.parent`` consistently
            (mirrors sections/roadmaps.py pattern) rather than ``self.repo_root`` directly.

        Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C0 / D2
        """
        if roadmap_id not in self._dag_cache:
            from coordinator_core.ops.roadmap_dag import assemble_roadmap_dag  # local import avoids circular dep at module load

            self._dag_cache[roadmap_id] = assemble_roadmap_dag(
                roadmap_id,
                worktree_root=self.central_state_root.parent,
            )
        return self._dag_cache[roadmap_id]

    @classmethod
    def resolve(
        cls,
        repo_root: Path,
        coordinator_root: Path,
        central_state_root: Path,
    ) -> "EmitContext":
        """Build an EmitContext by capturing live git / host / clock state ONCE.

        Parity with bash:117-122 — the git/host/observed_at fields are read a single time
        and reused for every record's provenance (``emitted_at`` emission-uniformity invariant).

        Attribution (REVERSED 2026-07-07 per-repo-emission-cutover): ``repo_name`` is
        resolved from the EMITTING REPO's own remote (``repo_root``) via
        ``resolve_repo_name``.  Each working repo emits under its own identity.
        When ``repo_root`` is a valid directory but has no parseable ``origin`` remote
        (local-only / air-gapped repo), emits ``local/<basename>`` (Q-B hybrid, AC5).
        Only raises when ``repo_root`` is underivable (None or non-existent directory).
        Do NOT pass ``coordinator_root`` (the resolved coordinator script/lib clone — see
        ``resolve_coordinator_root()``, NOT the DoE-claude clone on a current install) — it
        would attribute the wrong tree.  ``git_branch``/``git_sha`` reflect the main
        worktree HEAD (intended).
        # Review: code-reviewer (Slice-1 F1) — old text said "raises on no remote"; Q-B hybrid returns local/<basename> instead.

        Spawn count (2026-08-22, ``the-import-path-costs-nothing`` C11):
        ``git_branch``/``git_sha`` are resolved SPAWN-FREE — ``_resolve_git_branch`` and
        ``coordinator_core.git.git_state.head_sha`` both read ``.git`` files directly via
        ``resolve_git_dir``. ``repo_name`` is NOT spawn-free: ``resolve_repo_name`` still
        shells out to ``git remote get-url origin`` (``git.remote_url.get_remote_url``'s own
        docstring rejects a ``.git/config`` parse as a correctness regression — subsection
        quoting, multivalued keys, and ``insteadOf``/``includeIf`` rewrites all diverge from
        a literal read). So this call is zero-git-spawn ONLY for a caller that never reads
        ``ctx.repo_name`` — every current caller of ``resolve_context``/``EmitContext.resolve``
        (``recorder.py``, ``goal_append.py``, ``goal_close_day.py``, ``strategic_emit.py``,
        ``workday_complete/brief.py``) reads ``ctx.repo_name`` via ``ctx.provenance()`` or
        directly, so none of them is zero-spawn today; a narrow repo_name-free variant would
        have no live caller and was not added for that reason.
        """
        git_branch = _resolve_git_branch(repo_root)
        git_sha = _git_state_head_sha(repo_root) or "unknown"
        git_sha_short = git_sha[:8]
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        hostname = _resolve_hostname()
        repo_name = resolve_repo_name(repo_root)  # emitting repo's own remote, fail loud if absent
        return cls(
            repo_root=repo_root,
            coordinator_root=coordinator_root,
            central_state_root=central_state_root,
            git_branch=git_branch,
            git_sha=git_sha,
            git_sha_short=git_sha_short,
            observed_at=observed_at,
            hostname=hostname,
            repo_name=repo_name,
        )


def _resolve_hostname() -> str:
    """Return the machine hostname, or "unknown" on failure (bash:122 posture)."""
    try:
        return socket.gethostname() or "unknown"
    except OSError:
        return "unknown"
