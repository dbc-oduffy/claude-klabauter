"""doc_writes declares the delivery note path the sizing's writes names; the docs prompt directs writing it."""
import pytest

from coordinator_core.ops.dispatch_emit.ask_compose import _docs_prompt_parts
from coordinator_core.ops.dispatch_emit.ask_stage import doc_writes


def test_named_root_delivery_note_is_declared_after_run_note():
    out = doc_writes("r1", {"writes": ["src/a.py", "DELIVERY.md"]})
    assert out == ("docs/delivery/r1.md", "DELIVERY.md")


def test_non_delivery_writes_stay_undeclared():
    assert doc_writes("r1", {"writes": ["src/a.py", "docs/other.md"]}) == ("docs/delivery/r1.md",)
    assert doc_writes("r1", None) == ("docs/delivery/r1.md",)


def test_docs_prompt_directs_writing_named_delivery_note():
    text = "".join(_docs_prompt_parts(run_id="r1", manifest_rel="m.json"))
    assert "DELIVERY*.md" in text


@pytest.mark.parametrize("run_id", [
    "ask-20261124T135830-998c6d",
    "warp-2026-11-03-387be4-1-x-20261103T072122-bc1f3d",
    "run-1",
])
def test_verifier_names_the_delivery_note_declared_by_staging(run_id):
    note, = doc_writes(run_id, None)
    text = "".join(_docs_prompt_parts(run_id=run_id, manifest_rel="manifest.json"))
    assert note in text
