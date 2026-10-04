"""Receipt-derived `receipts`, `commits` and `loe.tshirt` in the completion-entry scaffold."""

from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

from coordinator_core.completion_receipts import store
from coordinator_core.completion_receipts.model import receipt_rel_path
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.ops import coordinator_complete_entry as m
from coordinator_core.ops.ceremony import wsc_disposition
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SCHEMA = Path(m.__file__).resolve().parents[1] / "frontmatter" / "schemas" / "completion-entry.schema.json"
_NOW = date.today().strftime("%Y-%m-%dT12:00:00Z")
_SLUG = "2026-10-01-demo"
_LEDGER_LOE = "loe:\n  agent_dispatches: 3\n  opus_dispatches: 1\n  em_tokens: null\n  tshirt: \"S\""


def _git(repo: Path, *args: str) -> None:
    r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, **no_console_creationflags())
    assert r.returncode == 0, r.stderr


def _receipt_fm(rid: str, actual, dlv: str = "dlv-demo-222222") -> dict:
    return {
        "schema": "completion-receipt",
        "receipt_id": rid,
        "baton_id": None,
        "deliverable_id": dlv,
        "workstream_id": None,
        "repo": "claude-klabauter",
        "plan_path": f"docs/plans/{_SLUG}.md",
        "branch": "main",
        "run": {"kind": "execute-plan", "id": "x.workflow"},
        "verdict": None,
        "judge": None,
        "commit_range": {"base": "abc", "head": None},
        "started_at": None,
        "concluded_at": _NOW,
        "loe": {"estimated": None, "actual": actual},
        "prose_ref": receipt_rel_path(rid, _NOW),
        "supersedes": None,
        "approved_by": None,
        "provenance": {"observed_at": _NOW, "derivation": "dispatch.terminal_commit", "ref": {"branch": "main", "sha": "abc"}},
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    (r / "docs" / "plans").mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "docs" / "plans" / f"{_SLUG}.md").write_text(
        '---\ntitle: "Demo"\ndeliverable_id: "dlv-demo-222222"\n---\n', encoding="utf-8"
    )
    monkeypatch.chdir(r)
    monkeypatch.setattr(m, "_native_single_session_loe", lambda: _LEDGER_LOE)
    monkeypatch.setattr(m, "_resolve_rollup_sentence", lambda *a, **k: "")
    monkeypatch.setattr(m, "_resolve_session_commits", lambda *a, **k: [])
    return r


def _commit_receipt(repo: Path, rid: str, actual) -> str:
    rel = store.write_receipt(repo, _receipt_fm(rid, actual), "prose\n")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", rid)
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _run() -> tuple[int, dict, str]:
    rc = m.main(["--sid", "abcdef123456", "--disposition", wsc_disposition.SINGLE_SESSION, "--governing-plan-slug", _SLUG])
    assert rc == 0
    entry = next(Path("archive/completed").rglob("*.md"))
    text = entry.read_text(encoding="utf-8")
    split = split_frontmatter(text)
    return rc, yaml.safe_load(split.fm_text), text


def test_two_joined_receipts_derive_receipts_commits_loe(repo):
    sha_a = _commit_receipt(repo, "rcp-demo-aaaaaa", "M")
    sha_b = _commit_receipt(repo, "rcp-demo-bbbbbb", "L")
    _, fm, text = _run()
    assert fm["receipts"] == [
        {"receipt_id": "rcp-demo-aaaaaa", "path": receipt_rel_path("rcp-demo-aaaaaa", _NOW)},
        {"receipt_id": "rcp-demo-bbbbbb", "path": receipt_rel_path("rcp-demo-bbbbbb", _NOW)},
    ]
    assert fm["commits"] == sorted({sha_a, sha_b})
    assert fm["loe"]["tshirt"] == "L"
    assert fm["loe"]["agent_dispatches"] == 3
    assert "session ledger (receipts carry no actual)" not in text


def test_entry_validates_against_schema_1_5_0(repo):
    _commit_receipt(repo, "rcp-demo-aaaaaa", "M")
    _, fm, _ = _run()
    fm = {k: (str(v) if k == "created" else v) for k, v in fm.items()}
    fm["nature"] = "infra"
    assert validate_frontmatter(fm, _SCHEMA) == []


def test_null_actuals_keep_ledger_tshirt_with_comment(repo):
    _commit_receipt(repo, "rcp-demo-aaaaaa", None)
    _, fm, text = _run()
    assert fm["loe"]["tshirt"] == "S"
    assert "# loe.tshirt: session ledger (receipts carry no actual)" in text


def test_no_joining_receipt_is_byte_identical_to_pre_change(repo):
    _, fm, text = _run()
    assert "receipts" not in fm
    assert fm["commits"] == []
    assert fm["loe"]["tshirt"] == "S"
    assert "receipts carry no actual" not in text
    assert "\nloe:\n  agent_dispatches: 3\n  opus_dispatches: 1\n  em_tokens: null\n  tshirt: \"S\"\n---\n" in text


def test_unrelated_deliverable_receipt_does_not_join(repo):
    rel = store.write_receipt(repo, _receipt_fm("rcp-demo-dddddd", "XL", dlv="dlv-other-999999"), "p\n")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", "x")
    _, fm, _ = _run()
    assert "receipts" not in fm
    assert fm["loe"]["tshirt"] == "S"


def test_one_git_spawn_for_n_receipts(repo, monkeypatch):
    for i, rid in enumerate(("rcp-demo-aaaaaa", "rcp-demo-bbbbbb", "rcp-demo-cccccc")):
        _commit_receipt(repo, rid, "M")
    calls = []
    real = subprocess.Popen

    class counting(real):
        def __init__(self, argv, *a, **k):
            calls.append(argv)
            super().__init__(argv, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", counting)
    _run()
    assert len([c for c in calls if c and c[0] == "git"]) == 1
