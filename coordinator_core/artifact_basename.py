"""
coordinator_core.artifact_basename — shared basename-fallback candidate generator.

Purpose: every markdown-artifact resolver in this repo (`pickup_assemble`'s
`_search_dirs_for_basename`, `ops.resolve_swept_baton._find_first_match`,
`ops.handoff_stamp._resolve_continued_into`) needs the same answer to "what
basename forms should I search for", because a caller-supplied slug is
routinely extensionless (bare handoff/plan slugs are the natural thing to
type) while every artifact class these resolvers serve is stored as
`<slug>.md` on disk. Before this module existed, the 3-line idiom that
answers that question was hand-copied into four call sites across three
modules — and the fourth copy (the not-found error's `tried_basenames`
re-derivation in `pickup_assemble.resolve_artifact`) drifted from the other
three and got it subtly wrong. This module exists so there is exactly one
place that answers "what basename forms" and the four call sites CALL it
rather than re-deriving it.

Negative-spec: this module is candidate-LIST generation only — it does not
walk a filesystem. `pickup_assemble._search_dirs_for_basename`'s own
docstring reserves the directory walk to itself ("do NOT add a second copy
of this loop"); that mandate is unaffected by this module, which sits
strictly upstream of any walk and is never a second walker.

Spec backlink: state/subagent-share/7facc798-5f06-4b23-b1e6-8acf6f20873c/
    coordinatorcode-reviewer-73b4d50f.md, Finding 5 (extraction) and
    Finding 3 (the `.md`-suffix guard fix folded in below).
"""

from __future__ import annotations


def md_fallback_candidates(basename: str) -> list[str]:
    """Return the basename forms to search for a markdown artifact.

    Always includes `basename` itself. When `basename` does not already end
    in `.md`, also includes `basename + ".md"` — the bare-slug fallback
    (2026-07-28 defect fix): every artifact class these resolvers serve
    (handoffs, memos, plans) is stored as `<slug>.md` on disk, so a literal
    search for a bare slug alone never matches the file that plainly exists.

    Deliberately tests `basename.endswith(".md")` rather than
    `Path(basename).suffix` — `Path(...).suffix` is truthy for ANY trailing
    dotted component, not just a real file extension, so a slug carrying an
    internal literal dot (e.g. `foo-v1.2-fix`) would have `.suffix ==
    ".2-fix"` under that check and silently skip the `.md` fallback even
    though `foo-v1.2-fix.md` exists on disk — reproducing the exact defect
    the 2026-07-28 fix closed. Every artifact class these resolvers serve is
    markdown-only, so `.endswith(".md")` is both correct and sufficient.
    """
    candidates = [basename]
    if not basename.endswith(".md"):
        candidates.append(basename + ".md")
    return candidates
