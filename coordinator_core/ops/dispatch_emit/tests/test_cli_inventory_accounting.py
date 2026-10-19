"""The emit CLI prints the inventory accounting and one `edited` line per plan file, single and parts mode."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


def _acc(emitted, skipped=(), refused=()):
    ids = list(emitted) + [i for i, _ in skipped] + [i for i, _ in refused]
    return {
        "inventory_rows": len(ids),
        "emitted": list(emitted),
        "skipped": [{"id": i, "reason": r} for i, r in skipped],
        "refused": [{"id": i, "reason": r} for i, r in refused],
        "counts": {"emitted": len(emitted), "skipped": len(skipped), "refused": len(refused)},
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    return tmp_path


def test_single_inventory_emit_prints_summary_and_each_edited_plan(repo, monkeypatch, capsys):
    inv = repo / "inv.md"
    inv.write_text("---\nrun_id: r1\n---\n", encoding="utf-8")
    reply = {
        "ok": True,
        "path": str(repo / "r1.workflow.mjs"),
        "sha256": "x",
        "inventory_accounting": _acc(["m1"], skipped=[("m2", "routed-out: plan not prepped")]),
        "landed_reconciled": {
            "docs/plans/a.md": ["C1", "C2"],
            "docs/plans/b.md": ["C3"],
        },
    }
    monkeypatch.setattr(cli, "_dispatch_emit", lambda params, repo_root=None: dict(reply))
    assert cli.main(["--inventory", str(inv), "--out", str(repo / "r1.workflow.mjs")]) == cli.EXIT_OK
    out, err = capsys.readouterr()
    json.loads(out)
    assert "inventory accounting: 2 rows = 1 emitted + 1 skipped + 0 refused" in err
    assert "skipped m2: routed-out: plan not prepped" in err
    assert "edited docs/plans/a.md: C1, C2 -> coded" in err
    assert "edited docs/plans/b.md: C3 -> coded" in err
    assert err.count("edited ") == 2


def test_single_emit_without_plan_edits_prints_no_edited_line(repo, monkeypatch, capsys):
    inv = repo / "inv.md"
    inv.write_text("---\nrun_id: r1\n---\n", encoding="utf-8")
    reply = {
        "ok": True,
        "path": str(repo / "r1.workflow.mjs"),
        "sha256": "x",
        "inventory_accounting": _acc(["m1"]),
        "landed_reconciled": {},
    }
    monkeypatch.setattr(cli, "_dispatch_emit", lambda params, repo_root=None: dict(reply))
    assert cli.main(["--inventory", str(inv), "--out", str(repo / "r1.workflow.mjs")]) == cli.EXIT_OK
    _, err = capsys.readouterr()
    assert "inventory accounting: 1 rows = 1 emitted" in err
    assert "edited " not in err


def test_parts_mode_merges_accounting_and_plan_edits_and_names_both_totals(tmp_path, monkeypatch, capsys):
    tranche = tmp_path / "run7.md"
    tranche.write_text("---\nrun_id: run7\n---\n", encoding="utf-8")
    from coordinator_core.ops.dispatch_emit import inventory_accounting as ia

    replies = {
        1: {
            "inventory_accounting": _acc(
                ["m1", "m2"], skipped=[("m3", ia.IN_ANOTHER_PART), ("m4", "routed-out: x")]
            ),
            "landed_reconciled": {"docs/plans/a.md": ["C1"]},
        },
        2: {
            "inventory_accounting": _acc(
                ["m3"], skipped=[("m1", ia.IN_ANOTHER_PART), ("m2", ia.IN_ANOTHER_PART), ("m4", "routed-out: x")]
            ),
            "landed_reconciled": {"docs/plans/a.md": ["C1", "C2"], "docs/plans/b.md": ["C9"]},
        },
    }

    def fake_emit(params, repo_root=None):
        index = params["inventory_part"][0]
        return {"ok": True, "path": params["output_path"], **replies[index]}

    monkeypatch.setattr(cli, "_dispatch_emit", fake_emit)
    over = SimpleNamespace(script_size=cli._emit._WORKFLOW_SCRIPT_BYTE_CAP * 2, row_count=44, tranche_inventory=str(tranche))
    over.script_size = int(cli._emit._WORKFLOW_SCRIPT_BYTE_CAP * 1.5)
    assert cli._emit_inventory_parts({"inventory_path": str(tranche)}, None, over, False, {}) == cli.EXIT_OK
    out, err = capsys.readouterr()
    json.loads(out)
    assert "44 minted rows from 4 inventory rows" in err
    assert "inventory accounting: 4 rows = 3 emitted + 1 skipped + 0 refused" in err
    assert "skipped m4: routed-out: x" in err
    assert "edited docs/plans/a.md: C1, C2 -> coded" in err
    assert "edited docs/plans/b.md: C9 -> coded" in err
    assert err.count("edited ") == 2


def test_parts_mode_with_an_unplaced_item_refuses(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import inventory_accounting as ia

    tranche = tmp_path / "run7.md"
    tranche.write_text("---\nrun_id: run7\n---\n", encoding="utf-8")

    def fake_emit(params, repo_root=None):
        return {
            "ok": True,
            "path": params["output_path"],
            "inventory_accounting": _acc(["m1"], skipped=[("m2", ia.IN_ANOTHER_PART)]),
        }

    monkeypatch.setattr(cli, "_dispatch_emit", fake_emit)
    over = SimpleNamespace(script_size=int(cli._emit._WORKFLOW_SCRIPT_BYTE_CAP * 1.5), row_count=2, tranche_inventory=str(tranche))
    with pytest.raises(ia.InventoryAccountingError):
        cli._emit_inventory_parts({"inventory_path": str(tranche)}, None, over, False, {})
