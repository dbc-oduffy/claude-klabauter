"""Sizing schema 1.24.0: the optional `baton` reverse edge.

Pins the pattern-bound string-or-null shape and that no record valid under
the prior schema (git HEAD) newly fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import validate_frontmatter

_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "sizing-object.schema.json"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SIZINGS_DIR = _REPO_ROOT / "state" / "sizings"


def _sizing(**overrides) -> dict:
    fm = {
        "schema": "sizing-object",
        "intent": "Example PM ask, verbatim.",
        "estimate": {"tshirt": "M", "provisional": True},
        "route": "plan",
        "detents": [],
        "fork": None,
        "xl_exit": None,
        "status": "routed",
        "premise": {"provenance": "unrecorded"},
    }
    fm.update(overrides)
    return fm


def _baton_errors(fm: dict) -> list:
    return [e for e in validate_frontmatter(fm, _SCHEMA) if "baton" in str(e.get("field", ""))]


def test_schema_declares_baton_at_1_24_0():
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    assert schema["x-schema-version"] == "1.24.0"
    assert "baton" in schema["properties"]


@pytest.mark.parametrize("baton", ["state/handoffs/x.md", None])
def test_valid_baton_values(baton):
    assert not _baton_errors(_sizing(baton=baton))


def test_absent_baton_is_valid():
    assert not _baton_errors(_sizing())


@pytest.mark.parametrize("baton", ["docs/plans/x.md", "state/handoffs/x.yaml", 7])
def test_invalid_baton_values_are_rejected(baton):
    assert _baton_errors(_sizing(baton=baton))


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_new_corpus_failures_under_1_24_0():
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    files = sorted(_SIZINGS_DIR.glob("*.yaml")) if _SIZINGS_DIR.is_dir() else []
    if not files:
        pytest.skip("no state/sizings/*.yaml records found")
    old = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "show",
         "HEAD:coordinator_core/frontmatter/schemas/sizing-object.schema.json"],
        capture_output=True, text=True, timeout=30, **no_console_creationflags(),
    )
    if old.returncode != 0:
        pytest.skip("could not read the pre-bump schema from git HEAD")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".schema.json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(old.stdout)
        old_path = Path(tmp.name)
    try:
        regressed = []
        for f in files:
            fm = yaml.safe_load(f.read_text(encoding="utf-8"))
            if not isinstance(fm, dict):
                continue
            if not validate_frontmatter(fm, old_path) and validate_frontmatter(fm, _SCHEMA):
                regressed.append(f.name)
        assert not regressed, regressed
    finally:
        old_path.unlink(missing_ok=True)
