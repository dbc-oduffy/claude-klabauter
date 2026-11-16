"""workflow.revise on a research pipeline run mails every dispatched stage or warns naming those it misses."""
from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT as DIR
from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER as MARKER

RID = "pipeline-research-20261024T000000-abc123"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    d = tmp_path / DIR
    d.mkdir(parents=True)
    (d / f"{RID}.workflow.mjs").write_text(f"// {MARKER}\n", encoding="utf-8")
    (d / f"{RID}.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")
    return tmp_path


def _call(repo, **params):
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    return rl._handler({"run_id": RID, **params}, repo_root=repo)


def _mail(repo):
    mail = repo / DIR / RID / "mail"
    mail.mkdir(parents=True)
    for name in ("specialist-a.jsonl", "specialist-b.jsonl", "_em.jsonl"):
        (mail / name).write_text("", encoding="utf-8")
    return mail


def test_revision_lands_in_every_stage_mailbox_but_not_the_em_one(repo):
    mail = _mail(repo)
    reply = _call(repo, note="narrow to cell A", **{"from": "em-1"})
    assert reply["delivered"] is True and reply["mailed_stages"] == ["specialist-a", "specialist-b"]
    assert "warning" not in reply
    for name in ("specialist-a", "specialist-b"):
        (line,) = (mail / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(line)
        assert entry["from"] == "em-1" and "narrow to cell A" in entry["text"]
    assert (mail / "_em.jsonl").read_text(encoding="utf-8") == ""


def test_verbatim_and_note_each_mail_one_line(repo):
    mail = _mail(repo)
    _call(repo, verbatim="also B", note="and C")
    assert len((mail / "specialist-a.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_no_mail_dir_warns_stages_cannot_be_enumerated(repo):
    reply = _call(repo, note="x")
    assert reply["delivered"] is True and reply["mailed_stages"] == []
    assert "not enumerable" in reply["warning"]


def test_unwritable_mailbox_is_named_in_the_warning(repo, monkeypatch):
    _mail(repo)
    from coordinator_core.ops.dispatch_emit import pipeline_run as pr

    real = pr.append_line

    def flaky(path, data):
        if path.name == "specialist-b.jsonl":
            raise OSError("locked")
        return real(path, data)

    monkeypatch.setattr(pr, "append_line", flaky)
    reply = _call(repo, note="x")
    assert reply["mailed_stages"] == ["specialist-a"]
    assert "specialist-b" in reply["warning"] and "specialist-a" not in reply["warning"]
