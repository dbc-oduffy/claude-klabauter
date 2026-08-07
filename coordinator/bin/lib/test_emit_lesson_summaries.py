"""test_emit_lesson_summaries.py — regression tests for the lesson summary emitter.

Tests:
  AC6  union-not-left-join: drained-only entry (no lessons.md row) IS emitted
  AC7  count-honesty / degraded-but-counted: malformed entry → parse_status="partial", still emitted
  AC8a promotion_state precedence: drained wins over lessons.md+drained
  AC8b promotion_state precedence: pending wins over lessons.md+outbox
  AC8c promotion_state precedence: captured for lessons.md-only
  AC8d captured-only nullability: change_kind/target_wiki/from_repo/created are null
  SCOPE      scope from [universal] tag survives partial parse
  SCOPE-OUTBOX block-list scope_tags:\n  - universal (no body marker) → scope=universal (F1 fix)
  KEY        lesson_key regex: ^[0-9a-f]{16}$
  AC7-nonnull  every emitted record has parse_status in ("ok","partial")

Spec backlink: docs/plans/2026-06-23-cockpit-contract-ext-wave2-emit-and-queue-migration.md § C3

Converted from a hand-rolled runner (`emit-lesson-summaries.test.py`) to a pytest-collectable
module — assertions preserved 1:1, `ok()`/`fail()` accounting replaced with plain `assert`.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

# Locate the emitter script relative to this test file
_THIS_DIR = Path(__file__).resolve().parent
_EMITTER = _THIS_DIR / "emit-lesson-summaries.py"
# Locate the byte-frozen contract schema. coordinator/cockpit-contract/ lives in the
# example-doctrine-repo repo, not the engine repo (DR-047: contract/data lives with example-doctrine-repo, engine
# with the engine repo) — a __file__ two-up walk off this test file's own location
# resolves to the engine repo's coordinator/, which has no cockpit-contract/ dir at
# all. Route through
# the shared doe_root() registry helper instead (same directory as this test file,
# no sys.path.insert needed — Python already puts the script's own dir on
# sys.path[0] when run directly).
from coordinator_registry import _DoeUnresolvable, doe_root  # noqa: E402

try:
    _SCHEMA_PATH = Path(doe_root()) / "coordinator" / "cockpit-contract" / "schema" / "lesson-summary.schema.json"
except _DoeUnresolvable:
    _SCHEMA_PATH = None

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^[0-9a-f]{16}$")


def _no_console_kw() -> dict:
    """Splat-ready Windows console-suppression kwarg for spawning the emitter
    under test. This file lives inside the engine checkout itself
    (coordinator/bin/lib/<this file>), so the engine root is this file's own
    repo root — three levels up. ``{}`` on any resolution/import failure."""
    try:
        claude_klabauter_root = str(Path(__file__).resolve().parents[3])
        if claude_klabauter_root not in sys.path:
            sys.path.insert(0, claude_klabauter_root)
        from coordinator_core.win_portability import no_console_creationflags

        return no_console_creationflags()
    except Exception:
        return {}


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def _lesson_key(title: str) -> str:
    normalized = _normalize_title(title)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _run_emitter(tmp_root: Path) -> list[dict]:
    """Run the emitter against the given tmp repo root, return parsed records."""
    result = subprocess.run(
        [sys.executable, str(_EMITTER), str(tmp_root), "test-repo", "test-branch", "abc123", "2026-01-01T00:00:00Z"],
        capture_output=True, text=True, timeout=30,
        **_no_console_kw(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"Emitter exited {result.returncode}:\nstdout={result.stdout}\nstderr={result.stderr}")
    return json.loads(result.stdout)


def _write_outbox_yaml(path: Path, data: dict) -> None:
    """Write a simple outbox YAML file.

    Limitation: if a body value itself contains a bare `---` line, the parser in
    _parse_outbox_yaml will misinterpret it as a fence boundary. The outbox corpus does
    not produce bodies with `---` lines, so this is acceptable but documented.
    """
    lines = ["---"]
    for k, v in data.items():
        if v is None:
            lines.append(f"{k}: null")
        elif "\n" in str(v):
            lines.append(f"{k}: |")
            for bl in str(v).splitlines():
                lines.append(f"  {bl}")
        elif isinstance(v, list):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        else:
            # Quote strings with colons or special chars
            sv = str(v)
            if any(c in sv for c in ":#{}[]!"):
                lines.append(f'{k}: "{sv}"')
            else:
                lines.append(f"{k}: {sv}")
    lines.append("---")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def _build_fixture_tree(tmp_path: Path) -> dict:
    """
    Build a tmp fixture tree and return a dict describing the titles used,
    so tests can look up expected keys.

    Titles used:
      A: "Clean lesson in lessons.md only"              (captured, universal) [state/lessons/]
      B: "Lesson in lessons.md and outbox"              (pending — outbox wins) [state/lessons/ + outbox]
      C: "Lesson in lessons.md and drained"             (drained — drained wins) [state/lessons/ + drained]
      D: "Drained-only lesson not in lessons.md"        (drained — AC6 load-bearing) [drained only]
      E: "Outbox-only lesson with empty body"            (pending, partial — AC7) [outbox only]
    """
    state = tmp_path / "state"
    state.mkdir(parents=True)

    outbox = state / "lessons-outbox"
    outbox.mkdir()
    drained = outbox / "drained"
    drained.mkdir()

    # Title constants
    title_A = "Clean lesson in lessons.md only"
    title_B = "Lesson in lessons.md and outbox"
    title_C = "Lesson in lessons.md and drained"
    title_D = "Drained-only lesson not in lessons.md"
    title_E = "Outbox-only lesson with empty body"

    # Write per-entry YAML files to state/lessons/ — does NOT include title_D or title_E.
    # C3c migration: first surface is now state/lessons/*.yaml, not lessons.md.
    lessons_dir = state / "lessons"
    lessons_dir.mkdir()
    _write_outbox_yaml(
        lessons_dir / "2026-01-01-lesson-a.yaml",
        {
            "title": title_A,
            "body": "This is the body of lesson A. It covers a clean capture.",
            "created": "2026-01-01",  # date-only — emitter normalizes to "2026-01-01T00:00:00Z"
            "status": "open",
            "scope": "universal",
            "from_repo": "test-repo",
            # target_wiki/evidence added so AC8d can assert exact forwarded values rather
            # than asserting null (born-attributable forward test).
            "target_wiki": "docs/wiki/test-lesson-a.md",
            "evidence": "Evidence string for lesson A",
        },
    )
    _write_outbox_yaml(lessons_dir / "2026-01-02-lesson-b.yaml", {
        "title": title_B,
        "body": "Body of lesson B. This one also has a pending outbox entry.",
        "created": "2026-01-02",
        "status": "open",
        "scope": "project",
        "from_repo": "test-repo",
    })
    _write_outbox_yaml(lessons_dir / "2026-01-03-lesson-c.yaml", {
        "title": title_C,
        "body": "Body of lesson C. This one also has a drained outbox entry.",
        "created": "2026-01-03",
        "status": "open",
        "scope": "universal",
        "from_repo": "test-repo",
    })

    # Write outbox entry for B (pending)
    _write_outbox_yaml(outbox / "2026-01-02-lesson-b.yaml", {
        "id": "aaaa-bbbb",
        "created": "2026-01-02T12:00:00+00:00",
        "from_repo": "claude-central-em",
        "change_kind": "doctrine-edit",
        "target_wiki": "docs/wiki/test.md",
        "title": title_B,
        "body": "Outbox body for lesson B.",
        "scope_tags": ["pending-tag"],
        "evidence": "Evidence for B",
    })

    # Write drained entry for C (drained + in lessons.md → drained wins)
    _write_outbox_yaml(drained / "2026-01-03-lesson-c.yaml", {
        "id": "cccc-dddd",
        "created": "2026-01-03T12:00:00+00:00",
        "from_repo": "claude-central-em",
        "change_kind": "wiki-edit",
        "target_wiki": "docs/wiki/other.md",
        "title": title_C,
        "body": "Drained body for lesson C.",
        "scope_tags": ["universal"],
        "evidence": None,
    })

    # Write drained entry for D (drained-only — NOT in lessons.md — AC6)
    _write_outbox_yaml(drained / "2026-01-04-lesson-d.yaml", {
        "id": "dddd-eeee",
        "created": "2026-01-04T12:00:00+00:00",
        "from_repo": "claude-central-em",
        "change_kind": "hook-edit",
        "target_wiki": None,
        "title": title_D,
        "body": "This lesson exists only in drained, not in lessons.md at all.",
        "scope_tags": [],
        "evidence": None,
    })

    # Write outbox entry for E (outbox-only, empty body → parse_status="partial" — AC7)
    _write_outbox_yaml(outbox / "2026-01-05-lesson-e.yaml", {
        "id": "eeee-ffff",
        "created": "2026-01-05T12:00:00+00:00",
        "from_repo": "claude-central-em",
        "change_kind": "doctrine-edit",
        "target_wiki": None,
        "title": title_E,
        "body": "",  # deliberately empty → parse_status="partial"
        "scope_tags": [],
        "evidence": None,
    })

    return {
        "title_A": title_A,
        "title_B": title_B,
        "title_C": title_C,
        "title_D": title_D,
        "title_E": title_E,
        "key_A": _lesson_key(title_A),
        "key_B": _lesson_key(title_B),
        "key_C": _lesson_key(title_C),
        "key_D": _lesson_key(title_D),
        "key_E": _lesson_key(title_E),
    }


@pytest.fixture(scope="module")
def base_fixture(tmp_path_factory) -> tuple[dict, list[dict], dict]:
    """Shared fixture tree + emitted records, reused by every AC* test below."""
    tmp_path = tmp_path_factory.mktemp("base-fixture")
    titles = _build_fixture_tree(tmp_path)
    records = _run_emitter(tmp_path)
    by_key = {r["lesson_key"]: r for r in records}
    return titles, records, by_key


# ---------------------------------------------------------------------------
# AC6: union-not-left-join (load-bearing)
# ---------------------------------------------------------------------------

def test_ac6_union_not_left_join(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_D"] in by_key, (
        f"AC6: CRITICAL — drained-only entry (key={titles['key_D']}) NOT in emitted "
        f"records (left-join bug)"
    )
    rec_D = by_key[titles["key_D"]]
    assert rec_D["promotion_state"] == "drained", (
        f"AC6: drained-only entry has promotion_state='{rec_D['promotion_state']}', expected 'drained'"
    )
    expected_title_D = titles["title_D"]
    assert rec_D.get("title") == expected_title_D, (
        f"AC6: drained-only entry title wrong — expected {expected_title_D!r}, got {rec_D.get('title')!r}"
    )
    drained_body_snippet = "only in drained"
    assert drained_body_snippet in (rec_D.get("body") or ""), (
        f"AC6: drained-only entry body missing expected content {drained_body_snippet!r}; "
        f"got {rec_D.get('body')!r}"
    )
    assert rec_D.get("parse_status") == "ok", (
        f"AC6: drained-only entry parse_status expected 'ok', got {rec_D.get('parse_status')!r}"
    )


# ---------------------------------------------------------------------------
# AC7: count-honesty / degraded-but-counted
# ---------------------------------------------------------------------------

def test_ac7_partial_and_count(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_E"] in by_key, (
        f"AC7: partial lesson (key={titles['key_E']}) was dropped — should be emitted "
        f"with parse_status=partial"
    )
    rec_E = by_key[titles["key_E"]]
    assert rec_E["parse_status"] == "partial", (
        f"AC7: expected parse_status='partial', got '{rec_E['parse_status']}'"
    )

    # Total count: 5 distinct keys (A,B,C,D,E)
    expected_min_count = 5
    assert len(records) >= expected_min_count, (
        f"AC7: expected >= {expected_min_count} records but got {len(records)}"
    )


# ---------------------------------------------------------------------------
# AC8a-c: promotion_state precedence
# ---------------------------------------------------------------------------

def test_ac8a_drained_wins(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_C"] in by_key, f"AC8a: lesson C (key={titles['key_C']}) not emitted"
    rec_C = by_key[titles["key_C"]]
    assert rec_C["promotion_state"] == "drained", (
        f"AC8a: expected 'drained', got '{rec_C['promotion_state']}'"
    )


def test_ac8b_pending_wins(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_B"] in by_key, f"AC8b: lesson B (key={titles['key_B']}) not emitted"
    rec_B = by_key[titles["key_B"]]
    assert rec_B["promotion_state"] == "pending", (
        f"AC8b: expected 'pending', got '{rec_B['promotion_state']}'"
    )
    assert rec_B.get("change_kind") == "doctrine-edit", (
        f"AC8b: expected change_kind='doctrine-edit', got '{rec_B.get('change_kind')}'"
    )
    # Outbox fixture has "2026-01-02T12:00:00+00:00"; _normalize_created passes it
    # through unchanged (contains "T" and matches ISO regex).
    expected_created_B = "2026-01-02T12:00:00+00:00"
    assert rec_B.get("created") == expected_created_B, (
        f"AC8b: created expected '{expected_created_B}', got '{rec_B.get('created')}' "
        f"(IsoDateTime offset accepted)"
    )


def test_ac8c_captured(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_A"] in by_key, f"AC8c: lesson A (key={titles['key_A']}) not emitted"
    rec_A = by_key[titles["key_A"]]
    assert rec_A["promotion_state"] == "captured", (
        f"AC8c: expected 'captured', got '{rec_A['promotion_state']}'"
    )


# ---------------------------------------------------------------------------
# AC8d: captured-only field contract (born-attributable)
# ---------------------------------------------------------------------------
# Born-attributable fix: `from_repo` and `created` are now forwarded from the per-entry
# YAML into captured records, so they are NON-null for lesson A (fixture has both).
# `change_kind` remains null for captured entries — it is only set from outbox/drained
# records and is NOT part of the per-entry YAML format.

def test_ac8d_captured_field_contract(base_fixture):
    titles, records, by_key = base_fixture
    assert titles["key_A"] in by_key
    rec_A = by_key[titles["key_A"]]

    # Fields that must remain null: change_kind is only from outbox/drained
    assert "change_kind" in rec_A, "AC8d: captured-only is missing field 'change_kind' (must be present-as-null per D9)"
    assert rec_A["change_kind"] is None, (
        f"AC8d: captured-only change_kind should be null but is '{rec_A['change_kind']}'"
    )

    assert rec_A.get("target_wiki") == "docs/wiki/test-lesson-a.md", (
        f"AC8d: captured-only target_wiki expected 'docs/wiki/test-lesson-a.md', "
        f"got '{rec_A.get('target_wiki')}'"
    )
    assert rec_A.get("evidence") == "Evidence string for lesson A", (
        f"AC8d: captured-only evidence expected 'Evidence string for lesson A', "
        f"got '{rec_A.get('evidence')}'"
    )
    # Born-attributable: from_repo and created ARE in the fixture YAML → must be non-null
    assert rec_A.get("from_repo") is not None, (
        "AC8d: captured-only from_repo should be non-null (born-attributable fix) but is null"
    )
    # "2026-01-01" (date-only in YAML) must be normalized to "2026-01-01T00:00:00Z"
    assert rec_A.get("created") == "2026-01-01T00:00:00Z", (
        f"AC8d: captured-only created expected '2026-01-01T00:00:00Z' (date-only normalized), "
        f"got '{rec_A.get('created')}'"
    )
    # scope should still be set from scope field in YAML (universal)
    assert rec_A.get("scope") == "universal", (
        f"AC8d: expected scope='universal' from scope field in YAML, got '{rec_A.get('scope')}'"
    )


def test_lesson_key_regex(base_fixture):
    titles, records, by_key = base_fixture
    for rec in records:
        key_val = rec.get("lesson_key", "")
        assert _KEY_RE.match(key_val), f"KEY: lesson_key '{key_val}' does not match ^[0-9a-f]{{16}}$"


def test_schema_validation(base_fixture):
    """Schema validation via in-process jsonschema (at least one record of each state).

    Re-homed to in-process `jsonschema` against the surviving byte-frozen contract after
    the node/Zod validator (validate-cockpit-record.mjs) was deleted (commit 7cca4d4c,
    2026-07-16) along with the rest of the TS toolchain — this removes the node
    dependency entirely, so the fail-loud-on-missing-contract posture now guards the
    schema JSON file itself, not an external binary.
    """
    titles, records, by_key = base_fixture
    if _SCHEMA_PATH is None:
        pytest.skip("example-doctrine-repo repo root unresolvable, skipping schema validation")
    assert _SCHEMA_PATH.is_file(), f"SCHEMA: contract schema not found at {_SCHEMA_PATH}"
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        _schema = json.load(f)
    for promotion_state in ("captured", "pending", "drained"):
        candidates = [r for r in records if r["promotion_state"] == promotion_state]
        if not candidates:
            # Not a failure if the fixture doesn't produce this state
            continue
        rec = candidates[0]
        try:
            jsonschema.validate(instance=rec, schema=_schema)
        except jsonschema.exceptions.ValidationError as e:
            pytest.fail(f"SCHEMA: {promotion_state} record FAILED schema validation: {e.message}")


# ---------------------------------------------------------------------------
# F7: 4-space-indented scope_tags list item
# ---------------------------------------------------------------------------
# The list-item parser was broadened to match any indent ≥2 (re.match r"^\s{2,}- ").
# Verify a YAML file with 4-space-indented scope_tags is parsed correctly by running
# the emitter against a minimal fixture containing one such entry.

def test_f7_four_space_indent_scope_tags(tmp_path):
    tmp2 = tmp_path
    (tmp2 / "state").mkdir(parents=True)
    ob2 = tmp2 / "state" / "lessons-outbox"
    ob2.mkdir()
    (ob2 / "drained").mkdir()
    title_F7 = "Four-space scope_tags indented lesson"
    yaml_content = (
        "---\n"
        f"title: {title_F7}\n"
        "body: body for F7 test\n"
        "scope_tags:\n"
        "    - universal\n"  # 4-space indent
        "created: 2026-01-06T00:00:00+00:00\n"
        "change_kind: doctrine-edit\n"
        "from_repo: test\n"
        "target_wiki: null\n"
        "evidence: null\n"
        "---\n"
    )
    (ob2 / "drained" / "2026-01-06-f7.yaml").write_text(yaml_content, encoding="utf-8")
    result_f7 = subprocess.run(
        [sys.executable, str(_EMITTER), str(tmp2), "test-repo", "test-branch", "abc123", "2026-01-01T00:00:00Z"],
        capture_output=True, text=True, timeout=30,
        **_no_console_kw(),
    )
    assert result_f7.returncode == 0, f"F7: emitter failed for 4-space scope_tags fixture: {result_f7.stderr}"
    recs_f7 = json.loads(result_f7.stdout)
    key_F7 = _lesson_key(title_F7)
    by_key_f7 = {r["lesson_key"]: r for r in recs_f7}
    assert key_F7 in by_key_f7, f"F7: 4-space scope_tags fixture not emitted (key={key_F7})"
    assert by_key_f7[key_F7].get("scope") == "universal", (
        f"F7: 4-space-indented scope_tags 'universal' not recognised; "
        f"got scope={by_key_f7[key_F7].get('scope')!r}"
    )


# ---------------------------------------------------------------------------
# SCOPE-OUTBOX: block-list scope_tags="universal" (no [universal] in body)
# ---------------------------------------------------------------------------
# Covers the F1-fixed path: block-list `scope_tags:\n  - universal` must survive the
# parser and produce scope="universal" even when body has no [universal] marker.

def test_scope_outbox_block_list(tmp_path):
    tmp3 = tmp_path
    (tmp3 / "state").mkdir(parents=True)
    ob3 = tmp3 / "state" / "lessons-outbox"
    ob3.mkdir()
    (ob3 / "drained").mkdir()
    title_scope_outbox = "Scope outbox universal block list lesson"
    yaml_scope = (
        "---\n"
        f"title: {title_scope_outbox}\n"
        "body: body text without the universal marker anywhere\n"
        "scope_tags:\n"
        "  - universal\n"  # block-list form — requires F1 fix to parse correctly
        "created: 2026-01-07T00:00:00+00:00\n"
        "change_kind: doctrine-edit\n"
        "from_repo: test\n"
        "target_wiki: null\n"
        "evidence: null\n"
        "---\n"
    )
    (ob3 / "drained" / "2026-01-07-scope-outbox.yaml").write_text(yaml_scope, encoding="utf-8")
    result_so = subprocess.run(
        [sys.executable, str(_EMITTER), str(tmp3), "test-repo", "test-branch", "abc123", "2026-01-01T00:00:00Z"],
        capture_output=True, text=True, timeout=30,
        **_no_console_kw(),
    )
    assert result_so.returncode == 0, f"SCOPE-OUTBOX: emitter failed: {result_so.stderr}"
    recs_so = json.loads(result_so.stdout)
    key_so = _lesson_key(title_scope_outbox)
    by_key_so = {r["lesson_key"]: r for r in recs_so}
    assert key_so in by_key_so, f"SCOPE-OUTBOX: block-list scope_tags fixture not emitted (key={key_so})"
    assert by_key_so[key_so].get("scope") == "universal", (
        f"SCOPE-OUTBOX: expected scope='universal' from block-list scope_tags, "
        f"got scope={by_key_so[key_so].get('scope')!r} — F1 fix may not have taken effect"
    )


# ---------------------------------------------------------------------------
# AC7-nonnull: every emitted record has parse_status in ("ok","partial")
# ---------------------------------------------------------------------------

def test_ac7_nonnull_parse_status(tmp_path):
    tmp4 = tmp_path
    _build_fixture_tree(tmp4)
    records4 = _run_emitter(tmp4)
    bad_parse_status = [
        r for r in records4
        if r.get("parse_status") not in ("ok", "partial")
    ]
    assert not bad_parse_status, (
        "AC7-nonnull: records with unexpected parse_status: "
        + ", ".join(f"{r.get('lesson_key')!r}={r.get('parse_status')!r}" for r in bad_parse_status)
    )


# ---------------------------------------------------------------------------
# SENTINEL: "0000-00-00" created → emitted created is None
# ---------------------------------------------------------------------------
# Verifies that the sentinel date used in legacy-migrated lessons emits honest null
# rather than a fake "0000-00-00T00:00:00Z" that would pollute date-bounded queries.

def test_sentinel_date_emits_null(tmp_path):
    tmp_s = tmp_path
    (tmp_s / "state").mkdir(parents=True)
    lessons_s = tmp_s / "state" / "lessons"
    lessons_s.mkdir()
    (tmp_s / "state" / "lessons-outbox").mkdir()
    (tmp_s / "state" / "lessons-outbox" / "drained").mkdir()
    title_s = "Sentinel date lesson migrated from legacy format"
    _write_outbox_yaml(lessons_s / "0000-00-00-sentinel.yaml", {
        "title": title_s,
        "body": "Body for sentinel date test.",
        "scope": "project",
        "created": "0000-00-00",  # sentinel — must emit null, not "0000-00-00T00:00:00Z"
        "from_repo": "test-repo",
        "status": "open",
    })
    result_s = subprocess.run(
        [sys.executable, str(_EMITTER), str(tmp_s), "test-repo", "test-branch", "abc123", "2026-01-01T00:00:00Z"],
        capture_output=True, text=True, timeout=30,
        **_no_console_kw(),
    )
    assert result_s.returncode == 0, f"SENTINEL: emitter failed: {result_s.stderr}"
    recs_s = json.loads(result_s.stdout)
    key_s = _lesson_key(title_s)
    by_key_s = {r["lesson_key"]: r for r in recs_s}
    assert key_s in by_key_s, f"SENTINEL: sentinel-date lesson not emitted (key={key_s})"
    rec_s = by_key_s[key_s]
    assert rec_s.get("created") is None, (
        f"SENTINEL: '0000-00-00' created expected null but got '{rec_s.get('created')}'"
    )
    # from_repo should still be forwarded (sentinel only affects created)
    assert rec_s.get("from_repo") == "test-repo", (
        f"SENTINEL: from_repo expected 'test-repo', got '{rec_s.get('from_repo')}'"
    )


# ---------------------------------------------------------------------------
# F1-OVERLAY: dual-presence — YAML has from_repo, outbox lacks it → captured value survives
# ---------------------------------------------------------------------------
# When a lesson is in BOTH per-entry YAML (from_repo="repo-a") AND outbox (no from_repo
# field), the overlay must fall back to the captured value rather than wiping it with
# None. Validates the `from_repo = outbox_rec.get("from_repo") or from_repo` fix.

def test_f1_overlay_dual_presence_from_repo_survives(tmp_path):
    tmp_f1 = tmp_path
    (tmp_f1 / "state").mkdir(parents=True)
    lessons_f1 = tmp_f1 / "state" / "lessons"
    lessons_f1.mkdir()
    outbox_f1 = tmp_f1 / "state" / "lessons-outbox"
    outbox_f1.mkdir()
    (outbox_f1 / "drained").mkdir()
    title_f1 = "Dual-presence lesson YAML has from_repo outbox lacks it"
    # Per-entry YAML: has from_repo="repo-a" — must survive overlay
    _write_outbox_yaml(
        lessons_f1 / "2026-06-30-dual-presence.yaml",
        {
            "title": title_f1,
            "body": "Body of dual-presence lesson.",
            "scope": "project",
            "created": "2026-06-30",
            "from_repo": "repo-a",
            "status": "open",
        },
    )
    # Outbox entry for the same lesson: deliberately omits from_repo (no field at all)
    _write_outbox_yaml(
        outbox_f1 / "2026-06-30-dual-presence-outbox.yaml",
        {
            "title": title_f1,
            "body": "Outbox body for dual-presence lesson.",
            "change_kind": "doctrine-edit",
            "scope_tags": ["project"],
            "created": "2026-06-30T09:00:00Z",
            # from_repo intentionally absent — overlay must fall back to captured "repo-a"
        },
    )
    result_f1 = subprocess.run(
        [
            sys.executable,
            str(_EMITTER),
            str(tmp_f1),
            "test-repo",
            "test-branch",
            "abc123",
            "2026-01-01T00:00:00Z",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        **_no_console_kw(),
    )
    assert result_f1.returncode == 0, f"F1-OVERLAY: emitter failed: {result_f1.stderr}"
    recs_f1 = json.loads(result_f1.stdout)
    key_f1 = _lesson_key(title_f1)
    by_key_f1 = {r["lesson_key"]: r for r in recs_f1}
    assert key_f1 in by_key_f1, f"F1-OVERLAY: dual-presence lesson not emitted (key={key_f1})"
    rec_f1 = by_key_f1[key_f1]
    assert rec_f1.get("from_repo") == "repo-a", (
        f"F1-OVERLAY: from_repo expected 'repo-a' (captured value), "
        f"got '{rec_f1.get('from_repo')}' — outbox overlay wiped it"
    )
    # promotion_state must be pending (outbox wins over YAML-only)
    assert rec_f1.get("promotion_state") == "pending", (
        f"F1-OVERLAY: expected promotion_state='pending', got '{rec_f1.get('promotion_state')}'"
    )


# ---------------------------------------------------------------------------
# AC1: born-attributable captured lesson (direct build_lesson_summaries import)
# ---------------------------------------------------------------------------
# Verifies that captured lessons with from_repo/created/evidence/target_wiki in their
# per-entry YAML emit those fields as non-null (the born-attributable null-drop fix).
# Uses importlib to import build_lesson_summaries from the hyphenated filename.

def _load_build_lesson_summaries():
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("emit_lesson_summaries", str(_EMITTER))
    _mod = _ilu.module_from_spec(_spec)
    # exec_module limitation: re-executes module-level side-effects (e.g. regex
    # compilation, sys.path mutation) on every call. Safe here because the module is
    # stateless at module-level, but would silently double-register any module-level
    # state. If this test is ever parallelised or the module gains module-level
    # side-effects, switch to the subprocess approach used by the other tests above.
    _spec.loader.exec_module(_mod)  # type: ignore[union-attr]
    return _mod.build_lesson_summaries


def test_ac1_born_attributable_captured_lesson(tmp_path):
    build_lesson_summaries = _load_build_lesson_summaries()

    tmp_ba = tmp_path
    (tmp_ba / "state").mkdir(parents=True)
    lessons_ba = tmp_ba / "state" / "lessons"
    lessons_ba.mkdir()
    # Write a captured lesson with ALL born-attributable fields: no outbox/drained copy.
    title_ba = "Born-attributable captured lesson with full metadata"
    body_ba = "This lesson body must survive unchanged through the emitter pipeline."
    _write_outbox_yaml(lessons_ba / "2026-06-30-born-attr.yaml", {
        "title": title_ba,
        "body": body_ba,
        "scope": "universal",
        "created": "2026-06-30T10:00:00Z",
        "from_repo": "claude-central-em",
        "evidence": "Observed in session 2026-06-30",
        "target_wiki": "docs/wiki/lessons-born-attributable.md",
        "status": "open",
    })
    records_ba = build_lesson_summaries(
        repo_root=tmp_ba,
        repo_name="test-repo",
        git_branch="test-branch",
        git_sha="abc123",
        observed_at="2026-06-30T10:00:00Z",
    )
    key_ba = _lesson_key(title_ba)
    by_key_ba = {r["lesson_key"]: r for r in records_ba}
    assert key_ba in by_key_ba, f"AC1: born-attributable captured lesson not in emitted records (key={key_ba})"
    rec_ba = by_key_ba[key_ba]
    # promotion_state must be "captured" (no outbox/drained copy)
    assert rec_ba.get("promotion_state") == "captured", (
        f"AC1: expected promotion_state='captured', got '{rec_ba.get('promotion_state')}'"
    )
    # parse_status must be "ok" (all fields present)
    assert rec_ba.get("parse_status") == "ok", (
        f"AC1: expected parse_status='ok', got '{rec_ba.get('parse_status')}'"
    )
    # body must equal the YAML body (unchanged)
    assert rec_ba.get("body") == body_ba, f"AC1: body changed — expected {body_ba!r}, got {rec_ba.get('body')!r}"
    # from_repo must be non-null and match YAML value
    assert rec_ba.get("from_repo") == "claude-central-em", (
        f"AC1: from_repo expected 'claude-central-em', got '{rec_ba.get('from_repo')}'"
    )
    # created must be non-null and match YAML value
    assert rec_ba.get("created") == "2026-06-30T10:00:00Z", (
        f"AC1: created expected '2026-06-30T10:00:00Z', got '{rec_ba.get('created')}'"
    )
    # evidence must be non-null and match YAML value
    assert rec_ba.get("evidence") == "Observed in session 2026-06-30", (
        f"AC1: evidence expected 'Observed in session 2026-06-30', got '{rec_ba.get('evidence')}'"
    )
    # target_wiki must be non-null and match YAML value
    assert rec_ba.get("target_wiki") == "docs/wiki/lessons-born-attributable.md", (
        f"AC1: target_wiki expected 'docs/wiki/lessons-born-attributable.md', "
        f"got '{rec_ba.get('target_wiki')}'"
    )
