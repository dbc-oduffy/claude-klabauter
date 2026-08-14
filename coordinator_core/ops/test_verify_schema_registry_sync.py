"""Tests for coordinator_core.ops.verify_schema_registry_sync.

Port of: verify-schema-registry-sync.sh (DoE b5a4192c, 2026-07-20),
snapshotted 2026-07-16. Positive corpus = the DoE repo's live
schemas/ dir. Negative corpus = missing schemas dir.

`test_golden_oracle_parity_against_live_doe_repo` (the file's one
live-DoE-repo test) was frozen to a committed golden via
`coordinator_core.testing.golden` (2026-07-22 de-node Gate A, C5), then
re-frozen the same day for the false-positive fix documented in
verify_schema_registry_sync's module docstring: the port's own
`_LEGACY_TYPE_RENAMES` disagreed with query-records.js's real 5-entry rename
map, so bug-backlog/debt-backlog/improvement-queue were reported MISSING when
they in fact resolve to registered types (bug/debt/improvement). Single-
sourcing the rename map fixed it; the golden now freezes exit_code=0 / zero
MISSING entries. See that test's own docstring for the full capture/
ordinary-run contract. Every other test in this file is a pure
fixture-based unit test with no live-repo dependency.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import List, Optional

import pytest

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.ops import verify_schema_registry_sync as vsrs
from coordinator_core.testing.golden import assert_matches_golden, is_capturing, load_golden

_GOLDEN_NAMESPACE = "verify_schema_registry_sync"

# Corpus-size pin: a golden captured over a directory-derived corpus (the
# DoE-claude sibling's live schemas/ dir) silently covers less and less of
# that corpus as the directory shrinks, without the golden ever failing --
# the drift-entry content alone is not a high enough bar. Bumping this
# constant is a deliberate acknowledgment that the corpus changed; a
# recapture over a shrunken corpus must fail loud rather than silently
# narrow the suite. See cross-repo/archive/2026-07-22-claude-central-em-
# corrections-accepted-and-verify-schema-registry-golden-corpus-narrow.md.
_EXPECTED_CORPUS_SIZE = 48


def _write_schema(schemas_dir: Path, name: str, applies_to: str) -> None:
    schemas_dir.mkdir(parents=True, exist_ok=True)
    (schemas_dir / name).write_text(
        f"kind: {name[:-5]}\napplies_to: {applies_to}\n", encoding="utf-8"
    )


def test_schema_to_query_type_legacy_renames():
    assert vsrs._schema_to_query_type("completion-entry.yaml") == "completion"
    assert vsrs._schema_to_query_type("lesson-entry.yaml") == "lesson"
    assert vsrs._schema_to_query_type("handoff.yaml") == "handoff"


def test_extract_applies_to_strips_quotes_and_whitespace(tmp_path):
    schema = tmp_path / "foo.yaml"
    schema.write_text('applies_to: "state/foo/*.yaml"\n', encoding="utf-8")
    assert vsrs._extract_applies_to(schema) == "state/foo/*.yaml"


def test_extract_applies_to_missing_returns_none(tmp_path):
    schema = tmp_path / "foo.yaml"
    schema.write_text("kind: foo\n", encoding="utf-8")
    assert vsrs._extract_applies_to(schema) is None


def test_run_missing_schemas_dir(tmp_path):
    # No schemas/ subdir created under plugin_root.
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)
    assert exit_code == 1
    assert any("schemas dir not found" in line for line in stderr_lines)


def test_run_deliberate_divergence_exempted(tmp_path, monkeypatch):
    _write_schema(tmp_path / "schemas", "cross-repo-memo.yaml", "state/memos/[0-9]*.md")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    # Even an always-fail type check must not surface — the file is skipped
    # before _type_recognised is ever called.
    monkeypatch.setattr(vsrs, "_type_recognised", lambda *a, **kw: False)
    exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)
    assert exit_code == 0
    assert "0" in stdout_lines[0]


def test_run_all_recognised(tmp_path, monkeypatch):
    _write_schema(tmp_path / "schemas", "handoff.yaml", "state/handoffs/*.yaml")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(vsrs, "_type_recognised", lambda *a, **kw: True)
    exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)
    assert exit_code == 0
    assert stderr_lines == []
    assert "OK" in stdout_lines[0]


def test_run_missing_type_reported(tmp_path, monkeypatch):
    # widget-thing has no entry in _SCHEMA_NAME_TO_QUERY_TYPE, so its derived
    # query type is the bare stem "widget-thing" (unlike bug-backlog/
    # debt-backlog/improvement-queue, which ARE mapped and now correctly
    # resolve to bug/debt/improvement -- see the false-positive-fix note in
    # the module docstring).
    _write_schema(tmp_path / "schemas", "widget-thing.yaml", "state/widget-thing/*.yaml")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(vsrs, "_type_recognised", lambda *a, **kw: False)
    exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)
    assert exit_code == 1
    assert any("MISSING: widget-thing.yaml" in line for line in stderr_lines)
    assert any("type='widget-thing'" in line for line in stderr_lines)


def test_run_derived_type_not_in_registry_map_reported(tmp_path, monkeypatch):
    """AC6: proves the port genuinely checks the natively-derived registry
    map rather than a stub that always reports recognised. Simulates a
    registry-side exclusion -- a schema declares applies_to but its derived
    query type has no entry in build_type_to_glob()'s output -- by
    monkeypatching build_type_to_glob (the derivation INPUT) to a map
    missing the schema's type, while leaving _type_recognised (the CHECK
    itself) untouched. A port that always returns recognised, or that never
    actually consults the derived map, would pass this test wrongly; this
    proves it doesn't."""
    _write_schema(tmp_path / "schemas", "widget.yaml", "state/widget/*.yaml")
    monkeypatch.setattr(vsrs, "build_type_to_glob", lambda schemas_dir: {"unrelated": "x/*.yaml"})
    exit_code, stdout_lines, stderr_lines = vsrs.run(tmp_path)
    assert exit_code == 1
    assert any("MISSING: widget.yaml" in line for line in stderr_lines)
    assert any("type='widget'" in line for line in stderr_lines)


def test_main_returns_exit_code(tmp_path, monkeypatch, capsys):
    _write_schema(tmp_path / "schemas", "handoff.yaml", "state/handoffs/*.yaml")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(vsrs, "_type_recognised", lambda *a, **kw: True)
    rc = vsrs.main([str(tmp_path)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out


def test_resolve_plugin_root_delegates_to_data_root(tmp_path, monkeypatch):
    """_resolve_plugin_root() (the no-argv/standalone-invocation path) must
    resolve via coordinator_core.data_root.data_root("schemas") and return its
    PARENT -- the coordinator root run() expects as plugin_root. Regression
    guard for the split-repo fix: this used to fall back to Path.cwd(), which
    silently pointed at the wrong (or a merely-coincidental) directory once
    schemas/ moved to a DoE-resident split-repo layout."""
    schemas_dir = tmp_path / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True)
    monkeypatch.setattr(vsrs, "data_root", lambda name: schemas_dir)
    assert vsrs._resolve_plugin_root() == tmp_path / "coordinator"


def test_main_no_argv_reports_and_exits_1_on_resolution_failure(monkeypatch, capsys):
    """main() with no argv (standalone invocation) must catch a RuntimeError
    from _resolve_plugin_root() and report+exit 1 rather than letting the
    exception propagate uncaught or silently misreporting a schemas-dir-not-
    found FAIL from run() against the wrong path."""

    def _raise():
        raise RuntimeError("cannot resolve data dir 'schemas' (test)")

    monkeypatch.setattr(vsrs, "_resolve_plugin_root", _raise)
    rc = vsrs.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "schemas dir resolution failed" in captured.err
    assert "cannot resolve data dir 'schemas' (test)" in captured.err


def test_main_no_argv_success_path(tmp_path, monkeypatch, capsys):
    """main() with no argv, resolution succeeds -- run() is invoked against
    the resolved plugin_root exactly as the argv-provided path already is."""
    _write_schema(tmp_path / "schemas", "handoff.yaml", "state/handoffs/*.yaml")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "query-records.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(vsrs, "_type_recognised", lambda *a, **kw: True)
    monkeypatch.setattr(vsrs, "_resolve_plugin_root", lambda: tmp_path)
    rc = vsrs.main([])
    assert rc == 0
    captured = capsys.readouterr()
    assert "OK" in captured.out


def _find_doe_root() -> Optional[Path]:
    """Resolve the DoE-claude sibling repo's coordinator/ dir via the pointer-file
    mechanism (coordinator_core.doe_root_pointer, DR-072) -- the established
    sibling-resolution convention used across claude-klabauter's other suites, not an
    author-machine hardcoded path.

    Only ever consulted during an explicit CAPTURE_GOLDENS=1 recapture -- never on
    an ordinary test run (see the test's own docstring). Returns None (never
    raises) if the pointer is absent/empty or the resolved directory doesn't
    exist; the caller raises loudly in that case since a recapture with no
    oracle to capture from is a hard user error, not a skip.
    """
    pointer = read_doe_root_pointer()
    if not pointer:
        return None
    coordinator_dir = Path(pointer) / "coordinator"
    return coordinator_dir if coordinator_dir.is_dir() else None


def _normalize_drift_output(stderr_lines: List[str], doe_root: Path) -> List[str]:
    """Strip the doe_root absolute path (if it appears in any line) before
    freezing/comparing -- a run-to-run/machine-to-machine unique value must never
    be baked into a committed golden verbatim (see module docstring hazard note
    in the parent conversion plan)."""
    root_str = str(doe_root)
    return [line.replace(root_str, "<DOE_ROOT>") for line in stderr_lines]


@pytest.mark.real_home
def test_golden_oracle_parity_against_live_doe_repo():
    """Freezes the CORRECTED verdict (2026-07-22 de-node port + false-positive
    fix): the port's own `_LEGACY_TYPE_RENAMES` used to carry only 2 of the
    registry's real 5 renames (completion-entry, lesson-entry), so
    bug-backlog/debt-backlog/improvement-queue schemas derived un-renamed
    types (bug-backlog/debt-backlog/improvement-queue) that
    query-records.js's registry does not carry under those names -- it
    carries bug/debt/improvement (see
    `coordinator_core.frontmatter.schema_validate._SCHEMA_NAME_TO_QUERY_TYPE`).
    That was two rename maps disagreeing, not real drift: this port
    single-sources on the shared map, so all three now derive their correct
    registry type and PASS. The golden below freezes exit_code=0 / zero
    MISSING entries against the live DoE-claude schemas/ corpus -- the 3
    old MISSING entries were rename-map false positives, not a real gap.

    Frozen to a committed golden (2026-07-22 de-node Gate A, C5; re-frozen
    same day for the false-positive fix above): the golden was captured ONCE
    from a live `vsrs.run()` against the DoE-claude sibling checkout and
    committed under `coordinator_core/ops/_goldens/verify_schema_registry_sync/`.
    Ordinary runs load that golden and assert its frozen content -- no live
    DoE-claude checkout needed at test time. `vsrs.run()` itself is now fully
    native (no node subprocess at all, post de-node port), so recapture no
    longer needs `node` on PATH either -- only the DoE-claude sibling
    checkout, to read its live schemas/ dir as the recapture input. Regenerate
    deliberately via:
        CAPTURE_GOLDENS=1 python3 -m pytest \
            coordinator_core/ops/test_verify_schema_registry_sync.py -q
    (requires the DoE-claude sibling checkout to be resolvable via the
    .doe-root pointer file -- see coordinator_core.doe_root_pointer -- not
    needed for an ordinary run).

    Negative-spec: does NOT `pytest.skip` when the live DoE-claude repo is
    unavailable -- that was the exact silent-green hazard this conversion
    closes (see docs/plans/2026-07-21-parity-suites-freeze-to-goldens.md).

    Carries `@pytest.mark.real_home` (see `coordinator_core/conftest.py`'s
    `_quarantine_real_home` docstring): `_find_doe_root()` resolves the
    `.doe-root` pointer file under the real HOME, which the suite-root autouse
    fixture otherwise redirects to a per-test throwaway dir. This is the
    documented read-only-oracle opt-out, exercised only on the
    `is_capturing()` branch -- the ordinary (golden-load) path touches neither
    HOME nor the live repo.
    """
    if is_capturing():
        doe_root = _find_doe_root()
        if doe_root is None:
            raise RuntimeError(
                "CAPTURE_GOLDENS=1 recapture requires the DoE-claude sibling "
                "checkout (resolvable via the .doe-root pointer file) to be "
                "present -- not needed for an ordinary (non-capture) run."
            )
        exit_code, stdout_lines, stderr_lines = vsrs.run(doe_root)
        payload = {
            "exit_code": exit_code,
            "stdout_lines": stdout_lines,
            "stderr_lines": _normalize_drift_output(stderr_lines, doe_root),
            "schemas_checked": len(vsrs._schemas_with_applies_to(doe_root / "schemas")),
        }
        assert_matches_golden(json.dumps(payload), _GOLDEN_NAMESPACE, "drift_entries", kind="json")
        expected = payload
    else:
        expected = load_golden(_GOLDEN_NAMESPACE, "drift_entries", kind="json")

    assert expected["exit_code"] == 0
    assert expected["stderr_lines"] == []
    joined = "\n".join(expected["stdout_lines"])
    assert "OK" in joined
    # Corpus-size pin (see _EXPECTED_CORPUS_SIZE docstring): guards against a
    # recapture over a silently-narrowed schemas/ dir producing a
    # byte-identical drift-entry golden while covering far fewer schemas.
    assert expected["schemas_checked"] == _EXPECTED_CORPUS_SIZE
