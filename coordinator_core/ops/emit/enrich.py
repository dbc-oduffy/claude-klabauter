"""
coordinator_core.ops.emit.enrich — single-walk, order-preserving last-modified-at enrichment.

Purpose: port the bash LMA (last-modified-at) enrichment pattern reused across Sections 1,
8.6, 8.8 (emit-cockpit-snapshot.sh, scout map § "LMA enrichment pattern"). The bash spawns
``git log -1 --format=%cI`` per path as background subprocesses, writes each result to a
per-index temp file, ``wait``s, then reassembles a path->LMA map.

PERF REWRITE (2026-07-17): the original Python port replicated the bash fan-out literally —
one ``git log -1 -- <path>`` subprocess PER path, bounded at 16 concurrent workers. envelope
``build()`` calls this three times (handoffs, plans, roadmaps) for ~190+ paths in this repo's
corpus, which is 190+ process spawns per ``emit.cadence`` tick — O(corpus), and the dominant
cost blowing the 10s invoke budget (measured ~24s, growing as ``docs/plans/`` fills). This
module now derives every path's last-modified-at from a SINGLE ``git log --name-only`` walk
of full history (newest-first): the first commit that touches a given path IS that path's
``git log -1 -- <path>`` answer, so one streamed walk resolves the whole batch. The walk is
terminated early (``proc.terminate()``) the moment every requested path has been resolved —
no need to read history older than the newest touch of the last unresolved path.

Output-preserving: paths not seen anywhere in the walk (untracked / no history / bad path)
fall back to the SAME ``None`` sentinel the old per-path spawn returned in that case — see
``_walk_last_modified_at`` docstring. Proven equivalent against the old per-path spawn for
every ``docs/plans/*.md`` + ``state/handoffs/*.md`` path in this repo's live corpus at
rewrite time (script not committed — see spec backlink for the perf-fix plan).

DRIFT FIX (2026-07-17, post-review): the initial single-walk rewrite was equivalence-tested
only against linear, plain-ASCII-filename history and had two silent divergences from the
``git log -1 --format=%cI -- <path>`` oracle, both fixed here:

  - **Merge commits** — plain ``git log --name-only`` (no ``-m``/``--cc``) emits ZERO path
    lines for a merge commit, so a merge that is the true last-touching commit for a path was
    invisible to the walk, which fell through to an OLDER non-merge ancestor — silently too
    old. The old per-path oracle's default history simplification KEEPS a merge in the
    simplified history only when it is NOT TREESAME to *every* parent for that path (i.e. the
    combined/``--cc``-style diff is non-empty), and otherwise transparently follows into
    whichever parent it IS treesame to. ``--cc`` reproduces exactly this: it shows a path for
    a merge commit only when the merge's resolution differs from ALL parents, which is the
    same predicate the oracle's simplification uses. (``-m``, by contrast, shows a per-parent
    diff — so a merge that is treesame to one parent but differs from another still surfaces
    a path line relative to the differing parent, which the oracle would have skipped past.
    Empirically verified against two merge fixtures: one where the merge's resolution differs
    from BOTH parents — oracle keeps the merge, ``--cc`` agrees, ``-m`` also agrees; one where
    the merge is a clean, non-conflicting merge and its content for the path is IDENTICAL to
    one parent — oracle skips the merge and resolves to that parent's touching commit,
    ``--cc`` agrees, ``-m`` does NOT (it still reports the merge commit, since the diff
    relative to the *other* parent is non-empty. ``--cc`` is therefore the equivalence-correct
    flag, not ``-m``.)
  - **Non-ASCII / special-character filenames** — ``git log --name-only`` quotes and
    C-escapes any path containing non-ASCII bytes by default (``core.quotepath=true``, git's
    own default), e.g. ``café.md`` is emitted as ``"caf\303\251.md"``. The parser's bare
    ``path in remaining`` string comparison against the raw, unquoted input never matched, so
    such paths silently fell through to the same ``None`` fallback as "no history". Fixed by
    invoking the walk with ``-c core.quotepath=false`` (a global git config override, not a
    ``log`` flag), which disables the quoting/escaping so the emitted path is byte-identical
    to the raw path string. Empirically verified: a fixture with an accented filename now
    round-trips through the walk exactly, matching the raw input path.

Both fixes stay in the SAME single batched walk — no per-path fallback subprocess was
reintroduced, so the perf win (one spawn instead of N) is unaffected.

Offline / bad-path posture (parity with bash): a git failure yields ``None`` for every path
and the run continues — never aborts the whole enrichment.

Note (spine): last_modified_at is an emit-DERIVED field. Section porters LEAVE it null in
collect(); this helper is invoked by the envelope/enrichment layer (C2/C3), not inside a
section's collect(). It is authored here so the wiring chunk can import a pinned interface.

Spec backlink: docs/plans/2026-07-04-tc3-emission-stack-python-port-and-backlog-history.md § C1
Perf-fix backlink: emit.cadence O(corpus) git-spawn-per-path fix, 2026-07-17.
Port of: emit-cockpit-snapshot.sh (example-doctrine-repo 07eedcfb, 2026-07-19) — Section 1 LMA, Sections 8.6 / 8.8.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

# Record/field separators for the single-walk log format. git's ``--pretty=format:`` takes
# the LITERAL strings "%x00"/"%x01" as placeholders and emits the corresponding raw byte in
# its OUTPUT — the argv passed to Popen must contain the literal 4-char placeholder, never
# an actual NUL/SOH byte (an embedded NUL in argv raises ValueError at exec time, which the
# broad except below would swallow as "nothing found"). ``_RECORD_SEP``/``_FIELD_SEP`` are
# the real bytes we parse FROM git's output; ``_RECORD_SEP_FMT``/``_FIELD_SEP_FMT`` are the
# placeholder strings we pass INTO the --pretty=format argument.
_RECORD_SEP = "\x00"
_FIELD_SEP = "\x01"
_RECORD_SEP_FMT = "%x00"
_FIELD_SEP_FMT = "%x01"


def _walk_last_modified_at(
    repo_root: Path,
    wanted: set[str],
    *,
    revrange: Optional[str] = None,
) -> dict[str, str]:
    """Resolve last-modified-at for every path in ``wanted`` via ONE streamed git-log walk.

    ``revrange`` (default None = full history from HEAD) restricts the walk to a revision
    range such as ``<old-head>..HEAD``. It exists for ``lma_cache``'s fast-forward extension
    path, which re-derives from only the commits added since a cached entry was written; see
    that module's docstring for the two conditions under which a range walk is provably
    equivalent to the full walk it replaces. A range walk resolves ONLY the paths touched
    within the range — every other path is absent from the return value, which for a range
    walk means "not touched in this range", NOT the "no history" meaning it carries for a
    full walk. Only ``lma_cache`` may pass this argument, because only it holds the cached
    answers that give the absent paths their real values.

    Walks full history newest-first (``git log --cc --name-only
    --pretty=format:%x00%H%x01%cI``, with ``-c core.quotepath=false``). For each path in
    ``wanted``, the first commit record in which it appears (i.e. the most recent commit
    touching it, since the walk is newest-first) supplies its committer date — exactly what
    ``git log -1 --format=%cI -- <path>`` returns for that path today, INCLUDING for
    merge-commit-introduced changes and non-ASCII filenames (see module docstring "DRIFT FIX"
    for the empirical justification of ``--cc`` and ``core.quotepath=false``). The walk is
    terminated as soon as every wanted path has been resolved (early-exit; avoids reading
    older history than necessary once the batch is satisfied).

    Returns a dict of only the paths actually found in history. Paths absent from the return
    value (untracked / no history / never committed) get the same ``None`` fallback the old
    per-path ``git log -1`` spawn produced for those cases — the caller applies that fallback.
    """
    resolved: dict[str, str] = {}
    if not wanted:
        return resolved
    remaining = set(wanted)
    cmd = [
        "git",
        "-c",
        "core.quotepath=false",
        "-C",
        str(repo_root),
        "log",
        "--cc",
        "--name-only",
        f"--pretty=format:{_RECORD_SEP_FMT}%H{_FIELD_SEP_FMT}%cI",
    ]
    if revrange:
        cmd.append(revrange)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError):
        return {}
    assert proc.stdout is not None
    current_ts: Optional[str] = None
    try:
        for line in proc.stdout:
            if not remaining:
                break
            if line.startswith(_RECORD_SEP):
                header = line[len(_RECORD_SEP):].rstrip("\n")
                _commit_hash, _sep, committer_date = header.partition(_FIELD_SEP)
                current_ts = committer_date or None
                continue
            path = line.rstrip("\n")
            if not path or current_ts is None:
                continue
            if path in remaining:
                resolved[path] = current_ts
                remaining.discard(path)
    finally:
        try:
            proc.stdout.close()
        except (OSError, ValueError):
            # Best-effort teardown; stdout already closed is not an error here.
            pass
        proc.terminate()
        proc.wait()
    return resolved


def batch_last_modified_at_grouped(
    repo_root: Path,
    path_groups: Sequence[Sequence[str]],
) -> list[list[Optional[str]]]:
    """Resolve last-modified-at for MULTIPLE independently-ordered groups of paths via
    ONE shared history walk.

    PERF (2026-07-29): ``envelope._stamp_lma`` used to call ``batch_last_modified_at`` once
    per record group (handoffs, plans, roadmaps) — three separate full ``git log --cc
    --name-only`` spawns over the SAME history. Measured on the example-doctrine-repo corpus (8264
    commits): each individual walk already costs close to a full-history read whenever its
    group contains even one rarely-touched (or never-committed) path, since the early exit
    only fires once ``remaining`` is fully empty — so three sequential walks cost roughly
    3x a single full walk instead of 1x. This function walks the UNION of every group's
    paths exactly once and slices the results back out per group, so the wall-clock cost
    is bounded by ``max`` of the group costs rather than their ``sum`` (measured ~2.3x
    speedup on example-doctrine-repo, ~2x on this repo's own corpus — see spec backlink).

    Each returned list preserves ITS OWN group's input order — positional alignment per
    group is unchanged from ``batch_last_modified_at`` (bash hazard 5.11 — the ``fleet.$i``
    / ``${!_HA_SHAS[@]}`` reassembly ordering must survive the port). Groups do not
    interact: a path present in two groups is resolved once but reported independently
    (correctly) in each group's output list.

    PERF (2026-07-29, Lever 1): the union walk is now served through
    ``lma_cache.resolve_last_modified_at``, a HEAD-keyed persistent cache that skips the walk
    entirely when git history has not moved and re-derives from only the new commits when it
    has fast-forwarded. Semantics are unchanged — that function's return shape and its
    "absent means no history" contract are identical to ``_walk_last_modified_at``'s, and its
    cache is served only when provably equivalent to a fresh walk (see its docstring for the
    exactness conditions and the never-serve-stale posture).

    Spec backlink: coordinator_core/ops/emit/enrich.py module docstring (single-walk LMA
    rewrite, 2026-07-17); this function extends that rewrite from one path list to N.
    """
    union: set[str] = set()
    for paths in path_groups:
        union.update(p for p in paths if p)
    from coordinator_core.ops.emit import lma_cache  # local import — avoids an import cycle

    resolved = lma_cache.resolve_last_modified_at(Path(repo_root), union)
    return [
        [resolved.get(p) if p else None for p in paths]
        for paths in path_groups
    ]


def batch_last_modified_at(
    repo_root: Path,
    paths: Sequence[str],
) -> list[Optional[str]]:
    """Resolve last-modified-at for each path IN INPUT ORDER, via a single history walk.

    Returns a list of ISO-8601 committer-date strings (or None) positionally aligned with
    ``paths``. Signature and return shape are UNCHANGED from the per-path-spawn version this
    replaces — callers are untouched (bash hazard 5.11 — the ``fleet.$i`` /
    ``${!_HA_SHAS[@]}`` reassembly ordering must survive the port). Single-group convenience
    wrapper over ``batch_last_modified_at_grouped``; the grouped form is what
    ``envelope._stamp_lma`` uses to share one walk across handoffs/plans/roadmaps.
    """
    if not paths:
        return []
    return batch_last_modified_at_grouped(repo_root, [paths])[0]
