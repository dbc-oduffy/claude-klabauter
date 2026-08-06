"""
test_coordinator_queue_append_workstream_end_to_end.py — AC7 regression oracle.

AC7 (docs/plans/2026-07-30-workstream-store-writer-and-parser.md, example-doctrine-repo): "A
workstream minted end-to-end through the sanctioned writer alone renders with its
deliverables, specs, and dependency_annotations — no hand-authoring step."

The writer side (--deliverables/--specs/--dependency-annotations emission) and the
render side (render_project_tracker displaying those fields) were each pinned in
isolation — test_coordinator_queue_append_workstream_deliverables.py for the former,
test_render_project_tracker.py for the latter — but nothing drove both in one pass.
This closes that gap: mint via the real CLI entrypoint (no hand-written definition
YAML anywhere in this file), then feed the resulting store straight into
render_project_tracker.render() and assert all three fields survive into the
rendered body.

Mirrors: test_coordinator_queue_append_workstream_deliverables.py (same
importlib.machinery.SourceFileLoader idiom for the extensionless CLI script, same
QUEUE_APPEND_OUTPUT_ROOT test-isolation env var).

Spec backlink: docs/plans/2026-07-30-workstream-store-writer-and-parser.md § AC7
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import render_project_tracker as rpt

pytestmark = pytest.mark.cadence

_QUEUE_APPEND_SCRIPT = Path(__file__).resolve().parent / "coordinator-queue-append"

# The coordinator_root_path the writer stamps and the renderer filters on — must
# match on both sides of the writer/render seam, same convention as
# test_render_project_tracker.py's _FAKE_ROOT (a value that plainly isn't a real
# repo, so a stray auto-resolve-from-cwd bug would surface as a mismatch, not a
# silent pass).
_ROOT = "/fake/coordinator/root"


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

    QUEUE_APPEND_OUTPUT_ROOT redirects the output root to an isolated tmp_path AND
    forces the legacy write path (the native op does not honour the override) —
    see the CLI's own "Test isolation gate" comment.
    """
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["coordinator-queue-append"] + argv)
    _mod.main()


def test_workstream_minted_via_sanctioned_writer_alone_renders_all_three_fields(
    monkeypatch, tmp_path
):
    workstream_id = "wks-ac7-end-to-end"
    # Both a comma AND a colon in one deliverable — the exact defect shape (flow-map
    # special-casing on commas/colons) this whole plan exists to fix.
    comma_colon_text = "Run /example-game-repo:doctor, then verify: the sidecar boots"
    plain_text = "Ship the AC7 closure test"
    spec_text = "docs/plans/2026-07-30-workstream-store-writer-and-parser.md"
    dep_note = "blocked by nothing — this is the end-to-end closure itself"

    _run_cli(
        monkeypatch, tmp_path,
        [
            "--schema", "workstream",
            "--title", "AC7 End-to-End Workstream",
            "--workstream-id", workstream_id,
            "--created", "2026-07-31",
            "--coordinator-root-path", _ROOT,
            "--deliverables", comma_colon_text,
            "--deliverables", plain_text,
            "--specs", spec_text,
            "--dependency-annotations", dep_note,
        ],
    )

    # No hand-authoring step anywhere above or below this line — the definition
    # file on disk is entirely the sanctioned writer's own emission.
    definition_path = tmp_path / "state" / "workstreams" / f"{workstream_id}.yaml"
    assert definition_path.is_file(), (
        "the writer did not create a definition file at the expected store path "
        f"({definition_path}) — nothing to render"
    )

    rendered = rpt.render(str(tmp_path), _ROOT, render_date="2026-07-31")

    assert comma_colon_text in rendered, (
        "the comma/colon-bearing deliverable did not survive the writer->render "
        f"round trip verbatim; rendered body:\n{rendered}"
    )
    assert plain_text in rendered
    assert spec_text in rendered, "the spec link did not survive into the rendered body"
    assert dep_note in rendered, "the dependency annotation did not survive into the rendered body"
