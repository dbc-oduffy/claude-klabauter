"""Cold-process pin: a fresh-interpreter guard() call per host answers inside DEFAULT_BUDGET_SECS of process time."""
import importlib.util
import json
import os
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


def allow_rows():
    try:
        lines = (guard_mod.degrade_log_path().parent / guard_mod.ALLOW_LOG_NAME).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


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


RUNS = 5
WALL_BUDGET = 0.5
SINGLE_PROCESS_BUDGET = 0.2
ROUTE_DENY, ROUTE_ALLOW = "engine", "neutral-unknown"
BACKSTOP_CLASS_DENIES = frozenset(("rm_rf", "git_clean", "git_stash_clear"))


@pytest.fixture
def fixture_repo(tmp_path):
    """A non-scratch git work tree with uncommitted and load-bearing untracked paths, built under tmp_path."""
    repo = tmp_path / "codex-fixture"
    probe.prepare_codex_cold_repo(repo)
    assert "scratch" not in repo.parts
    return repo


@pytest.mark.parametrize("case", probe.CODEX_COLD_CASES, ids=[c[0] for c in probe.CODEX_COLD_CASES])
def test_forced_cold_codex_launcher_answers_explicitly_inside_wall(case, fixture_repo, tmp_path):
    """Each case, RUNS fresh launcher processes on an unreachable engine: an explicit verdict and route label, never a
    timeout or neutral degrade, wall under the launcher's 0.5s, process time under budget, zero child spawns."""
    name = case[0]
    expect_deny = name in probe.CODEX_COLD_DENIED
    resolved = os.path.normcase(os.path.realpath(fixture_repo))
    if name in ("rm_rf", "git_clean") and os.name != "nt" and resolved.startswith("/tmp/"):
        pytest.skip("POSIX /tmp is exempt from the rm and git clean denies regardless of TMPDIR")
    env = probe.cold_child_env(os.environ, tmp_path / "child-tmp")
    walls = []
    for _ in range(RUNS):
        before = allow_rows()
        row = probe.codex_cold_run(case, fixture_repo, str(probe.REPO), env)
        assert "error" not in row, row
        new_rows = allow_rows()[len(before):]
        if expect_deny:
            assert len(new_rows) == 2, new_rows
            for deny_row in new_rows:
                assert deny_row["kind"] == "evaluated-deny" and deny_row["verdict"] == "deny", deny_row
                assert deny_row["route"] == "cold" and deny_row["verdicts"], deny_row
                assert deny_row["chain"] == "full", deny_row
                assert deny_row["backstop_class"] is (name in BACKSTOP_CLASS_DENIES), deny_row
                assert deny_row["verdicts"][0] == {"guard": "backstop", "verdict": "allow"}, deny_row
                assert deny_row["verdicts"][-1]["verdict"] == "deny", deny_row
        else:
            # codex_cold_run launches the hook twice (wall launcher, then the counted process): one row each.
            assert len(new_rows) == 2, new_rows
            for allow in new_rows:
                assert allow["kind"] == "evaluated-allow" and allow["host"] == "codex", allow
                assert allow["route"] == "cold", allow
                assert allow["guards_evaluated"], allow
                if name == "ls":
                    assert allow["chain"] == "backstop-only", allow
                    assert allow["verdicts"] == [{"guard": "backstop", "verdict": "allow"}], allow
        assert row["route"] == (ROUTE_DENY if expect_deny else ROUTE_ALLOW), row
        assert row["denied"] is expect_deny, row
        if expect_deny:
            body = row["answer"]["hookSpecificOutput"]
            assert body["permissionDecisionReason"], row
        else:
            assert row["answer"] == {}, row
        walls.append(row["launcher_wall_secs"])
        assert row["whole_process_secs"] < BUDGET, (row, new_rows)
        assert row["in_join_secs"] < SINGLE_PROCESS_BUDGET, (row, new_rows)
        assert row["spawned_children"] == 0, (row, new_rows)
    assert min(walls) < WALL_BUDGET, walls  # best-of-RUNS: wall includes peer load; process time is asserted per run
    assert "timeout" not in guard_degrade_causes()


def test_codex_fixture_base_refuses_unset_settings_home(monkeypatch):
    monkeypatch.delenv(probe.SETTINGS_HOME_KEY)
    with pytest.raises(probe.SettingsHomeKeyMissing, match=probe.SETTINGS_HOME_KEY):
        probe.codex_fixture_base()


@pytest.mark.parametrize("host", ["codex", "agy"])
def test_cold_host_answers_inside_budget_without_import_error(host, tmp_path):
    row = cold_child_row(host, tmp_path)
    assert row["whole_process_secs"] < BUDGET, row
    assert "import_error" not in guard_degrade_causes(), row
