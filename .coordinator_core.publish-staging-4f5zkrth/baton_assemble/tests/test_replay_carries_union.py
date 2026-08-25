"""coordinator_core.baton_assemble.tests.test_replay_carries_union

Plan: docs/plans/2026-08-19-unified-baton-inherits-every-parents-material.md,
C3 ("the replay path carries the union too").

`_build_directives`'s d1 is `already_satisfied` (skipped) whenever d1's `--out`
target already exists on disk -- a resumed successor
(`_resume_recorded_successor_path`) or an adopted one
(`_adopt_prior_attempt_scaffold_path`). `contract/apply_base.py ::
execute_directives` skips an `already_satisfied` directive without dispatching
its handler, so d1 never runs on that path and the `--deliverable-ids`/
`--plan-ids` flags C2 (`test_deliverable_ids_union_carry.py`) threads into its
argv are never passed -- this run's resolved union is silently discarded. This
module asserts the fix: `d1b`, an idempotent, frontmatter-only stamp of the
two keys onto the already-scaffolded successor, emitted between d1 and d2 and
named in d2's `depends_on`.

Run: python3 -m pytest
coordinator_core/baton_assemble/tests/test_replay_carries_union.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.test_baton_assemble import _init_repo, _write_artifact

# `_dispatch_baton_stamp_carried_ids` routes through `locked_rmw`, which
# shells out to real git (`git_common_dir`) to locate its lock sidecar --
# needs a real repo, not merely a directory. Runs at cadence gates, not
# per-commit, matching this package's other real-repo suites (e.g.
# `test_deliverable_ids_union_carry.py`).
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write_predecessor(
    root: Path,
    rel: str,
    deliverable_id: str,
    plan_id: str,
    *,
    handoff_id: str | None = None,
    extra_lines: list[str] | None = None,
) -> Path:
    lines = [f"deliverable_id: {deliverable_id}", f"origin_plan_id: {plan_id}"]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    if extra_lines:
        lines.extend(extra_lines)
    return _write_artifact(root / rel, lines)


def test_replay_stamps_the_resolved_union_onto_the_existing_successor(tmp_path):
    """AC1/AC2 on the replay path -- the path the reported incident actually
    took. A prior attempt of this same run got as far as d6: it scaffolded a
    successor (bare -- never stamped with the plural carriers) and recorded
    it on the primary predecessor (`deployment_state: continued` +
    `continued_into`), which is the ONLY way `_resume_recorded_successor_path`
    resumes it (DR-242 -- never the successor's own `predecessor:` pointer).
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    recorded_successor = "state/handoffs/2026-07-22-prior-attempt-successor.md"
    primary = _write_predecessor(
        repo,
        "state/handoffs/2026-07-21-second-alpha.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-1a2b46",
        extra_lines=[
            "claimed_at: 2026-07-20T10:00:00Z",
            "claimed_by: test-session",
            "deployment_state: continued",
            f"continued_into: {recorded_successor}",
        ],
    )
    extra = _write_predecessor(
        repo, "state/handoffs/2026-07-20-first-alpha.md", "DEL-EXTRA", "pln-extra-bbb222"
    )
    _write_artifact(repo / recorded_successor, ["kind: session-handoff"])

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        repo,
        additional_predecessor_paths=[str(extra)],
    )
    assert lineage["output_path"] == recorded_successor, "sanity: resumed onto the recorded path"
    assert lineage["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA"]
    assert lineage["plan_ids"] == ["pln-primary-aaa111", "pln-extra-bbb222"]

    directives = ba._build_directives("handoff", lineage, root=repo)
    d1 = next(d for d in directives if d["id"] == "d1")
    assert d1["already_satisfied"] is True, "sanity: this is the replay path"

    d1b = next((d for d in directives if d["id"] == "d1b"), None)
    assert d1b is not None, "the union-carry directive must be emitted on the replay path"
    assert d1b["cli"] == "baton-stamp-carried-ids"
    assert d1b["depends_on"] == ["d1"]
    assert d1b["already_satisfied"] is False

    d2 = next(d for d in directives if d["id"] == "d2")
    assert d2["depends_on"] == ["d1", "d1b"], "d2 must not lint ahead of the stamp"

    before_text = (repo / recorded_successor).read_text(encoding="utf-8")

    dispatch = ba_apply._resolve_cli("baton-stamp-carried-ids")
    result = dispatch(d1b["args"], repo)
    assert result["stamped"] == ["deliverable_ids", "plan_ids"]

    after_text = (repo / recorded_successor).read_text(encoding="utf-8")
    split_before = split_frontmatter(before_text)
    split_after = split_frontmatter(after_text)
    assert split_before.body_with_leading_newline == split_after.body_with_leading_newline, "body-only mutation, byte-unchanged"

    assert "deliverable_ids: \n" not in after_text, "no trailing space after the key name"
    assert "plan_ids: \n" not in after_text, "no trailing space after the key name"

    fm_dict = yaml.safe_load(split_after.fm_text)
    assert fm_dict["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA"]
    assert fm_dict["plan_ids"] == ["pln-primary-aaa111", "pln-extra-bbb222"]

    # A second application must write nothing -- read-back-equal short-circuits.
    second = dispatch(d1b["args"], repo)
    assert second["stamped"] == []
    assert (repo / recorded_successor).read_text(encoding="utf-8") == after_text


def test_clean_path_still_carries_the_union_via_d1(tmp_path):
    """Positive control -- a fixture that would pass universally (e.g. a
    dispatch table drifted so `d1b` always no-ops) cannot read as a pass here.
    On a CLEAN mint (no resumed successor) d1 fires normally and C2's own
    `--deliverable-ids`/`--plan-ids` flags land directly in its argv; no
    `d1b` directive is emitted at all."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    primary = _write_predecessor(
        repo,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-1a2b46",
    )
    extra = _write_predecessor(
        repo, "state/handoffs/extra.md", "DEL-EXTRA", "pln-extra-bbb222"
    )

    lineage = ba.resolve_lineage(
        "handoff", str(primary), repo, additional_predecessor_paths=[str(extra)]
    )
    directives = ba._build_directives("handoff", lineage, root=repo)

    d1 = next(d for d in directives if d["id"] == "d1")
    assert d1["already_satisfied"] is False
    assert "--deliverable-ids=DEL-PRIMARY" in d1["args"]
    assert "--deliverable-ids=DEL-EXTRA" in d1["args"]
    assert "--plan-ids=pln-primary-aaa111" in d1["args"]
    assert "--plan-ids=pln-extra-bbb222" in d1["args"]

    assert all(d["id"] != "d1b" for d in directives), "a clean run emits no d1b directive"

    d2 = next(d for d in directives if d["id"] == "d2")
    assert d2["depends_on"] == ["d1"], "d2's depends_on is unchanged on the clean path"


def test_no_union_to_carry_emits_no_d1b_even_on_replay(tmp_path):
    """A replay with nothing to carry (single predecessor, both keys None)
    must not emit an empty-handed `d1b` -- matching the optional-array
    convention `deliverable_ids`/`plan_ids` already follow."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    recorded_successor = "state/handoffs/2026-07-22-prior-attempt-successor.md"
    primary = _write_predecessor(
        repo,
        "state/handoffs/2026-07-21-second-alpha.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-1a2b46",
        extra_lines=[
            "claimed_at: 2026-07-20T10:00:00Z",
            "claimed_by: test-session",
            "deployment_state: continued",
            f"continued_into: {recorded_successor}",
        ],
    )
    _write_artifact(repo / recorded_successor, ["kind: session-handoff"])

    lineage = ba.resolve_lineage("handoff", str(primary), repo)
    assert lineage["output_path"] == recorded_successor
    assert lineage.get("deliverable_ids") is None
    assert lineage.get("plan_ids") is None

    directives = ba._build_directives("handoff", lineage, root=repo)
    d1 = next(d for d in directives if d["id"] == "d1")
    assert d1["already_satisfied"] is True

    assert all(d["id"] != "d1b" for d in directives)
    d2 = next(d for d in directives if d["id"] == "d2")
    assert d2["depends_on"] == ["d1"]
