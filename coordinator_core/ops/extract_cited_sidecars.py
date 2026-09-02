"""
coordinator_core.ops.extract_cited_sidecars — C4 of
pln-state-keeps-the-work-not-the-machinery-*: extract, BEFORE the history
rewrite, the record of which `state/subagent-share/<uuid>/` session
directories are cited by durable records outside `state/` (overwhelmingly
`archive/bug-backlog/*.yaml`, which outlive every session directory here).

Purpose: after the coming history rewrite, a UUID-shaped citation to a
session directory that no longer exists cannot be rewritten to anything that
resolves -- the target stops existing anywhere, including in git history.
So the citation content is preserved BEFORE the rewrite: this op walks the
repo once, finds every citing artifact, and writes one audit record mapping
each cited-and-still-on-disk UUID to (a) the artifacts that cite it and (b)
the sidecar filenames that exist under it right now.

A second, larger citation class rides the same rewrite and the same walk
(eng-director review on the C4 stub): any 40-hex string anywhere in a
tracked file is shape-identical to a git commit sha, an order of magnitude
more numerous than the UUID class. `filter-repo`'s own commit-map is the
free, exact translation for this class after the rewrite (a job for a later
chunk, not this one) -- this op's job is only to enumerate the raw
(file, cited-sha-shaped-token) set now, while the pre-rewrite shas still
resolve, and write it to
`state/audits/2026-09-02-cited-commit-shas.md`. Not every 40-hex token is
actually a git commit sha (a hash of something else, test fixture data, ...)
-- this op does not attempt to disambiguate; it records the raw set exactly
as the C4 stub body specifies ("record the raw set here").

Both extractions share ONE `git ls-files` process spawn and ONE tree walk
(reading each candidate file's content once, scanning it for both patterns)
-- never a per-UUID or per-file subprocess. At 678 candidate UUIDs alone,
a per-UUID subprocess would be 678 spawns against the repo's 500ms
brightline (`docs/decisions/DR-344-the-brightline-process-budget-for-claude-klabauter.md`);
this op holds to the "one git ls-files pass and one walk" budget the C4 stub
names explicitly.

Negative-spec (RAG-bait):
    - This op does NOT resolve, rewrite, or repoint any citation. It is a
      pure extraction/audit step: read-only over the citing corpus, single
      controlled write to the two named audit files. A citation found to
      depend on sidecar CONTENT (not just existence) is a finding for the
      PM, surfaced in the audit body -- never something this op tries to
      fix or paper over.
    - `.git/` is excluded structurally (git ls-files never lists it).
      `state/` (including `state/subagent-share/` and
      `state/audits/` themselves) is excluded from the CANDIDATE walk by
      the C4 stub's own instruction ("Walk the tree excluding `.git/` and
      `state/`") -- a citation living inside `state/` (e.g. a sizing record
      or another sidecar) is deliberately NOT captured by this pass. This
      is a stub-level scoping decision, not an oversight: the stub's
      concern is citations from durable records OUTSIDE `state/`, since
      state/ itself is the thing about to be rewritten/pruned.
    - The on-disk intersection (§ `_on_disk_session_ids`) is computed via
      one `os.listdir` on `state/subagent-share/`, not a subprocess per
      candidate UUID and not a `git ls-files` scan (session directories are
      overwhelmingly untracked/gitignored churn, not something `git
      ls-files` would enumerate completely).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session.machinery_paths import machinery_root
from coordinator_core.win_portability import leaf_spawn_creationflags

_LOG_PREFIX = "extract-cited-sidecars"

_UUID_CITATION_RE = re.compile(
    r"state/subagent-share/([0-9a-fA-F-]{8,36})"
)
_SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")

_UUID_AUDIT_PATH = "state/audits/2026-09-02-cited-subagent-share-sidecars.md"
_SHA_AUDIT_PATH = "state/audits/2026-09-02-cited-commit-shas.md"


def _resolve_root(root: Optional[str]) -> str:
    if root:
        return root
    found = show_toplevel(cwd=os.getcwd())
    return found or os.getcwd()


def _list_candidate_files(root: str) -> Optional[List[str]]:
    """Root-relative, forward-slash, sorted paths for every file `git
    ls-files` reports (tracked + untracked-but-not-.gitignore'd), excluding
    anything rooted at `state/`. `None` if `root` is not a git worktree /
    git is unavailable -- the caller falls back to a full filesystem walk.

    ONE subprocess spawn total, regardless of repo size or candidate-UUID
    count (see module docstring's spawn-budget paragraph)."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, timeout=60,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    raw = proc.stdout.decode("utf-8", errors="replace")
    paths = [p for p in raw.split("\0") if p]
    out = []
    for p in paths:
        rel = p.replace(os.sep, "/")
        if rel.startswith("state/"):
            continue
        if not os.path.isfile(os.path.join(root, p)):
            continue
        out.append(rel)
    return sorted(out)


def _walk_fallback(root: str) -> List[str]:
    out: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if d != ".git"
        ]
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        if rel_dir == ".":
            rel_dir = ""
        if rel_dir == "state" or rel_dir.startswith("state/"):
            dirnames[:] = []
            continue
        for fname in filenames:
            rel = f"{rel_dir}/{fname}" if rel_dir else fname
            out.append(rel)
    return sorted(out)


def _list_candidates(root: str) -> List[str]:
    tracked = _list_candidate_files(root)
    if tracked is not None:
        return tracked
    return _walk_fallback(root)


#: Every `subagent-share` root a CITATION may legitimately resolve against,
#: current convention first.
#:
#: Both, not one. This module answers "does this cited sidecar still exist",
#: and the corpus it is asked about spans the relocation: sidecars written
#: before the move are under `state/`, sidecars written after are under the
#: machinery root, and a citation to either was valid when it was written.
#: Resolving against only the new root reports the entire pre-move corpus as
#: dangling -- caught by `tests/test_no_dangling_machinery_citations.py`,
#: whose whole job is to notice a citation that used to work and stopped.
#: Drop the legacy entry only when the old directories are actually gone.
def _share_roots(root: str) -> List[str]:
    return [
        os.path.join(machinery_root(root), "subagent-share"),
        os.path.join(root, "state", "subagent-share"),
    ]


def _on_disk_session_ids(root: str) -> set:
    found = set()
    for share_root in _share_roots(root):
        if not os.path.isdir(share_root):
            continue
        found.update(
            name for name in os.listdir(share_root)
            if os.path.isdir(os.path.join(share_root, name))
        )
    return found


def _sidecar_filenames(root: str, session_id: str) -> List[str]:
    """Every filename for `session_id`, unioned across both roots.

    A session that straddles the relocation has files under each; returning
    only one root's would report the other's as absent.
    """
    out = set()
    for share_root in _share_roots(root):
        session_dir = os.path.join(share_root, session_id)
        if not os.path.isdir(session_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(session_dir):
            rel_dir = os.path.relpath(dirpath, session_dir).replace(os.sep, "/")
            for fname in filenames:
                out.add(f"{rel_dir}/{fname}" if rel_dir != "." else fname)
    return sorted(out)


def scan(root: str) -> Tuple[Dict[str, List[str]], Dict[str, List[Tuple[str, str]]]]:
    """One walk over `_list_candidates(root)`, reading each file once and
    scanning it for both citation shapes.

    Returns `(uuid_citations, sha_citations)`:
      - `uuid_citations`: cited UUID -> sorted list of citing file paths,
        restricted to UUIDs that also exist on disk under
        `state/subagent-share/` right now (the C4 stub's "intersect against
        the on-disk directory set").
      - `sha_citations`: citing file path -> sorted list of (dedup'd)
        40-hex tokens found in that file (the raw set, unfiltered against
        any commit-map -- see module docstring).
    """
    on_disk = _on_disk_session_ids(root)
    uuid_hits: Dict[str, set] = defaultdict(set)
    sha_hits: Dict[str, set] = defaultdict(set)

    for rel_path in _list_candidates(root):
        full = os.path.join(root, rel_path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue

        for match in _UUID_CITATION_RE.finditer(content):
            uid = match.group(1)
            if uid in on_disk:
                uuid_hits[uid].add(rel_path)

        for match in _SHA_RE.finditer(content):
            sha_hits[rel_path].add(match.group(0))

    uuid_citations = {
        uid: sorted(files) for uid, files in sorted(uuid_hits.items())
    }
    sha_citations = {
        path: sorted(shas) for path, shas in sorted(sha_hits.items())
    }
    return uuid_citations, sha_citations


def _render_uuid_audit(root: str, uuid_citations: Dict[str, List[str]]) -> str:
    lines = [
        "# Cited state/subagent-share/ sidecars — pre-rewrite extraction (C4)",
        "",
        "Purpose: `state/subagent-share/<uuid>/` session directories cited by",
        "durable records outside `state/` (chiefly `archive/bug-backlog/*.yaml`),",
        "captured before the history rewrite makes an unresolvable citation",
        "permanently unresolvable. Generated by",
        "`coordinator_core.ops.extract_cited_sidecars`.",
        "",
        f"Cited-and-on-disk UUID count: {len(uuid_citations)}",
        "",
    ]
    if not uuid_citations:
        lines.append("No citations found.")
        lines.append("")
        return "\n".join(lines)

    for uid, citers in uuid_citations.items():
        lines.append(f"## {uid}")
        lines.append("")
        lines.append("Citing artifacts:")
        for c in citers:
            lines.append(f"- {c}")
        lines.append("")
        lines.append("Sidecar filenames under this session directory:")
        sidecars = _sidecar_filenames(root, uid)
        if sidecars:
            for s in sidecars:
                lines.append(f"- {s}")
        else:
            lines.append("- (none found)")
        lines.append("")
    return "\n".join(lines)


def _render_sha_audit(sha_citations: Dict[str, List[str]]) -> str:
    lines = [
        "# Cited 40-hex-shaped tokens — pre-rewrite extraction (C4)",
        "",
        "Purpose: raw enumeration of every 40-hex-shaped token found in a",
        "tracked/untracked-and-not-ignored file outside `state/`, captured",
        "before the history rewrite. NOT all of these are git commit shas —",
        "no filtering or disambiguation is performed here. C11 cross-references",
        "this set against filter-repo's own commit-map after the rewrite.",
        "Generated by `coordinator_core.ops.extract_cited_sidecars`.",
        "",
        f"Citing file count: {len(sha_citations)}",
        "",
    ]
    if not sha_citations:
        lines.append("No 40-hex-shaped tokens found.")
        lines.append("")
        return "\n".join(lines)

    for path, shas in sha_citations.items():
        lines.append(f"## {path}")
        lines.append("")
        for sha in shas:
            lines.append(f"- {sha}")
        lines.append("")
    return "\n".join(lines)


def run_extraction(root: str) -> Tuple[str, str]:
    """Runs the scan and returns the two rendered audit-file bodies as
    `(uuid_audit_text, sha_audit_text)`. Does not write anything -- `main`
    owns the write step."""
    uuid_citations, sha_citations = scan(root)
    return (
        _render_uuid_audit(root, uuid_citations),
        _render_sha_audit(sha_citations),
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=None, help="Repo root (defaults to git toplevel).")
    args = parser.parse_args(argv)

    root = _resolve_root(args.root)
    uuid_text, sha_text = run_extraction(root)

    uuid_out = os.path.join(root, _UUID_AUDIT_PATH.replace("/", os.sep))
    sha_out = os.path.join(root, _SHA_AUDIT_PATH.replace("/", os.sep))
    os.makedirs(os.path.dirname(uuid_out), exist_ok=True)
    os.makedirs(os.path.dirname(sha_out), exist_ok=True)
    with open(uuid_out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(uuid_text)
    with open(sha_out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sha_text)

    print(f"{_LOG_PREFIX}: wrote {_UUID_AUDIT_PATH}")
    print(f"{_LOG_PREFIX}: wrote {_SHA_AUDIT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
