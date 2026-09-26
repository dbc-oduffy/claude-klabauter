"""
coordinator_core.ops.normalize_claimed_frontmatter

Purpose: native Python port of DoE-claude
coordinator/bin/normalize-consumed-frontmatter.js -- flips frontmatter to
match `<!-- consumed: YYYY-MM-DD [notes] -->` body markers. Belt-and-suspenders
companion to the claude-klabauter port of query-records.js: query-records normalizes
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
from typing import Dict, List, Optional, Set, Tuple

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

# of TYPE_TO_GLOB's directories, not a fixed artifact list.
MUTATES = [
    "state/handoffs/*.md",
    "archive/handoffs/**/*.md",
    "docs/plans/*.md",
    "docs/decisions/*.md",
    "state/reviews/*.md",
]


# point's -- `coordinator_core.shipped_in_tokens._SHA_HEX_RE` /
# `_NO_COMMIT_TOKEN_RE` (the same shape `stamp_shipped_in` validates a `sha`
# substantively-shipped-no-commit:<YYYY-MM-DD> token. Whole-value match via

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
    if specified:
        return os.path.abspath(specified)
    toplevel = _show_toplevel()
    return toplevel if toplevel else os.getcwd()


def normalize_one(file_path: str) -> Optional[Dict[str, object]]:
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

    current_gate_dep = read_fm_field(fm_text, "gate_dependency")
    if current_gate_dep is not None:
        fm_text = _retire_gate_dependency(fm_text)
        changes.append(f'gate_dependency: retired to blocking_notes (was "{current_gate_dep}")')

    if not current_shipped_in and notes:
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


def get_tracked_files_batch(dirs: List[str], root: str) -> Dict[str, Optional[Set[str]]]:
    """Batch counterpart to get_tracked_files: ONE `git ls-files` call across
    every directory pathspec, partitioned client-side by directory prefix --
    loop-invariant fix for the per-glob spawn `main()` used to issue (one
    `git ls-files <dir>` per TYPE_TO_GLOB directory, 5 by default). `git
    ls-files` natively accepts multiple pathspecs in one invocation, so this
    collapses N per-directory spawns to 1.

    Returns a dict keyed by the exact `dirs` entries. A directory's value is
    None only when the single git call failed entirely (mirrors
    get_tracked_files' None == "git unavailable, process all files"
    contract) -- there is no per-directory partial failure since all
    directories share the one subprocess call.
    """
    if not dirs:
        return {}
    rel_dirs: List[str] = []
    rel_to_abs: Dict[str, str] = {}
    for d in dirs:
        rel = os.path.relpath(d, root).replace("\\", "/")
        rel_dirs.append(rel)
        rel_to_abs[rel] = d
    try:
        result = subprocess.run(
            ["git", "ls-files", *rel_dirs],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            cwd=root,
            check=True,
            **_CREATIONFLAGS,
        )
    except Exception:
        print(
            f"skip: get_tracked_files_batch: git ls-files across {len(rel_dirs)} "
            f"dir(s) failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return {d: None for d in dirs}

    per_dir: Dict[str, Set[str]] = {d: set() for d in dirs}
    out = result.stdout.strip()
    if out:
        for f in out.split("\n"):
            for rel in rel_dirs:
                if f == rel or f.startswith(rel + "/"):
                    per_dir[rel_to_abs[rel]].add(os.path.abspath(os.path.join(root, f)))
    return per_dir  # type: ignore[return-value]


def main(argv: List[str]) -> int:
    opts = parse_args(argv)
    root = detect_root(opts.root)
    results: List[Dict[str, object]] = []
    exit_code = 0

    # of one call per TYPE_TO_GLOB directory.
    type_dirs: List[Tuple[str, str]] = []
    for type_ in opts.types:
        raw_glob = TYPE_TO_GLOB.get(type_)  # type: ignore[arg-type]
        globs = raw_glob if isinstance(raw_glob, list) else [raw_glob]
        for glob in globs:
            type_dirs.append((type_, os.path.join(root, glob)))  # type: ignore[arg-type]

    tracked_by_dir = get_tracked_files_batch([d for _, d in type_dirs], root)

    for _type_, dir_ in type_dirs:
        tracked = tracked_by_dir.get(dir_)
        for file in walk_dir(dir_):
            if tracked is not None and os.path.abspath(file) not in tracked:
                continue
            try:
                out = normalize_one(file)
            except Exception as err:
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
                # the contract is a report of what was ACTUALLY written,
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
