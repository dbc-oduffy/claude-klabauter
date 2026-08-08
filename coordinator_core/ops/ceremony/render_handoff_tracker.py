"""
coordinator_core.ops.ceremony.render_handoff_tracker — C9 disk seam for C8b's
``render_repo_section``.

Purpose: covers the per-repo production shape ``render-handoff-tracker.js``
served before its retirement — render (write ``state/handoff-tracker.md``
under the target repo root). ``coordinator_core.ops.ceremony.renderers.
render_repo_section`` (C8b) is a pure function returning markdown with no
disk I/O and no output-path opinion; this module is the C9 seam that wires
that renderer to disk for the single per-repo consumer.

Exposes both a registered op (``ceremony.render_handoff_tracker``, for
IPC-dispatch callers — the ceremony tail, ``/pickup``, future consumers) and a
trampoline-importable ``main(argv) -> int`` (for a bare CLI invocation or a
sibling-repo caller that shells out python directly), mirroring the
``coordinator_core.text.refresh_queries`` / ``coordinator_core.ops.
refresh_roadmap_callout`` precedent of a dual op-handler + CLI surface over
one render/write core.

Output-path resolution reuses ``coordinator_core.state_root.
coordinator_state_root`` rather than re-deriving the node oracle's
``resolvePerRepoStateRoot`` branch natively:
  - Per-repo mode  -> ``coordinator_state_root(central=False, git_root=root)``
    (Rule 5 — meta-repo lands in claude-klabauter's state, a sibling repo lands in its
    own ``state/``; the exact peer of the node script's meta-repo/sibling-repo
    branch).
This is the SAME seam ``coordinator_core.ops.ceremony.wsc_commit.
_tail_render_handoff_tracker`` (step_2.75) already uses for its own per-repo
write — this module does not touch that call site (a separate spinoff owns
repointing it off its own inline write logic onto this one); the two are
independent producers of the identical output shape today.

Spec backlink: docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md § C8b/C9
Ask backlink: cross-repo/inbox/2026-07-22-claude-central-em-c9-handoff-tracker-render-op-wiring.md

Port source (CLI/dispatch layer only — rendering itself is C8b's, not re-derived
here): coordinator/bin/render-handoff-tracker.js (example-doctrine-repo) `main()` (:595-664),
`resolvePerRepoStateRoot()` (:83-126).

Negative-spec:
  - Does NOT re-implement ``render_repo_section`` or any of its table/grouping
    helpers — always calls C8b's port, unchanged.
  - Does NOT support a fleet-aggregate ``--all-repos`` mode. That mode (example-doctrine-repo-
    aggregate render across every machine-local-registered ``repos.*`` repo,
    writing ``state/doe-handoff-tracker.md`` under the central root) was
    REMOVED 2026-07-23 (PM-ratified) after example-cockpit-repo-em confirmed it has
    no consumer for the fleet aggregate at any cadence and recommended
    dropping it outright rather than relocating it — see
    ``cross-repo/inbox/2026-07-23-example-cockpit-repo-em-fleet-tracker-cadence-reply.md``
    and ``state/improvement-queue/2026-07-23-fleet-tracker-on-demand-via-cockpit.yaml``.
    The per-repo mode this module still serves is unaffected and unchanged.
"""

from __future__ import annotations

import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Set, Tuple

from coordinator_core.ipc import register_op
from coordinator_core.ops.ceremony.renderers import (
    _GitHistoryCacheProvider,
    _join_handoff_lineage,
    render_handoff_lineage_markdown,
    render_plans_index_markdown,
    render_repo_section,
)
from coordinator_core.ops.fleet._common import main_worktree_root, rel_id
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.state_root import StateRootError, coordinator_state_root

_PROG = "render-handoff-tracker"


# ---------------------------------------------------------------------------
# Render (pure, no I/O) — returns (markdown, write-target path)
# ---------------------------------------------------------------------------


def _generated_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"


def render_repo(
    root: Path,
    *,
    joined_lineage: Optional[List[dict]] = None,
    git_history_cache: Optional[_GitHistoryCacheProvider] = None,
) -> Tuple[str, Path]:
    """Render one repo's tracker (header + C8b section); no disk I/O.

    ``joined_lineage`` (optional): forwarded to ``render_repo_section`` — see
    that function's docstring. Pass this when also rendering
    ``state/handoff-lineage.md`` in the same call, so both draw counts from
    one join over one disk snapshot (module-level docstring's reconciliation
    point).

    ``git_history_cache`` (optional): forwarded to ``render_repo_section``
    for its own plans-join tier-3 resolution — see that function's matching
    parameter docstring. Both call sites below construct a single
    ``_GitHistoryCacheProvider(str(root))`` per render invocation and thread
    it through every render in that invocation; constructing the provider
    does zero I/O, and its ``.get()`` call lazily builds ``dag.build_git_
    history_cache`` on the FIRST tier-3 need, memoizing the result for the
    rest of the invocation. A per-render rebuild, let alone a per-ref
    subprocess spawn, is the exact cost this parameter exists to avoid
    (dag.py:526 measured 1053 spawns / ~14.6s on the corpus that motivated
    the cache) — see ``_GitHistoryCacheProvider``'s own docstring for the
    lazy-build mechanics.

    Returns ``(full_markdown, out_path)`` where ``out_path`` is resolved via
    ``coordinator_state_root(central=False, git_root=root)`` — Rule 5's
    meta-repo/sibling-repo branch, the native peer of the node oracle's
    ``resolvePerRepoStateRoot``.
    """
    section = render_repo_section(
        root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
    )
    generated = _generated_timestamp()
    # `root.name` (the repo directory's basename), never the full absolute
    # `root` path -- an earlier cut baked the operator's own machine-
    # specific absolute path into a tracked, committed file every render,
    # which the concrete-path-citation guard's own escape hatch cannot fix
    # durably: a same-line marker gets silently wiped on the next render,
    # and the finding returns forever. The basename is the portable form
    # that still tells a reader which repo the tracker covers.
    header = f"# Handoff Tracker\n\n_Generated: {generated} | root: {root.name}_\n\n"
    output = header + section
    out_path = Path(coordinator_state_root(central=False, git_root=str(root))) / "handoff-tracker.md"
    return output, out_path


def _write(output: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output + "\n", encoding="utf-8")
    # DR-276: declared AFTER the write lands — one call site covers all three
    # renders (tracker, plans index, handoff lineage) that route through it.
    declare_write(out_path)


def render_plans_index(
    root: Path, *, git_history_cache: Optional[_GitHistoryCacheProvider] = None
) -> Optional[Tuple[str, Path]]:
    """Render ``docs/plans/INDEX.md`` (Item B — the durable plans index); no
    disk I/O. Returns ``None`` when ``root`` has no ``docs/plans/`` tree at
    all — writing an empty generated index into a repo with no plans would
    be noise, not signal.

    ``git_history_cache`` (optional): forwarded to ``render_plans_index_
    markdown`` — see ``render_repo``'s matching parameter docstring.

    Negative-spec: the output path is ``INDEX.md``, never ``README.md``. The
    sibling ``docs/plans/README.md`` is hand-authored narrative and is not a
    generator target.
    """
    if not (root / "docs" / "plans").is_dir():
        return None
    output = render_plans_index_markdown(root, git_history_cache=git_history_cache)
    out_path = root / "docs" / "plans" / "INDEX.md"
    return output, out_path


def render_handoff_lineage(
    root: Path,
    *,
    joined_lineage: Optional[List[dict]] = None,
    git_history_cache: Optional[_GitHistoryCacheProvider] = None,
) -> Optional[Tuple[str, Path]]:
    """Render ``state/handoff-lineage.md`` (handoff->handoff predecessor /
    origin_handoff / forked_from lineage); no disk I/O. Returns ``None`` when
    ``root`` has no ``state/handoffs/`` tree at all — same "no source, no
    generated artifact" rule ``render_plans_index`` follows.

    ``joined_lineage`` (optional): forwarded to ``render_handoff_lineage_
    markdown`` — see ``render_repo``'s matching parameter docstring.

    ``git_history_cache`` (optional): forwarded to ``render_handoff_lineage_
    markdown`` — only consulted when ``joined_lineage`` is ``None`` (that
    join already baked in whatever cache it was built with).
    """
    if not (root / "state" / "handoffs").is_dir():
        return None
    output = render_handoff_lineage_markdown(
        root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
    )
    out_path = root / "state" / "handoff-lineage.md"
    return output, out_path


# ---------------------------------------------------------------------------
# Registered op handler
# ---------------------------------------------------------------------------


@register_op("ceremony.render_handoff_tracker")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'ceremony.render_handoff_tracker' handler — C9 disk seam.

    Parameters (params dict):
        root       (str, optional)  — explicit repo root override for
                                       per-repo mode; disk-resolved repo_root
                                       (common_dir) is primary, this is the
                                       secondary/testing path (mirrors
                                       ceremony.session_instructions' own
                                       scope_mode override precedent).

    repo_root:
        Git common dir (``_OP_KEY_SCOPE = "common_dir"``).

    Returns ``{"exit_code": 0, "mode": "repo", "out_path": str}`` or
    ``{"exit_code": 1, "error": str}``.
    """
    root_override = params.get("root")
    if root_override:
        root = Path(root_override)
    elif repo_root is not None:
        root = main_worktree_root(Path(repo_root))
    else:
        return {
            "exit_code": 1,
            "error": (
                "ceremony.render_handoff_tracker: repo_root arg is None and no "
                "'root' param supplied — common_dir not supplied by engine "
                "(check _OP_KEY_SCOPE = 'common_dir')"
            ),
        }

    # Lazily built — the tier-3 (git-history) resolution ``_join_handoff_
    # lineage`` and the plans join both perform materializes this cache on
    # the FIRST tier-3 need, not upfront, so a render where every candidate
    # resolves in tier 1/2 spawns zero subprocesses. Constructing the
    # provider itself does no I/O. A git failure (e.g. not a repo) degrades
    # the built cache to None, a valid input everywhere it's threaded — every
    # consumer falls back to per-call resolution rather than treating it as
    # an error. See ``_GitHistoryCacheProvider``'s docstring.
    git_history_cache = _GitHistoryCacheProvider(str(root))

    # Computed ONCE and threaded into both render_repo (tracker's remainder
    # pointer) and render_handoff_lineage (the full file) so the two draw
    # counts from one join over one disk snapshot — see render_repo's
    # ``joined_lineage`` docstring.
    joined_lineage = _join_handoff_lineage(root, git_history_cache=git_history_cache)

    try:
        output, out_path = render_repo(
            root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
        )
    except StateRootError as exc:
        return {"exit_code": 1, "error": str(exc)}
    _write(output, out_path)

    result: dict = {"exit_code": 0, "mode": "repo", "out_path": str(out_path)}
    plans_index = render_plans_index(root, git_history_cache=git_history_cache)
    if plans_index is not None:
        plans_output, plans_out_path = plans_index
        _write(plans_output, plans_out_path)
        result["plans_readme_path"] = str(plans_out_path)
    handoff_lineage = render_handoff_lineage(
        root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
    )
    if handoff_lineage is not None:
        lineage_output, lineage_out_path = handoff_lineage
        _write(lineage_output, lineage_out_path)
        result["handoff_lineage_path"] = str(lineage_out_path)
    return result


# ---------------------------------------------------------------------------
# CLI trampoline (main(argv) -> int) — refresh_queries.py precedent
# ---------------------------------------------------------------------------


def _detect_root(specified: str) -> str:
    """``--root`` flag -> git-toplevel auto-discovery -> cwd fallback.

    Mirrors the node oracle's ``detectRoot`` exactly, including the
    non-fail-loud cwd fallback (deliberately looser than
    ``coordinator_state_root``'s own fail-loud git-root resolution — this is
    the bare-CLI entry point, not the op-dispatch path).
    """
    if specified:
        return str(Path(specified).resolve())
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
        if result.returncode == 0:
            toplevel = result.stdout.strip()
            if toplevel:
                return toplevel
    except (OSError, subprocess.SubprocessError):
        print(f"skip: _detect_root: git rev-parse failed: {sys.exc_info()[1]}", file=sys.stderr)
    return os.getcwd()


#: Caller label recorded in the shared housekeeping-failures log on a self-commit
#: failure (C5 residue) — lets a reader distinguish this render's follow-up
#: commit from a sibling render's or an archive sweep's own failure record.
_SELF_COMMIT_CALLER_LABEL = "render-handoff-tracker.py:self-commit"


def main(argv: List[str], *, self_commit: bool = False) -> int:
    """CLI entry: ``[--root <path>] [--stdout]`` — mirrors the node oracle's
    ``main()`` flag surface and stdout/write branching for per-repo mode.

    ``self_commit`` (keyword-only, default False — C5 residue, 2026-07-23
    wsc-tail-slim-down): when True and the write actually landed under ``root``
    (the Rule-5 SIBLING-REPO branch — see ``render_repo``'s own docstring), the
    written artifact is committed with an explicit pathspec via
    ``detached_render_commit.commit_own_artifact``. The Rule-5 META-REPO branch
    (``out_path`` outside ``root``, e.g. a meta-repo worktree landing the
    tracker in claude-klabauter's own central state) is a DIFFERENT git repo entirely —
    never dirty in THIS worktree, so it MUST NOT grow a commit here; the
    written file there is left exactly as ``_write`` leaves it, matching the
    plan's C5 disposition. Default False preserves this function's PRE-EXISTING
    behavior for every other caller — the live, still-synchronous
    ``coordinator_core.ops.ceremony.wsc_commit`` module has its own separate,
    unrelated inline write and never reaches here, and no other in-process
    caller of THIS module's ``main`` is known to exist. Only the
    ``coordinator/bin/render-handoff-tracker.py`` CLI trampoline — the exact
    entry point both a human and the new detached spawn
    (``tail_ops.fire_tracker_and_roadmap_detached``) invoke as a subprocess —
    opts in.
    """
    root_arg = ""
    want_stdout = False

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if arg == "--stdout":
            want_stdout = True
            i += 1
            continue
        print(f"Unknown argument: {arg}", file=sys.stderr)
        return 1

    root = Path(_detect_root(root_arg))
    # Lazily built, threaded through every render below — see the matching
    # comment in ``_handler`` above for the perf rationale.
    git_history_cache = _GitHistoryCacheProvider(str(root))
    # Computed ONCE and threaded into both render_repo and
    # render_handoff_lineage below — see render_repo's ``joined_lineage``
    # docstring.
    joined_lineage = _join_handoff_lineage(root, git_history_cache=git_history_cache)
    try:
        output, out_path = render_repo(
            root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
        )
    except StateRootError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if want_stdout:
        print(output)
        return 0
    _write(output, out_path)
    print(f"Wrote tracker → {out_path}")

    plans_index = render_plans_index(root, git_history_cache=git_history_cache)
    if plans_index is not None:
        plans_output, plans_out_path = plans_index
        _write(plans_output, plans_out_path)
        print(f"Wrote plans index → {plans_out_path}")

    handoff_lineage = render_handoff_lineage(
        root, joined_lineage=joined_lineage, git_history_cache=git_history_cache
    )
    if handoff_lineage is not None:
        lineage_output, lineage_out_path = handoff_lineage
        _write(lineage_output, lineage_out_path)
        print(f"Wrote handoff lineage → {lineage_out_path}")

    if self_commit:
        root_resolved = root.resolve()
        out_path_resolved = out_path.resolve()
        try:
            rel_path = rel_id(out_path_resolved, root_resolved)
            sibling_repo_branch = True
        except ValueError:
            sibling_repo_branch = False
        if sibling_repo_branch:
            from coordinator_core.ops.ceremony.detached_render_commit import (
                commit_own_artifact,
            )

            ok = commit_own_artifact(
                root_resolved, rel_path,
                "tracker: refresh handoff-tracker.md (detached render)",
                caller_label=_SELF_COMMIT_CALLER_LABEL,
            )
            if not ok:
                print(
                    "render-handoff-tracker: self-commit failed after retries — "
                    "see state/housekeeping-failures.log",
                    file=sys.stderr,
                )
        # META-REPO branch: out_path lands in a DIFFERENT git repo (claude-klabauter's
        # own central state) — nothing to stage/commit in THIS worktree.
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
