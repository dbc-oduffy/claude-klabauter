"""An executor reply that omits or marks unmet a revise checklist item is caught before the ledger leg."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
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

pytestmark = [pytest.mark.skipif(shutil.which("node") is None, reason="node evaluates the composed JS"), pytest.mark.spawns_process, pytest.mark.cadence]


def _seed(repo):
    notes = rl.run_dir(repo, ASK) / rl.NOTES_FILE
    notes.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"note": n, "at": f"2026-11-20T10:0{i}:00Z"}) for i, n in enumerate((N1, N2))]
    notes.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _gaps(script, rows, replies, groups=None):
    """`_reviseReceipt()` evaluated in node over the composed block, fed stub executor replies per row id."""
    start = script.index("  const _reviseNoteItems")
    end = script.index("    return out;\n  };", script.index("const _reviseReceipt")) + len("    return out;\n  };")
    js = (
        f"const _manifest = {json.dumps({'rows': rows})};\n"
        f"const _rows = {json.dumps(replies)};\nconst _groups = {json.dumps(groups or {})};\n"
        "const log = () => {};\n"
        f"{script[start:end]}\n"
        "_reviseReceipt().then((g) => console.log(JSON.stringify(g)));"
    )
    out = subprocess.run(
        ["node", "-e", js], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


def _script(repo, monkeypatch):  # noqa: F811
    seen = _spy_texts(monkeypatch)
    _halt(repo, ASK, BASE)
    _seed(repo)
    _fire(repo)
    return seen["script"]


def _reply(*items):
    return "DONE: A did it\nrevise_items: " + json.dumps([{"id": i, "status": s, "evidence": "e"} for i, s in items])


def test_a_reply_missing_n2_surfaces_n2_with_its_verbatim_text(repo, monkeypatch):  # noqa: F811
    gaps = _gaps(_script(repo, monkeypatch), [{"id": "A"}], {"A": _reply(("N1", "met"))})
    assert [g["id"] for g in gaps] == ["N2"]
    assert gaps[0]["why"] == "unreported" and gaps[0]["row"] == "A"
    assert gaps[0]["item"] == "N2: N2-MARK update the readers too\n  second line of N2"


def test_an_unmet_item_surfaces_and_a_complete_reply_passes(repo, monkeypatch):  # noqa: F811
    script = _script(repo, monkeypatch)
    unmet = _gaps(script, [{"id": "A"}], {"A": _reply(("N1", "met"), ("N2", "unmet"))})
    assert [(g["id"], g["why"]) for g in unmet] == [("N2", "unmet")]
    assert _gaps(script, [{"id": "A"}], {"A": _reply(("N1", "met"), ("N2", "met"))}) == []


def test_an_item_one_row_reports_met_is_not_a_gap_for_a_row_outside_its_footprint(repo, monkeypatch):  # noqa: F811
    script = _script(repo, monkeypatch)
    rows = [{"id": "A"}, {"id": "B"}]
    replies = {"A": _reply(("N1", "met"), ("N2", "met")), "B": _reply(("N1", "unmet"), ("N2", "unmet"))}
    assert _gaps(script, rows, replies) == []


def test_a_grouped_member_is_read_from_the_group_reply(repo, monkeypatch):  # noqa: F811
    script = _script(repo, monkeypatch)
    rows = [{"id": "A", "group": "g"}]
    assert _gaps(script, rows, {"A": "DONE: A"}, {"g": _reply(("N1", "met"), ("N2", "met"))}) == []
    assert [g["id"] for g in _gaps(script, rows, {"A": "DONE: A"}, {"g": "DONE: A"})] == ["N1", "N2"]


def test_an_incomplete_row_adds_no_receipt_gap(repo, monkeypatch):  # noqa: F811
    assert _gaps(_script(repo, monkeypatch), [{"id": "A"}], {"A": "INCOMPLETE A"}) == []
