"""model-for-role CLI: resolves through model_for_role, BLOCKED on an unverified slot."""

import subprocess
import sys
from pathlib import Path

from coordinator_core.agent_pipe.model_tiers import model_for_role

BIN = Path(__file__).resolve().parents[1] / "model-for-role.py"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(BIN), *args], capture_output=True, text=True, cwd=cwd,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_prints_the_resolved_model_per_family():
    for family in ("claude", "codex", "grok"):
        r = _run(family, "executor")
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == model_for_role(family, "executor")


def test_reviewer_role_resolves_its_own_tier():
    r = _run("codex", "reviewer")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == model_for_role("codex", "reviewer")


def test_agy_executor_prints_its_verified_sonnet_slot():
    r = _run("agy", "executor")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "gemini-4-hydrogen-medium"


def test_unverified_slot_is_blocked_naming_the_slot_and_never_falls_back(monkeypatch, capsys):
    import importlib.util

    from coordinator_core.agent_pipe import model_tiers

    spec = importlib.util.spec_from_file_location("model_for_role_cli", BIN)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setitem(model_tiers.PROVIDER_TIER_MODELS["agy"], "sonnet", None)
    assert cli.main(["agy", "executor"]) == 3
    out = capsys.readouterr()
    assert out.out == ""
    assert "BLOCKED" in out.err and "agy" in out.err and "sonnet" in out.err


def test_unknown_role_and_family_and_bad_arity_are_usage_errors():
    assert _run("codex", "janitor").returncode == 2
    assert _run("nofamily", "executor").returncode == 2
    assert _run("codex").returncode == 2


def test_root_comes_from_the_file_not_the_cwd(tmp_path):
    r = _run("codex", "executor", cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == model_for_role("codex", "executor")


def test_windows_shims_sit_beside_the_entrypoint():
    for ext in (".cmd", ".ps1"):
        shim = BIN.with_suffix(ext)
        assert shim.is_file()
        assert "model-for-role.py" in shim.read_text(encoding="utf-8")
