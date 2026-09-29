"""install-sentinel-write keeps version.txt a 40-hex SHA and adds plugin-version.txt."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "install_sentinel_write", Path(__file__).with_name("install-sentinel-write.py")
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

SHA = "a" * 40


def _source(tmp_path: Path, version: str | None) -> Path:
    src = tmp_path / "src"
    (src / ".claude-plugin").mkdir(parents=True)
    if version is not None:
        (src / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return src


def test_writes_sha_baseline_and_plugin_version(tmp_path):
    src = _source(tmp_path, "4.3.0")
    dest = tmp_path / "live"
    dest.mkdir()
    assert mod.main(["--path", str(dest), "--source", str(src), "--sha", SHA]) == 0
    assert (dest / "version.txt").read_text() == SHA + "\n"
    assert (dest / "plugin-version.txt").read_text() == "4.3.0\n"


def test_no_plugin_json_leaves_only_the_sha(tmp_path):
    src = _source(tmp_path, None)
    dest = tmp_path / "live"
    dest.mkdir()
    assert mod.main(["--path", str(dest), "--source", str(src), "--sha", SHA]) == 0
    assert (dest / "version.txt").read_text() == SHA + "\n"
    assert not (dest / "plugin-version.txt").exists()
