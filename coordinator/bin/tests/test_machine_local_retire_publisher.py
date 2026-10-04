"""`machine-local retire-publisher`, and the messages `unset` prints on a no-op
or when a lower layer still answers the key."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"

_PUBLISHER_REGISTRY = """\
[engine.working_repos]
Content_root = '/x'

[publish.mirrors.coordinator_claude]
path = '/a'
owner = 'me'

[publish]
targets = ["a", "b"]
"""


def _run(reg: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, MACHINE_LOCAL_REGISTRY_DIR=str(reg))
    return subprocess.run(
        [sys.executable, str(_IMPL), *argv], env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_retire_publisher_removes_every_publish_key_and_keeps_the_rest(tmp_path):
    (tmp_path / "registry.local.toml").write_text(_PUBLISHER_REGISTRY, encoding="utf-8")

    proc = _run(tmp_path, "retire-publisher")

    assert proc.returncode == 0, proc.stderr
    flat = tomllib.loads((tmp_path / "registry.local.toml").read_text(encoding="utf-8"))
    assert "publish" not in flat
    assert "[publish" not in (tmp_path / "registry.local.toml").read_text(encoding="utf-8")
    assert flat["engine"]["working_repos"]["content_root"] == "/x"
    assert "3 key(s)" in proc.stdout

    again = _run(tmp_path, "retire-publisher")
    assert again.returncode == 0 and "nothing to retire" in again.stdout


def test_retire_publisher_dry_run_writes_nothing(tmp_path):
    reg = tmp_path / "registry.local.toml"
    reg.write_text(_PUBLISHER_REGISTRY, encoding="utf-8")

    proc = _run(tmp_path, "retire-publisher", "--dry-run")

    assert proc.returncode == 0
    assert reg.read_text(encoding="utf-8") == _PUBLISHER_REGISTRY
    assert "publish.targets" in proc.stdout


def test_unset_of_an_absent_key_says_so(tmp_path):
    (tmp_path / "registry.local.toml").write_text("[a]\nb = 'c'\n", encoding="utf-8")

    proc = _run(tmp_path, "unset", "no.such.key")

    assert proc.returncode == 1
    assert "not set" in proc.stderr


def test_unset_names_the_layer_that_still_answers(tmp_path):
    (tmp_path / "registry.toml").write_text("[k]\nv = 'base'\n", encoding="utf-8")
    (tmp_path / "registry.local.toml").write_text("[k]\nv = 'local'\n", encoding="utf-8")

    proc = _run(tmp_path, "unset", "k.v")

    assert proc.returncode == 0
    assert "still resolves to 'base'" in proc.stderr


def test_retire_publisher_keeps_a_header_that_still_carries_a_comment(tmp_path):
    reg = tmp_path / "registry.local.toml"
    reg.write_text("[publish]\n# operator note\ntargets = [\"a\"]\n", encoding="utf-8")

    assert _run(tmp_path, "retire-publisher").returncode == 0
    assert "# operator note" in reg.read_text(encoding="utf-8")


def test_dump_show_origin_marks_a_defaults_layer_value(tmp_path):
    (tmp_path / "registry.toml").write_text("[k]\nv = ''\nw = 'base'\n", encoding="utf-8")
    (tmp_path / "registry.local.toml").write_text("[k]\nw = 'local'\n", encoding="utf-8")

    proc = _run(tmp_path, "dump", "--prefix", "k", "--show-origin")

    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["k.v"] == {"value": "", "layer": "registry.toml (defaults)"}
    assert out["k.w"] == {"value": "local", "layer": "registry.local.toml"}
    plain = json.loads(_run(tmp_path, "dump", "--prefix", "k").stdout)
    assert plain == {"k.v": "", "k.w": "local"}
