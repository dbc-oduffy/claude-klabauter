"""Section porter — RoutineSignals (envelope key: ``routine_signals``).

Emits exactly six RoutineSignal records — weekly, docs, arch-audit, bug-sweep,
dormant-repo, distill-backlog — in that order. Each carries a computed staleness
``computed_state`` + ``overdue`` boolean derived from a live source: two staleness
checks (natively ported ``coordinator_core.ops.check_weekly_staleness`` /
``check_arch_audit_staleness``, invoked in-process), git-log commit counts since the
last update-docs / bug-sweep commit, a static "unknown" dormant-repo placeholder
(cross-repo scan needs the tc-4 connector), and a native distill-backlog count
(``_count_distill_backlog`` below — port of ``count-distill-backlog.sh --format json``).

All six use provenance derivation ``rolled_up``; there is no malformed bucket for this
section (the shapes are constructed, never parsed). Byte/semantic parity port.

Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) § SECTION 4 —
  RoutineSignals.
Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § P04
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.check_arch_audit_staleness import main as _arch_staleness_main
from coordinator_core.ops.check_weekly_staleness import (
    _claude_home as _cws_claude_home,
    _git_root as _cws_git_root,
    _claude_klabauter_root as _cws_claude_klabauter_root,
    _same_path as _cws_same_path,
    main as _weekly_staleness_main,
)

# Hardcoded threshold-description strings, verbatim from the bash oracle (one per signal).
_THRESHOLD_WEEKLY = (
    "STALE: >= 5 days AND >= 15 commits since last weekly-reset; "
    "MILD: one condition; FRESH: neither"
)
_THRESHOLD_DOCS = "STALE: >10 commits since last update-docs run; MILD: 1-10 commits; FRESH: 0 commits"
_THRESHOLD_ARCH = "STALE: arch audit not run in >=30 days; MILD: 15-29 days; FRESH: <15 days"
_THRESHOLD_BUG_SWEEP = "STALE: >15 commits since last bug-sweep; MILD: 1-15 commits; FRESH: 0 commits"
_THRESHOLD_DORMANT = (
    "STALE: sibling repo with no commits in >=30 days; tc-3 emits unknown without REST access"
)
_THRESHOLD_DISTILL = (
    "fresh (pending=0), mild (pending 1-5), stale (pending >= 6), "
    "unknown (archive empty or unreadable)"
)

# count-distill-backlog.sh --format json failure fallback.
_DISTILL_FALLBACK = {"pending_count": 0, "threshold_days": 30, "computed_state": "unknown"}

_logger = logging.getLogger(__name__)


def _resolve_coordinator_state_root(coordinator_root: Path) -> Optional[str]:
    """Resolve the COORDINATOR's own state root (not the emitting repo's) from an
    explicit *coordinator_root* directory, without touching process cwd.

    Mirrors ``check_weekly_staleness._resolve_state_root()`` /
    ``check_arch_audit_staleness._resolve_state_root()``'s identical Rule-5 (bare, no
    --central/--subject/--artifact) resolution logic — meta-repo root (git root ==
    CLAUDE_HOME) routes to CLAUDE_KLABAUTER_ROOT/state; any other (sibling-repo) git root uses
    ``<git-root>/state`` directly — but pins the git invocation to *coordinator_root*
    explicitly via ``_git_root(cwd=...)`` instead of the process-global cwd the ``_chdir``
    bridge used to mutate (AC-5 no-implicit-cwd).

    Both staleness modules carry duplicate copies of ``_git_root``/``_same_path``/
    ``_claude_home``/``_claude_klabauter_root``; this reuses ``check_weekly_staleness``'s copies
    (the two modules' copies are identical) rather than adding a third duplicate here.
    Returns None if the coordinator root isn't a git repo, or (meta-repo case) if
    CLAUDE_KLABAUTER_ROOT can't be resolved — the caller degrades to "unknown" on None.
    """
    git_root = _cws_git_root(cwd=str(coordinator_root))
    if git_root is None:
        return None
    if _cws_same_path(git_root, _cws_claude_home()):
        claude_klabauter_root = _cws_claude_klabauter_root()
        if claude_klabauter_root is None:
            return None
        return str(Path(claude_klabauter_root) / "state")
    return str(Path(git_root) / "state")


def _run_staleness_native(main_fn, state_root: Optional[str]) -> str:
    """Call a natively-ported staleness ``main(argv)`` in-process and lowercase its stdout.

    In-process replacement for ``_run_staleness`` (was: ``bash "$script" 2>/dev/null ||
    echo "UNKNOWN"``, then lowercase). Both ``check-weekly-staleness.sh`` and
    ``check-arch-audit-staleness.sh`` were already fully ported to native
    ``coordinator_core.ops`` modules on an earlier wave — this retires the last bash-spawn
    hop by calling those modules' ``main()`` directly instead of shelling out to their
    (bash-gone) ``.sh`` names. The coordinator's own state root (AC-5: cwd-not-repo-scoped
    — the staleness signal is about the COORDINATOR's own state, not the emitting repo's)
    is resolved once by the caller (``collect()``, via ``_resolve_coordinator_state_root``)
    and handed in here as *state_root*, replacing the former ``_chdir(bin_dir)``
    process-cwd bridge with an explicit argv-threaded root (no process-global mutation).

    When *state_root* is ``None`` (the coordinator root isn't a git repo, or the
    meta-repo/CLAUDE_KLABAUTER_ROOT branch can't resolve), this short-circuits to "unknown"
    WITHOUT calling ``main_fn`` — calling it with an empty argv would let the staleness
    module fall through to its own cwd-based ``_resolve_state_root()``, silently
    reintroducing the exact implicit-cwd dependency this fix exists to eliminate. A
    non-zero exit or any exception on the call itself also yields the "unknown"
    sentinel; stderr is discarded, matching the oracle's ``2>/dev/null`` posture.
    """
    if state_root is None:
        return "unknown"
    argv = ["--root", state_root]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = main_fn(argv)
    except Exception:
        _logger.debug("staleness check %r raised", main_fn, exc_info=True)
        return "unknown"
    if rc != 0:
        return "unknown"
    return buf.getvalue().strip().lower()


# ---------------------------------------------------------------------------
# Distill-backlog native port (count-distill-backlog.sh --format json)
# ---------------------------------------------------------------------------
_DISTILL_THRESHOLD_DAYS = 30
# Suffix strip: bash `${base%-??????}` — literal "-" + exactly six arbitrary
# characters at the end (NOT hex-specific; a positional glob strip).
_HASH_SUFFIX_LEN = 7  # "-" + 6 chars
# Prefix strip: bash `${no_hash#????-??-??-}` — 11 arbitrary characters at
# positions 0-10 with literal "-" at offsets 4, 7, 10 (a positional glob strip,
# NOT a validated YYYY-MM-DD regex).
_DATE_PREFIX_LEN = 11


def _distill_field(line: str) -> str:
    """awk ``$2`` semantics: second whitespace-delimited token, "" if absent."""
    parts = line.split()
    return parts[1] if len(parts) > 1 else ""


def _distill_slug(entry_path: str, chain: str) -> str:
    """Derive the wiki-corpus match slug for one archive entry (bash:97-106)."""
    if chain and chain != "null":
        return chain
    base = os.path.basename(entry_path)
    base_noext = base[:-3] if base.endswith(".md") else base
    no_hash = base_noext
    if len(base_noext) >= _HASH_SUFFIX_LEN and base_noext[-_HASH_SUFFIX_LEN] == "-":
        no_hash = base_noext[: -_HASH_SUFFIX_LEN]
    if (
        len(no_hash) >= _DATE_PREFIX_LEN
        and no_hash[4] == "-"
        and no_hash[7] == "-"
        and no_hash[10] == "-"
    ):
        return no_hash[_DATE_PREFIX_LEN:]
    return no_hash


def _resolve_distill_root(coordinator_root: Path) -> Path:
    """Resolve the archive/wiki scan root (bash:19-27 script-location + env-fallback).

    Prefers the script-location-inferred root (``bin/../../../..``, the pre-W4.2
    ``.claude/plugins/coordinator/bin`` nesting) when it has an
    ``archive/completed`` dir; else falls back to ``CLAUDE_HOME (or ~) /.claude`` —
    mirroring the bash oracle's own two-rung resolution verbatim (its "mandatory
    env-fallback form (verbatim per spec)" comment).
    """
    inferred = (coordinator_root / "bin").parent.parent.parent.parent
    if (inferred / "archive" / "completed").is_dir():
        return inferred
    return Path(os.environ.get("CLAUDE_HOME", str(Path.home()))) / ".claude"


def _count_distill_backlog(coordinator_root: Path) -> dict:
    """Native port of ``count-distill-backlog.sh --format json``.

    Returns ``{"pending_count": int, "threshold_days": 30, "computed_state": str}``.
    Raises ``RuntimeError`` when the archive root is missing — mirrors the bash oracle's
    ``exit 1`` (caller degrades to ``_DISTILL_FALLBACK`` on any exception, exactly as it
    previously degraded on a non-zero subprocess exit).

    computed_state bands (bash:143-154):
        unknown — the archive/*/*.md glob matched zero (non-empty) files, OR one or more
                  dated subdirectories could not be scanned (see skipped-subtree note below)
        fresh   — files scanned, none older than the 30-day cutoff
        mild    — 1-5 pending
        stale   — >=6 pending

    Skipped-subtree handling (state/audits/2026-07-22 silent-success audit): the original
    ``archive_root.glob("*/*.md")`` walk only guarded ``archive_root.is_dir()`` at the top —
    a dated subdirectory made unreadable AFTER that check (e.g. ``chmod 0o000``) would have
    ``glob()`` silently swallow the ``PermissionError`` and enumerate zero files from it,
    indistinguishable from that subdirectory genuinely having no pending entries. Each dated
    subdirectory is now listed explicitly (``os.listdir`` in a try/except), so an unreadable
    subdir is observed rather than silently dropped. Per the threshold text above
    ("unknown … archive empty or unreadable"), any skipped subtree downgrades the whole
    verdict to ``unknown`` — never ``fresh``/``mild`` — and the return dict carries an
    additional ``skipped_subtree_count`` key (present only when >0) so callers can
    distinguish "verified fresh/mild" from "state unknown because part of the tree was
    unreadable".
    """
    root = _resolve_distill_root(coordinator_root)
    archive_root = root / "archive" / "completed"
    if not archive_root.is_dir():
        raise RuntimeError(f"count-distill-backlog: archive root not found: {archive_root}")

    wiki_local = root / "docs" / "wiki"
    wiki_coord = root / "plugins" / "coordinator-claude" / "coordinator" / "docs" / "wiki"
    corpus_parts: list[str] = []
    for wiki_dir in (wiki_local, wiki_coord):
        if wiki_dir.is_dir():
            for md in sorted(wiki_dir.glob("*.md")):
                try:
                    corpus_parts.append(md.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    # Unreadable wiki file -- excluded from the corpus; at worst this
                    # inflates pending_count below (a missed "already distilled" match),
                    # never masks a real backlog entry.
                    continue
    corpus = "\n".join(corpus_parts)

    cutoff = (date.today() - timedelta(days=_DISTILL_THRESHOLD_DAYS)).isoformat()

    archive_files_found = 0
    pending_count = 0
    skipped_subtree_count = 0

    # NOTE: walks dated subdirectories explicitly (os.listdir per subdir, in a try/except),
    # NOT archive_root.glob("*/*.md") — glob()'s selector silently swallows PermissionError
    # while walking a subdirectory (verified: an unreadable dir yields an empty iterator, no
    # exception), which would make a chmod'd dated subdir indistinguishable from one that
    # genuinely has zero pending entries. See the skipped-subtree docstring note above.
    try:
        subdirs = sorted(
            (e.path for e in os.scandir(archive_root) if e.is_dir()),
        )
    except OSError:
        # archive_root.is_dir() was already checked above; a failure here means it became
        # unreadable between the check and this scan (TOCTOU) — treat as one skipped subtree
        # so the verdict still degrades to "unknown" below rather than reporting archive_files_found == 0.
        subdirs = []
        skipped_subtree_count += 1

    for subdir in subdirs:
        try:
            names = sorted(fn for fn in os.listdir(subdir) if fn.endswith(".md"))
        except OSError as exc:
            _logger.warning(
                "count_distill_backlog: cannot scan archive subtree %s — %s; excluded "
                "from the scan (verdict degrades to 'unknown', never fresh/mild, per the "
                "skipped-subtree contract)",
                subdir,
                exc,
            )
            skipped_subtree_count += 1
            continue

        for name in names:
            md = Path(subdir) / name
            try:
                if md.stat().st_size == 0:
                    continue  # zero-byte files produce no awk record — skipped (bash comment)
                text = md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            archive_files_found += 1

            created = ""
            chain = ""
            for line in text.splitlines():
                if not created and line.startswith("created:"):
                    created = _distill_field(line)
                if not chain and line.startswith("chain:"):
                    chain = _distill_field(line)
                if created and chain:
                    break

            if not created:
                continue  # no frontmatter — skip (legacy rollup files)
            if not (created < cutoff):
                continue  # only strictly-older-than-cutoff entries count

            slug = _distill_slug(str(md), chain)
            found = bool(slug) and slug in corpus
            if not found:
                pending_count += 1

    # --- Tier 2 (behaviour change -- PM sign-off required) ---
    # A skipped subtree forces the verdict to "unknown" regardless of what was counted in
    # the readable subtrees — pending_count from a partial scan is an undercount, never a
    # trustworthy "fresh"/"mild" signal (state/audits/2026-07-22 silent-success audit).
    if skipped_subtree_count:
        computed_state = "unknown"
    elif archive_files_found == 0:
        computed_state = "unknown"
    elif pending_count == 0:
        computed_state = "fresh"
    elif pending_count <= 5:
        computed_state = "mild"
    else:
        computed_state = "stale"
    # --- end Tier 2 ---

    result = {
        "pending_count": pending_count,
        "threshold_days": _DISTILL_THRESHOLD_DAYS,
        "computed_state": computed_state,
    }
    if skipped_subtree_count:
        result["skipped_subtree_count"] = skipped_subtree_count
    return result


def _commits_since_last(repo_root: Path, grep: str) -> int:
    """Count commits from the last commit matching ``grep`` to HEAD (parity: bash:854-865/877-886).

    Finds the newest commit across all refs whose message matches ``grep`` (git basic-regex),
    then counts ``<sha>..HEAD``. No matching commit → 99 (the "very stale" sentinel).

    Both invocations pin ``GIT_CEILING_DIRECTORIES=<repo_root's parent>`` so git's own
    upward repository discovery cannot escape *repo_root* into an ancestor repo when
    *repo_root* itself is not (or is no longer, e.g. a relocated/detached fixture tree)
    a git root — git stops climbing exactly at the ceiling instead of silently adopting
    whatever enclosing repo happens to sit above it. ``repo_root`` is contractually the
    emitting working tree's own root (context.py:137), which normally has ``.git``
    directly inside it, so this ceiling is a no-op there; it only changes behavior for
    the not-a-git-root case, from "escape to an ancestor repo" to the intended
    "not a git repository" failure.
    """
    env = dict(os.environ, GIT_CEILING_DIRECTORIES=str(Path(repo_root).parent))
    last_sha = ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--oneline", "--all",
             "--extended-regexp", "--grep", grep, "--format=%H"],
            capture_output=True, text=True, check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            env=env,
            **no_console_creationflags(),
            # AC-5 no-implicit-cwd: explicit cwd= is redundant with `git -C <repo_root>`
            # (which already overrides git's working directory) but required by the
            # AST-level no-implicit-cwd gate (test_no_implicit_cwd.py), which flags any
            # subprocess.run call lacking an explicit cwd= keyword regardless of -C.
        )
        if proc.returncode == 0:
            first = proc.stdout.splitlines()
            last_sha = first[0].strip() if first else ""
    except (OSError, subprocess.TimeoutExpired):
        last_sha = ""

    if not last_sha:
        return 99

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--oneline", f"{last_sha}..HEAD"],
            capture_output=True, text=True, check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            cwd=str(repo_root),
            env=env,
            **no_console_creationflags(),
        )
        if proc.returncode != 0:
            return 0
        return len(proc.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return 0


def _build_signal(ctx: EmitContext, kind: str, state: str, overdue: bool,
                  inputs: dict, threshold: str) -> dict:
    """Assemble one RoutineSignal record (parity: bash build_routine_signal:904-939)."""
    return {
        "kind": kind,
        "repo": ctx.repo_name,
        "coordinator_root_path": ".",
        "inputs": inputs,
        "threshold": threshold,
        "computed_state": state,
        "overdue": overdue,
        "observed_at": ctx.observed_at,
        "computed_as_of": ctx.observed_at,
        "provenance": ctx.provenance("coordinator_artifact", path="", derivation="rolled_up"),
    }


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build the six RoutineSignal records (records=[6 signals], malformed=[])."""
    # Resolved once (not per staleness check): both check-weekly and check-arch-audit
    # calls below share the same coordinator root for the duration of this collect()
    # invocation, so re-resolving per call is a redundant subprocess spawn on a
    # per-op-budgeted path (DR-215).
    state_root = _resolve_coordinator_state_root(ctx.coordinator_root)

    weekly_state = _run_staleness_native(_weekly_staleness_main, state_root)
    arch_state = _run_staleness_native(_arch_staleness_main, state_root)

    # Distill-backlog signal (bash:849-852) — native port, no subprocess.
    try:
        distill = _count_distill_backlog(ctx.coordinator_root)
    except (RuntimeError, OSError):
        distill = dict(_DISTILL_FALLBACK)
    distill_pending = distill.get("pending_count", 0)
    distill_state = distill.get("computed_state", "unknown")
    distill_skipped_subtrees = distill.get("skipped_subtree_count", 0)
    # A skipped subtree makes distill_pending an undercount — never report overdue=False
    # from a partial scan (state/audits/2026-07-22 silent-success audit); treat any
    # skipped subtree as overdue conservatively rather than falsely-clean.
    distill_overdue = distill_pending >= 6 or distill_skipped_subtrees > 0

    # Docs staleness (bash:854-875).
    docs_commits = _commits_since_last(ctx.repo_root, "update-docs")
    if docs_commits == 0:
        docs_state, docs_overdue = "fresh", False
    elif docs_commits <= 10:
        docs_state, docs_overdue = "mild", False
    else:
        docs_state, docs_overdue = "stale", True

    # Bug-sweep staleness (bash:877-896). Basic-regex alternation matches either spelling.
    bug_commits = _commits_since_last(ctx.repo_root, "bug-sweep|bug_sweep")
    if bug_commits == 0:
        bug_state, bug_overdue = "fresh", False
    elif bug_commits <= 15:
        bug_state, bug_overdue = "mild", False
    else:
        bug_state, bug_overdue = "stale", True

    signals = [
        _build_signal(ctx, "weekly", weekly_state, weekly_state == "stale",
                      {"check": "check-weekly-staleness.sh"}, _THRESHOLD_WEEKLY),
        _build_signal(ctx, "docs", docs_state, docs_overdue,
                      {"commits_since_update_docs": docs_commits}, _THRESHOLD_DOCS),
        _build_signal(ctx, "arch-audit", arch_state, arch_state == "stale",
                      {"check": "check-arch-audit-staleness.sh"}, _THRESHOLD_ARCH),
        _build_signal(ctx, "bug-sweep", bug_state, bug_overdue,
                      {"commits_since_bug_sweep": bug_commits}, _THRESHOLD_BUG_SWEEP),
        _build_signal(ctx, "dormant-repo", "unknown", False,
                      {"note": "cross-repo commit scan requires tc-4 connector; "
                               "emitting unknown from tc-3"}, _THRESHOLD_DORMANT),
        # Review: code-reviewer — distill-backlog is the one signal here where `overdue`
        # can be True while `computed_state` is "unknown" rather than "stale" (the
        # skipped-subtree case above); every sibling signal above ties overdue directly
        # to its own computed_state == "stale" branch. Deliberate (see distill_overdue
        # comment above / _count_distill_backlog docstring) — flagged here too since this
        # call site is the one most likely to get "simplified" to match its siblings.
        _build_signal(
            ctx, "distill-backlog", distill_state, distill_overdue,
            {"pending_count": distill_pending, **(
                {"skipped_subtree_count": distill_skipped_subtrees}
                if distill_skipped_subtrees else {}
            )},
            _THRESHOLD_DISTILL,
        ),
    ]

    return signals, []
