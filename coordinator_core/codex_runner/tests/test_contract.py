"""Tests for codex_runner.contract and the findings schema / fake codex fixture."""
import importlib.util
import json
from pathlib import Path

import pytest

from coordinator_core.codex_runner import contract

FIXTURES = Path(__file__).parent / "fixtures"


def test_review_request_requires_artifact_only():
    contract.RunRequest(mode="review", repo_root="/r", artifact="a.md")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="review", repo_root="/r")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="review", repo_root="/r", artifact="a", plan="p", chunk="C0")


def test_agy_review_request_requires_artifact_only():
    assert "agy-review" in contract.MODES
    contract.RunRequest(mode="agy-review", repo_root="/r", artifact="a.md")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="agy-review", repo_root="/r")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="agy-review", repo_root="/r", artifact="a", plan="p", chunk="C0")


def test_execute_request_requires_plan_and_chunk_only():
    contract.RunRequest(mode="execute", repo_root="/r", plan="p.md", chunk="C0")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="execute", repo_root="/r", plan="p.md")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="execute", repo_root="/r", plan="p.md", chunk="C0", artifact="a")


def test_unknown_mode_and_missing_repo_root_refused():
    with pytest.raises(ValueError):
        contract.RunRequest(mode="chat", repo_root="/r", artifact="a")
    with pytest.raises(ValueError):
        contract.RunRequest(mode="review", repo_root="", artifact="a")


def test_request_and_result_are_frozen_and_roundtrip():
    req = contract.RunRequest(mode="review", repo_root="/r", artifact="a", parent_session_id="s")
    with pytest.raises(Exception):
        req.mode = "execute"  # type: ignore[misc]
    assert contract.RunRequest.from_dict(req.to_dict()) == req
    res = contract.RunResult(
        run_id="r1", status="limit_exhausted", fallback=contract.FALLBACK_REVIEWER, codex_pid=7
    )
    assert contract.RunResult.from_dict(json.loads(json.dumps(res.to_dict()))) == res


def test_allow_api_key_defaults_off_and_roundtrips():
    req = contract.RunRequest(mode="review", repo_root="/r", artifact="a")
    assert req.allow_api_key is False and req.to_dict()["allow_api_key"] is False
    on = contract.RunRequest(mode="review", repo_root="/r", artifact="a", allow_api_key=True)
    assert contract.RunRequest.from_dict(json.loads(json.dumps(on.to_dict()))).allow_api_key is True
    legacy = {"mode": "review", "repo_root": "/r", "artifact": "a"}
    assert contract.RunRequest.from_dict(legacy).allow_api_key is False
    assert contract.RunRequest.from_dict({**legacy, "allow_api_key": "yes"}).allow_api_key is False


def test_status_set_is_closed():
    assert contract.STATUS == {"ok", "limit_exhausted", "confinement_breach", "scope_breach", "failed"}
    with pytest.raises(ValueError):
        contract.RunResult(run_id="r", status="done")
    assert contract.FALLBACK_REVIEWER == "claude-reviewer"


def test_run_dir_under_settings_home(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path))
    assert contract.run_dir("abc") == tmp_path / "codex-runs" / "abc"
    for bad in ("", "..", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            contract.run_dir(bad)


def test_schema_is_strict_and_shaped():
    schema = json.loads(contract.findings_schema_path().read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"verdict", "findings"}
    assert schema["properties"]["verdict"]["enum"] == ["OK", "WARN", "FAIL"]
    item = schema["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    # codex --output-schema strict mode: every property must be required.
    assert set(item["required"]) == set(item["properties"]) == {"severity", "title", "location", "detail"}


def _load_fake():
    spec = importlib.util.spec_from_file_location("fake_codex", FIXTURES / "fake_codex.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fake_codex_replays_jsonl_and_writes_last_json(tmp_path, monkeypatch, capsys):
    jsonl = tmp_path / "run.jsonl"
    jsonl.write_text('{"type":"a"}\n{"type":"b"}', encoding="utf-8")
    last_src = tmp_path / "src.json"
    last_src.write_text('{"verdict":"OK","findings":[]}', encoding="utf-8")
    dest = tmp_path / "last.json"
    monkeypatch.setenv("FAKE_CODEX_JSONL", str(jsonl))
    monkeypatch.setenv("FAKE_CODEX_LAST_JSON", str(last_src))
    monkeypatch.setenv("FAKE_CODEX_EXIT", "3")
    rc = _load_fake().main(["exec", "--json", "-o", str(dest)])
    assert rc == 3
    assert capsys.readouterr().out.splitlines() == ['{"type":"a"}', '{"type":"b"}']
    assert json.loads(dest.read_text(encoding="utf-8"))["verdict"] == "OK"
