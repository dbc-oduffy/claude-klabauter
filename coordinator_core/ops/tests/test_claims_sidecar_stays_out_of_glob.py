"""
Regression pin for DoE tripwire CLAIMS-SIDECAR-STAYS-OUT-OF-THE-CLAIMS-GLOB.

Spec backlink:
  archive/specs/2026-08/2026-08-06-claims-emit-writer-atomic-pair.md — chunk C4, AC11.
  (Review: coordinator:code-reviewer — plan was archived by
  fleet.archive_completed_plans before this diff's slice landed; the
  docs/plans/ path 404s.)

Why this matters: records_query._collect_research_claim_records reads every
file matching _TYPE_TO_GLOB['research-claim'] and does `isinstance(claims,
list)` on its parsed JSON to decide whether the file holds claim records. The
sidecar (`<stem>.claims.meta.json`) is a JSON OBJECT, not an array. If the
sidecar's filename ever fell inside that glob, the isinstance check would
silently classify it as an empty claims corpus rather than raising, so this
test asserts the sidecar name stays outside the glob's match set while the
claims file's name stays inside it — reading the glob string itself from
records_query rather than hardcoding it, so a future change to the glob is
what this test would actually catch.
"""
from __future__ import annotations

import fnmatch

from coordinator_core.ops.records_query import _TYPE_TO_GLOB


def test_claims_sidecar_does_not_match_research_claim_glob_but_claims_file_does():
    glob = _TYPE_TO_GLOB["research-claim"]
    glob_basename = glob.rsplit("/", 1)[-1]

    claims_name = "some-run.claims.json"
    sidecar_name = "some-run.claims.meta.json"

    assert fnmatch.fnmatch(claims_name, glob_basename)
    assert not fnmatch.fnmatch(sidecar_name, glob_basename)
