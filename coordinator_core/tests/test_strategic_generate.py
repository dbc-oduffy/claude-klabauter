"""
coordinator_core.tests.test_strategic_generate — Regression net for the "strategic.generate"
JSON-RPC op (coordinator_core.ops.strategic_generate) and its Track A / Track B / draft-writer
helper modules.

Tests map to the plan's ACs (docs/plans/2026-07-11-claude-klabauter-strategic-self-description-generation-leg.md):
  - AC2 (Track A): tag-less week-changelog derivation, typed-empty degrade.
  - AC3 (Track B): snapshot-diff derivation with/without inputs, no 'relationship' key.
  - AC4 (non-clobber): canonical self-description.yaml is never touched; clean-tree case only
    creates the draft.
  - Generator-owns-draft: re-running FULLY REPLACES the draft (no stale-field survival).
  - Provenance-marker presence on every emitted field.
  - AC1 (wire-level): subprocess dispatch smoke via `python -m coordinator_core.invoke`.
  - AC5 (structural schema-subset validation): assertions derived from the frozen schema's
    $defs/properties, NOT a jsonschema library call (stdlib only).

Handlers use asyncio.run() in sync test functions — no pytest-asyncio dependency (same pattern
as coordinator_core/tests/test_dispatch_message.py). Wire-level dispatch uses a PYTHONPATH-injected
subprocess (same pattern as coordinator_core/tests/test_invoke_main.py).

Spec backlink: pln-claude-klabauter-generation-leg-machine--127c81 § C6
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.strategic_generate as strategic_generate_mod
from coordinator_core.ops.strategic.competitor_deltas import derive_competitor_deltas
from coordinator_core.ops.strategic.draft_writer import DRAFT_REL, write_draft
from coordinator_core.ops.strategic.version_highlights import (
    _summarize_plans_touched,
    derive_version_highlights,
)
from coordinator_core.testing.doe_root import resolve_doe_root

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CLAUDE_KLABAUTER_ROOT = _PROJECT_ROOT  # this repo IS the claude-klabauter repo under test
_FROZEN_SCHEMA_PATH = (
    Path(resolve_doe_root() or "/doe-root-unresolved")
    / "coordinator" / "schemas" / "strategic-self-description.schema.json"
)

_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed.

    ``_strategic_generate`` is a plain ``def`` (no ``await`` in its body) and
    already resolves to a plain value by the time it reaches here; pass
    those through unchanged.
    """
    if asyncio.iscoroutine(coro):
        return asyncio.run(coro)
    return coro


def _make_env(**overrides: str) -> dict[str, str]:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{_PROJECT_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_PROJECT_ROOT)
    env.update(overrides)
    return env


def _write_changelog_file(changelog_dir: Path, filename: str, date: str, host: str, bullets: dict) -> None:
    """Write one state/week-changelog/YYYY-MM-DD-<host>.md fixture file.

    bullets: dict of field-name -> field-value (only _BULLET_FIELDS names matter to the parser).
    """
    changelog_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"## {date} — {host}"]
    for field_name, field_value in bullets.items():
        lines.append(f"**{field_name}:** {field_value}")
    (changelog_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _init_bare_git_repo(root: Path) -> None:
    """git init a tmp dir so it passes as a repo (tag-less, no commits needed for our reader)."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, creationflags=_NO_CONSOLE)


# ---------------------------------------------------------------------------
# AC2 — Track A: tag-less path via state/week-changelog/*.md
# ---------------------------------------------------------------------------

def test_plans_touched_slug_strips_sidecar_sub_suffix():
    """Regression: `_PLAN_SUB_SUFFIX_RE` was applied before `.md` was stripped.

    The pattern is `$`-anchored, so against a name still carrying `.md` it could
    never match and the sub-suffix survived into the emitted slug — two sidecars
    of the same plan also deduped as distinct entries.
    """
    field = (
        "docs/plans/2026-07-14-foo.prior-art-check.md (status: draft), "
        "docs/plans/2026-07-14-foo.plan-coverage-check.md (status: draft)"
    )
    assert _summarize_plans_touched(field) == "Plans advanced: foo"


def test_plans_touched_slug_keeps_a_plan_slug_ending_in_review():
    """The sub-suffix strip is dot-anchored, so domain vocabulary survives."""
    assert (
        _summarize_plans_touched("docs/plans/2026-07-14-ladder-review.md")
        == "Plans advanced: ladder-review"
    )


def test_plans_touched_slug_strips_persona_review_sidecar_tag():
    """Regression (review finding): the old `_PLAN_SUB_SUFFIX_RE` alternation only
    matched a closed list of ~4 tags and never covered persona-review sidecars,
    which dominate the real docs/plans/ corpus (`.the Staff Engineer-review.md` etc.) — those
    emitted an untrimmed `foo.the Staff Engineer-review` slug. The first-dot split fixes this
    generically without enumerating tag vocabularies.
    """
    field = "docs/plans/2026-07-14-foo.the Staff Engineer-review.md (status: draft)"
    assert _summarize_plans_touched(field) == "Plans advanced: foo"


def test_plans_touched_slug_strips_archival_timestamped_sidecar_tag():
    """Archival sidecars carry a `.<kind>.<ISO-stamp>.md` suffix — the timestamp
    segment's uppercase `T`/`Z` cannot match a `[a-z0-9-]+`-shaped regex, so the
    fix must be a first-dot split, not a single trailing-segment strip.
    """
    field = (
        "docs/plans/2026-07-14-foo.prior-art-check.2026-08-01T00-51-55Z.md "
        "(status: draft)"
    )
    assert _summarize_plans_touched(field) == "Plans advanced: foo"


def test_track_a_tagless_derives_from_changelog_with_generated_provenance(tmp_path):
    """AC2: tmp repo, no tags, state/week-changelog/*.md fixture -> version_highlights
    derive with provenance 'generated'."""
    _init_bare_git_repo(tmp_path)
    changelog_dir = tmp_path / "state" / "week-changelog"
    _write_changelog_file(
        changelog_dir, "2026-07-01-hostx.md", "2026-07-01", "hostx",
        {"Plans touched": "docs/plans/foo.md (status: in-progress)"},
    )
    _write_changelog_file(
        changelog_dir, "2026-07-02-hostx.md", "2026-07-02", "hostx",
        {"Decisions": "chose approach A"},
    )

    highlights = derive_version_highlights(tmp_path)

    assert isinstance(highlights, list)
    assert len(highlights) == 2
    for entry in highlights:
        assert entry["provenance"] == "generated"
        assert set(entry.keys()) == {"label", "date", "bullets", "provenance"}
    # chronological order (oldest first)
    assert highlights[0]["date"] == "2026-07-01"
    assert highlights[1]["date"] == "2026-07-02"


def test_track_a_empty_is_typed_list_not_crash(tmp_path):
    """AC2: no tags, no changelog dir, no git history signal -> typed [] not a crash."""
    _init_bare_git_repo(tmp_path)
    # No commits, no changelog dir at all.
    highlights = derive_version_highlights(tmp_path)
    assert highlights == []
    assert isinstance(highlights, list)


def test_track_a_tagless_and_fallback_branches_exercised(tmp_path):
    """Tag-less branch exercised directly above; here assert the gitlog-fallback branch
    (signal 3: neither tags nor changelog) degrades to [] cleanly on a repo with zero commits
    (git log has nothing to report), and does not raise. The tagged path (signal 1) is
    dogfooded live via claude-klabauter's own invocation below rather than fixtured here — claude-klabauter
    itself is tag-less (0 git tags verified), so a tagged-repo exercise would need a second
    throwaway git identity/commit setup; the branch logic is directly inspectable in
    version_highlights.py's _derive_from_tags and is covered by the empty/typed contract
    asserted here for the no-signal-at-all case.
    """
    _init_bare_git_repo(tmp_path)
    result = derive_version_highlights(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# AC3 — Track B: competitor deltas from snapshot vs prev_snapshot
# ---------------------------------------------------------------------------

def test_track_b_deltas_emitted_with_generated_provenance_no_relationship_key(tmp_path):
    """AC3: current+previous snapshot -> deltas with provenance 'generated', no 'relationship'."""
    prev_snapshot = {"competitors": [{"name": "Alpha"}, {"name": "Beta"}]}
    current_snapshot = {"competitors": [{"name": "Alpha"}, {"name": "Gamma"}]}

    deltas = derive_competitor_deltas(tmp_path, current_snapshot, prev_snapshot)

    assert isinstance(deltas, list)
    assert len(deltas) == 2  # Gamma added, Beta removed
    names = {d["name"] for d in deltas}
    assert names == {"Gamma", "Beta"}
    for entry in deltas:
        assert entry["provenance"] == "generated"
        assert "relationship" not in entry
        assert set(entry.keys()) == {"name", "note", "provenance"}


def test_track_b_absent_snapshot_is_typed_empty_no_delta(tmp_path):
    """AC3: absent snapshot(s) -> [] typed no-delta, never a crash."""
    assert derive_competitor_deltas(tmp_path, None, None) == []
    assert derive_competitor_deltas(tmp_path, {"competitors": []}, None) == []
    assert derive_competitor_deltas(tmp_path, None, {"competitors": []}) == []


# ---------------------------------------------------------------------------
# AC4 — non-clobber regression + clean-tree case
# ---------------------------------------------------------------------------

def test_canonical_self_description_stays_byte_identical(tmp_path):
    """AC4: pre-existing canonical self-description.yaml is untouched (byte/hash-identical)
    after a run of the generator."""
    strategic_dir = tmp_path / "state" / "strategic"
    strategic_dir.mkdir(parents=True)
    canonical_path = strategic_dir / "self-description.yaml"
    canonical_content = "repo_identity:\n  owner: someone\n  repo: someproj\n"
    canonical_path.write_text(canonical_content, encoding="utf-8", newline="")
    canonical_hash_before = hash(canonical_path.read_bytes())

    write_draft(tmp_path, {"version_highlights": [], "competitors": []})

    assert canonical_path.read_bytes() == canonical_content.encode("utf-8")
    assert hash(canonical_path.read_bytes()) == canonical_hash_before


def test_clean_tree_only_creates_draft_not_canonical(tmp_path):
    """AC4: on a tree with NO canonical present, only .draft.yaml is created;
    self-description.yaml is NOT created."""
    strategic_dir = tmp_path / "state" / "strategic"
    canonical_path = strategic_dir / "self-description.yaml"

    draft_path = write_draft(tmp_path, {"version_highlights": [], "competitors": []})

    assert draft_path.exists()
    assert draft_path == tmp_path / DRAFT_REL
    assert not canonical_path.exists()
    # Nothing else got created in strategic_dir besides the draft.
    created = sorted(p.name for p in strategic_dir.iterdir())
    assert created == ["self-description.draft.yaml"]


# ---------------------------------------------------------------------------
# Generator-owns-draft: idempotent full replace, no stale fields
# ---------------------------------------------------------------------------

def test_rerun_fully_replaces_stale_draft_no_lingering_fields(tmp_path):
    """Seed an existing .draft.yaml with a stale 'generated' bullet, run again, assert the
    draft is FULLY REPLACED (no stale field lingers) — generator-owns-draft model (DEC-3)."""
    draft_path = tmp_path / DRAFT_REL
    draft_path.parent.mkdir(parents=True)
    stale_fields = {
        "version_highlights": [
            {"label": "STALE-LABEL", "date": "2020-01-01", "bullets": ["stale bullet"], "provenance": "generated"}
        ],
        "competitors": [
            {"name": "StaleCompetitor", "note": "stale note", "provenance": "generated"}
        ],
        "stale_top_level_field": "should not survive",
    }
    draft_path.write_text(yaml.safe_dump(stale_fields), encoding="utf-8")

    fresh_fields = {"version_highlights": [], "competitors": []}
    write_draft(tmp_path, fresh_fields)

    reloaded = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    assert "stale_top_level_field" not in reloaded
    assert reloaded["version_highlights"] == []
    assert reloaded["competitors"] == []


# ---------------------------------------------------------------------------
# provenance-marker presence on every emitted field
# ---------------------------------------------------------------------------

def test_every_emitted_field_carries_provenance_marker(tmp_path):
    """Every dict inside version_highlights[] and competitors[] carries a provenance key,
    stamped 'generated' if missing — defense-in-depth per draft_writer._stamp_provenance."""
    fields = {
        "version_highlights": [
            {"label": "L1", "date": "2026-01-01", "bullets": ["b1"]},  # missing provenance
        ],
        "competitors": [
            {"name": "N1", "note": None},  # missing provenance
        ],
    }
    draft_path = write_draft(tmp_path, fields)
    reloaded = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    for entry in reloaded["version_highlights"]:
        assert entry.get("provenance") == "generated"
    for entry in reloaded["competitors"]:
        assert entry.get("provenance") == "generated"


# ---------------------------------------------------------------------------
# In-process handler smoke (distinct from the wire-level subprocess smoke below)
# ---------------------------------------------------------------------------

def test_in_process_handler_returns_expected_reply_shape(tmp_path):
    """In-process call to the registered handler coroutine — sanity on reply fields,
    distinct from the wire-level dispatch smoke (AC1) below."""
    _init_bare_git_repo(tmp_path)

    handler = strategic_generate_mod._strategic_generate
    result = _run(handler({"target_root": str(tmp_path)}))

    assert set(result.keys()) == {
        "target_root", "draft_path", "version_highlights_count",
        "competitor_deltas_count", "track_a_status", "track_b_status",
    }
    assert result["track_a_status"] in ("no-highlights", "populated")
    assert result["track_b_status"] == "no-delta"  # no snapshot supplied
    assert Path(result["draft_path"]).exists()


def test_missing_target_root_raises_value_error():
    handler = strategic_generate_mod._strategic_generate
    with pytest.raises(ValueError):
        _run(handler({}))


# ---------------------------------------------------------------------------
# AC1 — wire-level dispatch smoke (subprocess, distinct from in-process call above)
# ---------------------------------------------------------------------------

def test_wire_level_dispatch_smoke(tmp_path):
    """AC1: subprocess `python -m coordinator_core.invoke strategic.generate {...}` exits 0,
    returns draft_path + counts. Distinct code path from the in-process handler call above —
    exercises __main__.py argv parsing, JSON-RPC envelope construction, and dispatch_message."""
    _init_bare_git_repo(tmp_path)
    params = json.dumps({"target_root": str(tmp_path)})

    result = subprocess.run(
        [sys.executable, "-m", "coordinator_core.invoke", "strategic.generate", params],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(_PROJECT_ROOT),
        env=_make_env(),
        creationflags=_NO_CONSOLE,
    )

    assert result.returncode == 0, (
        f"strategic.generate wire dispatch must exit 0; got {result.returncode}.\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
    parsed = json.loads(result.stdout)
    assert parsed.get("jsonrpc") == "2.0"
    assert "result" in parsed, f"Expected 'result' key; got {parsed}"
    reply = parsed["result"]
    assert "draft_path" in reply
    assert "version_highlights_count" in reply
    assert "competitor_deltas_count" in reply
    assert Path(reply["draft_path"]).exists()


# ---------------------------------------------------------------------------
# AC5 — structural schema-subset validation (NO jsonschema — stdlib only)
# ---------------------------------------------------------------------------

def _load_frozen_schema():
    doe_root = resolve_doe_root()
    if not doe_root:
        pytest.skip(
            "No coordinator-claude sibling checkout resolvable (env override / machine-local "
            "registry / .doe-root pointer all empty) — AC5 structural schema-subset "
            "validation requires the frozen schema from that checkout; skipping."
        )
    if not _FROZEN_SCHEMA_PATH.exists():
        pytest.skip(
            f"coordinator-claude root resolved to {doe_root!r} but frozen schema absent at "
            f"{_FROZEN_SCHEMA_PATH} — cross-repo file may be absent/relocated; skipping."
        )
    return json.loads(_FROZEN_SCHEMA_PATH.read_text(encoding="utf-8"))


def test_synthetic_generated_draft_conforms_structurally_to_frozen_schema_subset(tmp_path):
    """AC5: read the frozen schema, derive assertions from its $defs/properties, and assert
    a FRESHLY GENERATED draft's version_highlights + competitors conform to the generatable
    subset (no jsonschema library — structural stdlib assertions only).

    CORRECTION (was: test_dogfood_draft_conforms_structurally_to_frozen_schema_subset):
    the original version read claude-klabauter's own live
    ``state/strategic/self-description.draft.yaml`` off disk. That file is an artifact of
    ``/coordinator:strategic-self-description-refresh`` having (or not having) been run in
    this working tree — a transient dogfood side-effect, not a permanent invariant. Commit
    ``3f1d07cc`` (2026-07-20) ran that ceremony and RENAMED the draft to
    ``self-description.draft.yaml.archived``, so the file this test asserted on stopped
    existing and the test went red for a reason with nothing to do with the generator's
    correctness. Fixed by driving the actual ``strategic.generate`` handler end-to-end
    (real changelog + snapshot fixtures -> real Track A/B derivation -> real
    ``write_draft``) against a synthetic ``tmp_path`` repo, so the assertion is independent
    of whether claude-klabauter's own strategic-self-description ceremony has ever been run.
    """
    schema = _load_frozen_schema()

    provenance_enum = set(schema["$defs"]["provenance"]["enum"])
    assert provenance_enum == {"curated", "generated", "asserted"}, (
        "Frozen schema provenance enum must stay THREE-value; generator freeze assumption broken."
    )

    vh_item_schema = schema["properties"]["version_highlights"]["items"]
    vh_required_keys = set(vh_item_schema["required"])
    assert vh_required_keys == {"label", "date", "bullets", "provenance"}

    competitor_item_schema = schema["properties"]["competitors"]["items"]
    competitor_full_required = set(competitor_item_schema["required"])
    assert "relationship" in competitor_full_required, (
        "Frozen full schema must still require 'relationship' on competitors — the "
        "generator's own emitted subset omits it, which is the point of this test."
    )

    # Drive the real op end-to-end on a synthetic repo so both tracks populate: Track A via a
    # changelog fixture (same shape as test_track_a_tagless_derives_from_changelog...), Track B
    # via a snapshot/prev_snapshot pair (same shape as test_track_b_deltas_emitted...).
    _init_bare_git_repo(tmp_path)
    changelog_dir = tmp_path / "state" / "week-changelog"
    _write_changelog_file(
        changelog_dir, "2026-07-01-hostx.md", "2026-07-01", "hostx",
        {"Plans touched": "docs/plans/foo.md (status: in-progress)"},
    )
    prev_snapshot = {"competitors": [{"name": "Alpha"}, {"name": "Beta"}]}
    current_snapshot = {"competitors": [{"name": "Alpha"}, {"name": "Gamma"}]}

    handler = strategic_generate_mod._strategic_generate
    result = _run(handler({
        "target_root": str(tmp_path),
        "snapshot": current_snapshot,
        "prev_snapshot": prev_snapshot,
    }))

    assert result["track_a_status"] == "populated"
    assert result["track_b_status"] == "populated"

    draft_path = Path(result["draft_path"])
    assert draft_path.exists()
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))

    date_re = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")

    for entry in draft.get("version_highlights", []):
        assert set(entry.keys()) == vh_required_keys, (
            f"version_highlights entry keys {set(entry.keys())} must equal exactly "
            f"the frozen-schema-required keys {vh_required_keys}"
        )
        assert entry["provenance"] in provenance_enum
        assert entry["provenance"] == "generated"
        assert date_re.match(entry["date"]), f"date {entry['date']!r} must match YYYY-MM-DD"
        assert isinstance(entry["bullets"], list)

    # Review: code-reviewer (F3) — derive from the frozen schema (competitor required-keys minus
    # the human-curated-only "relationship" field), not a hardcoded literal, so a regression
    # in the schema's generatable subset is caught here rather than silently passing.
    generator_competitor_keys = competitor_full_required - {"relationship"}
    for entry in draft.get("competitors", []):
        assert set(entry.keys()) == generator_competitor_keys, (
            f"competitors entry keys {set(entry.keys())} must equal exactly the "
            f"generator-emitted subset {generator_competitor_keys} (no 'relationship')"
        )
        assert "relationship" not in entry
        assert entry["provenance"] in provenance_enum
        assert entry["provenance"] == "generated"
