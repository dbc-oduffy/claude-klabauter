"""
coordinator_core.ops.tests.test_initiatives_store

Schema-conformance tests for the ``state/initiatives/`` on-disk store (C1).

Coverage:
  (a) production_store   — every seeded file in the live state/initiatives/ dir parses
                           cleanly and satisfies the required-field + enum constraints
  (b) required_fields    — id and label are present and non-empty strings
  (c) target_date        — is either None (burn-down shape) or a valid ISO YYYY-MM-DD string
  (d) status_enum        — status is in canonical-4 {active, paused, shipped, abandoned}
  (e) gate_b_constraint  — ALL currently seeded files use status: active (Gate-B constraint;
                           non-active is reserved until coordinator-claude InitiativeStatus source widen lands)
  (f) id_filename_match  — id field matches the YAML filename stem
  (g) fixture_roundtrip  — real on-disk YAML fixture files (in tmp_path) parse correctly via
                           _simple_yaml_load (lesson: test-fidelity-seed-fixtures-in-the-real)
  (h) optional_fields    — status_reason and description are absent or string (never crash)
  (i) catch_all_present  — the catch-all burn-down bucket is present and has target_date: null

Spec backlink: pln-claude-klabauter-served-initiative-roadm-8e0492 § C1
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from coordinator_core.ops.emit.sections.initiatives import _simple_yaml_load

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Canonical-4 status values; DR-207 D2 amended 2026-07-05.
_CANONICAL_4 = frozenset({"active", "paused", "shipped", "abandoned"})

# ISO date pattern — YYYY-MM-DD
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Required YAML fields in every initiative file.
_REQUIRED_FIELDS = ("id", "label", "status", "target_date")

# ---------------------------------------------------------------------------
# Locate the production state/initiatives/ directory
# ---------------------------------------------------------------------------

# Navigate from coordinator_core/ops/tests/ → coordinator_core/ → repo root.
_REPO_ROOT = Path(__file__).parent.parent.parent.parent.resolve()
_INITIATIVES_DIR = _REPO_ROOT / "state" / "initiatives"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_initiative_file(path: Path) -> dict:
    """Parse a single flat initiative YAML file via _simple_yaml_load.

    Returns the parsed dict.  The caller is responsible for assertions.
    """
    content = path.read_text(encoding="utf-8")
    return _simple_yaml_load(content)


def _collect_seeded_yaml() -> list[Path]:
    """Return sorted list of *.yaml paths from the production initiatives dir."""
    if not _INITIATIVES_DIR.is_dir():
        return []
    return sorted(_INITIATIVES_DIR.glob("*.yaml"))


# ---------------------------------------------------------------------------
# (a) Production store — existence + non-empty
# ---------------------------------------------------------------------------


def test_initiatives_dir_exists() -> None:
    """state/initiatives/ directory must exist (created by C1)."""
    assert _INITIATIVES_DIR.is_dir(), (
        f"state/initiatives/ not found at {_INITIATIVES_DIR} — "
        "C1 seed step may not have run"
    )


def test_seeded_files_present() -> None:
    """At minimum six files must be seeded: five named epics + catch-all bucket."""
    files = _collect_seeded_yaml()
    assert len(files) >= 6, (
        f"Expected ≥6 seeded initiative files, found {len(files)}: "
        f"{[f.name for f in files]}"
    )


# ---------------------------------------------------------------------------
# (b)–(h) Per-file schema conformance — parametrised over seeded files
# ---------------------------------------------------------------------------


@pytest.fixture(params=_collect_seeded_yaml(), ids=lambda p: p.stem)
def initiative_file(request) -> tuple[Path, dict]:
    """Fixture: (path, parsed_dict) for each seeded initiative YAML file."""
    path: Path = request.param
    data = _load_initiative_file(path)
    return path, data


def test_required_fields_present(initiative_file: tuple[Path, dict]) -> None:
    """(b) Every seeded file must carry id, label, status, and target_date."""
    path, data = initiative_file
    for field in _REQUIRED_FIELDS:
        assert field in data, (
            f"{path.name}: required field {field!r} is absent"
        )


def test_id_is_non_empty_string(initiative_file: tuple[Path, dict]) -> None:
    """(b) id must be a non-empty string."""
    path, data = initiative_file
    assert isinstance(data.get("id"), str) and data["id"], (
        f"{path.name}: 'id' must be a non-empty string, got {data.get('id')!r}"
    )


def test_label_is_non_empty_string(initiative_file: tuple[Path, dict]) -> None:
    """(b) label must be a non-empty string."""
    path, data = initiative_file
    assert isinstance(data.get("label"), str) and data["label"], (
        f"{path.name}: 'label' must be a non-empty string, got {data.get('label')!r}"
    )


def test_target_date_nullable(initiative_file: tuple[Path, dict]) -> None:
    """(c) target_date must be None (burn-down) or a valid ISO YYYY-MM-DD string."""
    path, data = initiative_file
    td = data.get("target_date")
    if td is None:
        return  # burn-down shape — valid
    assert isinstance(td, str) and _ISO_DATE_RE.match(td), (
        f"{path.name}: target_date must be null or YYYY-MM-DD, got {td!r}"
    )


def test_status_in_canonical_4(initiative_file: tuple[Path, dict]) -> None:
    """(d) status must be one of the canonical-4 values."""
    path, data = initiative_file
    status = data.get("status")
    assert status in _CANONICAL_4, (
        f"{path.name}: status {status!r} not in canonical-4 {sorted(_CANONICAL_4)}"
    )


def test_gate_b_constraint_status_active(initiative_file: tuple[Path, dict]) -> None:
    """(e) Gate-B constraint: ALL currently seeded files must use status: active.

    Non-active statuses (paused, shipped, abandoned) are reserved until the coordinator-claude
    InitiativeStatus source widen vendors in claude-klabauter (Gate B).  Setting a non-active
    status before Gate B produces cockpit records the vendored Zod validator rejects.
    See SCHEMA.md § Gate B constraint.
    """
    path, data = initiative_file
    status = data.get("status")
    assert status == "active", (
        f"{path.name}: Gate-B constraint violated — status must be 'active' until "
        f"coordinator-claude InitiativeStatus source widen lands, found {status!r}"
    )


def test_id_matches_filename_stem(initiative_file: tuple[Path, dict]) -> None:
    """(f) The id field must match the YAML filename stem."""
    path, data = initiative_file
    assert data.get("id") == path.stem, (
        f"{path.name}: id {data.get('id')!r} does not match filename stem {path.stem!r}"
    )


def test_optional_fields_string_or_absent(initiative_file: tuple[Path, dict]) -> None:
    """(h) Optional fields status_reason and description are absent, None, or string."""
    path, data = initiative_file
    for field in ("status_reason", "description"):
        val = data.get(field)
        assert val is None or isinstance(val, str), (
            f"{path.name}: optional field {field!r} must be absent, null, or string; "
            f"got {type(val).__name__} {val!r}"
        )


# ---------------------------------------------------------------------------
# (i) Catch-all bucket — specific assertions
# ---------------------------------------------------------------------------


def test_catch_all_present() -> None:
    """(i) The catch-all burn-down bucket must be seeded and have target_date: null."""
    catch_all = _INITIATIVES_DIR / "catch-all.yaml"
    assert catch_all.exists(), (
        "catch-all.yaml missing from state/initiatives/ — "
        "DR-209 requires exactly one catch-all burn-down bucket per repo"
    )
    data = _load_initiative_file(catch_all)
    assert data.get("id") == "catch-all", (
        f"catch-all.yaml: id must be 'catch-all', got {data.get('id')!r}"
    )
    assert data.get("target_date") is None, (
        "catch-all.yaml: target_date must be null (burn-down shape — DR-209 rejects period labels)"
    )
    assert data.get("status") == "active", (
        "catch-all.yaml: status must be active (Gate-B constraint)"
    )


# ---------------------------------------------------------------------------
# (g) Fixture roundtrip — real on-disk YAML files in tmp_path
# ---------------------------------------------------------------------------
# Lesson: test-fidelity-seed-fixtures-in-the-real — do not inject bare in-process
# dicts; write actual .yaml files to disk and parse them to confirm the parse path.


def test_fixture_roundtrip_minimal(tmp_path: Path) -> None:
    """(g) A minimal initiative YAML file written to disk parses via _simple_yaml_load."""
    fpath = tmp_path / "python-core.yaml"
    fpath.write_text(
        "id: python-core\nlabel: Python core\nstatus: active\ntarget_date: null\n",
        encoding="utf-8",
    )
    data = _load_initiative_file(fpath)
    assert data["id"] == "python-core"
    assert data["label"] == "Python core"
    assert data["status"] == "active"
    assert data["target_date"] is None


def test_fixture_roundtrip_with_date(tmp_path: Path) -> None:
    """(g) A completion-shape initiative (target_date set) parses correctly."""
    fpath = tmp_path / "some-completion.yaml"
    fpath.write_text(
        "id: some-completion\nlabel: Some completion\nstatus: active\ntarget_date: 2026-12-31\n",
        encoding="utf-8",
    )
    data = _load_initiative_file(fpath)
    assert data["target_date"] == "2026-12-31"
    assert _ISO_DATE_RE.match(data["target_date"])


def test_fixture_roundtrip_optional_fields(tmp_path: Path) -> None:
    """(g) Optional fields (owner, description, status_reason) round-trip without crashing."""
    fpath = tmp_path / "full-example.yaml"
    fpath.write_text(
        (
            "id: full-example\n"
            "label: Full example\n"
            "owner: test-em\n"
            "status: active\n"
            "target_date: null\n"
            "description: A complete fixture file with all optional fields present.\n"
            "status_reason: null\n"
        ),
        encoding="utf-8",
    )
    data = _load_initiative_file(fpath)
    assert data["id"] == "full-example"
    assert data["owner"] == "test-em"
    assert data["description"] == "A complete fixture file with all optional fields present."
    assert data["status_reason"] is None


def test_fixture_roundtrip_all_canonical_4(tmp_path: Path) -> None:
    """(g) Each canonical-4 status value is accepted by the schema enum check.

    NOTE: This confirms the parser accepts all four values.  The Gate-B constraint
    (test_gate_b_constraint_status_active) separately enforces that the production
    seeded files ONLY use 'active' until Gate B lands.  These are different concerns:
    the schema admits canonical-4; the gate constrains the current production set.
    """
    for status in _CANONICAL_4:
        fpath = tmp_path / f"test-{status}.yaml"
        fpath.write_text(
            f"id: test-{status}\nlabel: Test {status}\nstatus: {status}\ntarget_date: null\n",
            encoding="utf-8",
        )
        data = _load_initiative_file(fpath)
        assert data["status"] in _CANONICAL_4, (
            f"Status {status!r} parsed as {data['status']!r} — not in canonical-4"
        )
