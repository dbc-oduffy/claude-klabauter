"""A refire's live revise notes and revise-added register rows reach every executor row prompt as `- [ ]` items."""

from __future__ import annotations

import json
import shutil

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_notes
from coordinator_core.ops.dispatch_emit.tests.test_refire_carries_notes_every_mint_path import _spy_texts
from coordinator_core.ops.dispatch_emit.tests.test_refire_inherits_halted_base import (  # noqa: F401
    ASK,
    BASE,
    _fire,
    _halt,
    composed,
    repo,
)

N1 = "N1-MARK write the use_restrictions writers"
N2 = "N2-MARK update the readers too\nsecond line of N2"
N3 = "N3-MARK obsolete ask"
ROW_TEXT = "ROW-MARK raw-stream receipt, config attribution, provenance"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node evaluates the composed JS")


def _seed(repo):
    notes = rl.run_dir(repo, ASK) / rl.NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"note": n, "at": f"2026-11-20T10:0{i}:00Z"}) for i, n in enumerate((N1, N2, N3))]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with notes.open("ab") as fh:
        fh.write(run_notes.retraction_line("N3", "applied by hand", "2026-11-20T10:09:00Z", "em"))
    rl.revise(repo, ASK, verbatim=ROW_TEXT, sender="peer-session-1")


def test_the_checklist_precedes_the_brief_pointer_in_the_row_prompt(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    _fire(repo)
    line = next(ln for ln in seen["script"].splitlines() if ln.lstrip().startswith("const _rowPrompt = "))
    assert line.index("_reviseChecklist(r)") < line.index("Your brief is the file")


def test_a_grouped_member_gets_its_own_checklist_through_row_prompt(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    _fire(repo)
    script = seen["script"]
    assert "_rowPrompt(m) + '\\n\\n'" in script
    assert "_reviseChecklist(r)" in next(ln for ln in script.splitlines() if ln.lstrip().startswith("const _rowPrompt = "))


def test_a_fresh_fire_composes_no_checklist(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _fire(repo)
    assert "_reviseChecklist" not in seen["script"]


def test_a_halted_predecessors_revise_row_is_read_for_a_refire_without_notes(repo):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import ask_compose

    _halt(repo, ASK, BASE)
    rl.revise(repo, ASK, verbatim=ROW_TEXT, sender="peer-session-1")
    new_run = "ask-20261121T000000-cccccc"
    rl.start(repo, new_run)
    assert ask_compose._revise_register_rows(repo.as_posix(), new_run) == []
    rows = ask_compose._revise_register_rows(repo.as_posix(), new_run, [ASK])
    assert [r["text"] for r in rows] == [ROW_TEXT]
