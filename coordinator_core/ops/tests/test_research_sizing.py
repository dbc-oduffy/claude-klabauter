"""drop_research_block: removes a sizing's research block under the lock, schema-validated."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops._research_sizing import ResearchSizingRefused, drop_research_block

_HEAD = (
    "schema: sizing-object\n"
    "intent: an intent\n"
    "estimate:\n"
    "  tshirt: M\n"
    "route: plan\n"
    "detents: []\n"
    "fork: null\n"
    "xl_exit: null\n"
    "status: routed\n"
    "premise:\n"
    "  provenance: read\n"
    "  evidence: looked\n"
)
_RESEARCH = (
    "research:\n"
    "  value_class: decision\n"
    "  appetite: S\n"
    "  sources:\n"
    "  - web\n"
)
_TAIL = "plan: docs/plans/x.md\n"


@pytest.fixture(autouse=True)
def _repo_marker(tmp_path):
    (tmp_path / ".git").mkdir()


def _sizing(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def test_the_block_is_removed_and_the_prior_bytes_returned(tmp_path):
    before = _HEAD + _RESEARCH + _TAIL
    p = _sizing(tmp_path, "state/sizings/a.yaml", before)
    got = drop_research_block(tmp_path, "state/sizings/a.yaml")
    assert got == before.encode("utf-8")
    after = p.read_text(encoding="utf-8")
    assert after == _HEAD + _TAIL
    doc = yaml.safe_load(after)
    assert "research" not in doc
    assert doc == {k: v for k, v in yaml.safe_load(before).items() if k != "research"}


def test_an_absent_block_returns_none_and_writes_nothing(tmp_path):
    p = _sizing(tmp_path, "state/sizings/a.yaml", _HEAD + _TAIL)
    mtime = p.stat().st_mtime_ns
    assert drop_research_block(tmp_path, "state/sizings/a.yaml") is None
    assert p.read_bytes() == (_HEAD + _TAIL).encode("utf-8")
    assert p.stat().st_mtime_ns == mtime


def test_an_archived_sizing_resolves(tmp_path):
    p = _sizing(tmp_path, "archive/sizings/2026/a.yaml", _HEAD + _RESEARCH + _TAIL)
    assert drop_research_block(tmp_path, "state/sizings/a.yaml") is not None
    assert "research" not in yaml.safe_load(p.read_text(encoding="utf-8"))


def test_a_path_outside_the_sizings_home_is_refused(tmp_path):
    p = _sizing(tmp_path, "docs/x.yaml", _HEAD + _RESEARCH)
    with pytest.raises(ResearchSizingRefused):
        drop_research_block(tmp_path, "docs/x.yaml")
    with pytest.raises(ResearchSizingRefused):
        drop_research_block(tmp_path, "state/sizings/../../docs/x.yaml")
    assert p.read_bytes() == (_HEAD + _RESEARCH).encode("utf-8")


def test_a_missing_sizing_is_refused(tmp_path):
    with pytest.raises(ResearchSizingRefused):
        drop_research_block(tmp_path, "state/sizings/nope.yaml")


def test_crlf_is_preserved(tmp_path):
    before = (_HEAD + _RESEARCH + _TAIL).replace("\n", "\r\n")
    p = _sizing(tmp_path, "state/sizings/a.yaml", before)
    assert drop_research_block(tmp_path, "state/sizings/a.yaml") == before.encode("utf-8")
    assert p.read_bytes() == (_HEAD + _TAIL).replace("\n", "\r\n").encode("utf-8")


def test_a_legacy_invalid_sizing_still_drops(tmp_path):
    p = _sizing(tmp_path, "state/sizings/a.yaml", "size: S\n" + _RESEARCH)
    assert drop_research_block(tmp_path, "state/sizings/a.yaml") is not None
    assert p.read_text(encoding="utf-8") == "size: S\n"


def test_a_removal_that_introduces_a_schema_error_is_refused(tmp_path, monkeypatch):
    import coordinator_core.ops._research_sizing as mod

    calls = iter([{("research", "boom")}, set()])
    monkeypatch.setattr(mod, "_error_keys", lambda _t: next(calls))
    before = _HEAD + _RESEARCH + _TAIL
    p = _sizing(tmp_path, "state/sizings/a.yaml", before)
    with pytest.raises(ResearchSizingRefused, match="boom"):
        drop_research_block(tmp_path, "state/sizings/a.yaml")
    assert p.read_bytes() == before.encode("utf-8")
