"""A refire's live revise notes and revise-added register rows reach every executor row prompt as `- [ ]` items."""

from __future__ import annotations

import json
import shutil
import subprocess

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

pytestmark = [pytest.mark.skipif(shutil.which("node") is None, reason="node evaluates the composed JS"), pytest.mark.spawns_process, pytest.mark.cadence]


def _seed(repo):
    notes = rl.run_dir(repo, ASK) / rl.NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"note": n, "at": f"2026-11-20T10:0{i}:00Z"}) for i, n in enumerate((N1, N2, N3))]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with notes.open("ab") as fh:
        fh.write(run_notes.retraction_line("N3", "applied by hand", "2026-11-20T10:09:00Z", "em"))
    rl.revise(repo, ASK, verbatim=ROW_TEXT, sender="peer-session-1")


def _revise_row_id(repo, run_id):
    ledger = rl.read_ledger(repo, rl.run_dir(repo, run_id))
    return next(i.id for i in ledger.items if i.origin == rl.ORIGIN_REVISION)


def _row_prompts(script, manifest_rows):
    """`_reviseChecklist(r)` evaluated in node over the composed block, once per manifest row."""
    start = script.index("  const _reviseNoteItems")
    end = script.index("  };", script.index("const _reviseChecklist")) + len("  };")
    js = (
        f"const _manifest = {json.dumps({'rows': manifest_rows})};\n{script[start:end]}\n"
        "console.log(JSON.stringify(_manifest.rows.map((r) => _reviseChecklist(r))));"
    )
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _items(prompt):
    return [ln for ln in prompt.splitlines() if ln.startswith("- [ ] ")]


def test_every_executor_prompt_carries_the_live_notes_and_only_its_scoped_revise_rows(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    reply = _fire(repo)
    row_id = _revise_row_id(repo, reply["run_id"])
    script = seen["script"]
    prompts = _row_prompts(script, [
        {"id": "A", "register_rows": [row_id]},
        {"id": "B", "register_rows": ["R1"]},
    ])
    served, other = prompts
    assert _items(served) == [f"- [ ] N1: {N1}", "- [ ] N2: N2-MARK update the readers too", f"- [ ] {row_id}: {ROW_TEXT}"]
    assert _items(other) == _items(served)[:2]
    assert "  second line of N2" in served and "  second line of N2" in other
    assert run_notes.CHECKLIST_HEADING in served
    assert N3 not in served + other and "N3-MARK" not in script


def test_a_revise_row_no_manifest_row_claims_lands_on_every_row(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    reply = _fire(repo)
    row_id = _revise_row_id(repo, reply["run_id"])
    prompts = _row_prompts(seen["script"], [{"id": "A", "register_rows": ["R1"]}, {"id": "B"}])
    assert all(f"- [ ] {row_id}: {ROW_TEXT}" in p for p in prompts)


def test_a_refire_carrying_only_a_revise_row_puts_it_in_every_executor_prompt(repo, monkeypatch):  # noqa: F811
    """No free-text note exists, so only the op's `prior_run_ids` wiring lets compose read the predecessor's row."""
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    rl.revise(repo, ASK, verbatim=ROW_TEXT, sender="peer-session-1")
    assert not (rl.run_dir(repo, ASK) / rl.NOTES_FILE).exists()
    _fire(repo)
    prompts = _row_prompts(seen["script"], [{"id": "A", "register_rows": ["R1"]}, {"id": "B"}])
    for prompt in prompts:
        assert [i for i in _items(prompt) if ROW_TEXT in i] and run_notes.CHECKLIST_HEADING in prompt
