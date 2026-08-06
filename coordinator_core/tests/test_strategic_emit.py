"""
coordinator_core.tests.test_strategic_emit — Regression net for the "strategic.emit" JSON-RPC
op (coordinator_core.ops.strategic_emit) and its emit_writer helper module.

Tests map to the stub's Acceptance criteria (tasks/strategic-feed-emission/stub.md):
  - AC1 (wire-level): registered; subprocess dispatch smoke via
    `python -m coordinator_core.invoke` (scope "none", target_root required, path-guarded).
  - AC2 (canonical-present emit): strategic-emission.json written with a valid
    ProvenanceEnvelope record (ref=null, source_kind=coordinator_artifact, timezone-aware
    observed_at).
  - AC3 (canonical-absent no-op): typed no-op, NO file written, no crash — validated on
    claude-klabauter's own root (which has only the draft) via the dogfood test below.
  - AC4 (guard behaviour): target_root missing -> ValueError; path-escape -> PathEscapeError.
  - AC5 (field-shape subset check): canonical record fields checked against the frozen
    canonical schema v1.0.0's required top-level keys.
  - AC6 (classification): MUTATING asserted.

Handlers use asyncio.run() in sync test functions — no pytest-asyncio dependency (same pattern
as coordinator_core/tests/test_strategic_generate.py). Wire-level dispatch uses a
PYTHONPATH-injected subprocess (same pattern as test_invoke_main.py).

Spec backlink: tasks/strategic-feed-emission/stub.md
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.strategic_emit as strategic_emit_mod
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.cartography._guard import PathEscapeError, path_guard
from coordinator_core.ops.strategic.emit_writer import (
    CANONICAL_REL,
    OUTPUT_NAME,
    build_feed,
    emit_strategic_feed,
    read_canonical,
)
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.testing.doe_root import resolve_doe_root

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CLAUDE_KLABAUTER_ROOT = _PROJECT_ROOT  # this repo IS the claude-klabauter repo under test
_FROZEN_SCHEMA_PATH = (
    Path(resolve_doe_root() or "/doe-root-unresolved")
    / "coordinator" / "schemas" / "strategic-self-description.schema.json"
)

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_MINIMAL_CANONICAL: dict = {
    "repo_identity": {"owner": "someone", "repo": "someproj", "coordinator_root_path": "/tmp/someproj"},
    "lifecycle": "alpha",
    "vision": {"value": "Do the thing.", "provenance": "curated"},
    "version_highlights": [],
    "competitors": [],
    "call_to_action": {"kind": "none", "label": "N/A"},
    "hero_asset": None,
}


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_PROJECT_ROOT)
    env.update(overrides)
    return env


def _init_bare_git_repo(root: Path) -> None:
    """git init a tmp dir so it passes as a repo (path_guard/EmitContext don't require commits)."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, creationflags=_NO_CONSOLE)


def _write_canonical(root: Path, content: dict) -> Path:
    canonical_path = root / CANONICAL_REL
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")
    return canonical_path


# ---------------------------------------------------------------------------
# AC2 — canonical-present emit: valid ProvenanceEnvelope record
# ---------------------------------------------------------------------------

def test_canonical_present_emits_feed_with_valid_provenance(tmp_path):
    """AC2: canonical present -> strategic-emission.json written with a valid
    ProvenanceEnvelope record (ref=null, source_kind=coordinator_artifact,
    timezone-aware observed_at)."""
    _init_bare_git_repo(tmp_path)
    _write_canonical(tmp_path, _MINIMAL_CANONICAL)

    result = emit_strategic_feed(EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state"))

    assert result["status"] == "emitted"
    assert result["emitted"] == 1
    out_path = Path(result["out"])
    assert out_path == tmp_path / "state" / OUTPUT_NAME
    assert out_path.exists()

    feed = json.loads(out_path.read_text(encoding="utf-8"))
    assert feed["schema_version"] == "1.0.0"
    assert "emitted_at" in feed
    assert "emitted_by_machine" in feed
    assert "coordinator_root_path" in feed
    assert len(feed["strategic_self_descriptions"]) == 1

    record = feed["strategic_self_descriptions"][0]
    prov = record["provenance"]
    assert prov["source_kind"] == "coordinator_artifact"
    assert prov["ref"] is None
    assert prov["path"] == CANONICAL_REL
    assert prov["derivation"] == "parsed"
    # timezone-aware ISO-8601 with UTC offset (ends in "Z" per EmitContext.resolve's format).
    assert prov["observed_at"].endswith("Z")
    datetime.strptime(prov["observed_at"], "%Y-%m-%dT%H:%M:%SZ")  # raises on malformed
    # Review: code-reviewer (Finding 4) — the above asserts the literal string shape
    # EmitContext happens to produce today, not the actual AC2 criterion (timezone-aware
    # observed_at, i.e. "would pass rag's provenance.py validation"). Reimplement rag's
    # own Z-normalization + aware-datetime check here so this test fails if the
    # aware-datetime property regresses, not just the literal string shape.
    # Mirrors example-retrieval-repo:core/workstate_store/provenance.py's Z-normalization.
    observed_at = prov["observed_at"]
    parsed_dt = (
        datetime.fromisoformat(observed_at[:-1] + "+00:00")
        if observed_at.endswith("Z")
        else datetime.fromisoformat(observed_at)
    )
    assert parsed_dt.tzinfo is not None

    assert record["self_description"] == _MINIMAL_CANONICAL


def test_curation_status_rides_as_record_data_not_provenance(tmp_path):
    """CORRECTION captured (stub § Wire contract): per-field curation-status markers
    (curated|generated|asserted) must ride as record DATA inside the wrapped payload, NEVER
    inside the ProvenanceEnvelope, which is always derivation='parsed' at the envelope layer."""
    _init_bare_git_repo(tmp_path)
    _write_canonical(tmp_path, _MINIMAL_CANONICAL)

    ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")
    canonical = read_canonical(tmp_path)
    feed = build_feed(ctx, canonical)
    record = feed["strategic_self_descriptions"][0]

    assert record["provenance"]["derivation"] == "parsed"
    assert "curation_status" not in record["provenance"]
    assert "curated" not in record["provenance"].values()
    # the field-granularity provenance marker lives inside self_description, untouched.
    assert record["self_description"]["vision"]["provenance"] == "curated"


# ---------------------------------------------------------------------------
# AC3 — canonical-absent: typed no-op, no file written, no crash
# ---------------------------------------------------------------------------

def test_canonical_absent_is_typed_no_op_no_file_written(tmp_path):
    """AC3: canonical absent -> typed no-op dict, NO file written, no crash."""
    _init_bare_git_repo(tmp_path)
    # No state/strategic/ dir at all.

    ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")
    result = emit_strategic_feed(ctx)

    assert result == {"status": "no-canonical", "emitted": 0}
    assert not (tmp_path / "state" / OUTPUT_NAME).exists()


def test_draft_present_but_canonical_absent_still_no_ops(tmp_path):
    """AC3 corollary: a draft-only tree (strategic.generate's live output shape) must NOT be
    mistaken for the canonical — only the exact CANONICAL_REL filename counts."""
    _init_bare_git_repo(tmp_path)
    draft_dir = tmp_path / "state" / "strategic"
    draft_dir.mkdir(parents=True)
    (draft_dir / "self-description.draft.yaml").write_text(
        yaml.safe_dump({"version_highlights": [], "competitors": []}), encoding="utf-8"
    )

    ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")
    result = emit_strategic_feed(ctx)

    assert result == {"status": "no-canonical", "emitted": 0}
    assert not (tmp_path / "state" / OUTPUT_NAME).exists()


# ---------------------------------------------------------------------------
# Finding 1/2 — malformed-canonical / non-dict-canonical: fail loud, not crash-uncaught
# or silently-degrade.
# ---------------------------------------------------------------------------

def test_canonical_present_but_invalid_yaml_raises_structured_value_error(tmp_path):
    """Present-but-malformed canonical (invalid YAML syntax) must raise a structured
    ValueError, not let yaml.YAMLError bubble uncaught out of the JSON-RPC handler.
    # Review: code-reviewer (Finding 1/2) — proves the module's "never crashes" claim
    # actually holds for a present-but-garbage canonical, not just an absent one.
    """
    _init_bare_git_repo(tmp_path)
    canonical_path = tmp_path / CANONICAL_REL
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text('not: valid: yaml: [": :"', encoding="utf-8")

    with pytest.raises(ValueError, match="not valid YAML"):
        read_canonical(tmp_path)


def test_canonical_present_but_non_dict_raises_structured_value_error(tmp_path):
    """Present-but-non-dict canonical (valid YAML that parses to a bare list) must raise a
    structured ValueError rather than silently forwarding a non-dict value downstream into
    build_feed, where it would break the AC5 subset-check contract silently at the consumer.
    # Review: code-reviewer (Finding 1/2/5) — also verifies the Optional[dict] promise on
    # read_canonical's return type is now honest (Finding 5 is subsumed by this guard).
    """
    _init_bare_git_repo(tmp_path)
    canonical_path = tmp_path / CANONICAL_REL
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_text(yaml.safe_dump(["not", "a", "mapping"]), encoding="utf-8")

    with pytest.raises(ValueError, match="did not parse to a mapping"):
        read_canonical(tmp_path)


def test_handler_no_ops_on_draft_only_synthetic_root(tmp_path):
    """AC3 (handler-level, distinct from the emit_strategic_feed-level coverage above): a
    target_root shaped like a pre-ratification repo — draft present, no curated canonical —
    correctly no-ops when routed through the wire handler (_strategic_emit), not just
    emit_strategic_feed directly.

    CORRECTION (was: test_dogfood_claude_klabauter_own_root_no_ops_today): the original version called
    the handler against ``_CLAUDE_KLABAUTER_ROOT`` (this repo's own live working tree), asserting that
    claude-klabauter's own root had "only the draft, no curated canonical yet" as a permanent invariant.
    It is not one: ``/coordinator:strategic-self-description-refresh`` — a ceremony this repo
    itself ships — ratifies the draft into a canonical ``self-description.yaml`` and archives
    the draft. Commit ``3f1d07cc`` (2026-07-20) ran that ceremony in this working tree, so the
    live root now HAS a curated canonical and the old assertion is simply false going forward.
    Fixed by asserting the same behavioral contract against a synthetic ``tmp_path`` repo
    shaped to match the pre-ratification case, so the test is independent of whether this
    repo's own strategic self-description has been ratified yet.
    """
    _init_bare_git_repo(tmp_path)
    draft_dir = tmp_path / "state" / "strategic"
    draft_dir.mkdir(parents=True)
    (draft_dir / "self-description.draft.yaml").write_text(
        yaml.safe_dump({"version_highlights": [], "competitors": []}), encoding="utf-8"
    )
    # No self-description.yaml (canonical) written — draft-only shape.

    handler = strategic_emit_mod._strategic_emit
    result = _run(handler({"target_root": str(tmp_path)}))

    assert result["status"] == "no-canonical"
    assert result["emitted"] == 0
    assert "out" not in result
    assert not (tmp_path / "state" / OUTPUT_NAME).exists()


# ---------------------------------------------------------------------------
# AC4 — guard behaviour: missing target_root -> ValueError; path-escape -> PathEscapeError
# ---------------------------------------------------------------------------

def test_missing_target_root_raises_value_error():
    handler = strategic_emit_mod._strategic_emit
    with pytest.raises(ValueError):
        _run(handler({}))


def test_path_guard_rejects_path_that_escapes_target_root(tmp_path):
    """AC4: path-escape -> PathEscapeError. strategic.emit resolves target_root via
    ``path_guard(target_root, ".")`` — the same guard cartography/strategic.generate rely on.
    This directly exercises the guard's escape rejection (a relative candidate that traverses
    outside the resolved root raises), which is what the handler propagates unmodified."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    with pytest.raises(PathEscapeError):
        path_guard(real_dir, "../../../../etc/passwd")


# ---------------------------------------------------------------------------
# AC1 — wire-level dispatch smoke (subprocess, distinct from the in-process call above)
# ---------------------------------------------------------------------------

def test_in_process_handler_returns_expected_reply_shape_no_canonical(tmp_path):
    """In-process call to the registered handler coroutine on a canonical-absent tree —
    sanity on reply fields, distinct from the wire-level dispatch smoke (AC1) below."""
    _init_bare_git_repo(tmp_path)

    handler = strategic_emit_mod._strategic_emit
    result = _run(handler({"target_root": str(tmp_path)}))

    assert result["target_root"] == str(tmp_path.resolve())
    assert result["status"] == "no-canonical"
    assert result["emitted"] == 0


def test_wire_level_dispatch_smoke(tmp_path):
    """AC1: subprocess `python -m coordinator_core.invoke strategic.emit {...}` exits 0,
    scope 'none' + target_root required + path-guarded. Distinct code path from the
    in-process handler call above — exercises __main__.py argv parsing, JSON-RPC envelope
    construction, and dispatch_message."""
    _init_bare_git_repo(tmp_path)
    _write_canonical(tmp_path, _MINIMAL_CANONICAL)
    params = json.dumps({"target_root": str(tmp_path)})

    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "strategic.emit", params],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )

    assert result.returncode == 0, (
        f"strategic.emit wire dispatch must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed.get("jsonrpc") == "2.0"
    assert "result" in parsed, f"Expected 'result' key; got {parsed}"
    reply = parsed["result"]
    assert reply["status"] == "emitted"
    assert reply["emitted"] == 1
    assert Path(reply["out"]).exists()


# ---------------------------------------------------------------------------
# AC5 — structural schema-subset validation (NO jsonschema — stdlib only)
# ---------------------------------------------------------------------------

def _load_frozen_schema():
    doe_root = resolve_doe_root()
    if not doe_root:
        pytest.skip(
            "No example-doctrine-repo sibling checkout resolvable (env override / machine-local "
            "registry / .doe-root pointer all empty) — AC5 structural schema-subset "
            "validation requires the frozen schema from that checkout; skipping."
        )
    if not _FROZEN_SCHEMA_PATH.exists():
        pytest.skip(
            f"example-doctrine-repo root resolved to {doe_root!r} but frozen schema absent at "
            f"{_FROZEN_SCHEMA_PATH} — cross-repo file may be absent/relocated; skipping."
        )
    return json.loads(_FROZEN_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_canonical_top_level_required_keys_subset_of_frozen_schema(tmp_path):
    """AC5: the canonical record wrapped into the feed must carry (a superset containing)
    every top-level key the frozen schema v1.0.0 requires — a regression here means the
    canonical fixture drifted from the frozen contract's required set."""
    schema = _load_frozen_schema()
    required_keys = set(schema["required"])

    assert required_keys.issubset(_MINIMAL_CANONICAL.keys()), (
        f"fixture canonical is missing frozen-schema-required keys: "
        f"{required_keys - _MINIMAL_CANONICAL.keys()}"
    )

    _init_bare_git_repo(tmp_path)
    _write_canonical(tmp_path, _MINIMAL_CANONICAL)
    ctx = EmitContext.resolve(tmp_path, tmp_path, tmp_path / "state")
    result = emit_strategic_feed(ctx)
    out_path = Path(result["out"])
    feed = json.loads(out_path.read_text(encoding="utf-8"))
    emitted_record = feed["strategic_self_descriptions"][0]["self_description"]
    assert required_keys.issubset(emitted_record.keys())


# ---------------------------------------------------------------------------
# AC6 — MUTATING classification asserted
# ---------------------------------------------------------------------------

def test_strategic_emit_is_classified_mutating():
    assert classify("strategic.emit") == OpClass.MUTATING


def test_strategic_emit_classification_mirrors_strategic_generate():
    """Drift-guard (Finding 6): strategic.emit's docstring repeatedly claims it "mirrors
    strategic.generate" — assert classification parity against the sibling op directly,
    not just against a fixed literal, so future divergence between the two ops'
    classification is caught even if someone edits strategic.generate without touching
    this test.
    # Review: code-reviewer (Finding 6) — equality-with-sibling-op check, not fixed literal.
    """
    assert classify("strategic.emit") == classify("strategic.generate")
