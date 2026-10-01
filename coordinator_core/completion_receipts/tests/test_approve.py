"""receipt.approve: superseding human verdict receipts and their refusals."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.completion_receipts.approve import ApproveRefusal, approve
from coordinator_core.completion_receipts.model import validate
from coordinator_core.completion_receipts.store import read_receipts, write_receipt
from coordinator_core.completion_receipts.tests.test_model_and_store import _JUDGE, _fm

QUOTE = "ship it, looks good"
REF = "state/handoff.md"


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "state").mkdir()
    (tmp_path / REF).write_text("PM said:\n  ship it,\nlooks   good\nthanks\n", encoding="utf-8")
    write_receipt(tmp_path, _fm(verdict="agent-delivered", judge=_JUDGE), "agent prose")
    return tmp_path


def _target(root: Path) -> tuple[str, bytes]:
    r = read_receipts(root)[0]
    return r["receipt_id"], (root / r["_path"]).read_bytes()


def _new(root: Path, old_id: str) -> dict:
    return next(r for r in read_receipts(root) if r["receipt_id"] != old_id)


@pytest.mark.parametrize("verdict", ["human-approved", "rejected"])
def test_writes_superseding_receipt(root, verdict):
    rid, before = _target(root)
    rel = approve(root, rid, verdict, "pm", QUOTE, REF)
    new = _new(root, rid)
    fm = {k: v for k, v in new.items() if k != "_path"}
    assert validate(fm) == []
    assert new["_path"] == rel
    assert new["verdict"] == verdict and new["supersedes"] == rid and new["approved_by"] == "pm"
    body = (root / rel).read_text(encoding="utf-8")
    assert "## Approval" in body and f"> {QUOTE}" in body and REF in body
    assert yaml.safe_load(body.split("---")[1])["provenance"]["derivation"].startswith("receipt.approve")
    assert (root / _path_of(root, rid)).read_bytes() == before


def _path_of(root: Path, rid: str) -> str:
    return next(r for r in read_receipts(root) if r["receipt_id"] == rid)["_path"]


def test_refuses_superseded_target(root):
    rid, before = _target(root)
    approve(root, rid, "human-approved", "pm", QUOTE, REF)
    with pytest.raises(ApproveRefusal):
        approve(root, rid, "rejected", "pm", QUOTE, REF)
    assert (root / _path_of(root, rid)).read_bytes() == before


def test_refuses_empty_quote(root):
    rid, _ = _target(root)
    with pytest.raises(ApproveRefusal):
        approve(root, rid, "human-approved", "pm", "  ", REF)


def test_refuses_absent_quote(root):
    rid, _ = _target(root)
    with pytest.raises(ApproveRefusal):
        approve(root, rid, "human-approved", "pm", "never said this", REF)
    assert len(read_receipts(root)) == 1


def test_refuses_uncontained_ref(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("out") / "q.md"
    outside.write_text(QUOTE, encoding="utf-8")
    rid, _ = _target(root)
    with pytest.raises(ApproveRefusal):
        approve(root, rid, "human-approved", "pm", QUOTE, str(outside))
    with pytest.raises(ApproveRefusal):
        approve(root, rid, "human-approved", "pm", QUOTE, "../" + outside.name)
    assert len(read_receipts(root)) == 1
