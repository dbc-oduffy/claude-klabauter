"""
coordinator_core.ops.normalize_claimed_frontmatter

Purpose: native Python port of DoE-claude
coordinator/bin/normalize-consumed-frontmatter.js -- flips frontmatter to
match `<!-- consumed: YYYY-MM-DD [notes] -->` body markers. Belt-and-suspenders
companion to the makima port of query-records.js: query-records normalizes
marker-bearing records at read time, but the on-disk frontmatter is what an
EM sees first. Drift between marker and frontmatter is a confusion source --
this module eliminates it.

DR-084 (docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md
C5) renamed this module and repointed its writer at the new handoff-axis
status/field vocabulary; the body-comment marker convention it reads from
(`<!-- consumed: ... -->`, CONSUMED_MARKER_RE) is a separately-scoped,
never-renamed surface -- see Negative-spec.

For each record whose body contains `<!-- consumed: YYYY-MM-DD [notes] -->`:
  - `status: active` (or anything non-terminal) -> `status: claimed`
  - `deployment_state: {ready_to_fire,awaiting_gate,in_flight}` -> `shipped`
  - inserts `claimed_at:` if absent (date from marker); reads `claimed_at:`
    or the legacy `consumed_at:` field dual-tolerantly to decide whether
    the field is already present
  - inserts `shipped_in:` (+ lockstep `shipped_in_kind:`) if absent and marker
    carries CONFORMING notes (a bare hex SHA -> `shipped_in_kind: ship-commit`,
    or the sanctioned substantively-shipped-no-commit:<YYYY-MM-DD> token ->
    `shipped_in_kind: no-commit` -- see `coordinator_core.shipped_in_tokens`'s
    `_SHA_HEX_RE` / `_NO_COMMIT_TOKEN_RE` below, the single choke point this
    module's own bespoke value-grammar copy was retired in favor of, DR-096).
    Non-conforming notes (prose, annotated SHAs) are refused, not written; the
    refusal is recorded in `changes[]` so the run's stdout summary surfaces it
    instead of silently dropping it.
  - strips `gate_dependency:` (only meaningful while awaiting_gate)

Terminal states (HANDOFF_TERMINAL_STATUS / HANDOFF_TERMINAL_DEPLOYMENT --
`claimed`/`consumed`/`superseded` and `shipped`/`abandoned`/`continued`/
`closed` respectively) are preserved.

Only git-tracked files are modified -- untracked drafts (e.g. stubs with a
consumed-marker placeholder) are silently skipped.

Usage (argv, mirrors the node CLI verbatim -- this module's main() receives
argv already stripped of the interpreter/script pair, same convention as
plan_status_transition.main() / handoff_gate_aging.main()):
    normalize-claimed-frontmatter [--dry-run] [--root <path>]
                                   [--type handoff|plan|decision|review]

Default scans handoff + plan + decision + review.

Exit codes (byte-parity with the node oracle -- both business codes are
pre-existing overloads inherited verbatim from the JS original, NOT
introduced by this port; see Negative-spec):
    0 -- run completed: no drift found, OR drift found and (fixed / reported
         in dry-run) with zero per-file processing errors.
    1 -- EITHER a CLI usage error (unrecognized flag, immediate abort before
         any scanning) OR at least one per-file processing error occurred
         during the scan (e.g. a block-scalar frontmatter value that
         replace_fm_field/remove_fm_field refuses to touch) -- scanning
         continues past a per-file error, so exit 1 does not mean "nothing
         was written"; check stdout for the per-file summary.

This module is NOT a JSON-RPC op (no `@register_op`, no registry-map entry).
It is a plain module with a `main(argv)` CLI entry point, called by the
DoE-side trampoline (coordinator/bin/normalize-consumed-frontmatter.js) via
a spawned `python -c` bootstrap that direct-imports and calls main() --
template-variant #1 (safe-leaf early-win port), same shape as
plan_status_transition / handoff_gate_aging. The DoE trampoline stays a
Node CLI (not an sh/python polyglot shebang) because its one live caller
(coordinator/commands/workday-start.md) invokes it as `node <script>` --
node parses the whole file as JavaScript regardless of a leading shebang
line, so a polyglot sh/python shebang would be inert there; the trampoline
instead bridges via a synchronous `python -c` child-process spawn.

Port source: coordinator/bin/normalize-consumed-frontmatter.js (DoE-claude)
Parity oracle: DoE-claude coordinator/bin/normalize-consumed-frontmatter.js
    (node, golden-oracle diff run during the port).

Negative-spec:
    - Does NOT do a full YAML parse -- uses the shared text-based frontmatter
      primitives (coordinator_core.frontmatter.primitives), the same module
      handoff_transition.py / memo_transition.py / plan_status_transition.py
      already share, byte-identical rebuild behavior with the node oracle's
      splitFrontmatter/readFmField/replaceFmField/insertFmField/removeFmField
      (bin/lib/schema.js) -- not a re-derivation.
    - Consumes coordinator_core.frontmatter.consumed_marker for
      TERMINAL_STATUS/TERMINAL_DEPLOYMENT/CONSUMED_MARKER_RE (already ported,
      shared with the query-records.js read-time port) rather than
      re-declaring them here -- mirrors the JS original's own stated
      write-time/read-time alignment purpose.
    - Directory-walk order (walk_dir) is NOT sorted -- neither is the node
      oracle's (fs.readdirSync has no implicit sort); both reflect raw OS
      readdir(3) order, which is consistent for the same fixtures on the
      same filesystem, but multi-file stdout ordering is not a portable
      cross-OS parity guarantee.
    - An unrecognized `--type` value crashes uncaught (TypeError from
      os.path.join(root, None), mirroring the node oracle's own
      path.join(root, undefined) TypeError) -- this is a faithfully
      reproduced oracle bug, not a gap: the CLI's only advertised `--type`
      values are handoff/plan/decision/review, and a caller passing
      anything else was already crashing pre-port. Not silently hardened.
    - gate_dependency content is no longer dropped on flip (C8, AC11): it is
      retired into blocking_notes via the shared
      coordinator_core.frontmatter.primitives._retire_gate_dependency
      primitive before the key itself is stripped -- superseding the JS
      original's own documented no-retention tradeoff.
"""
from __future__ import annotations

import os
import stat as stat_module
import subprocess
import sys
from typing import Dict, List, Optional, Set

from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.frontmatter.consumed_marker import (
    CONSUMED_MARKER_RE,
    TERMINAL_DEPLOYMENT,
    TERMINAL_STATUS,
)
from coordinator_core.frontmatter.primitives import (
    _retire_gate_dependency,
    insert_fm_field,
    read_fm_field,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.shipped_in_tokens import _NO_COMMIT_TOKEN_RE, _SHA_HEX_RE
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

_PROG = "normalize-claimed-frontmatter"

# Generator-provenance declaration (C2, generator_provenance.py's AST reader).
# main() rewrites whichever tracked handoff/plan/decision/review files
# currently carry a <!-- consumed: --> body marker -- a data-dependent subset
# of TYPE_TO_GLOB's directories, not a fixed artifact list.
MUTATES = [
    "state/handoffs/*.md",
    "archive/handoffs/**/*.md",
    "docs/plans/*.md",
    "docs/decisions/*.md",
    "state/reviews/*.md",
]

# DR-054 console-flash guard: suppresses the transient console window Windows
# spawns for a subprocess.run child when the parent has no console of its
# own (e.g. this op invoked from a GUI-launched context). No-op on POSIX
# (getattr falls back to 0). Applied to both `git` subprocess.run calls below.

# shipped_in shape guard: DR-096 (DoE-claude 2026-07-26 ruling) retires this
# module's own bespoke copy of the value grammar in favor of the single choke
# point's -- `coordinator_core.shipped_in_tokens._SHA_HEX_RE` /
# `_NO_COMMIT_TOKEN_RE` (the same shape `stamp_shipped_in` validates a `sha`
# override against). Accepts a bare hex SHA (7-64 chars) OR the sanctioned
# substantively-shipped-no-commit:<YYYY-MM-DD> token. Whole-value match via
# `.fullmatch` below -- no accept-anything-after-the-colon.

# RAG-bait: scan top-level archive/handoffs/ only. tasks/handoffs/archive/ is
#           a grandfathered holodeck-specific path (read-only) -- do NOT add
#           it here, mirroring the node oracle's own comment verbatim.
TYPE_TO_GLOB: Dict[str, object] = {
    "handoff": ["state/handoffs", "archive/handoffs"],
    "plan": "docs/plans",
    "decision": "docs/decisions",
    "review": "state/reviews",
}


class _Opts:
    __slots__ = ("dry_run", "root", "types")

    def __init__(self, dry_run: bool, root: Optional[str], types: List[str]) -> None:
        self.dry_run = dry_run
        self.root = root
        self.types = types


def parse_args(argv: List[str]) -> _Opts:
    """Parse argv (post-program-name, i.e. sys.argv[1:] equivalent).

    Mirrors the node oracle's parseArgs(process.argv.slice(2)) contract --
    this module's main() already receives argv stripped of the
    interpreter/script-path pair, so no extra slice is needed here.
    """
    dry_run = False
    root: Optional[str] = None
    types: List[str] = list(TYPE_TO_GLOB.keys())
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--root":
            i += 1
            root = argv[i] if i < len(argv) else None
        elif a == "--type":
            i += 1
            types = [argv[i] if i < len(argv) else None]  # type: ignore[list-item]
        else:
            sys.stderr.write(f"Unknown argument: {a}\n")
            sys.exit(1)
        i += 1
    return _Opts(dry_run=dry_run, root=root, types=types)


def detect_root(specified: Optional[str]) -> str:
    """Resolve the scan root -- explicit --root, else `git rev-parse --show-toplevel`,
    else cwd fallback (mirrors the node oracle's detectRoot exactly)."""
    if specified:
        return os.path.abspath(specified)
    toplevel = _show_toplevel()
    return toplevel if toplevel else os.getcwd()


def normalize_one(file_path: str) -> Optional[Dict[str, object]]:
    """Flip one file's frontmatter to match its body's consumed-marker, if present.

    Returns None when the file has no parseable frontmatter, no consumed
    marker, or the marker produces zero field changes. Otherwise returns
    ``{"rebuilt": <full file text>, "changes": [<human-readable change line>, ...]}``.

    May raise ValueError -- propagated from replace_fm_field/remove_fm_field's
    block-scalar guard (a `status:`/`deployment_state:`/`gate_dependency:`
    value that is itself a multi-line YAML block scalar cannot be safely
    rewritten single-line); callers are responsible for catching this per
    file so one malformed record doesn't halt the whole scan (see main()).
    """
    with open(file_path, "r", encoding="utf-8", newline="") as f:
        original = f.read()

    split = split_frontmatter(original)
    if split is None:
        return None

    marker_match = CONSUMED_MARKER_RE.search(split.body_with_leading_newline)
    if marker_match is None:
        return None

    date = marker_match.group(1)
    notes = (marker_match.group(2) or "").strip()

    current_status = read_fm_field(split.fm_text, "status")
    current_deployment = read_fm_field(split.fm_text, "deployment_state")
    # Dual-tolerant read (DR-084 P1 migration window): a record may still
    # carry the legacy `consumed_at` field if it hasn't been touched since
    # the rename; either presence means the field doesn't need inserting.
    current_claimed_at = read_fm_field(split.fm_text, "claimed_at") or read_fm_field(
        split.fm_text, "consumed_at"
    )
    current_shipped_in = read_fm_field(split.fm_text, "shipped_in")

    changes: List[str] = []
    fm_text = split.fm_text

    if current_status and current_status not in TERMINAL_STATUS:
        fm_text = replace_fm_field(fm_text, "status", "claimed")
        changes.append(f"status: {current_status} → claimed")
    elif not current_status:
        fm_text = insert_fm_field(fm_text, "status", "claimed", "title")
        changes.append("status: (missing) → claimed")

    if current_deployment and current_deployment not in TERMINAL_DEPLOYMENT:
        fm_text = replace_fm_field(fm_text, "deployment_state", "shipped")
        changes.append(f"deployment_state: {current_deployment} → shipped")

    if not current_claimed_at:
        fm_text = insert_fm_field(fm_text, "claimed_at", date, "status")
        changes.append(f"claimed_at: + {date}")

    # gate_dependency is only meaningful while deployment_state is
    # awaiting_gate. Once flipped to shipped, the field is stale noise --
    # retire it into blocking_notes (C8: _retire_gate_dependency APPENDS the
    # full value rather than the former truncated-echo-then-drop) and strip
    # the key itself (schema requires absence outside awaiting_gate).
    current_gate_dep = read_fm_field(fm_text, "gate_dependency")
    if current_gate_dep is not None:
        fm_text = _retire_gate_dependency(fm_text)
        changes.append(f'gate_dependency: retired to blocking_notes (was "{current_gate_dep}")')

    if not current_shipped_in and notes:
        # C4b: gate the write through the same shape guard schema.js
        # enforces at validate-time -- refuse (do not write) a
        # non-conforming value instead of inserting free-text notes
        # straight into a field the rest of the pipeline treats as a
        # validated SHA/token.
        is_no_commit_token = bool(_NO_COMMIT_TOKEN_RE.fullmatch(notes))
        is_hex = bool(_SHA_HEX_RE.fullmatch(notes))
        if is_no_commit_token or is_hex:
            fm_text = insert_fm_field(
                fm_text, "shipped_in", notes, "claimed_at", numeric_quoting=True
            )
            kind = "no-commit" if is_no_commit_token else "ship-commit"
            fm_text = insert_fm_field(fm_text, "shipped_in_kind", kind, "shipped_in")
            truncated = notes[:60] + ("…" if len(notes) > 60 else "")
            changes.append(f"shipped_in: + {truncated}")
            changes.append(f"shipped_in_kind: + {kind}")
        else:
            truncated = notes[:60] + ("…" if len(notes) > 60 else "")
            changes.append(
                f'shipped_in: refused — marker notes "{truncated}" do not match a '
                "commit SHA or the substantively-shipped-no-commit:<YYYY-MM-DD> token; "
                "not written"
            )

    if not changes:
        return None

    fm_text_normalized = fm_text if fm_text.endswith("\n") else fm_text + "\n"
    rebuilt = f"{split.preamble or ''}---\n{fm_text_normalized}---{split.body_with_leading_newline}"
    return {"rebuilt": rebuilt, "changes": changes}


def walk_dir(abs_dir: str) -> List[str]:
    """List candidate `.md` files under abs_dir: top-level flat files plus one
    level into month-subdirs (e.g. archive/handoffs/YYYY-MM/*.md, added by the
    2026-06-18 month-foldering migration). state/handoffs and the
    plan/decision/review dirs are flat, so the subdir descent is a harmless
    no-op there. Mirrors the node oracle's walkDir exactly, including its
    stat-error-skips-the-entry behavior and its lack of a try/except around
    the inner (one-level-deep) listdir call.
    """
    if not os.path.exists(abs_dir):
        return []
    if not os.path.isdir(abs_dir):
        return []
    out: List[str] = []
    for f in os.listdir(abs_dir):
        full = os.path.join(abs_dir, f)
        try:
            st = os.stat(full)
        except OSError:
            print(f"skip: walk_dir: st = os.stat(full) failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        if stat_module.S_ISDIR(st.st_mode):
            for sub in os.listdir(full):
                if sub.endswith(".md"):
                    out.append(os.path.join(full, sub))
        elif f.endswith(".md"):
            out.append(full)
    return out


def get_tracked_files(abs_dir: str, root: str) -> Optional[Set[str]]:
    """Return the set of git-tracked file paths (absolute) in a given
    directory. Returns None (no filter) when git is unavailable, since
    detect_root() already fell back to cwd in that case -- mirrors the node
    oracle's getTrackedFiles exactly, including the Patrik F5 gate this
    filter enforces (untracked drafts with a consumed-marker stub are not
    silently rewritten).
    """
    try:
        rel_dir = os.path.relpath(abs_dir, root).replace("\\", "/")
        result = subprocess.run(
            ["git", "ls-files", rel_dir],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            cwd=root,
            check=True,
            **_CREATIONFLAGS,
        )
        out = result.stdout.strip()
        if not out:
            return set()
        return {os.path.abspath(os.path.join(root, f)) for f in out.split("\n")}
    except Exception:
        print(f"skip: get_tracked_files: rel_dir = os.path.relpath(abs_dir, root).replace(\"\\\\\", \"/\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None


def main(argv: List[str]) -> int:
    """CLI entry: scan configured record-type directories and flip drifted
    frontmatter. Mirrors the node oracle's main() shape and its
    process.exitCode convention (see module docstring's exit-code table)."""
    opts = parse_args(argv)
    root = detect_root(opts.root)
    results: List[Dict[str, object]] = []
    exit_code = 0

    for type_ in opts.types:
        raw_glob = TYPE_TO_GLOB.get(type_)  # type: ignore[arg-type]
        globs = raw_glob if isinstance(raw_glob, list) else [raw_glob]
        for glob in globs:
            dir_ = os.path.join(root, glob)  # type: ignore[arg-type]
            tracked = get_tracked_files(dir_, root)
            for file in walk_dir(dir_):
                # Patrik F5: skip untracked files silently (tracked is None
                # means git is unavailable, so we process all files as a
                # fallback).
                if tracked is not None and os.path.abspath(file) not in tracked:
                    continue
                try:
                    out = normalize_one(file)
                except Exception as err:
                    # Patrik F1: block-scalar field detected; fail loud with
                    # filename, but keep scanning remaining files.
                    rel = os.path.relpath(file, root).replace("\\", "/")
                    sys.stderr.write(f"ERROR {rel}: {err}\n")
                    exit_code = 1
                    continue
                if out is None:
                    continue
                rel = os.path.relpath(file, root).replace("\\", "/")
                results.append({"file": rel, "changes": out["changes"]})
                if not opts.dry_run:
                    with open(file, "w", encoding="utf-8", newline="") as f:
                        f.write(out["rebuilt"])  # type: ignore[arg-type]
                    # DR-276: declared AFTER the write lands, never before —
                    # the contract is a report of what was ACTUALLY written,
                    # not of an intended surface.
                    declare_write(file)

    if not results:
        sys.stderr.write("No drift between body markers and frontmatter.\n")
        return exit_code

    verb = "Would update" if opts.dry_run else "Updated"
    for r in results:
        sys.stdout.write(f"{verb} {r['file']}\n")
        for c in r["changes"]:  # type: ignore[union-attr]
            sys.stdout.write(f"  {c}\n")
    sys.stdout.write(f"\n{verb.lower()} {len(results)} file(s).\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
