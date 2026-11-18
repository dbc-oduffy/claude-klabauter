"""Cold-process pin: a fresh-interpreter guard() call per host answers inside DEFAULT_BUDGET_SECS of process time."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_guard_cost_probe as probe  # noqa: E402

GUARD_SPEC = importlib.util.spec_from_file_location("unimatrix_host_guard_cold", probe.BIN / "unimatrix-host-guard.py")
guard_mod = importlib.util.module_from_spec(GUARD_SPEC)
GUARD_SPEC.loader.exec_module(guard_mod)
BUDGET = guard_mod.DEFAULT_BUDGET_SECS


@pytest.fixture(autouse=True)
def scratch_env(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    warm = tmp_path / "warm-base"
    warm.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("COORDINATOR_WARM_RUNTIME_BASE", str(warm))


def guard_degrade_causes():
    try:
        lines = guard_mod.degrade_log_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line).get("cause") for line in lines if line.strip()]


def cold_child_row(host, tmp_path):
    """One guard() call in a fresh interpreter; the payload cwd is tmp_path so no session state lands in the real repo."""
    payload = dict(probe.PAYLOADS[host])
    if "cwd" in payload:
        payload["cwd"] = str(tmp_path)
    out = probe.run_child([str(probe.REPO), host, json.dumps(payload)])
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cold_grok_delivers_nag_inside_budget(tmp_path):
    row, _imports = probe.guard_row("grok", tmp_path)
    assert "error" not in row, row
    assert row["whole_process_secs"] < BUDGET, row
    assert row["nag_in_answer"], row
    assert row["answer"]["hookSpecificOutput"]["additionalContext"], row


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_cold_host_answers_inside_budget_without_import_error(host, tmp_path):
    row = cold_child_row(host, tmp_path)
    assert row["whole_process_secs"] < BUDGET, row
    assert "import_error" not in guard_degrade_causes(), row
