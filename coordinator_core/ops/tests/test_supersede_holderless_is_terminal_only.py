"""Supersede's holder-less `status: claimed` flip is sanctioned only on a terminal target.

Contract: docs/reference/baton-claim-lifecycle.md. The corpus pin fails on any
`status: claimed` record with no `claimed_by` whose deployment_state is non-terminal.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.claim_state import handoff_claim_dir
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.test_archive_stamp import _init_repo
from coordinator_core.ops.handoff_archive_transition import (
    _attribute_claim_holder,
    _supersede_continued,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _seed(tmp_path: Path, status: str, deployment: str) -> Path:
    _init_repo(tmp_path)
    path = tmp_path / "state" / "handoffs" / "h.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        '---\ntitle: "T"\ncreated: 2026-01-01\nbranch: work/test/2026-01-01\n'
        f'status: {status}\npredecessor: "none"\ndeployment_state: {deployment}\n---\n\n# H\n',
        encoding="utf-8",
    )
    return path


def _fm(path: Path) -> str:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return split.fm_text


def test_shipped_never_claimed_supersede_is_holderless_without_warning(tmp_path):
    path = _seed(tmp_path, "open", "shipped")
    result = _supersede_continued(path, "succ.md", tmp_path)
    fm = _fm(path)
    assert read_fm_field(fm, "status") == "claimed"
    assert read_fm_field(fm, "deployment_state") == "continued"
    assert read_fm_field(fm, "claimed_by") is None
    assert not (result.get("warnings") or [])


def test_ledger_holder_is_still_stamped(tmp_path):
    path = _seed(tmp_path, "open", "shipped")
    claim_dir = handoff_claim_dir(tmp_path / ".git", path)
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text("ledger-sid", encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-01-02T00:00:00Z", encoding="utf-8")
    (claim_dir / "pid").write_text("1", encoding="utf-8")
    _supersede_continued(path, "succ.md", tmp_path)
    assert "ledger-sid" in (read_fm_field(_fm(path), "claimed_by") or "")


def test_non_terminal_target_without_holder_warns_as_defect(tmp_path):
    warnings: list = []
    fm = 'status: claimed\ndeployment_state: in_flight\n'
    path = tmp_path / "h.md"
    out = _attribute_claim_holder(fm, path, tmp_path, warnings, "in_flight")
    assert out == fm
    assert len(warnings) == 1 and "DEFECT" in warnings[0]


def test_terminal_target_without_holder_is_silent(tmp_path):
    warnings: list = []
    fm = "status: claimed\ndeployment_state: continued\n"
    _attribute_claim_holder(fm, tmp_path / "h.md", tmp_path, warnings, "continued")
    assert warnings == []


def test_corpus_has_no_holderless_non_terminal_claimed_record():
    paths = sorted((_REPO_ROOT / "state" / "handoffs").glob("*.md"))
    paths += sorted((_REPO_ROOT / "archive" / "handoffs").rglob("*.md"))
    offenders = []
    for p in paths:
        try:
            fm = parse_frontmatter(p.read_text(encoding="utf-8")).get("frontmatter")
        except (OSError, UnicodeDecodeError):
            continue
        if not isinstance(fm, dict) or fm.get("status") != "claimed":
            continue
        if fm.get("claimed_by"):
            continue
        if fm.get("deployment_state") not in HANDOFF_TERMINAL_DEPLOYMENT:
            offenders.append(str(p.relative_to(_REPO_ROOT)))
    assert not offenders, f"holder-less non-terminal claimed records: {offenders}"
