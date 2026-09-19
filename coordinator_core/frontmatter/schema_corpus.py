"""coordinator_core.frontmatter.schema_corpus — is this schemas DIRECTORY a
published subset, i.e. one no gate may draw a verdict from?

Purpose: the frontmatter schema corpus does not live in this repo. It is
resolved — `coordinator_core.data_root.data_root("schemas")`, which lands on a
DoE-claude content root, either layout (see
`coordinator_core._content_root_primitive.content_root_for`). Two of those
layouts hold DIFFERENT-SIZED corpora, both correct:

  <doe_root>/coordinator/schemas   the private AUTHORING tree — the whole set
  <doe_root>/schemas (flat)        the published MIRROR — the published subset

Percolation is one-way and the OSS editorial principle keeps DoE-internal
schemas out of the naked publish, so a mirror root legitimately carries a
handful of schemas where the authoring tree carries dozens. A consumer
container with no authoring tree mounted resolves the mirror on both keys and
gets the small set. That is not a fault.

The FAULT is that nothing downstream could tell the small-because-published
case from the small-because-broken case, because both present as a directory
that exists, parses, and yields a registry. Measured shape of that gap:
`coordinator_core/ops/verify_schema_registry_sync.py` derives its recognised-type
set from the SAME directory it then checks against that set, so on a mirror root
it printed `OK - all 2 schema applies_to types are recognised` and exited 0 —
a green verdict from a gate that had just skipped 61 of the 63 types it exists
to protect.

This module is the discriminator, and it answers off DISK EVIDENCE, never off a
count threshold. A count cannot separate the two cases (a published subset and a
half-deleted authoring dir are the same number) and a threshold would need
re-tuning every time the publish set moves. The evidence is the markers the
fleet already uses to tell an authoring checkout from a published one:
`.coordinator-dev-repo` (the fleet-wide dev-vs-OSS discriminant, at the REPO
root, never under `coordinator/`) and `FLAT_CONTENT_ROOT_MARKER`
(`.claude-plugin/plugin.json`, what makes a flat clone a content root at all).

Negative-spec:
  - Answers ONE bit, because one bit is what the live requirement is: a
    published subset, or not. The not-case deliberately does NOT distinguish
    an authoring tree from an unmarked directory from an absent one — all
    three mean "this module has no objection", and every caller in tree treats
    them identically. A caller that genuinely needs those apart is the event
    that earns the wider return type; until then a four-way classification
    would be the module's own framing, not a stated requirement.
  - Does NOT validate, load, or parse a single schema, and does NOT import
    `schema_validate`. Classification is about the DIRECTORY's provenance; it
    must stay usable by a caller deciding whether to bother loading at all,
    and `schema_validate.py` is a hard external dependency another repo imports
    by file path — nothing here may add to its import surface.
  - Does NOT raise, ever. An absent directory, an unreadable one, and a
    permission error all fold into a returned answer, same contract as
    `schema_drift_watch`.
  - Does NOT decide what a caller should DO about a partial corpus. It hands
    back an operator-readable reason; standing a gate down is the gate's call.
  - Is NOT a second spelling of `content_root_for`. It CALLS that primitive and
    reads two markers around it; the layout join itself stays in the one place
    it belongs.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core._content_root_primitive import FLAT_CONTENT_ROOT_MARKER

#: Repo-root sentinel marking a DoE-claude AUTHORING checkout. DoE-claude's
#: own `CLAUDE.md` names it the dev-vs-OSS discriminant fleet-wide and pins it
#: to the REPO root, one level above a private layout's `coordinator/` content
#: root; `scripts/cloud_setup.py :: locate_doe_authoring_tree` detects a mounted
#: authoring tree by this same name. Spelled here rather than imported because
#: that script is an installer, not an importable engine module.
DEV_REPO_SENTINEL = ".coordinator-dev-repo"


def _has_flat_content_root_marker(base: Path) -> bool:
    try:
        return base.joinpath(*FLAT_CONTENT_ROOT_MARKER).is_file()
    except OSError:
        return False


def _has_dev_repo_sentinel(base: Path) -> bool:
    try:
        return (base / DEV_REPO_SENTINEL).exists()
    except OSError:
        return False


def _count_schema_files(schemas_dir: Path) -> int:
    """Plain disk tally of the two loadable dialects — reported so a caller can
    put a number in a message, never used to DECIDE anything."""
    try:
        return sum(
            1
            for entry in schemas_dir.iterdir()
            if entry.is_file() and (entry.name.endswith(".yaml") or entry.name.endswith(".schema.json"))
        )
    except OSError:
        return 0


def published_subset_reason(schemas_dir: str | Path) -> str | None:
    """Return an operator-readable reason when `schemas_dir` is a PUBLISHED
    SUBSET — a corpus whose shortfall is by design and from which no count may
    be reported as covering the authoring set. Return None otherwise.

    Resolution, in order — first match answers:

      1. `<schemas_dir>/..` carries `.coordinator-dev-repo`  -> None
         (flat authoring checkout: repo root IS the content root.)
      2. `<schemas_dir>/../..` carries `.coordinator-dev-repo` -> None
         (private layout: `<repo>/coordinator/schemas`, sentinel at `<repo>`.)
      3. `<schemas_dir>/..` carries `.claude-plugin/plugin.json` and no
         sentinel above -> a reason string.
      4. otherwise -> None.

    The sentinel is probed BEFORE the published marker because an authoring
    checkout carries BOTH (it is the tree the mirror is published FROM), and
    authoring is the stronger claim. An absent directory answers None: there is
    no published-subset claim to make about a directory that is not there.
    """
    directory = Path(schemas_dir)
    if not directory.is_dir():
        return None

    # Review: code-reviewer — resolve before deriving parent/grandparent so a
    # symlinked or relative schemas_dir walks the real target's markers, not
    # the link's own containing directory or the process CWD.
    directory = directory.resolve()

    parent = directory.parent
    if _has_dev_repo_sentinel(parent) or _has_dev_repo_sentinel(parent.parent):
        return None

    if not _has_flat_content_root_marker(parent):
        return None

    count = _count_schema_files(directory)
    return (
        f"{parent} is a published content root with no {DEV_REPO_SENTINEL}: "
        f"{count} schema file(s) is the published subset, not the authoring set — "
        "percolation is one-way and DoE-internal schemas are not published"
    )
