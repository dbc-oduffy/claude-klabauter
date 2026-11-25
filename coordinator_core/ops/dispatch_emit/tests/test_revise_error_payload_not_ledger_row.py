"""workflow.revise refuses a JSON-RPC error envelope as a verbatim: no register row, no queued revision."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import requirement_register as rr
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

RUN = "ask-20261125T000001-abc124"
REL = "state/sizings/2026-11-25-error-payload.yaml"
ENVELOPE = json.dumps({"jsonrpc": "2.0", "id": 7, "error": {"code": -32004, "message": "warm dispatch failed"}})


def _sizing():
    return {
        "schema": "sizing-object", "intent": "do it", "estimate": {"tshirt": "S", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "lint the thing"}],
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / REL).write_text(yaml.safe_dump(_sizing(), sort_keys=False), encoding="utf-8")
    rl.start(tmp_path, RUN)
    rl.bind(tmp_path, RUN, REL)
    return tmp_path


def _revise(repo, **params):
    return rl._handler({"run_id": RUN, **params}, repo_root=repo)


@pytest.mark.parametrize(
    "words",
    [
        ENVELOPE,
        "  " + ENVELOPE + "\n",
        'warm dispatch replied {"jsonrpc":"2.0","error":{"code": -32004,"message":"x"},"id":1} -- ledger it',
    ],
)
def test_an_error_envelope_verbatim_is_refused_and_adds_no_row(repo, words) -> None:
    before = (repo / REL).read_bytes()
    out = _revise(repo, verbatim=words)
    assert "error" in out and "JSON-RPC error payload" in out["error"] and "-32004" in out["error"], out
    assert (repo / REL).read_bytes() == before
    assert [r["id"] for r in rr.read_register((repo / REL).read_text(encoding="utf-8")).rows] == ["R1"]


def test_an_error_envelope_with_out_of_scope_is_refused(repo) -> None:
    before = (repo / REL).read_bytes()
    out = _revise(repo, verbatim=ENVELOPE, out_of_scope=["R1"])
    assert "JSON-RPC error payload" in out["error"]
    assert (repo / REL).read_bytes() == before


def test_a_queued_run_refuses_an_error_envelope_too(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    rl.start(tmp_path, RUN)
    out = _revise(tmp_path, verbatim=ENVELOPE)
    assert "JSON-RPC error payload" in out["error"]
    assert not (tmp_path / rl.RECORD_DIR_ROOT / RUN / rl.INBOX_FILE).exists()


@pytest.mark.parametrize(
    "words",
    [
        "also add a changelog entry",
        "handle the -32004 error code in the client",
        json.dumps({"result": {"ok": True}}),
        json.dumps({"error": "plain string, not an envelope"}),
        '{"note": "the error code is -32004"}',
    ],
)
def test_ordinary_requirement_words_still_mint_a_row(repo, words) -> None:
    out = _revise(repo, verbatim=words)
    assert out["ok"] and out["item"] == "R2", out


def test_error_envelope_code_reads_the_code() -> None:
    assert rl.error_envelope_code(ENVELOPE) == -32004
    assert rl.error_envelope_code("lint the thing") is None
