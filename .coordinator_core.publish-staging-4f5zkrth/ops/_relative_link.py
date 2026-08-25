"""
coordinator_core.ops._relative_link — shared markdown-link relativization.

Purpose: a generator whose output file is NOT at the repo root, but whose
link targets are computed/authored as repo-root-relative paths, must
relativize each target against its OWN output location before writing it —
never against the repo root, and never by concatenating a fixed prefix onto
whatever the target already looks like. This module is the one seam every
such generator routes through, so the relativization rule is defined once.

Spec backlink: cross-repo/inbox/2026-08-06-market-intelligence-em-update-docs-distill-ceremony-defects.md
    (Defect 1: docs/plans/INDEX.md; Defect 2: docs/exec-summary.md — same
    generating-rule shape, named once here instead of twice.)

Negative-spec:
    - Does NOT resolve the target against the filesystem (no ``os.path.exists``
      check) — a dangling target relativizes the same as a live one; that is
      a different concern (link-validity checking), not this module's job.
    - Does NOT special-case http(s)/mailto/anchor/absolute targets — callers
      that mix those in (e.g. inline markdown link rewriting) must skip this
      helper for those forms themselves; this module only ever sees targets
      it should relativize.
"""

from __future__ import annotations

import os


def normalize_repo_relative(target: str) -> str:
    """Strip any leading ``./``/``../`` segments from `target`, including a
    LONE leading ``./`` on its own (not just a ``../`` chain).

    A target harvested verbatim from another file's own content (e.g. a
    markdown link copied out of an archived, nested source document) may
    already carry a relative prefix computed for THAT file's location, not
    for the eventual output file's location. Repo-root-relative targets, by
    contract, never legitimately start with ``../`` OR a bare ``./`` —
    nothing referenced by this corpus lives above the repo root, and a
    repo-root-relative path never needs a same-directory marker either — so
    any such prefix present on input is noise from a mismatched source
    depth, not a signal to leave the target untouched. Stripping it here,
    before relativizing, makes the output correct regardless of what depth
    the target happened to arrive from: a bare target and a stray-prefixed
    target (``../``-chain OR lone ``./``) that resolve to the same file
    produce the identical, correct output.
    """
    parts = target.split("/")
    while parts and parts[0] in (".", ".."):
        parts.pop(0)
    return "/".join(parts)


def relative_markdown_target(target: str, out_path: str) -> str:
    """Compute the correct link target for `target` when written into a
    markdown file at `out_path`.

    Both `target` and `out_path` are repo-root-relative, forward-slash
    paths. `target` is normalized first (see `normalize_repo_relative`) so a
    target that already carries a stray relative prefix from an unrelated
    source location does not get double-relativized. The result is computed
    with ``os.path.relpath`` — not hardcoded ``../`` concatenation — so it
    stays correct if `out_path`'s own location ever moves, and forward
    slashes are forced so the emitted link is identical on Windows and
    POSIX.
    """
    normalized = normalize_repo_relative(target)
    rel = os.path.relpath(normalized, start=os.path.dirname(out_path))
    return rel.replace("\\", "/")
