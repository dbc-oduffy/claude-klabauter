"""
coordinator_core.ops.refresh_roadmap_callout — scoped refresh of a single
roadmap's STUB-INDEX query callout.

Purpose: parse `<roadmap_id> [--root <repo>]`, resolve `state/roadmap/<roadmap_id>/
STUB-INDEX.md`, and — when it exists and carries a `<!-- BEGIN query:`
callout — delegate rendering to `coordinator_core.text.refresh_queries`
(the single source of truth for callout rendering; this module never
re-implements callout-body rendering). Clean no-op (exit 0 + one-line note)
when the roadmap dir or callout is absent — a roadmap_id with no index is
not an error.

Trust guard: before delegating, resolves and trust-checks the coordinator-claude
coordinator root (CLAUDE_PLUGIN_ROOT env, else `~/.claude/.doe-root` pointer
+ `/coordinator`) via the canonical
`coordinator_core.trusted_root_guard.is_trusted` — see that module for the
full anchor list. cc_root is still validated before any rendering proceeds,
even though the delegate below is now a direct in-process import rather
than a subprocess-to-node call (refresh-queries.js itself is retired — see
Negative-spec).

Port of: refresh-roadmap-callout.sh (coordinator-claude a1a568d2, 2026-07-22)
Spec backlink: docs/plans/2026-07-09-roadmap-callout-refresh-at-pickup-and-wsc.md § C1
               docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md (R1 DOE-PORT)

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix"):
    - `refresh-queries.js` has been fully ported to
      `coordinator_core.text.refresh_queries` (BIG_PORT item `refresh-queries`,
      2026-07-17) and is now invoked via a direct in-process import
      (`refresh_queries.main([...])`), not `subprocess.run(["node", ...])`
      — this is a mechanical delegate-target swap only; arg-parse/
      validation/path-resolution/trust-guard behavior in THIS module is
      unchanged. `refresh_queries.main()` itself ALSO no longer bridges to
      `coordinator/bin/query-records.js` (that dependency was ported
      in-process too, 2026-07-22 — see that module's own docstring); `node`
      is not a runtime dependency anywhere in this call chain any more.
    - The `roadmap_id` quote-strip only strips ONE layer of matching
      double- or single-quotes (mirrors the bash `${VAR%\"}"`/`${VAR#\"}`
      parameter-expansion pair) — a doubly-quoted id is not fully unwrapped.
    - The allowlist regex `^[A-Za-z0-9][A-Za-z0-9._-]*$` plus an explicit
      `..` substring reject (belt-and-braces even though the character
      class already excludes `/`) is preserved verbatim, including the
      false-reject edge on a `..`-containing-but-otherwise-safe id.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.trusted_root_guard import is_trusted as _is_trusted_root

_PROG = "refresh-roadmap-callout.sh"  # literal program-name prefix — matches bash oracle's messages

_ALLOWLIST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _strip_one_quote_layer(value: str) -> str:
    """Strip one matching layer of surrounding double- or single-quotes.

    Mirrors bash: ROADMAP_ID="${ROADMAP_ID%\\"}"; ROADMAP_ID="${ROADMAP_ID#\\"}"
    then the same pair for single-quotes. Each expansion trims at most one
    trailing/leading quote char independently (not a matched-pair strip),
    so behavior is reproduced exactly, including odd inputs like `"foo`.
    """
    if value.endswith('"'):
        value = value[:-1]
    if value.startswith('"'):
        value = value[1:]
    if value.endswith("'"):
        value = value[:-1]
    if value.startswith("'"):
        value = value[1:]
    return value


def _validate_roadmap_id(roadmap_id: str) -> bool:
    """Allowlist + traversal check — mirrors the bash `case` guard exactly."""
    if not roadmap_id:
        return False
    if ".." in roadmap_id:
        return False
    return bool(_ALLOWLIST_RE.match(roadmap_id))


def _resolve_root(root_arg: str) -> str:
    """--root flag -> git-toplevel auto-discovery -> cwd fallback.

    Toplevel discovery is delegated to the shared, cwd-keyed
    ``coordinator_core.git.repo_root.show_toplevel`` seam (C24,
    2026-08-07 n-plus-one-git-spawn-class-and-amplification-gate plan)
    rather than a private ``git rev-parse --show-toplevel`` spawn: that
    seam WALKS for the ordinary case (climbs cwd looking for a `.git`
    entry) and only spawns as a memoized fallback when no `.git` entry is
    found. This module has two in-process callers
    (``coordinator_core.ops.ceremony.tail_ops.refresh_roadmap_callout``
    and ``coordinator_core.ops.promote_shipped_in_flight_stubs``), both of
    which invoke ``main()`` — and therefore this function — once per
    roadmap id in a loop with an INVARIANT cwd across iterations; the
    seam's per-resolved-cwd memo collapses that to at most one spawn
    (often zero, via the walk) per process instead of one spawn per
    roadmap id. Neither caller needed a call-site edit — see this
    chunk's own scope note.
    """
    if root_arg:
        return root_arg
    toplevel = _show_toplevel()
    if toplevel:
        return toplevel
    return os.getcwd()


def _resolve_cc_root() -> str:
    """CLAUDE_PLUGIN_ROOT env -> ~/.claude/.doe-root pointer + /coordinator."""
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if env_root:
        return env_root
    claude_home = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~")
    doe_root_pointer = Path(claude_home) / ".claude" / ".doe-root"
    try:
        doe_root = doe_root_pointer.read_text(encoding="utf-8").strip()
    except OSError:
        doe_root = ""
    if not doe_root:
        return ""
    return str(Path(doe_root) / "coordinator")


#: Caller label recorded in the shared housekeeping-failures log on a self-commit
#: failure (C5 residue, 2026-07-23 wsc-tail-slim-down) — lets a reader
#: distinguish this render's follow-up commit from a sibling render's or an
#: archive sweep's own failure record.
_SELF_COMMIT_CALLER_LABEL = "refresh-roadmap-callout.py:self-commit"


def main(argv: List[str], *, self_commit: bool = False) -> int:
    """CLI entry: arg parse, validation, no-op checks, trust guard, node delegate.

    ``self_commit`` (keyword-only, default False — C5 residue, 2026-07-23
    wsc-tail-slim-down): when True and the delegate (`refresh_queries.main`)
    reports full success (return code 0 — no per-callout errors, whether or not
    the callout body actually changed), the rewritten ``STUB-INDEX.md`` is
    committed with an explicit pathspec via
    ``detached_render_commit.commit_own_artifact``, which is itself a safe no-op
    (success, no commit) when the file turns out not to be dirty relative to
    HEAD — so a render that produced no textual change never grows an empty
    commit. Unlike `render_handoff_tracker`, there is no Rule-5 meta-repo
    branch here: `stub_index` is always resolved under `root` (this repo's own
    ``state/roadmap/<roadmap_id>/STUB-INDEX.md``), so the "own it end-to-end"
    disposition applies unconditionally whenever the write happened.

    Default False preserves this function's PRE-EXISTING behavior for its
    existing in-process callers — most notably
    ``coordinator_core.ops.ceremony.tail_ops.refresh_roadmap_callout``, the
    STILL-LIVE synchronous ceremony-tail call that folds this file's write into
    the SAME commit as the ceremony's own via ``extra_stage_paths`` (see that
    function's own docstring) — a self-commit here would double-commit (or
    commit ahead of) that ride-along staging. Only the
    ``coordinator/bin/refresh-roadmap-callout.py`` CLI trampoline — the exact
    entry point both a human (``/pickup``) and the new detached spawn
    (``tail_ops.fire_tracker_and_roadmap_detached``) invoke as a subprocess —
    opts in.
    """
    roadmap_id = ""
    root_arg = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root":
            root_arg = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if arg in ("-h", "--help"):
            print(f"Usage: {_PROG} <roadmap_id> [--root <repo>]")
            return 0
        if not roadmap_id:
            roadmap_id = arg
        i += 1

    if not roadmap_id:
        print(f"ERROR: {_PROG} requires <roadmap_id>", file=sys.stderr)
        return 1

    roadmap_id = _strip_one_quote_layer(roadmap_id)

    if not _validate_roadmap_id(roadmap_id):
        print(
            f"ERROR: invalid roadmap_id '{roadmap_id}' — must match "
            "^[A-Za-z0-9][A-Za-z0-9._-]*$ with no '..' traversal",
            file=sys.stderr,
        )
        return 1

    root = _resolve_root(root_arg)
    stub_index = Path(root) / "state" / "roadmap" / roadmap_id / "STUB-INDEX.md"

    if not stub_index.is_file():
        print(
            f"refresh-roadmap-callout: no STUB-INDEX.md for roadmap '{roadmap_id}' "
            f"at {stub_index} — no-op"
        )
        return 0

    try:
        text = stub_index.read_text(encoding="utf-8")
    except OSError:
        text = ""
    if "<!-- BEGIN query:" not in text:
        print(f"refresh-roadmap-callout: {stub_index} has no query callout — no-op")
        return 0

    cc_root = _resolve_cc_root()
    if not _is_trusted_root(cc_root):
        print(
            f"ERROR: coordinator root '{cc_root}' outside trusted prefix — refusing to "
            "source; re-run coordinator:install (or set COORDINATOR_PLUGIN_ROOT_TRUSTED=1 "
            "for a sanctioned --plugin-dir spike)",
            file=sys.stderr,
        )
        return 1

    if not cc_root or not os.path.isdir(cc_root):
        print(
            "ERROR: coordinator root unresolved — ~/.claude/.doe-root missing/invalid; "
            "re-run coordinator:install",
            file=sys.stderr,
        )
        return 1

    from coordinator_core.text.refresh_queries import main as _refresh_queries_main

    rc = _refresh_queries_main(["--files", str(stub_index), "--root", root])

    if rc == 0:
        # DR-276: the actual byte-rewrite happens inside
        # coordinator_core.text.refresh_queries (out of this module's scope),
        # but this is the orchestrator that knows the final destination path
        # and that the delegate reported success — declare it here so the
        # rewrite becomes a session scope-touch claim rather than an orphan
        # at the scoped_git_commit sink.
        from coordinator_core.session.declared_writes import declare_write

        declare_write(str(stub_index))

    # Do NOT fire into a failed render (constraint 4) — a non-zero rc means the
    # delegate hit a per-callout error (or worse); nothing here is trustworthy
    # to commit.
    if self_commit and rc == 0:
        from coordinator_core.ops.ceremony.detached_render_commit import (
            commit_own_artifact,
        )

        root_path = Path(root).resolve()
        stub_index_resolved = stub_index.resolve()
        try:
            rel_path = str(stub_index_resolved.relative_to(root_path))
        except ValueError:
            # stub_index somehow landed outside root — cannot express as a
            # pathspec relative to the commit cwd; skip rather than guess.
            rel_path = None
        if rel_path is not None:
            ok = commit_own_artifact(
                root_path, rel_path,
                f"roadmap: refresh {roadmap_id} STUB-INDEX callout (detached render)",
                caller_label=_SELF_COMMIT_CALLER_LABEL,
            )
            if not ok:
                print(
                    "refresh-roadmap-callout: self-commit failed after retries — "
                    "see state/housekeeping-failures.log",
                    file=sys.stderr,
                )

    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
