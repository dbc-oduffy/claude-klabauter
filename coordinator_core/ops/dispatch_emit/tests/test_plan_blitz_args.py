"""Covers plan_blitz_args.resolve: roster detection, key omission, gateReportPath passthrough."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import plan_blitz_args as pba


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setattr(pba.shutil, "which", lambda name: None)
    return home


def _plugin(tmp_path: Path, agents=("plan-author.md", "blitz-em.md"), bins=()) -> Path:
    root = tmp_path / "plugin"
    (root / "agents").mkdir(parents=True)
    (root / "bin").mkdir()
    for a in agents:
        (root / "agents" / a).write_text("x", encoding="utf-8")
    for b in bins:
        (root / "bin" / b).write_text("x", encoding="utf-8")
    return root


def test_full_roster_true_and_gate_path_passthrough(isolated, tmp_path):
    out = pba.resolve(plugin_root=_plugin(tmp_path), engine_root=None, sizing_abs="/abs/s.yaml")
    assert out["pluginAgentsAvailable"] is True
    assert out["gateReportPath"] == "/abs/s.yaml"


def test_missing_agents_dir_is_false_key_present(isolated, tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert out["pluginAgentsAvailable"] is False


def test_no_plugin_root_is_false(isolated):
    out = pba.resolve(plugin_root=None, engine_root=None, sizing_abs="s")
    assert out["pluginAgentsAvailable"] is False


def test_unresolvable_clis_are_omitted_not_null(isolated, tmp_path):
    out = pba.resolve(plugin_root=_plugin(tmp_path), engine_root=None, sizing_abs="s")
    assert set(out) == {"pluginAgentsAvailable", "gateReportPath"}


def test_plugin_bins_resolve_when_present(isolated, tmp_path):
    root = _plugin(tmp_path, bins=("plan-spine-check.py", "instrument-can-report-red.py"))
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert "plan-spine-check.py" in out["spineCheckCli"]
    assert "instrument-can-report-red.py" in out["armingCheckCli"]


def test_sidecar_resolves_from_settings_home(isolated, tmp_path):
    (isolated / "bin").mkdir()
    (isolated / "bin" / "provision-sidecar").write_text("x", encoding="utf-8")
    out = pba.resolve(plugin_root=None, engine_root=None, sizing_abs="s")
    assert out["provisionSidecarCli"].endswith("provision-sidecar")


def test_sidecar_resolves_from_engine_bin(isolated, tmp_path):
    eng = tmp_path / "eng"
    (eng / "coordinator" / "bin").mkdir(parents=True)
    (eng / "coordinator" / "bin" / "provision-sidecar.py").write_text("x", encoding="utf-8")
    out = pba.resolve(plugin_root=None, engine_root=eng, sizing_abs="s")
    assert "provision-sidecar.py" in out["provisionSidecarCli"]


def test_chatty_run_id_none_adds_no_keys(isolated, tmp_path):
    root = _plugin(tmp_path)
    plain = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    explicit = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s", chatty_run_id=None)
    assert plain == explicit
    assert "chatty" not in plain and "chattyReview" not in plain


_ROSTER = {
    "plan_review": {
        "tiers": {
            "M": {"reviewers": [{"agentType": "coordinator:staff-eng", "model": "opus"}]},
            "XXL": {
                "reviewers": [
                    {"agentType": "coordinator:staff-eng", "model": "opus"},
                    {"agentType": "coordinator:the Staff Engineer", "model": "sonnet"},
                ]
            },
        },
        "signal_reviewers": {},
        "lead_round_cap": None,
        "revise": {"step": "merged", "after": "parallel-review-wave"},
    }
}


def _write_roster(root: Path, data) -> None:
    d = root / "coordinator" / "contract"
    d.mkdir(parents=True, exist_ok=True)
    (d / "review-roster-fragment.json").write_text(json.dumps(data), encoding="utf-8")


def test_default_path_emits_parallel_review_from_roster(isolated, tmp_path):
    root = _plugin(tmp_path)
    _write_roster(root, _ROSTER)
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert out["parallelReview"]["models"] == {"coordinator:staff-eng": "opus", "coordinator:the Staff Engineer": "sonnet"}
    assert out["parallelReview"]["tiers"] == {
        "coordinator:staff-eng": ["M", "XXL"],
        "coordinator:the Staff Engineer": ["XXL"],
    }
    assert out["parallelReview"]["revise"] == {"agent": "coordinator:plan-author", "model": "opus"}


def test_roster_edit_changes_wave_without_code_edit(isolated, tmp_path):
    root = _plugin(tmp_path)
    _write_roster(root, _ROSTER)
    first = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")["parallelReview"]
    edited = json.loads(json.dumps(_ROSTER))
    edited["plan_review"]["tiers"]["XXL"]["reviewers"][1]["model"] = "haiku"
    edited["plan_review"]["tiers"]["L"] = {
        "reviewers": [{"agentType": "coordinator:the Staff Engineer", "model": "haiku"}]
    }
    _write_roster(root, edited)
    second = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")["parallelReview"]
    assert second != first
    assert second["models"]["coordinator:the Staff Engineer"] == "haiku"
    assert second["tiers"]["coordinator:the Staff Engineer"] == ["XXL", "L"]


def test_roster_read_from_plugin_root_contract_dir(isolated, tmp_path):
    root = _plugin(tmp_path)
    (root / "contract").mkdir()
    (root / "contract" / "review-roster-fragment.json").write_text(json.dumps(_ROSTER), encoding="utf-8")
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert out["parallelReview"]["revise"]["agent"] == "coordinator:plan-author"


def test_chatty_branch_has_no_parallel_review(isolated, tmp_path):
    root = _plugin(tmp_path)
    _write_roster(root, _ROSTER)
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s", chatty_run_id="r")
    assert "parallelReview" not in out
    assert "chattyReview" in out


def test_parallel_review_omitted_when_unresolvable(isolated, tmp_path):
    root = _plugin(tmp_path)
    assert "parallelReview" not in pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    _write_roster(root, {"other": 1})
    assert "parallelReview" not in pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    (root / "coordinator" / "contract" / "review-roster-fragment.json").write_text("{bad", encoding="utf-8")
    assert "parallelReview" not in pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert "parallelReview" not in pba.resolve(plugin_root=None, engine_root=None, sizing_abs="s")


def test_chatty_run_id_adds_review_wave(isolated, tmp_path):
    out = pba.resolve(
        plugin_root=_plugin(tmp_path), engine_root=None, sizing_abs="s", chatty_run_id="run-9"
    )
    assert out["chatty"] is True
    assert out["chattyReview"]["roster"]["run_id"] == "run-9"
    assert out["pluginAgentsAvailable"] is True


_BLOCK = {
    "tiers": {"XS": {"reviewers": [{"agentType": "coordinator:plan-reviewer", "model": "sonnet"}]}},
    "signal_reviewers": {"data-science": {"agentType": "coordinator:staff-data-sci", "model": "opus"}},
    "revise": {"step": "merged", "after": "parallel-review-wave"},
}


def _write_fragment(root: Path, rel: Path, body: str) -> None:
    (root / rel).mkdir(parents=True, exist_ok=True)
    (root / rel / "review-roster-fragment.json").write_text(body, encoding="utf-8")


@pytest.mark.parametrize("rel", [Path("contract"), Path("coordinator") / "contract"])
def test_plan_review_block_is_forwarded_as_arg(isolated, tmp_path, rel):
    root = _plugin(tmp_path)
    _write_fragment(root, rel, json.dumps({"plan_review": _BLOCK}))
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert out["plan_review"] == _BLOCK


@pytest.mark.parametrize("body", [None, "{not json", '{"execute_review": {}}', '{"plan_review": []}', '{"plan_review": {"tiers": {}}}'])
def test_unusable_plan_review_block_is_omitted(isolated, tmp_path, body):
    root = _plugin(tmp_path)
    if body is not None:
        _write_fragment(root, Path("contract"), body)
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s")
    assert "plan_review" not in out


def test_plan_review_block_forwarded_on_chatty_path_too(isolated, tmp_path):
    root = _plugin(tmp_path)
    _write_fragment(root, Path("contract"), json.dumps({"plan_review": _BLOCK}))
    out = pba.resolve(plugin_root=root, engine_root=None, sizing_abs="s", chatty_run_id="r")
    assert out["plan_review"] == _BLOCK
