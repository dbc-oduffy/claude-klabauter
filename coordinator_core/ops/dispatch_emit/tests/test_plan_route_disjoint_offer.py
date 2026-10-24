"""The `--plan` route prints the disjoint-write split offer on stderr and changes nothing else."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import dispatch_row_lint, plan_route
from coordinator_core.ops.dispatch_emit.tests.test_plan_route import (  # noqa: F401
    _quiet_env,
    _repo,
    _run,
    _write_set_reads_clean,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CONJOINED = (
    "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
    '- id: C7\n  title: Add alpha and beta\n  writes: ["x/alpha.py", "y/beta.md"]\n  body: |\n'
    "    BODY\n```\n"
)
_OFFER_MARK = "row C7 writes 2 disjoint targets"


def _plan(body: str) -> str:
    return _CONJOINED.replace("BODY", body)


def test_conjoined_spine_prints_the_offer_and_exit_is_unchanged(tmp_path, capsys):
    repo, plan = _repo(tmp_path, _plan("Write both."))
    assert _run(repo, plan) == plan_route.EXIT_OK
    captured = capsys.readouterr()
    assert _OFFER_MARK in captured.err
    assert captured.err.count(_OFFER_MARK) == 1
    assert json.loads(captured.out)["ok"] is True


def test_coherence_line_quiets_the_offer(tmp_path, capsys):
    repo, plan = _repo(tmp_path, _plan("Coherence: one change.\n    Write both."))
    assert _run(repo, plan) == plan_route.EXIT_OK
    assert _OFFER_MARK not in capsys.readouterr().err


def test_offer_does_not_change_script_bytes_or_receipt(tmp_path, monkeypatch):
    repo, plan = _repo(tmp_path, _plan("Write both."))
    out = repo / "out" / "p.workflow.mjs"
    receipt_path = out.with_name(out.name + ".emitted.json")
    seen = []
    for offer in ("split this row", None):
        monkeypatch.setattr(dispatch_row_lint, "disjoint_write_offer", lambda row, _o=offer: _o)
        assert _run(repo, plan, "--force") == plan_route.EXIT_OK
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt.pop("emitted_at", None)
        seen.append((out.read_bytes(), receipt))
    assert seen[0] == seen[1]


def test_offer_pass_adds_no_read_spine_call_and_unreadable_spine_prints_none(tmp_path, monkeypatch, capsys):
    repo, plan = _repo(tmp_path, _plan("Write both."))
    calls = []
    real = plan_route.read_spine

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(plan_route, "read_spine", counting)
    monkeypatch.setattr(plan_route, "_write_set_provably_clean", lambda *a, **k: False)
    assert _run(repo, plan) == plan_route.EXIT_OK
    assert len(calls) == 1
    assert _OFFER_MARK in capsys.readouterr().err

    broken = tmp_path / "broken"
    broken.mkdir()
    repo2, plan2 = _repo(broken, "---\n---\n\n# A plan\n\nno spine here\n")
    code = _run(repo2, plan2)
    err = capsys.readouterr().err
    assert code != plan_route.EXIT_OK
    assert "ERROR" in err and "disjoint targets" not in err
