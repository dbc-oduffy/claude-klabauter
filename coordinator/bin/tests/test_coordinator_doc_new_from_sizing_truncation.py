"""Baton mint from a sizing: summary truncation on serialized length, soft refusal, scout_evidence.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_from_sizing_truncation.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"
_SIZING_REL = "state/sizings/2026-10-02-fixture.yaml"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cdn_from_sizing_trunc_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_from_sizing_trunc_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                   **no_console_creationflags())
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo: Path, intent: str, evidence: list | None = None, baton: str | None = None) -> Path:
    doc = {
        "schema": "sizing-object", "name": "fixture", "intent": intent,
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan", "detents": [],
        "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "deliverable_id": "dlv-fixture-abc123",
    }
    if evidence is not None:
        doc["scout_evidence"] = evidence
    if baton:
        doc["baton"] = baton
    path = repo / _SIZING_REL
    path.write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    return path


def _fm(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def test_windows_path_intent_past_140_serialized_chars_mints_valid_baton(repo):
    intent = "Fix C:\\Users\\x\\very\\long\\path " * 8
    _put(repo, intent)
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    summary = _fm(repo / result["path"])["summary"]
    assert summary.endswith("…")
    assert len(_cli._yaml_quote(summary)) - 2 <= 140


@pytest.mark.parametrize("ch", ["\\", '"', "a"])
def test_boundary_is_exact_on_serialized_length(ch):
    fits = ch * (140 if ch == "a" else 70)
    assert _cli._fit_summary(fits) == fits
    over = fits + ch
    cut = _cli._fit_summary(over)
    assert cut != over and cut.endswith("…")
    assert len(_cli._yaml_quote(cut)) - 2 == 140 or len(_cli._yaml_quote(cut)) - 2 == 139


def test_validator_failure_is_a_soft_refusal_and_writes_nothing(repo, monkeypatch):
    sizing = _put(repo, "an intent")
    before = sizing.read_bytes()

    def _fail(content, out_path, repo_root):
        raise SystemExit(1)

    monkeypatch.setattr(_cli, "_assert_scaffold_content_valid", _fail)
    with pytest.raises(_cli.SizingMintRefused) as exc:
        _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert exc.value.fields == ["intent"]
    assert sizing.read_bytes() == before
    assert not (repo / "state" / "handoffs").exists()


def test_scout_evidence_renders_one_bullet_per_item(repo):
    _put(repo, "an intent", evidence=["docs/a.md", {"kind": "k", "finding": "b"}])
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    text = (repo / result["path"]).read_text(encoding="utf-8")
    section = text.split("## Reference materials (read first)")[1].split("## Specification")[0]
    assert [ln for ln in section.splitlines() if ln.startswith("- ")] == ["- docs/a.md", "- b"]


def test_no_scout_evidence_keeps_the_comment(repo):
    _put(repo, "an intent")
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    text = (repo / result["path"]).read_text(encoding="utf-8")
    section = text.split("## Reference materials (read first)")[1].split("## Specification")[0]
    assert "<!-- List file paths" in section
