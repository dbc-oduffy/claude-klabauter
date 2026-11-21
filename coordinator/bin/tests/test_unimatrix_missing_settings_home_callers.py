"""Callers of roster.settings_home() surface an unset COORDINATOR_SETTINGS_HOME instead of crashing or going silent;
the membership signer keeps signing under a restricted env."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), BIN / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_doctor_reports_an_unset_settings_home_as_its_one_problem(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    roster = load("unimatrix-roster")
    out = io.StringIO()
    rc = load("unimatrix-doctor").main([], out=out, roster=roster, autolink=object())
    assert rc == 1
    assert out.getvalue().splitlines() == [
        "problem: missing-settings: COORDINATOR_SETTINGS_HOME is unset; set it to the settings home"]


@pytest.mark.skipif(os.name != "nt" or shutil.which("ssh-keygen") is None, reason="Windows OpenSSH only")
def test_a_restricted_env_signer_still_signs_on_windows(tmp_path):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "k")], check=True,
                   capture_output=True, timeout=30)
    assert load("unimatrix-membership").FakeSigner(tmp_path / "k", environ={}).sign(b"payload").startswith(
        b"-----BEGIN SSH SIGNATURE-----")


def test_agy_sessionstart_starts_the_session_and_names_the_unset_key(monkeypatch):
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
    hook = load("unimatrix-agy-hook-sessionstart")
    roster = load("unimatrix-roster")

    def register(payload, agy_pid):
        return roster.settings_home()

    monkeypatch.setattr(hook, "register", register)
    monkeypatch.setattr(hook, "load_sibling", lambda name: type("S", (), {
        "worker_pointer": staticmethod(lambda: None), "agy_ancestor_pid": staticmethod(lambda: None)}))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"conversationId": "c"})))
    printed = io.StringIO()
    monkeypatch.setattr("sys.stdout", printed)
    assert hook.main() == 0
    steps = json.loads(printed.getvalue())["injectSteps"]
    assert [s["systemMessage"]["systemMessage"] for s in steps] == [
        "unimatrix: peer registration failed: missing-settings: COORDINATOR_SETTINGS_HOME is unset; "
        "set it to the settings home"]
