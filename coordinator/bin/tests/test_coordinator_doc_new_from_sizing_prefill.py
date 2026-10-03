"""Coverage for `mint_baton_from_sizing` pre-filling `## Specification` / `## Acceptance criteria`
from the sizing's premise evidence and exit criterion, and for the `deliverable_id` write-back
and backfill on the sizing.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_from_sizing_prefill.py -v
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
_SIZING_REL = "state/sizings/2026-10-03-prefill-fixture.yaml"
_EVIDENCE = "dogfood report observed the emitted receipt"
_CRITERION = "(1) the baton carries the evidence; (2) the sizing carries the id"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("cdn_prefill_test", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("cdn_prefill_test", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _sizing(dlv: str | None = None, baton: str | None = None, evidence: str | None = _EVIDENCE,
            criterion: str | None = _CRITERION) -> str:
    lines = [
        "schema: sizing-object",
        'name: "prefill fixture"',
        'intent: "Prefill a baton from a sizing"',
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        "status: sized",
    ]
    if baton is not None:
        lines.append(f'baton: "{baton}"')
    lines.append("premise:")
    lines.append("  provenance: read")
    if evidence is not None:
        lines.append(f'  evidence: "{evidence}"')
    lines.append(f'deliverable_id: "{dlv}"' if dlv else "deliverable_id: null")
    if criterion is not None:
        lines += [
            "exit_criterion:",
            f"  statement: '{criterion}'",
            "  accepted:",
            "    pm_quote: fine",
            "    'on': '2026-10-03'",
            "    mode: pm",
        ]
    return "\n".join(lines) + "\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], capture_output=True,
                   **no_console_creationflags())
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    return tmp_path


def _put(repo: Path, text: str) -> Path:
    path = repo / _SIZING_REL
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _baton_id(path: Path) -> str:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])["deliverable_id"]


def test_mint_prefills_specification_and_acceptance(repo):
    _put(repo, _sizing())
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    body = (repo / result["path"]).read_text(encoding="utf-8")

    assert f"## Specification\n\n{_EVIDENCE}\n" in body
    assert f"## Acceptance criteria\n\n- [ ] {_CRITERION}\n" in body
    assert body.count("- [ ] ") == 1


def test_absent_evidence_and_criterion_keep_the_skeleton(repo):
    _put(repo, _sizing(evidence="TBD", criterion=None))
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    body = (repo / result["path"]).read_text(encoding="utf-8")

    assert "<!-- The actual work spec." in body
    assert "<!-- Checklist the picking-up EM gates completion against. -->" in body


def test_placeholder_evidence_is_skipped(repo):
    _put(repo, _sizing(evidence="PLACEHOLDER — fill in"))
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    assert "PLACEHOLDER — fill in" not in (repo / result["path"]).read_text(encoding="utf-8")


def test_fresh_mint_writes_the_baton_id_back_onto_a_null_sizing(repo):
    sizing = _put(repo, _sizing())
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    edge = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert edge["deliverable_id"] and edge["deliverable_id"] == _baton_id(repo / result["path"])
    assert edge["baton"] == result["path"]


def test_fresh_mint_writes_the_id_when_the_key_is_absent(repo):
    text = _sizing().replace("deliverable_id: null\n", "")
    sizing = _put(repo, text)
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    edge = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert edge["deliverable_id"] == _baton_id(repo / result["path"])


def test_non_null_sizing_id_is_kept(repo):
    sizing = _put(repo, _sizing(dlv="dlv-keep-me-abc123"))
    result = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["deliverable_id"] == "dlv-keep-me-abc123"
    assert _baton_id(repo / result["path"]) == "dlv-keep-me-abc123"


def test_remint_backfills_null_id_from_existing_baton_and_leaves_baton(repo):
    sizing = _put(repo, _sizing())
    first = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    baton = repo / first["path"]
    sizing.write_text(_sizing(baton=first["path"]), encoding="utf-8", newline="\n")
    baton_before = baton.read_bytes()

    second = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert second["created"] is False
    assert baton.read_bytes() == baton_before
    edge = yaml.safe_load(sizing.read_text(encoding="utf-8"))
    assert edge["deliverable_id"] == _baton_id(baton)


def test_remint_never_overwrites_a_non_null_id(repo):
    sizing = _put(repo, _sizing())
    first = _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))
    sizing.write_text(_sizing(dlv="dlv-other-999999", baton=first["path"]),
                      encoding="utf-8", newline="\n")
    before = sizing.read_bytes()

    _cli.mint_baton_from_sizing(_SIZING_REL, str(repo))

    assert sizing.read_bytes() == before
