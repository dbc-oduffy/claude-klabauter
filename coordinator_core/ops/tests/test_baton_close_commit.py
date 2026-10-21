"""End-to-end tests for the baton.close_commit handler on a tmp_path git repo.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_baton_close_commit.py -q
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops import baton_close_commit as op
from coordinator_core.ops import baton_close_report as report_op
from coordinator_core.ops.baton_close_commit_contract import OP_KEY, RESULT_KEYS
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git, _init_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/done.md"


def _baton(deliverable_id: str | None, *, status: str = "open", state: str = "ready_to_fire",
           predecessor: str = "none", plan: str | None = _PLAN) -> str:
    lines = [
        "title: t",
        "created: 2026-10-01",
        "branch: work/test/2026-10-01",
        f"predecessor: {predecessor}",
        "category: infra",
        "summary: fixture baton",
        f"status: {status}",
        f"deployment_state: {state}",
    ]
    if deliverable_id:
        lines.append(f"deliverable_id: {deliverable_id}")
    if plan:
        lines.append(f"governing_plan: {plan}")
    return "---\n" + "\n".join(lines) + "\n---\n\nbody\n"


def _write(repo: Path, rel: str, text: str, newline: str = "\n") -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", newline).encode("utf-8"))


def _fm(repo: Path, rel: str, key: str):
    split = split_frontmatter((repo / rel).read_text(encoding="utf-8", newline=""))
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, key)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _changed(repo: Path) -> set[str]:
    out = _git(repo, "status", "--porcelain").stdout
    return {line[3:].strip() for line in out.splitlines() if line.strip()}


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    assert _git(repo, "commit", "-m", "seed").returncode == 0


def _fixture(repo: Path) -> dict[str, str]:
    _init_repo(repo)
    _write(repo, _PLAN, "---\nstatus: implemented\n---\n")
    _write(repo, "docs/plans/live.md", "---\nstatus: reviewed\n---\n")
    h = "state/handoffs/"
    _write(repo, h + "ok1.md", _baton("dlv-ok1-000001"))
    _write(repo, h + "ok2-crlf.md", _baton("dlv-ok2-000002"), newline="\r\n")
    _write(repo, h + "claimed.md", _baton("dlv-claimed-3", status="claimed"))
    _write(repo, h + "continued.md", _baton("dlv-continued-4", state="continued"))
    _write(repo, h + "parent.md", _baton("dlv-parent-000005"))
    _write(repo, h + "child.md", _baton("dlv-child-000006", predecessor="state/handoffs/parent.md",
                                         plan=None))
    _write(repo, h + "nodlv.md", _baton(None))
    _write(repo, h + "terminal.md", _baton("dlv-terminal-7", state="shipped"))
    _write(repo, h + "stale.md", _baton("dlv-stale-000008", plan="docs/plans/stale.md"))
    _write(repo, "docs/plans/stale.md", "---\nstatus: implemented\n---\n")
    _commit_all(repo)
    return {p.name: f"state/handoffs/{p.name}" for p in (repo / "state/handoffs").glob("*.md")}


def _row(rel: str, plan: str = _PLAN) -> dict:
    return {
        "handoff_path": rel,
        "evidence": [{"class": "plan", "source_field": "governing_plan", "target_path": plan,
                      "target_status": "implemented", "done": True, "required": True}],
    }


def _call(repo: Path, rows: list, **extra) -> dict:
    return asyncio.run(op._handler({"rows": rows, **extra}, repo))


def _snapshot(repo: Path, rels) -> dict[str, bytes]:
    return {r: (repo / r).read_bytes() for r in rels}


def _reasons(res: dict) -> dict[str, str]:
    return {r["handoff_path"]: r["reason"] for r in res["refused"]}


def test_closable_rows_flip_and_one_scoped_commit_lands_them(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    report = asyncio.run(report_op._handler({}, repo))
    closable = {c["handoff_path"] for c in report["closable"]}
    assert paths["ok1.md"] in closable and paths["ok2-crlf.md"] in closable
    head = _head(repo)

    rows = [c for c in report["closable"] if c["handoff_path"] in (paths["ok1.md"], paths["ok2-crlf.md"])]
    res = _call(repo, rows)

    assert tuple(res) == RESULT_KEYS and res["exit_code"] == 0 and res["rows_received"] == 2
    assert _head(repo) == head
    assert res["pathspec"] == sorted([paths["ok1.md"], paths["ok2-crlf.md"]])
    assert _changed(repo) == set(res["pathspec"])
    assert res["commit_subject"] == "baton.close_commit: 2 baton(s) terminal"
    for item in res["applied"]:
        rel = item["handoff_path"]
        assert _fm(repo, rel, "deployment_state") == "shipped"
        assert _fm(repo, rel, "shipped_in_kind") == "successor"
        assert _fm(repo, rel, "advanced_by") == item["advanced_by"] == _fm(repo, rel, "deliverable_id")
        assert _fm(repo, rel, "advanced_at") == item["advanced_at"]

    assert _git(repo, "add", "--", *res["pathspec"]).returncode == 0
    assert _git(repo, "commit", "-m", res["commit_subject"], "--", *res["pathspec"]).returncode == 0
    assert _changed(repo) == set()


def test_every_refusal_reason_leaves_the_file_byte_identical(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    _write(repo, "docs/plans/stale.md", "---\nstatus: reviewed\n---\n")
    _commit_all(repo)
    rows = [
        _row(paths["claimed.md"]),
        _row(paths["continued.md"]),
        _row(paths["parent.md"]),
        _row(paths["stale.md"], "docs/plans/stale.md"),
        _row(paths["nodlv.md"]),
        _row("state/handoffs/ghost.md"),
        {"handoff_path": "", "evidence": []},
        _row(paths["terminal.md"]),
        _row(paths["ok1.md"]),
    ]
    before = _snapshot(repo, [r for r in paths.values() if not r.endswith("ok1.md")])
    res = _call(repo, rows)

    assert _reasons(res) == {
        paths["claimed.md"]: "claimed",
        paths["continued.md"]: "continued",
        paths["parent.md"]: "live_successor",
        paths["stale.md"]: "evidence_stale",
        paths["nodlv.md"]: "no_deliverable_id",
        "state/handoffs/ghost.md": "not_live",
        "": "bad_row",
    }
    assert res["already_terminal"] == [{"handoff_path": paths["terminal.md"]}]
    assert [a["handoff_path"] for a in res["applied"]] == [paths["ok1.md"]]
    assert res["pathspec"] == [paths["ok1.md"]]
    assert _changed(repo) == {paths["ok1.md"]}
    assert _snapshot(repo, before) == before


def test_all_refused_batch_has_empty_pathspec_and_null_subject(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    res = _call(repo, [_row(paths["claimed.md"])])
    assert res["pathspec"] == [] and res["commit_subject"] is None and res["applied"] == []
    assert _changed(repo) == set()


def test_evidence_made_stale_after_report_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    report = asyncio.run(report_op._handler({}, repo))
    rows = [c for c in report["closable"] if c["handoff_path"] == paths["stale.md"]]
    assert rows
    _write(repo, "docs/plans/stale.md", "---\nstatus: reviewed\n---\n")
    before = _snapshot(repo, [paths["stale.md"]])
    res = _call(repo, rows)
    assert _reasons(res) == {paths["stale.md"]: "evidence_stale"}
    assert _snapshot(repo, [paths["stale.md"]]) == before


def test_terminal_baton_outside_the_open_scan_is_already_terminal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _fixture(repo)
    _write(repo, "state/handoffs/done.md", _baton("dlv-done-000009", status="concluded", state="shipped"))
    _commit_all(repo)
    res = _call(repo, [_row("state/handoffs/done.md")])
    assert res["already_terminal"] == [{"handoff_path": "state/handoffs/done.md"}]
    assert res["refused"] == [] and res["pathspec"] == []


def test_empty_or_missing_rows_is_a_named_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _fixture(repo)
    assert _call(repo, [])["exit_code"] == 1
    assert asyncio.run(op._handler({}, repo))["exit_code"] == 1
    assert asyncio.run(op._handler({"rows": [1]}, None))["exit_code"] == 1


def test_the_call_spawns_no_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    paths = _fixture(repo)
    spawned: list = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        spawned.append(a)
        real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    res = _call(repo, [_row(paths["ok1.md"])])
    assert res["pathspec"] == [paths["ok1.md"]]
    assert spawned == []
