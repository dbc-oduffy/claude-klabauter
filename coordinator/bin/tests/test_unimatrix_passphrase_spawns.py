"""Tests for unimatrix-passphrase.py: the 1Password source, askpass feed, encrypted mint and agent load."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

_SRC = Path(__file__).resolve().parents[1] / "unimatrix-passphrase.py"
_spec = importlib.util.spec_from_file_location("unimatrix_passphrase", _SRC)
pp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pp)

SECRET = "correct-horse-battery-staple-9"


def test_script_entry_with_ref_env_and_prompt_arg_takes_askpass_path_not_usage(monkeypatch):
    script = Path(pp.__file__)
    env = {**os.environ, pp.REF_ENV: "op://Vault/none/password", "PATH": os.devnull}
    with_ref = subprocess.run([sys.executable, str(script), 'Enter passphrase for "k": '],
                              env=env, capture_output=True, text=True, timeout=30)
    assert "askpass:" in with_ref.stderr and with_ref.returncode == 1
    env.pop(pp.REF_ENV)
    no_ref = subprocess.run([sys.executable, str(script), 'Enter passphrase for "k": '],
                            env=env, capture_output=True, text=True, timeout=30)
    assert no_ref.returncode == 2 and "askpass:" not in no_ref.stderr
