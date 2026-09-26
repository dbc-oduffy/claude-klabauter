
from __future__ import annotations

import re
from pathlib import Path

import pytest

import coordinator_core.baton_assemble as ba
import coordinator_core.baton_assemble.apply as ba_apply
from coordinator_core.test_baton_assemble import (
    _FAKE_OPERATOR_CONFIG,
    _REPO_CLAUDE_KLABAUTER_BIN,
    _write_artifact,
)

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


@pytest.fixture(autouse=True)
def _stub_operator_config(monkeypatch):
    monkeypatch.setattr(ba, "resolve_operator_config", lambda: dict(_FAKE_OPERATOR_CONFIG))
    monkeypatch.setattr(ba_apply, "_resolve_claude_klabauter_bin", lambda: _REPO_CLAUDE_KLABAUTER_BIN)


def _write_predecessor(
    root: Path, rel: str, deliverable_id: str, plan_id: str, handoff_id: str | None = None
) -> Path:
    lines = [
        f"deliverable_id: {deliverable_id}",
        f"origin_plan_id: {plan_id}",
    ]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    return _write_artifact(root / rel, lines)


def test_fan_in_successor_mints_fresh_id_never_carrying_any_rung(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra_a = _write_predecessor(
        tmp_path, "state/handoffs/extra-a.md", "DEL-EXTRA-A", "pln-extra-a-bbb222"
    )
    extra_b = _write_predecessor(
        tmp_path, "state/handoffs/extra-b.md", "DEL-EXTRA-B", "pln-extra-b-ccc333"
    )

    lineage = ba.resolve_lineage(
        "handoff",
        str(primary),
        tmp_path,
        additional_predecessor_paths=[str(extra_a), str(extra_b)],
    )

    assert lineage["deliverable_id"] not in {"DEL-PRIMARY", "DEL-EXTRA-A", "DEL-EXTRA-B"}, (
        "DR-388: the successor's own deliverable_id must be freshly minted, "
        "never carried verbatim from any rung"
    )
    assert lineage["discovery"] == "fan-in-mint"
    assert lineage["deliverable_ids"] == ["DEL-PRIMARY", "DEL-EXTRA-A", "DEL-EXTRA-B"]

    directives = ba._build_directives("handoff", lineage, root=tmp_path)
    d1 = next(d for d in directives if d["id"] == "d1")
    assert f"--deliverable-id={lineage['deliverable_id']}" in d1["args"]
    assert "--deliverable-id=DEL-PRIMARY" not in d1["args"]


def test_single_predecessor_still_carries_verbatim_dr207(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-LONE",
        "pln-lone-aaa111",
        handoff_id="hnd-primary-aaa111",
    )

    lineage = ba.resolve_lineage("handoff", str(primary), tmp_path)

    assert lineage["deliverable_id"] == "DEL-LONE"
    assert lineage["discovery"] != "fan-in-mint"


def test_fan_in_mint_is_reproducibly_unique_across_two_resolutions(tmp_path):
    primary = _write_predecessor(
        tmp_path,
        "state/handoffs/primary.md",
        "DEL-PRIMARY",
        "pln-primary-aaa111",
        handoff_id="hnd-primary-aaa111",
    )
    extra = _write_predecessor(
        tmp_path, "state/handoffs/extra.md", "DEL-EXTRA", "pln-extra-bbb222"
    )

    first = ba.resolve_lineage(
        "handoff", str(primary), tmp_path, additional_predecessor_paths=[str(extra)]
    )
    second = ba.resolve_lineage(
        "handoff", str(primary), tmp_path, additional_predecessor_paths=[str(extra)]
    )

    assert first["deliverable_id"] != second["deliverable_id"]


_DELIVERABLE_ID_RE = re.compile(
    r"^dlv-(?!placeholder-replace-with)[0-9a-zA-Z][0-9a-zA-Z.-]*$"
)


@pytest.mark.parametrize(
    "raw, why",
    [
        (
            "2026-08-30_215434_the-cockpit-publish-plan-awaits-ratification",
            "the mint convention's own output-path stem — two underscores",
        ),
        ("A Title, With Prose!", "a caller-supplied title is free prose"),
        ("___", "a slug that reduces to nothing must still mint"),
        (".hidden", "leading punctuation is not an alphanumeric first char"),
    ],
)
def test_sanitized_slug_always_mints_a_schema_valid_id(raw, why):
    minted = f"dlv-{ba._sanitize_mint_slug(raw)}-abc123"
    assert _DELIVERABLE_ID_RE.match(minted), f"{why}: {minted!r}"


def test_sanitizer_does_not_truncate():
    raw = "a" * 120
    assert ba._sanitize_mint_slug(raw) == raw


def _mark_repo(root: Path) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)


def test_foreign_repo_artifact_yields_citation_fresh_id_and_no_predecessor(tmp_path):
    session_root = tmp_path / "repo"
    foreign_root = tmp_path / "foreign"
    _mark_repo(session_root)
    _mark_repo(foreign_root)

    foreign_artifact = _write_predecessor(
        foreign_root,
        "state/handoffs/foreign.md",
        "DEL-FOREIGN",
        "pln-foreign-aaa111",
        handoff_id="hnd-foreign-aaa111",
    )

    lineage = ba.resolve_lineage("handoff", str(foreign_artifact), session_root)

    assert lineage["foreign_repo_artifact"] is True
    assert lineage["foreign_repo_citation"] == str(foreign_artifact.resolve())
    assert lineage["deliverable_id"] != "DEL-FOREIGN"
    assert lineage["discovery"] == "foreign-repo-mint"
    assert lineage["predecessor"] is None
    assert lineage["predecessor_id"] is None
    assert lineage["predecessor_handoff"] is None

    directives = ba._build_directives("handoff", lineage, root=session_root)
    assert not any(d["cli"] == "handoff.supersede_predecessor" for d in directives)


def test_local_artifact_still_inherits_lineage_unchanged(tmp_path):
    session_root = tmp_path / "repo"
    _mark_repo(session_root)

    primary = _write_predecessor(
        session_root,
        "state/handoffs/primary.md",
        "DEL-LOCAL",
        "pln-local-aaa111",
        handoff_id="hnd-local-aaa111",
    )

    lineage = ba.resolve_lineage("handoff", str(primary), session_root)

    assert lineage["foreign_repo_artifact"] is False
    assert "foreign_repo_citation" not in lineage
    assert lineage["deliverable_id"] == "DEL-LOCAL"
    assert lineage["predecessor"] is not None

    directives = ba._build_directives("handoff", lineage, root=session_root)
    assert any(d["cli"] == "handoff.supersede_predecessor" for d in directives)


def test_same_repo_different_spelling_is_not_misread_as_foreign(tmp_path):
    session_root = tmp_path / "repo"
    _mark_repo(session_root)

    primary = _write_predecessor(
        session_root,
        "state/handoffs/primary.md",
        "DEL-SPELLING",
        "pln-spelling-aaa111",
        handoff_id="hnd-spelling-aaa111",
    )

    respelled = (
        session_root / "state" / "handoffs" / ".." / "handoffs" / "primary.md"
    )

    lineage = ba.resolve_lineage("handoff", str(respelled), session_root)

    assert lineage["foreign_repo_artifact"] is False
    assert lineage["deliverable_id"] == "DEL-SPELLING"
    assert lineage["predecessor"] is not None
