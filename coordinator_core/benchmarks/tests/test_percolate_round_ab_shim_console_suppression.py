"""Guards `percolate_round_ab._SHIM_SOURCE`'s own `dump` spawn against the
Windows console-popup defect (bug-backlog 2026-08-30-nine-benchmark-modules-
spawn-children-wi-6742e0d41435): the generated `machine_local_shim.py`
inlines `getattr(subprocess, "CREATE_NO_WINDOW", 0)` into its own
`subprocess.run` call, since it runs standalone from a scratch dir with no
guarantee `coordinator_core` is importable there.
"""

from __future__ import annotations

import ast

from coordinator_core.benchmarks.percolate_round_ab import (
    _SHIM_SOURCE,
    _write_machine_local_shim,
)


def _formatted_shim_source() -> str:
    return _SHIM_SOURCE.format(real_bin="/bin/does-not-matter", overrides={})


def test_shim_source_is_syntactically_valid_python():
    ast.parse(_formatted_shim_source())


def test_shim_source_dump_spawn_suppresses_windows_console():
    tree = ast.parse(_formatted_shim_source())
    run_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert run_calls, "expected exactly one subprocess.run call in the generated shim"
    keyword_names = {kw.arg for call in run_calls for kw in call.keywords}
    assert "creationflags" in keyword_names


def test_write_machine_local_shim_writes_suppressed_script(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.benchmarks.percolate_round_ab._real_machine_local_exe",
        lambda: "/bin/does-not-matter",
    )
    _write_machine_local_shim({"repos.claude_klabauter": "/tmp/dest"}, tmp_path)

    written = (tmp_path / "machine_local_shim.py").read_text(encoding="utf-8")
    assert "creationflags=getattr(subprocess, \"CREATE_NO_WINDOW\", 0)" in written
