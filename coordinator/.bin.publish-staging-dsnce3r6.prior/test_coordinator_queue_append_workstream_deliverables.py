"""
test_coordinator_queue_append_workstream_deliverables.py — C2 regression oracle for
`coordinator-queue-append --schema workstream`'s `--deliverables` / `--specs` /
`--dependency-annotations` flags.

Purpose: pin the block-map emission form workstream.schema.json requires for
`deliverables` (`- text: "..."`, never the inline flow-map `- {text: "..."}`),
prove comma/colon-bearing deliverable text round-trips through the vendored
schema validator and `schema_validate.parse_yaml`, and pin the newline-in-item
rejection contract for all three flags.

Spec backlink: DoE-claude:pln-workstream-store-make-the-sanc-546afa § C2

Mirrors: coordinator/tests/test_workstream_store_collision.py (same
importlib.machinery.SourceFileLoader idiom for the extensionless CLI script,
same QUEUE_APPEND_OUTPUT_ROOT test-isolation + legacy-path-forcing env var).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate

pytestmark = pytest.mark.cadence

_QUEUE_APPEND_SCRIPT = Path(__file__).resolve().parent / "coordinator-queue-append.py"


def _load_queue_append():
    """Load coordinator-queue-append (no .py extension) as a Python module."""
    loader = importlib.machinery.SourceFileLoader("coordinator_queue_append", str(_QUEUE_APPEND_SCRIPT))
    spec = importlib.util.spec_from_loader("coordinator_queue_append", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_mod = _load_queue_append()


def _run_cli(monkeypatch, tmp_path, argv):
    """Invoke coordinator-queue-append's main() with argv patched and legacy forced.

    QUEUE_APPEND_OUTPUT_ROOT redirects the output root to an isolated tmp_path
    AND forces the legacy write path (the native op does not honour the
    override) — see the CLI's own "Test isolation gate" comment.
    """
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["coordinator-queue-append"] + argv)
    _mod.main()


def _definition_path(tmp_path, workstream_id: str) -> Path:
    return tmp_path / "state" / "workstreams" / f"{workstream_id}.yaml"


class TestDeliverablesBlockMapRoundTrip:
    def test_comma_and_colon_bearing_deliverables_validate_and_round_trip(self, monkeypatch, tmp_path):
        workstream_id = "wks-c2-deliverables"
        comma_text = "Ship A, B, and C together"
        colon_text = "Run /example-game-repo:doctor before merging"

        _run_cli(
            monkeypatch, tmp_path,
            [
                "--schema", "workstream",
                "--title", "C2 Deliverables Workstream",
                "--workstream-id", workstream_id,
                "--created", "2026-07-30",
                "--deliverables", comma_text,
                "--deliverables", colon_text,
            ],
        )

        raw = _definition_path(tmp_path, workstream_id).read_text(encoding="utf-8")

        # Negative-spec: the block-map form only, never the inline flow-map form.
        assert "- {text:" not in raw, (
            "deliverables must emit as block-map items (`- text: \"...\"`), "
            "not the inline flow-map form (`- {text: \"...\"}`)"
        )
        assert "- text:" in raw

        parsed = schema_validate.parse_yaml(raw)
        assert parsed["deliverables"] == [
            {"text": comma_text},
            {"text": colon_text},
        ], f"deliverables did not round-trip through parse_yaml: {parsed.get('deliverables')!r}"

        result = schema_validate.validate("workstream", parsed)
        assert result.get("ok") is True, (
            f"emitted workstream record failed schema.validate: {result.get('errors')!r}"
        )

    def test_specs_round_trip_as_plain_strings(self, monkeypatch, tmp_path):
        workstream_id = "wks-c2-specs"
        spec_text = "docs/plans/2026-07-30-workstream-store-writer-and-parser.md"

        _run_cli(
            monkeypatch, tmp_path,
            [
                "--schema", "workstream",
                "--title", "C2 Specs Workstream",
                "--workstream-id", workstream_id,
                "--created", "2026-07-30",
                "--specs", spec_text,
            ],
        )

        raw = _definition_path(tmp_path, workstream_id).read_text(encoding="utf-8")
        parsed = schema_validate.parse_yaml(raw)
        assert parsed["specs"] == [spec_text]

        result = schema_validate.validate("workstream", parsed)
        assert result.get("ok") is True, (
            f"emitted workstream record failed schema.validate: {result.get('errors')!r}"
        )


class TestDependencyAnnotationsRoundTrip:
    def test_dependency_annotations_round_trip(self, monkeypatch, tmp_path):
        workstream_id = "wks-c2-deps"
        note_a = "blocked by example-game-repo's flow-map migration"
        note_b = "depends on: the C1 renderer swap"

        _run_cli(
            monkeypatch, tmp_path,
            [
                "--schema", "workstream",
                "--title", "C2 Dependency Annotations Workstream",
                "--workstream-id", workstream_id,
                "--created", "2026-07-30",
                "--dependency-annotations", note_a,
                "--dependency-annotations", note_b,
            ],
        )

        raw = _definition_path(tmp_path, workstream_id).read_text(encoding="utf-8")
        parsed = schema_validate.parse_yaml(raw)
        assert parsed["dependency_annotations"] == [note_a, note_b], (
            f"dependency_annotations did not round-trip: {parsed.get('dependency_annotations')!r}"
        )

        result = schema_validate.validate("workstream", parsed)
        assert result.get("ok") is True, (
            f"emitted workstream record failed schema.validate: {result.get('errors')!r}"
        )


class TestNewlineRejection:
    """A newline inside a --deliverables/--specs/--dependency-annotations item is a
    named-error rejection at write time, never an emitted unparseable block scalar."""

    @pytest.mark.parametrize("flag", ["--deliverables", "--specs", "--dependency-annotations"])
    def test_embedded_newline_is_rejected_with_named_error(self, monkeypatch, tmp_path, capsys, flag):
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setattr(
            sys, "argv",
            [
                "coordinator-queue-append",
                "--schema", "workstream",
                "--title", "C2 Newline Rejection Workstream",
                "--workstream-id", "wks-c2-newline",
                "--created", "2026-07-30",
                flag, "line one\nline two",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            _mod.main()
        assert exc_info.value.code != 0

        stderr = capsys.readouterr().err
        assert flag in stderr, f"expected the offending flag {flag!r} named in the error, got: {stderr!r}"
        assert "newline" in stderr.lower(), f"expected a named newline-rejection error, got: {stderr!r}"

        assert not _definition_path(tmp_path, "wks-c2-newline").exists(), (
            "a newline-bearing item must be rejected before any write — found a "
            "definition file on disk despite the rejection"
        )
