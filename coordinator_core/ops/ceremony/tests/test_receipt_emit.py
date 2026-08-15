"""
coordinator_core.ops.ceremony.tests.test_receipt_emit

Tests for the ceremony evidence receipt emitter (C2.4).

Coverage:
  (a) roundtrip         — emit → re-read produces a faithful, schema-valid receipt
  (b) absent_tolerance  — read_receipt on a missing path returns NOT_YET_RUN_SENTINEL
  (c) atomicity         — no .tmp file survives a successful write; final file is
                          valid JSON with no partial content observable
  (d) phase_emit        — phase-1 then phase-2 write to the same path; phase-2
                          overwrites phase-1 in place
  (e) op_tail_derived   — op_tail in the written receipt is derived from D-node
                          tail entries in ctx.nodes, not stored independently
  (f) dir_created       — state/ceremony/ is created if absent
  (g) is_not_yet_run    — is_not_yet_run() correctly identifies the sentinel and
                          rejects a real receipt dict
  (h) sentinel_copy     — read_receipt returns a copy of the sentinel (mutating
                          the return value does not corrupt the constant)
  (i) explicit_path     — out_path overrides the default path derivation
  (j) default_path_helper — default_receipt_path returns expected path shape
  (k) applicable_node_ids — the persisted receipt carries the declared-membership
                            list from ctx.applicable_node_ids (op-spec §3)
  (l) engine_sha         — the persisted receipt carries resolve_engine_sha() (C2);
                            covers both the present case and the resolver-returns-
                            None graceful-absent case at the emit call site

Spec backlink:
  coordinator_core/ops/ceremony/receipt_emit.py
  docs/plans/2026-07-06-ceremony-as-pipeline-2-invert-workstream.md § C2.4
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.ops.ceremony.receipt_emit import (
    NOT_YET_RUN_SENTINEL,
    default_receipt_path,
    emit_receipt,
    is_not_yet_run,
    read_receipt,
)
from coordinator_core.ops.ceremony.receipt_schema import (
    compute_op_tail,
    make_b_node,
    make_d_node,
    make_f_node,
    make_j_node,
    make_x_node,
    validate,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ctx(
    nodes: list | None = None,
    ceremony: str = "wsc",
    scope_mode: str = "architecture",
    disposition: str = "single-session",
) -> PipelineContext:
    """Return a minimal PipelineContext for emit tests."""
    ctx = PipelineContext(
        ceremony=ceremony,
        scope_mode=scope_mode,
        disposition=disposition,
    )
    for node in (nodes or []):
        ctx.add_node(node)
    return ctx


# ---------------------------------------------------------------------------
# (a) roundtrip — emit → re-read
# ---------------------------------------------------------------------------


def test_emit_roundtrip_fields(tmp_path: Path) -> None:
    """emit_receipt writes a valid receipt; re-read matches key fields."""
    ctx = _make_ctx(
        nodes=[
            make_d_node("step-0", resolving_op="wsc_session_init",
                        evidence={"disposition": "single-session"}),
            make_j_node("step-1a", question="Is this generalizable?"),
            make_f_node("step-1b", slot="lesson_body"),
            make_b_node("B1", pre_resolved_evidence={"slice_count": 1}),
            make_x_node("step-2.6.3", missing_signal="SESSION_START_TIME"),
            make_d_node("step-3.5a", resolving_op="cs_archive",
                        evidence={"acted": ["sid-abc"], "skipped": [], "failed": []},
                        tail_step=True),
        ]
    )
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", receipt_phase="phase-1",
                           emitted_at="2026-07-06T12:00:00Z")
    assert path.exists()

    receipt = read_receipt(path)

    # Top-level fields
    assert receipt["schema_version"] == 1
    assert receipt["ceremony"] == "wsc"
    assert receipt["phase"] == "phase-1"
    assert receipt["emitted_at"] == "2026-07-06T12:00:00Z"
    assert receipt["scope_mode"] == "architecture"
    assert "nodes" in receipt
    assert "op_tail" in receipt
    assert "coverage_pointer" in receipt


def test_emit_roundtrip_node_count(tmp_path: Path) -> None:
    """The nodes list in the written receipt preserves all nodes from ctx."""
    nodes = [
        make_d_node("d1"),
        make_j_node("j1"),
        make_f_node("f1"),
        make_b_node("b1"),
        make_x_node("x1"),
    ]
    ctx = _make_ctx(nodes=nodes)
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)
    assert len(receipt["nodes"]) == 5


def test_emit_roundtrip_validates_clean(tmp_path: Path) -> None:
    """The written receipt passes receipt_schema.validate() with no errors."""
    ctx = _make_ctx(
        nodes=[
            make_d_node("step-3.5a", resolving_op="cs_archive",
                        evidence={"acted": ["s1"], "skipped": [], "failed": []},
                        tail_step=True),
        ]
    )
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)
    errors = validate(receipt)
    assert errors == [], f"Unexpected validation errors: {errors}"


# ---------------------------------------------------------------------------
# (k) applicable_node_ids — persisted receipt carries the declared-membership list
# ---------------------------------------------------------------------------


def test_emit_persists_applicable_node_ids(tmp_path: Path) -> None:
    """The receipt written to disk carries ctx.applicable_node_ids as a top-level
    field (op-spec §3, Option B) — a receipt-reading consumer must find it there,
    not just on the ephemeral op-return dict.
    """
    ctx = _make_ctx(
        nodes=[
            make_d_node("step-0", resolving_op="wsc_session_init"),
            make_j_node("step-1a", question="Is this generalizable?"),
        ]
    )
    ctx.applicable_node_ids = ["step-0"]

    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid",
                           receipt_phase="phase-1", emitted_at="2026-07-06T12:00:00Z")
    receipt = read_receipt(path)

    assert receipt["applicable_node_ids"] == ["step-0"]
    errors = validate(receipt)
    assert errors == [], f"Unexpected validation errors: {errors}"


def test_emit_omits_applicable_node_ids_when_ctx_default_empty(tmp_path: Path) -> None:
    """When ctx.applicable_node_ids is the dataclass default ([]), make_receipt still
    receives an explicit (empty) list — not None — so the field IS present as [].
    This is distinct from a hand-crafted receipt that never passed the kwarg at all
    (receipt_schema tests cover the None/absent-key case separately).
    """
    ctx = _make_ctx(nodes=[make_d_node("step-0")])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)
    assert receipt["applicable_node_ids"] == []


# ---------------------------------------------------------------------------
# (l) engine_sha — emitted receipt carries the resolved engine self-version (C2)
# ---------------------------------------------------------------------------


def test_emit_persists_engine_sha(tmp_path: Path) -> None:
    """emit_receipt threads resolve_engine_sha() into the written receipt.

    This test runs inside the real coordinator_core git checkout (not a
    synthetic tmp_path repo — emit_receipt writes to tmp_path, but
    resolve_engine_sha() resolves the git dir containing engine_version.py
    itself via Path(__file__), which is this repo), so the resolved SHA is
    a real 40-char hex HEAD SHA, not a placeholder.
    """
    ctx = _make_ctx(nodes=[make_d_node("step-0")])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)

    assert "engine_sha" in receipt
    assert isinstance(receipt["engine_sha"], str)
    assert len(receipt["engine_sha"]) == 40
    assert all(c in "0123456789abcdef" for c in receipt["engine_sha"])

    errors = validate(receipt)
    assert errors == [], f"Unexpected validation errors: {errors}"


def test_emit_omits_engine_sha_when_resolver_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """emit_receipt's own resolve_engine_sha() -> None path is graceful-absent.

    Review: code-reviewer Finding 1 (2026-07-12 engine-sha-receipt-field slice) —
    test_emit_persists_engine_sha only exercises the present case (this repo's
    real git checkout); nothing proved the None-return branch propagates through
    emit_receipt's own call site (as opposed to make_receipt's factory-level
    handling, which test_receipt_schema.py already covers directly). Monkeypatch
    the name as imported into receipt_emit's namespace (see
    `from coordinator_core.engine_version import resolve_engine_sha` at the top
    of receipt_emit.py) so the None branch is forced regardless of the actual
    git environment running the test.
    """
    monkeypatch.setattr(
        "coordinator_core.ops.ceremony.receipt_emit.resolve_engine_sha",
        lambda: None,
    )
    ctx = _make_ctx(nodes=[make_d_node("step-0")])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)

    assert "engine_sha" not in receipt

    errors = validate(receipt)
    assert errors == [], f"Unexpected validation errors: {errors}"


# ---------------------------------------------------------------------------
# (b) absent_tolerance — missing file → NOT_YET_RUN_SENTINEL
# ---------------------------------------------------------------------------


def test_read_absent_receipt_returns_sentinel(tmp_path: Path) -> None:
    """read_receipt on a non-existent path returns NOT_YET_RUN_SENTINEL."""
    path = tmp_path / "state" / "ceremony" / "wsc-receipt.json"
    result = read_receipt(path)
    assert is_not_yet_run(result), (
        "Expected NOT_YET_RUN_SENTINEL for absent receipt; "
        f"got {result!r}"
    )


def test_read_absent_receipt_does_not_raise(tmp_path: Path) -> None:
    """read_receipt on a missing path must not raise any exception."""
    path = tmp_path / "no-such-dir" / "no-such-receipt.json"
    result = read_receipt(path)  # must not raise
    assert result is not None


def test_sentinel_is_not_yet_run_true(tmp_path: Path) -> None:
    """is_not_yet_run() returns True for an absent-file sentinel."""
    path = tmp_path / "missing.json"
    result = read_receipt(path)
    assert is_not_yet_run(result) is True


# ---------------------------------------------------------------------------
# (c) atomicity — no .tmp file remains after a successful write
# ---------------------------------------------------------------------------


def test_atomicity_no_tmp_files_remain(tmp_path: Path) -> None:
    """No .tmp files survive a successful emit_receipt call."""
    ctx = _make_ctx()
    emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    ceremony_dir = tmp_path / "state" / "ceremony"
    # Review: code-reviewer F9 — previous glob "*.tmp.json" never matched actual temp names;
    # mkstemp produces ".wsc-receipt.tmp.<random>.json" (dot-prefixed, random suffix between
    # prefix and .json), so the pattern must anchor on the known prefix and use a wildcard
    # for the random suffix.  Using iterdir() for the robust form.
    out_path = tmp_path / "state" / "ceremony" / "wsc-receipt.json"
    remaining = [f for f in ceremony_dir.iterdir() if f != out_path and f.suffix == ".json"]
    assert remaining == [], (
        f"Temporary files leaked after successful write: {remaining}"
    )


def test_atomicity_file_is_valid_json_after_write(tmp_path: Path) -> None:
    """The receipt file is valid JSON immediately after write (no partial content)."""
    ctx = _make_ctx(nodes=[make_d_node("d1", tail_step=True,
                                       evidence={"acted": ["x"], "skipped": [], "failed": []})])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    # Read raw bytes and parse — if partial, json.loads will raise
    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert "schema_version" in parsed


def test_atomicity_write_creates_single_file(tmp_path: Path) -> None:
    """emit_receipt creates exactly one file in state/ceremony/ (no leftover temp)."""
    ctx = _make_ctx()
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    ceremony_dir = path.parent
    files = [f for f in ceremony_dir.iterdir() if f.is_file()]
    assert len(files) == 1, (
        f"Expected exactly 1 file in ceremony dir; found {files}"
    )
    assert files[0] == path


# ---------------------------------------------------------------------------
# (d) phase_emit — phase-1 then phase-2 write to the same path
# ---------------------------------------------------------------------------


def test_phase2_overwrites_phase1(tmp_path: Path) -> None:
    """Phase-2 emit overwrites the phase-1 receipt at the same path."""
    ctx = _make_ctx()
    path1, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", receipt_phase="phase-1",
                            emitted_at="2026-07-06T10:00:00Z")
    path2, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", receipt_phase="phase-2",
                            emitted_at="2026-07-06T11:00:00Z")

    assert path1 == path2, "Phase-1 and phase-2 must write to the same path"
    receipt = read_receipt(path2)
    assert receipt["phase"] == "phase-2"
    assert receipt["emitted_at"] == "2026-07-06T11:00:00Z"


def test_emit_receipt_mints_fresh_path_for_new_sid(tmp_path: Path) -> None:
    """A brand-new sid with no existing shard takes the mint-fresh (default_receipt_path) branch.

    Review: code-reviewer 2026-07-08 Finding 4 (nit) — test_phase2_overwrites_phase1
    only proves the reuse-existing-shard order works; this asserts the OTHER side of
    the ternary (existing = resolve_latest_receipt_path(...); out_path = existing or
    default_receipt_path(...)) — a fresh sid with no shard mints via default_receipt_path.
    """
    from coordinator_core.ops.ceremony.receipt_emit import resolve_latest_receipt_path

    ctx = _make_ctx()
    sid = "brand-new-sid"
    assert resolve_latest_receipt_path(tmp_path, "wsc", sid) is None, (
        "no shard should exist yet for a fresh sid"
    )

    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid=sid, receipt_phase="phase-1",
                            emitted_at="2026-07-08T12:00:00Z")

    expected = default_receipt_path(tmp_path, "wsc", sid=sid, emitted_at="2026-07-08T12:00:00Z")
    assert path == expected, (
        f"a fresh sid with no existing shard must mint via default_receipt_path's "
        f"naming scheme; got {path}, expected {expected}"
    )


def test_phase1_receipt_has_correct_phase_field(tmp_path: Path) -> None:
    """A phase-1 emit sets receipt.phase = 'phase-1'."""
    ctx = _make_ctx()
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", receipt_phase="phase-1")
    receipt = read_receipt(path)
    assert receipt["phase"] == "phase-1"


# ---------------------------------------------------------------------------
# (e) op_tail_derived — derived from D-node tail entries, not stored independently
# ---------------------------------------------------------------------------


def test_op_tail_derived_from_ledger(tmp_path: Path) -> None:
    """op_tail in the written receipt is computed from tail D-nodes, not stored independently."""
    tail_node = make_d_node(
        "step-3.5a",
        resolving_op="cs_archive",
        evidence={"acted": ["sid-1", "sid-2"], "skipped": ["sid-3"], "failed": []},
        tail_step=True,
    )
    non_tail_node = make_d_node(
        "step-0",
        resolving_op="wsc_session_init",
        evidence={"acted": ["should-not-appear"], "skipped": [], "failed": []},
        tail_step=False,
    )
    ctx = _make_ctx(nodes=[tail_node, non_tail_node])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", tail_phase="archival")
    receipt = read_receipt(path)

    op_tail = receipt["op_tail"]
    assert op_tail["phase"] == "archival"
    assert "sid-1" in op_tail["acted"]
    assert "sid-2" in op_tail["acted"]
    assert "sid-3" in op_tail["skipped"]
    assert op_tail["failed"] == []
    # Non-tail D-node must NOT feed op_tail
    assert "should-not-appear" not in op_tail["acted"]


def test_op_tail_empty_when_no_tail_nodes(tmp_path: Path) -> None:
    """op_tail has empty arrays when ctx.nodes has no tail D-nodes."""
    ctx = _make_ctx(nodes=[make_j_node("j1"), make_f_node("f1"), make_x_node("x1")])
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid", tail_phase="archival")
    receipt = read_receipt(path)
    op_tail = receipt["op_tail"]
    assert op_tail["phase"] == "archival"
    assert op_tail["acted"] == []
    assert op_tail["skipped"] == []
    assert op_tail["failed"] == []


def test_op_tail_always_present_even_when_empty(tmp_path: Path) -> None:
    """op_tail is ALWAYS present in the receipt, even for an empty context."""
    ctx = _make_ctx()  # no nodes
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)
    assert "op_tail" in receipt
    tail = receipt["op_tail"]
    for key in ("phase", "acted", "skipped", "failed"):
        assert key in tail, f"op_tail.{key!r} must always be present"


# ---------------------------------------------------------------------------
# (f) dir_created — state/ceremony/ created if absent
# ---------------------------------------------------------------------------


def test_dir_created_if_absent(tmp_path: Path) -> None:
    """emit_receipt creates state/ceremony/ if it does not exist yet."""
    ceremony_dir = tmp_path / "state" / "ceremony"
    assert not ceremony_dir.exists()
    ctx = _make_ctx()
    emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    assert ceremony_dir.is_dir()


def test_dir_creation_idempotent(tmp_path: Path) -> None:
    """Calling emit_receipt twice does not fail when the directory already exists."""
    ctx = _make_ctx()
    emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")  # must not raise


# ---------------------------------------------------------------------------
# (g) is_not_yet_run — sentinel vs real receipt
# ---------------------------------------------------------------------------


def test_is_not_yet_run_false_for_real_receipt(tmp_path: Path) -> None:
    """is_not_yet_run() returns False for a receipt dict emitted by emit_receipt."""
    ctx = _make_ctx()
    path, _ = emit_receipt(ctx, repo_root=tmp_path, sid="test-sid")
    receipt = read_receipt(path)
    assert is_not_yet_run(receipt) is False


def test_is_not_yet_run_true_for_sentinel() -> None:
    """is_not_yet_run() returns True for NOT_YET_RUN_SENTINEL directly."""
    assert is_not_yet_run(NOT_YET_RUN_SENTINEL) is True


def test_is_not_yet_run_false_for_empty_dict() -> None:
    """is_not_yet_run() returns False for an empty dict (not the sentinel)."""
    assert is_not_yet_run({}) is False


# ---------------------------------------------------------------------------
# (h) sentinel_copy — mutating the returned sentinel does not corrupt the constant
# ---------------------------------------------------------------------------


def test_sentinel_copy_is_independent(tmp_path: Path) -> None:
    """read_receipt returns a copy of the sentinel — mutating it is safe."""
    path = tmp_path / "missing.json"
    sentinel_copy = read_receipt(path)
    sentinel_copy["mutated_key"] = "mutated"
    # The constant must be unaffected
    assert "mutated_key" not in NOT_YET_RUN_SENTINEL


# ---------------------------------------------------------------------------
# (i) explicit_path — out_path overrides default path derivation
# ---------------------------------------------------------------------------


def test_explicit_out_path(tmp_path: Path) -> None:
    """Supplying out_path writes to that explicit path, not the default."""
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    out_path = custom_dir / "my-receipt.json"
    ctx = _make_ctx()
    returned, _ = emit_receipt(ctx, out_path=out_path)
    assert returned == out_path
    assert out_path.exists()
    receipt = read_receipt(out_path)
    assert receipt["ceremony"] == "wsc"


def test_emit_requires_out_path_or_repo_root() -> None:
    """emit_receipt raises ValueError when neither out_path nor repo_root is supplied."""
    ctx = _make_ctx()
    with pytest.raises(ValueError, match="repo_root"):
        emit_receipt(ctx)


# ---------------------------------------------------------------------------
# (j) default_path_helper — default_receipt_path returns expected shape (C3
#     session-keyed shard: <repo_root>/state/ceremony/<ceremony>/<sid-short>-<emitted_at>.json)
# ---------------------------------------------------------------------------


def test_default_receipt_path_shape(tmp_path: Path) -> None:
    """default_receipt_path returns <repo_root>/state/ceremony/<ceremony>/<sid-short>-<emitted_at>.json."""
    path = default_receipt_path(tmp_path, "wsc", sid="test-sid", emitted_at="2026-07-08T10:00:00Z")
    assert path.parent == tmp_path / "state" / "ceremony" / "wsc"
    assert path.name == "test-sid-20260708T100000Z.json"


def test_default_receipt_path_ceremony_name(tmp_path: Path) -> None:
    """default_receipt_path uses the given ceremony name in the shard directory."""
    path = default_receipt_path(tmp_path, "pickup", sid="test-sid", emitted_at="2026-07-08T10:00:00Z")
    assert path.parent == tmp_path / "state" / "ceremony" / "pickup"


def test_default_receipt_path_defaults_to_wsc(tmp_path: Path) -> None:
    """default_receipt_path defaults to 'wsc' when no ceremony is specified."""
    path = default_receipt_path(tmp_path, sid="test-sid", emitted_at="2026-07-08T10:00:00Z")
    assert path.parent.name == "wsc"


# ---------------------------------------------------------------------------
# (k) sid_short_slug — _sid_short filesystem-safety and truncation
# ---------------------------------------------------------------------------


def test_sid_short_truncates_and_slugifies() -> None:
    """_sid_short slugifies non-filename-safe chars and truncates to _SID_SHORT_LEN."""
    from coordinator_core.ops.ceremony.receipt_emit import _sid_short, _SID_SHORT_LEN

    raw = "sess/weird:chars with spaces-and-a-very-long-tail-1234567890"
    short = _sid_short(raw)
    assert len(short) <= _SID_SHORT_LEN
    assert all(c.isalnum() or c in "-_" for c in short)


def test_resolve_latest_receipt_path_none_when_absent(tmp_path: Path) -> None:
    """resolve_latest_receipt_path returns None when no shard dir/file exists yet."""
    from coordinator_core.ops.ceremony.receipt_emit import resolve_latest_receipt_path

    assert resolve_latest_receipt_path(tmp_path, "wsc", "no-such-sid") is None


# ---------------------------------------------------------------------------
# (l) short_sid_prefix_hazard — Finding 1: a short sid must never cross-match
#     a different sid whose slug happens to start with the short sid + "-"
# ---------------------------------------------------------------------------


def test_resolve_latest_receipt_path_short_sid_no_cross_match(tmp_path: Path) -> None:
    """A short sid ("a1") must resolve ONLY its own shard, never "a1-x"'s shard.

    Review: code-reviewer 2026-07-08 Finding 1 — _sid_short("a1") == "a1" (well
    under _SID_SHORT_LEN), and the raw glob "a1-*.json" would previously also
    match a shard written for sid="a1-x" (filename "a1-x-<ts>.json" starts with
    "a1-"). resolve_latest_receipt_path must anchor the match so each sid only
    ever resolves its own shard.
    """
    ctx = _make_ctx()
    path_a1, _ = emit_receipt(ctx, repo_root=tmp_path, sid="a1", receipt_phase="phase-1",
                               emitted_at="2026-07-08T10:00:00Z")
    path_a1x, _ = emit_receipt(ctx, repo_root=tmp_path, sid="a1-x", receipt_phase="phase-1",
                                emitted_at="2026-07-08T11:00:00Z")

    assert path_a1 != path_a1x, "distinct sids must mint distinct shards"

    from coordinator_core.ops.ceremony.receipt_emit import resolve_latest_receipt_path

    resolved_a1 = resolve_latest_receipt_path(tmp_path, "wsc", "a1")
    resolved_a1x = resolve_latest_receipt_path(tmp_path, "wsc", "a1-x")

    assert resolved_a1 == path_a1, (
        f"sid='a1' must resolve its OWN shard, not sid='a1-x''s shard; "
        f"got {resolved_a1}, expected {path_a1}"
    )
    assert resolved_a1x == path_a1x, (
        f"sid='a1-x' must resolve its OWN shard; got {resolved_a1x}, expected {path_a1x}"
    )
