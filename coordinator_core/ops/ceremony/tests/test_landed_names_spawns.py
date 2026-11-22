"""`uncommitted_name_refusals` starts no process, on a refusing and on a clean landing."""

from __future__ import annotations

import os
import subprocess

from coordinator_core.ops.ceremony.landed_names import uncommitted_name_refusals


def test_checker_spawns_nothing(tmp_path, monkeypatch):
    for rel, text in {"pkg/__init__.py": "", "pkg/idx.py": "def a(): ...\ndef b(): ...\n"}.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    landed = {"pkg/__init__.py": b"", "pkg/idx.py": b"def a(): ...\n"}
    landed["pkg/caller.py"] = b"def f():\n    from pkg import idx\n    idx.a()\n    idx.b()\n"

    spawned = []

    def refuse(*a, **k):
        spawned.append(a)
        raise AssertionError("process spawned")

    for target in (subprocess, os):
        for name in ("Popen", "run", "system", "popen"):
            if hasattr(target, name):
                monkeypatch.setattr(target, name, refuse)

    out = uncommitted_name_refusals(tmp_path, ["pkg/caller.py"], landed.get)
    assert out == ["pkg/caller.py:4: names pkg.idx.b, which exists only uncommitted"]
    assert uncommitted_name_refusals(tmp_path, ["pkg/idx.py"], landed.get) == []
    assert spawned == []
