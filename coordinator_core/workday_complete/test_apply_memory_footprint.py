"""Workday-complete apply: bytes retained after `_execute_directives` must not
scale with the directive count.

Each synthetic directive is a real script under `tmp_path` that prints 256 KiB
and holds a module-level 1 MiB buffer, loaded through the module's own
`_load_cli_module`. Retention is read with `tracemalloc` while the returned
report is still alive, after the loaded-module caches are counted in.

Run scoped only:
    python -m pytest coordinator_core/workday_complete/test_apply_memory_footprint.py -q
"""

from __future__ import annotations

import gc
import sys
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.ceremony_common import cli_dispatch
from coordinator_core.workday_complete import apply as wc_apply

_STDOUT_BYTES = 256 * 1024
_BUFFER_BYTES = 1024 * 1024
_FLOOR_BYTES = 128 * 1024

_SCRIPT = f"""\
import sys

_HELD = bytearray({_BUFFER_BYTES})


def main(argv):
    sys.stdout.write("x" * {_STDOUT_BYTES})
    return 0
"""


class _NullOut:
    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def _retained_after(n: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    """Bytes `tracemalloc` still attributes to the run after `n` synthetic
    directives, with the report returned by `_execute_directives` held."""
    names = [f"fake-cli-{n}-{i}" for i in range(n)]
    table: dict[str, Path] = {}
    for name in names:
        script = tmp_path / f"{name}.py"
        script.write_text(_SCRIPT, encoding="utf-8")
        table[name] = script
    directives: list[dict[str, Any]] = [
        {
            "id": f"d{i}",
            "cli": name,
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
            "stdin_from": None,
        }
        for i, name in enumerate(names)
    ]
    monkeypatch.setattr(wc_apply, "_CLI_DISPATCH", table)
    monkeypatch.setattr(wc_apply, "assert_dispatchable", lambda *_a, **_k: None)
    monkeypatch.setattr(wc_apply, "_LOADED_MODULES", {})
    monkeypatch.setattr(cli_dispatch, "_LOADED_MODULES", {})
    monkeypatch.setattr(sys, "stdout", _NullOut())
    monkeypatch.setattr(sys, "stderr", _NullOut())

    gc.collect()
    tracemalloc.start()
    try:
        baseline, _ = tracemalloc.get_traced_memory()
        _code, report = wc_apply._execute_directives(directives, [], {})
        gc.collect()
        retained, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        for module_name in [m for m in sys.modules if m.startswith("_workday_complete_cli_fake_cli_")]:
            sys.modules.pop(module_name, None)
    assert len(report["landed"]) == n, report
    return retained - baseline


def test_retained_bytes_do_not_scale_with_directive_count(tmp_path, monkeypatch):
    """Apply drops stdout text from `results[]`, keeps `stdout_by_id` to
    `stdin_from` producers, and unloads each CLI module after its last use."""
    small = _retained_after(5, tmp_path, monkeypatch)
    large = _retained_after(40, tmp_path, monkeypatch)
    assert large < 2 * max(small, _FLOOR_BYTES), (
        f"retained {small} B after 5 directives, {large} B after 40"
    )
