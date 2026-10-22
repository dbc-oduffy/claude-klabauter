"""A ledger resume whose plan carries an em-discharged row owes no since-halt diff; with none it still refuses."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import UNCHANGED_SINCE_HALT_REASON
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]

VERDICT = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


def _resume(halt, **extra):
    return {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C2"], **extra}


_PLAIN_TEMPLATE: list = []


@pytest.fixture
def plain_repo(tmp_path):
    import shutil
    import tempfile
    from pathlib import Path

    if not _PLAIN_TEMPLATE:
        tpl = Path(tempfile.mkdtemp(prefix="emdis-tpl-")) / "repo"
        tpl.mkdir()
        _git(tpl, "init", "-q")
        _git(tpl, "config", "user.email", "t@t.example")
        _git(tpl, "config", "user.name", "t")
        (tpl / "prod.py").write_text("x = 1\n", encoding="utf-8")
        _git(tpl, "add", ".")
        _git(tpl, "commit", "-q", "-m", "halt")
        _PLAIN_TEMPLATE.append(tpl)
    shutil.copytree(_PLAIN_TEMPLATE[0], tmp_path, dirs_exist_ok=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _git(tmp_path, "rev-parse", "HEAD")


def test_em_discharged_row_exempts_a_met_row_without_a_git_spawn(plain_repo, monkeypatch):
    root, manifest, halt = plain_repo

    def boom(*a, **k):
        raise AssertionError("git spawned")

    monkeypatch.setattr(tc, "run_git", boom)
    resume = _resume(halt, em_discharged={"V1": "R1 landed by hand at c72e924dbb"})
    assert tc._unchanged_since_halt_failures(root, resume, VERDICT, manifest) == []


def test_em_discharge_exempts_a_met_row_when_a_hand_commit_landed_between_halts(plain_repo):
    root, manifest, halt = plain_repo
    (root / "CLAUDE.md").write_text("rule\n", encoding="utf-8")
    _git(root, "add", "CLAUDE.md")
    _git(root, "commit", "-q", "-m", "hand")
    resume = _resume(halt, em_discharged={"V1": "R1 landed by hand"})
    assert tc._unchanged_since_halt_failures(root, resume, VERDICT, manifest) == []


def test_discharge_credits_only_the_linked_row_and_refuses_the_other(plain_repo):
    root, manifest, halt = plain_repo
    verdict = {"items": [{"id": "R1", "status": "met"}, {"id": "R2", "status": "met"}]}
    resume = _resume(halt, blocking=["R1", "R2"], em_discharged={"V1": "R1 landed by hand"})
    got = tc._unchanged_since_halt_failures(root, resume, verdict, manifest)
    assert got == [{"id": "R2", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_discharge_text_naming_a_longer_id_does_not_credit_the_prefix_row(plain_repo):
    root, manifest, halt = plain_repo
    verdict = {"items": [{"id": "R1", "status": "met"}, {"id": "R10", "status": "met"}]}
    resume = _resume(halt, blocking=["R1", "R10"], em_discharged={"V1": "R10 landed by hand"})
    got = tc._unchanged_since_halt_failures(root, resume, verdict, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_met_by_naming_the_discharged_row_links_it(plain_repo):
    root, manifest, halt = plain_repo
    verdict = {"items": [{"id": "R1", "status": "met", "met_by": ["V1"]}, {"id": "R2", "status": "met"}]}
    resume = _resume(halt, blocking=["R1", "R2"], em_discharged={"V1": "landed by hand"})
    got = tc._unchanged_since_halt_failures(root, resume, verdict, manifest)
    assert got == [{"id": "R2", "reason": UNCHANGED_SINCE_HALT_REASON}]


@pytest.mark.parametrize("discharged", [{}, None, {"V1": "  "}, {"V1": 3}, ["V1"]])
def test_no_usable_em_discharge_still_refuses(plain_repo, discharged):
    root, manifest, halt = plain_repo
    resume = _resume(halt, em_discharged=discharged)
    got = tc._unchanged_since_halt_failures(root, resume, VERDICT, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
