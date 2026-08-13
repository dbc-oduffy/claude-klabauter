"""
coordinator_core.ops.tests.test_deliverable_cascade_kinds — C6: the test surface
for the sizing kind's second-target-kind cascade seam.

Purpose: this is the AUTHORING home for the AC coverage the plan names as C6's
own — C2/C3/C5 landed the seam, this chunk proves it. Each test maps to exactly
one AC (named in its docstring/name) per the plan's own instruction not to fold
several ACs into one assertion (a broken layer must not hide behind a passing
sibling).

Spec backlink: pln-sizing-objects-join-the-delive-53c06a § C6

Negative-spec: does NOT promote the spike's own throwaway probe script into this
suite (throwaway-probe discipline, per the plan body) — every test here is
written fresh against the shipped seam, not derived from spike scratch code.
Does NOT re-test `_claimant`'s ledger-first resolution
(`test_deliverable_cascade_claim_state.py` already owns that) or the handoff
kind's pre-existing behaviour beyond a single byte-for-byte regression check.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_deliverable_cascade_kinds.py -q
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
import yaml

import coordinator_core.ipc as ipc_mod
import coordinator_core.ops.backfill_deliverable_spine as backfill_mod
import coordinator_core.ops.cascade_backstop_sweep  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.cascade_retract  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.deliverable_cascade as cascade_mod
import coordinator_core.ops.handoff_children  # noqa: F401 — fires @register_op side effect
import coordinator_core.ops.handoff_transition  # noqa: F401 — fires @register_op side effect
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_handler = cascade_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _run(params: dict, repo_root: Path) -> dict:
    return asyncio.run(_handler(params, repo_root=repo_root))


def _sizing_body(
    *,
    status: str = "routed",
    deliverable_id: str = "dlv-test-000000",
    plan: Optional[str] = None,
) -> str:
    """A schema-valid whole-document sizing-object YAML body (1.8.0)."""
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
        f"deliverable_id: {deliverable_id}",
    ]
    if plan is not None:
        lines.append(f"plan: {plan}")
    return "\n".join(lines) + "\n"


def _seed_sizing(
    repo: Path,
    name: str,
    *,
    status: str = "routed",
    deliverable_id: str = "dlv-test-000000",
    plan: Optional[str] = None,
) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _sizing_body(status=status, deliverable_id=deliverable_id, plan=plan),
        encoding="utf-8",
    )
    return path


def _seed_handoff(
    repo: Path,
    name: str,
    *,
    deployment_state: str = "ready_to_fire",
    deliverable_id: str = "dlv-test-000000",
) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"deliverable_id: {deliverable_id}\n"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# AC5 — closed kind set, fail-loud on unknown kind
# ---------------------------------------------------------------------------


def test_ac5_kind_descriptor_raises_on_unknown_kind():
    with pytest.raises(ValueError, match="unknown target kind"):
        cascade_mod._kind_descriptor("bogus-kind")


def test_ac5_handler_raises_not_empty_result_on_unknown_target_kind(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(ValueError, match="unknown target kind"):
        _run(
            {
                "deliverable_id": "dlv-test-000000",
                "source_kind": "plan",
                "source_path": "docs/plans/dummy.md",
                "target_kind": "bogus-kind",
            },
            repo_root=repo / ".git",
        )


# ---------------------------------------------------------------------------
# AC6 — sizing corpus discrimination (the silent-zero-match regression)
# ---------------------------------------------------------------------------


def test_ac6_matching_deliverable_id_yields_nonempty_candidates(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-a.yaml", deliverable_id="dlv-match-000000")

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-match-000000", kind=cascade_mod._SIZING_KIND
    )

    assert len(matches) == 1
    assert scan_incomplete is False
    assert unreadable == []


def test_ac6_nonmatching_deliverable_id_yields_empty_candidates(tmp_path):
    """The discrimination arm — an over-matching reader that returns every
    record must fail this."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-a.yaml", deliverable_id="dlv-match-000000")

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-does-not-exist-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []
    assert scan_incomplete is False
    assert unreadable == []


# ---------------------------------------------------------------------------
# AC6a — unparseable/partially-written sizing record != a clean zero
# ---------------------------------------------------------------------------


def test_ac6a_unreadable_record_reports_scan_incomplete_not_clean_zero(tmp_path):
    """A corpus with only a genuinely-empty match set (AC6's discrimination
    arm) must report scan_incomplete=False/unreadable=[]. A corpus whose only
    record is unparseable must be distinguishable from that — this is exactly
    the pair AC6a exists to keep from collapsing into the same silence."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    bad = repo / "state" / "sizings" / "20260101-bad.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("this: [is, not, valid: yaml\n", encoding="utf-8")

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-test-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []
    assert scan_incomplete is True
    assert len(unreadable) == 1
    assert unreadable[0]["path"] == str(bad)
    assert unreadable[0]["reason"]


def test_ac6a_non_mapping_record_reports_scan_incomplete(tmp_path):
    """A sizing 'record' that parses as valid YAML but not a mapping (e.g. a
    bare scalar/list) must also route into scan_incomplete, not a silent
    drop — `_read_sizing_meta` raises ValueError on this, not just on a
    parse error."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    empty = repo / "state" / "sizings" / "20260101-scalar.yaml"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("just a bare string\n", encoding="utf-8")

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-test-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []
    assert scan_incomplete is True
    assert len(unreadable) == 1


def test_ac6a_clean_empty_corpus_is_distinguishable_from_unreadable(tmp_path):
    """The discrimination this AC names: a genuinely-empty match set (no
    records at all) must NOT set scan_incomplete — the two states this AC
    exists to keep apart."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-test-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []
    assert scan_incomplete is False
    assert unreadable == []


def test_missing_corpus_dir_reports_scan_incomplete_not_clean_zero(tmp_path):
    """2026-08-10 fix: a `base_dir` that does not exist at all (e.g. a
    misresolved worktree_root, or a corpus that has genuinely never been
    created) must NOT read the same as a corpus dir that exists and legitimately
    has zero matches (test_ac6a_clean_empty_corpus_is_distinguishable_from_unreadable
    above, which explicitly mkdir()s the corpus dir first). Regression guard for
    the confident-zero defect: a bad root previously returned scan_incomplete=False
    here, indistinguishable from a real clean scan."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    assert not (repo / "state" / "sizings").exists()

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-test-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []
    assert scan_incomplete is True
    assert unreadable == []


# ---------------------------------------------------------------------------
# AC2 — headline capability: end-to-end via the REGISTERED op
# ---------------------------------------------------------------------------


def test_ac2_end_to_end_sizing_flips_to_shipped_through_registered_op(tmp_path):
    """Routes through `get_op_handler("deliverable.cascade_terminal")` — the
    same lookup the JSON-RPC dispatch path uses — rather than importing
    `_handler` directly, so this proves the REGISTERED op, not a unit-level
    stand-in."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-e2e.yaml", status="routed", deliverable_id="dlv-e2e-000000")
    plan_path = "docs/plans/2026-01-01-e2e-plan.md"

    handler = ipc_mod.get_op_handler("deliverable.cascade_terminal")
    assert handler is not None

    result = asyncio.run(
        handler(
            {
                "deliverable_id": "dlv-e2e-000000",
                "source_kind": "plan",
                "source_path": plan_path,
                "target_kind": "sizing",
            },
            repo_root=repo / ".git",
        )
    )

    assert result["exit_code"] == 0
    assert len(result["advanced"]) == 1
    assert result["advanced"][0]["handoff_path"] == str(sizing)

    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["status"] == "shipped"
    assert doc["plan"] == plan_path

    # The mutation must still validate against the vendored schema.
    errors = cascade_mod._validate_sizing_fm(sizing.read_text(encoding="utf-8"))
    assert errors == []


# ---------------------------------------------------------------------------
# AC3 — double-run no-op at BOTH layers, separately
# ---------------------------------------------------------------------------


def test_ac3_readside_second_run_returns_zero_candidates(tmp_path):
    """Read-side terminal-exclusion, in isolation: once a sizing is shipped,
    a fresh read-side scan for the same deliverable_id must return zero
    candidates — the record no longer advertises as live."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-a.yaml", status="shipped", deliverable_id="dlv-idem-000000")

    matches, _scan_incomplete, _unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-idem-000000", kind=cascade_mod._SIZING_KIND
    )

    assert matches == []


def test_ac3_writeside_floor_is_byte_identical_on_already_terminal_record(tmp_path):
    """Write-side floor, in isolation: `_advance_one_sizing` on an
    already-terminal record must produce BYTE-IDENTICAL file content —
    read-before/read-after equality, not merely an already-advanced flag."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(
        repo, "20260101-a.yaml", status="shipped", deliverable_id="dlv-idem-000001",
        plan="docs/plans/2026-01-01-already-shipped.md",
    )
    before = sizing.read_text(encoding="utf-8")

    advanced, refusal = cascade_mod._advance_one_sizing(
        sizing, "docs/plans/2026-01-01-different-plan.md", repo / ".git",
    )

    after = sizing.read_text(encoding="utf-8")
    assert advanced is False
    assert refusal == cascade_mod._ALREADY_ADVANCED_MARKER
    assert after == before


def test_ac3_writeside_floor_holds_for_a_quoted_on_disk_status(tmp_path):
    """Review: staff-eng — Finding 0: the idempotency-floor comparison must
    read through `read_fm_field_unquoted`, not the raw on-disk bytes — a
    record carrying `status: 'shipped'` (single-quoted) must still be
    recognised as already-terminal and left BYTE-IDENTICAL, not rewritten."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = repo / "state" / "sizings" / "20260101-quoted.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    quoted_body = _sizing_body(
        status="shipped", deliverable_id="dlv-idem-quoted-0",
        plan="docs/plans/2026-01-01-already-shipped.md",
    ).replace("status: shipped", "status: 'shipped'")
    sizing.write_text(quoted_body, encoding="utf-8")
    before = sizing.read_text(encoding="utf-8")

    advanced, refusal = cascade_mod._advance_one_sizing(
        sizing, "docs/plans/2026-01-01-different-plan.md", repo / ".git",
    )

    after = sizing.read_text(encoding="utf-8")
    assert advanced is False
    assert refusal == cascade_mod._ALREADY_ADVANCED_MARKER
    assert after == before


def test_ac3_writeside_floor_crlf_document_has_no_mixed_line_endings(tmp_path):
    """Review: staff-eng — Finding 9: every fixture in this suite is
    LF-authored and every assertion reads through `Path.read_text`
    (universal newlines) or `yaml.safe_load`, both of which normalize CRLF
    away — so Findings 3/5(a) were structurally unfalsifiable here. This
    fixture is written with `newline=""` so the `\\r\\n` bytes land on disk
    verbatim, then asserts the post-mutation bytes carry NO mixed line
    endings (a `\\r` not immediately followed by `\\n`, or a `\\n` not
    preceded by `\\r`) — `_advance_one_sizing` routes through
    `locked_write.locked_rmw`, whose own read leg is a UNIVERSAL-newline
    text read (out of this module's write surface): a CRLF-authored record
    is therefore homogenised to LF end-to-end, never left with a MIX of the
    two, which is the property this assertion pins.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = repo / "state" / "sizings" / "20260101-crlf.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    body = _sizing_body(status="routed", deliverable_id="dlv-crlf-000000")
    crlf_body = body.replace("\n", "\r\n")
    with open(sizing, "w", encoding="utf-8", newline="") as fh:
        fh.write(crlf_body)

    advanced, refusal = cascade_mod._advance_one_sizing(
        sizing, "docs/plans/2026-01-01-crlf-plan.md", repo / ".git",
    )
    assert advanced is True
    assert refusal is None

    with open(sizing, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8")
    # No mixed line endings: every "\r" is immediately followed by "\n" and
    # every "\n" is immediately preceded by "\r", OR none are (pure LF) —
    # never a document carrying both a bare "\n" and a "\r\n" pair.
    has_crlf = "\r\n" in text
    bare_lf = text.replace("\r\n", "").count("\n")
    bare_cr = text.replace("\r\n", "").count("\r")
    assert bare_cr == 0, "stray bare \\r found — mixed line endings"
    if has_crlf:
        assert bare_lf == 0, "mixed CRLF and bare LF in the same document"


def test_ac3_full_cascade_second_run_is_a_clean_noop(tmp_path):
    """Both layers together, through the real entrypoint: run the cascade
    twice against the same live sizing; the second run must find zero
    candidates (exit_code 1, nothing advanced) and must not have touched the
    file a second time."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-a.yaml", status="routed", deliverable_id="dlv-idem-000002")
    plan_path = "docs/plans/2026-01-01-idem-plan.md"
    params = {
        "deliverable_id": "dlv-idem-000002",
        "source_kind": "plan",
        "source_path": plan_path,
        "target_kind": "sizing",
    }

    first = _run(params, repo_root=repo / ".git")
    assert first["exit_code"] == 0
    assert len(first["advanced"]) == 1
    after_first = sizing.read_text(encoding="utf-8")

    second = _run(params, repo_root=repo / ".git")
    assert second["exit_code"] == 1
    assert second["advanced"] == []
    assert second["candidates_matched"] == 0
    after_second = sizing.read_text(encoding="utf-8")
    assert after_second == after_first


# ---------------------------------------------------------------------------
# AC4 — exactly one registered op for the flip-to-terminal mutation class
# ---------------------------------------------------------------------------


def test_ac4_exactly_one_cascade_terminal_op_registered():
    registered = {n for n in ipc_mod._REGISTRY if n.endswith(".cascade_terminal")}
    assert registered == {"deliverable.cascade_terminal"}


# ---------------------------------------------------------------------------
# AC9 — a plan-less sizing is reported UNKEYED, distinct from ambiguous
# ---------------------------------------------------------------------------


def test_ac9_planless_sizing_is_unkeyed_not_ambiguous(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-noworkstream.yaml", status="routed", deliverable_id="")

    corpus = backfill_mod.enumerate_corpus(str(repo))
    sizing_files = [f for f in corpus if backfill_mod.classify_artifact(f) == "sizing"]
    assert len(sizing_files) == 1

    result = backfill_mod.group_corpus(corpus)
    assert backfill_mod._UNKEYED_SIZING_GROUP_KEY in result.group_files
    assert sizing_files[0] in result.group_files[backfill_mod._UNKEYED_SIZING_GROUP_KEY]

    ambiguous = backfill_mod.detect_ambiguous_groups(result)
    assert backfill_mod._UNKEYED_SIZING_GROUP_KEY not in ambiguous


# ---------------------------------------------------------------------------
# AC10 — no chunk moves the vendored schema version without AC1's answer
# ---------------------------------------------------------------------------


def test_ac10_vendored_sizing_schema_version_is_pinned():
    """Discharge for the prohibition (not the prohibition itself): a version
    bump reds this loudly, with the coordinator-claude coordinate to check named in the
    failure message, rather than relying on an executor remembering the
    rule. See the vendored schema's own x-bump-note for the coordinate."""
    schema_path = (
        Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    pinned = "1.14.0"
    assert schema["x-schema-version"] == pinned, (
        f"sizing-object.schema.json's x-schema-version moved off the pinned "
        f"{pinned!r} — check coordinator-claude@1fabbc9c3 "
        "(coordinator/schemas/sizing-object.schema.json) for a correspondingly "
        "recorded answer before re-pinning this test."
    )


# Review: staff-eng — Finding 11: C0 vendored FOUR schemas, but only
# sizing-object carried a version-pin regression above — roadmap/goal/
# initiative reopened the identical EQUAL_VERSION_SHAPE_DRIFT silent-divergence
# hazard C0 exists to close, the moment coordinator-claude bumps any of the other three.
# Table-driven so a future vendored schema is one row, not a new function.
_VENDORED_SCHEMA_VERSION_PINS = (
    # Re-pinned 1.8.0 -> 1.14.0 on coordinator-claude's four-field probe/intent widen
    # (1fabbc9c3), vendored here at f0085198a. The recorded answer AC1 demands
    # is memo 2026-08-13-coordinator-claude-em-sizing-object-schema-widened-1-14-0.md.
    ("sizing-object.schema.json", "1.14.0", "coordinator-claude@1fabbc9c3 (coordinator/schemas/sizing-object.schema.json)"),
    ("roadmap.schema.json", "1.3.0", "coordinator-claude coordinator/schemas/roadmap.schema.json"),
    ("goal.schema.json", "1.2.0", "coordinator-claude coordinator/schemas/goal.schema.json"),
    ("initiative.schema.json", "1.1.0", "coordinator-claude coordinator/schemas/initiative.schema.json"),
)


@pytest.mark.parametrize("filename,pinned,doe_coordinate", _VENDORED_SCHEMA_VERSION_PINS)
def test_ac10_vendored_schema_versions_are_pinned(filename, pinned, doe_coordinate):
    schema_path = Path(__file__).parent.parent.parent / "frontmatter" / "schemas" / filename
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["x-schema-version"] == pinned, (
        f"{filename}'s x-schema-version moved off the pinned {pinned!r} — check "
        f"{doe_coordinate} for a correspondingly recorded answer before "
        "re-pinning this test."
    )


# ---------------------------------------------------------------------------
# AC11 — DR-263 predicate: leg (a) live-claim refusal, leg (c) terminal refusal
# ---------------------------------------------------------------------------


def test_ac11_predicate_leg_shape_is_closed_vocabulary():
    for kind in (cascade_mod._HANDOFF_KIND, cascade_mod._SIZING_KIND):
        for leg_name, leg in kind.predicate_legs.items():
            assert leg_name in ("a", "b", "c")
            assert isinstance(leg, cascade_mod._PredicateLeg)
            assert isinstance(leg.applies, bool)
            if not leg.applies:
                assert leg.reason


def test_ac11_sizing_leg_b_is_exempt_with_recorded_reason():
    leg_b = cascade_mod._SIZING_KIND.predicate_legs["b"]
    assert leg_b.applies is False
    assert leg_b.reason


def test_ac11_live_claimed_sizing_target_is_refused_not_flipped(tmp_path, monkeypatch):
    import coordinator_core.claim_state as claim_state_mod
    from unittest import mock

    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-claimed.yaml", status="routed", deliverable_id="dlv-claim-000000")

    session_id = "33333333-3333-3333-3333-333333333333"
    claim_dir = claim_state_mod.handoff_claim_dir(repo / ".git", Path("state/sizings/20260101-claimed.yaml"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-10T10:00:00Z", encoding="utf-8")

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True), \
        mock.patch.object(cascade_mod, "resolve_live_session_ids", return_value={session_id}):
        result = _run(
            {
                "deliverable_id": "dlv-claim-000000",
                "source_kind": "plan",
                "source_path": "docs/plans/dummy.md",
                "target_kind": "sizing",
            },
            repo_root=repo / ".git",
        )

    assert result["advanced"] == []
    assert result["exit_code"] == 1
    assert len(result["refused"]) == 1
    refusal = result["refused"][0]
    assert refusal["handoff_path"] == str(sizing)
    assert "claimed by live session" in refusal["reason"]
    assert session_id in refusal["reason"]

    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["status"] == "routed"


def test_ac11_already_terminal_sizing_target_is_refused_leg_c(tmp_path):
    """A sizing target past its terminal status (leg c) is refused with a
    named reason, matching `_predicate_refusal`'s existing refuse-and-name
    contract — reached via the predicate directly (not the write-side floor
    test above, which exercises `_advance_one_sizing` in isolation)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-shipped.yaml", status="shipped", deliverable_id="dlv-leg-c-0")

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            sizing,
            {"status": "shipped", "deliverable_id": "dlv-leg-c-0"},
            repo / ".git",
            kind=cascade_mod._SIZING_KIND,
        )
    )

    assert reason is not None
    # Review: staff-eng — Finding 1 (root cause of the leg-c rewrite): leg
    # (c) is now a uniform positive `live_values` check for every kind, so
    # the refusal message reads "not consistent with live-and-advanceable"
    # rather than the handoff-only "already terminal" wording this test
    # previously pinned.
    assert "not consistent with live-and-advanceable" in reason


def test_ac11_superseded_sizing_target_is_refused_leg_c(tmp_path):
    """Review: staff-eng — Finding 1's own named regression test: a
    `superseded` sizing is not terminal (`_SIZING_TERMINAL_STATUS` is
    `{shipped, declined}`, per 2026-08-10's `declined` addition) but is also
    not live — leg (c) must refuse it rather than let it clear through to a
    plan-triggered flip to `shipped`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(repo, "20260101-superseded.yaml", status="superseded", deliverable_id="dlv-leg-c-1")

    reason = asyncio.run(
        cascade_mod._predicate_refusal(
            sizing,
            {"status": "superseded", "deliverable_id": "dlv-leg-c-1"},
            repo / ".git",
            kind=cascade_mod._SIZING_KIND,
        )
    )

    assert reason is not None
    assert "not consistent with live-and-advanceable" in reason

    # And end to end: the cascade must never flip a superseded sizing to shipped.
    result = _run(
        {
            "deliverable_id": "dlv-leg-c-1",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
            "target_kind": "sizing",
        },
        repo_root=repo / ".git",
    )
    assert result["advanced"] == []
    doc = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert doc["status"] == "superseded"


def test_declined_sizing_is_excluded_from_live_candidates(tmp_path):
    """2026-08-10 (docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md
    § C2): `declined` was folded into `_SIZING_TERMINAL_STATUS` alongside `shipped`
    — a declined sizing must never even reach the candidate set (unlike
    `superseded`, which is excluded from `terminal_values` and instead caught by
    leg (c) below)."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_sizing(repo, "20260101-declined.yaml", status="declined", deliverable_id="dlv-declined-0")

    matches, scan_incomplete, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-declined-0", kind=cascade_mod._SIZING_KIND
    )
    assert matches == []

    # And end to end: the cascade must never flip a declined sizing to shipped.
    result = _run(
        {
            "deliverable_id": "dlv-declined-0",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
            "target_kind": "sizing",
        },
        repo_root=repo / ".git",
    )
    assert result["advanced"] == []
    assert result["candidates_matched"] == 0


# ---------------------------------------------------------------------------
# Handoff-path regression — existing behaviour unchanged
# ---------------------------------------------------------------------------


def test_handoff_kind_end_to_end_still_advances_unchanged(tmp_path, monkeypatch):
    session_id = "44444444-4444-4444-4444-444444444444"
    monkeypatch.setenv("CLAUDE_SESSION_ID", session_id)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    repo = tmp_path / "repo"
    _init_repo(repo)
    scoped = repo / "feature.txt"
    scoped.write_text("feature body\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(
        repo, "commit", "-m",
        f"implement the feature this handoff scopes\n\nSession-Id: {session_id}",
    )

    handoff = repo / "state" / "handoffs" / "20260101-h.md"
    handoff.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        'title: "Test Handoff 20260101-h.md"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        'predecessor: "none"\n'
        "deployment_state: ready_to_fire\n"
        "deliverable_id: dlv-handoff-regress-0\n"
        "scope:\n"
        "  - feature.txt\n"
    )
    handoff.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(handoff.relative_to(repo)))
    _git(repo, "commit", "-m", "add handoff")

    result = _run(
        {
            "deliverable_id": "dlv-handoff-regress-0",
            "source_kind": "plan",
            "source_path": "docs/plans/dummy.md",
        },
        repo_root=repo / ".git",
    )

    assert result["exit_code"] == 0
    assert len(result["advanced"]) == 1
    assert result["unreadable"] == []
    assert result["scan_incomplete"] is False

    split = split_frontmatter(handoff.read_text(encoding="utf-8"))
    assert split is not None
    assert read_fm_field(split.fm_text, "deployment_state") == "shipped"


def test_handoff_kind_two_tuple_wrapper_still_used_by_backstop_sweep(tmp_path):
    """`_collect_live_candidates` (the 2-tuple wrapper) stays byte-for-byte
    equivalent to the handoff leg of the parameterised form — proves
    `cascade_backstop_sweep.py`'s direct caller is unaffected by this
    chunk's parameterisation."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "20260101-h.md", deliverable_id="dlv-wrapper-0")

    matches_2tuple, scan_incomplete_2tuple = cascade_mod._collect_live_candidates(
        repo, "dlv-wrapper-0"
    )
    matches_3tuple, scan_incomplete_3tuple, unreadable = cascade_mod._collect_live_candidates_for_kind(
        repo, "dlv-wrapper-0", kind=cascade_mod._HANDOFF_KIND
    )

    assert len(matches_2tuple) == len(matches_3tuple) == 1
    assert scan_incomplete_2tuple == scan_incomplete_3tuple == False
    assert unreadable == []


# ---------------------------------------------------------------------------
# AC7 end-to-end gap (named in C4, closed here per the plan's C6 assignment)
# ---------------------------------------------------------------------------


_DOC_NEW_CLI = (
    Path(__file__).parent.parent.parent.parent / "coordinator" / "bin" / "coordinator-doc-new"
)


@pytest.mark.skipif(not _DOC_NEW_CLI.is_file(), reason="coordinator-doc-new CLI not found at expected path")
def test_ac7_coordinator_doc_new_writes_reverse_edge_end_to_end(tmp_path):
    """The end-to-end leg C4's executor could not exercise (its sandbox denied
    creating a scratch git repo). Runs the REAL CLI as a subprocess against a
    real git repo, proving the plan-creation write-back actually lands on
    disk through `main()`, not just through the mutation function in
    isolation."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing_rel = "state/sizings/2026-08-10-ac7-e2e.yaml"
    sizing_path = repo / sizing_rel
    sizing_path.parent.mkdir(parents=True, exist_ok=True)
    sizing_path.write_text(
        _sizing_body(status="sized", deliverable_id="dlv-ac7-000000"), encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            str(_DOC_NEW_CLI),
            "--type", "plan",
            "--title", "AC7 end-to-end test plan",
            "--sizing-object", sizing_rel,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode == 0, (
        f"coordinator-doc-new failed (rc={result.returncode})\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    plan_rel = result.stdout.strip()
    assert plan_rel, f"no plan path printed; stderr={result.stderr}"
    plan_abs = repo / plan_rel
    assert plan_abs.is_file(), f"plan file not written at {plan_abs}"

    sizing_after = yaml.safe_load(sizing_path.read_text(encoding="utf-8"))
    assert sizing_after["status"] == "routed"
    assert sizing_after["plan"] == plan_rel

    # Review: staff-eng — Finding 10: the mutated sizing must still validate
    # against the vendored schema — unlike test_ac2_..., this test previously
    # asserted values only, so a `plan:` value the schema's
    # `^docs/plans/.+\.md$` pattern rejects (e.g. a Windows-separator path
    # `os.path.relpath` failed to normalize) would pass silently.
    errors = cascade_mod._validate_sizing_fm(sizing_path.read_text(encoding="utf-8"))
    assert errors == []


@pytest.mark.skipif(not _DOC_NEW_CLI.is_file(), reason="coordinator-doc-new CLI not found at expected path")
def test_ac7_mutate_sizing_reverse_edge_preserves_crlf():
    """Review: staff-eng — Finding 9 (doc-new reverse-edge leg): unlike the
    cascade's write side, `_mutate_sizing_reverse_edge` is a PURE text
    function — no `locked_rmw`/universal-newline read in between when called
    directly — so a CRLF-authored document's line endings must survive the
    mutation genuinely UNCHANGED, not merely homogenised. Exercises Findings
    2 and 3's fix directly: the old `re.sub(r"(?m)^status:.*$", ...)`
    consumed the line's own trailing `\\r` without re-emitting it.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("coordinator_doc_new_crlf_test", str(_DOC_NEW_CLI))
    spec = importlib.util.spec_from_loader("coordinator_doc_new_crlf_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    old_text = _sizing_body(status="sized", deliverable_id="dlv-crlf-doc-new-0").replace("\n", "\r\n")
    new_text = mod._mutate_sizing_reverse_edge(old_text, "docs/plans/2026-01-01-crlf-doc-new.md")

    bare_lf = new_text.replace("\r\n", "").count("\n")
    bare_cr = new_text.replace("\r\n", "").count("\r")
    assert bare_cr == 0, "stray bare \\r found — mixed line endings"
    assert bare_lf == 0, "mixed CRLF and bare LF — CRLF document not preserved end to end"
    assert "\r\n" in new_text


@pytest.mark.skipif(not _DOC_NEW_CLI.is_file(), reason="coordinator-doc-new CLI not found at expected path")
def test_ac7_reverse_edge_write_precedes_plan_write_and_reverts_on_failure(tmp_path):
    """The write-order + revert-on-failure mechanism, exercised directly
    (not through the CLI, whose plan-write step has no easy induced-failure
    hook): a failing plan-file write must leave the sizing reverted to its
    pre-mutation text, not half-written."""
    import importlib.util

    # `coordinator-doc-new` has no `.py` suffix, so `spec_from_file_location`
    # must be told the loader explicitly (SourceFileLoader) rather than
    # relying on suffix-based loader inference, which returns None for an
    # extensionless path.
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("coordinator_doc_new_under_test", str(_DOC_NEW_CLI))
    spec = importlib.util.spec_from_loader("coordinator_doc_new_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)

    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing_path = repo / "state" / "sizings" / "2026-08-10-revert.yaml"
    sizing_path.parent.mkdir(parents=True, exist_ok=True)
    before = _sizing_body(status="sized", deliverable_id="dlv-revert-000000")
    sizing_path.write_text(before, encoding="utf-8")

    old_text = mod._write_sizing_reverse_edge(
        str(sizing_path), "docs/plans/2026-08-10-revert.md", str(repo)
    )
    assert old_text == before
    mid = sizing_path.read_text(encoding="utf-8")
    assert "status: routed" in mid

    mod._revert_sizing_reverse_edge(str(sizing_path), old_text, str(repo))
    after = sizing_path.read_text(encoding="utf-8")
    assert after == before
