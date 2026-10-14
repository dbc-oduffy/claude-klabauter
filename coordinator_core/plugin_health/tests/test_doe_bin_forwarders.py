"""Tests for the engine-native bin-forwarder probe."""

from __future__ import annotations

from coordinator_core.plugin_health import doe_bin_forwarders as mod

CLEAN = "\n".join(
    ["#!/usr/bin/env python3", "# forwarder", *mod.CANONICAL_CODE]
) + "\n"
FORKED = CLEAN + "print('regrown logic')\n"


def _bins(tmp_path):
    doe = tmp_path / "doe"
    (doe / "coordinator" / "bin").mkdir(parents=True)
    eng = tmp_path / "eng" / "coordinator" / "bin"
    eng.mkdir(parents=True)
    return doe, eng


def test_clean_forwarder_exits_zero(tmp_path):
    doe, eng = _bins(tmp_path)
    (doe / "coordinator/bin/a.py").write_text(CLEAN, encoding="utf-8")
    (eng / "a.py").write_text("x", encoding="utf-8")
    assert mod.run(content_root=doe, engine_bin=eng) == (0, [])


def test_regrown_fork_is_listed(tmp_path):
    doe, eng = _bins(tmp_path)
    (doe / "coordinator/bin/a.py").write_text(FORKED, encoding="utf-8")
    (doe / "coordinator/bin/b.py").write_text("print(1)\n", encoding="utf-8")
    for n in ("a.py", "b.py"):
        (eng / n).write_text("x", encoding="utf-8")
    rc, lines = mod.run(content_root=doe, engine_bin=eng)
    assert rc == 1
    assert any("a.py: carries logic beyond" in line for line in lines)
    assert any("b.py: does not reference _engine_forward" in line for line in lines)


def test_name_absent_from_engine_bin_is_ignored(tmp_path):
    doe, eng = _bins(tmp_path)
    (doe / "coordinator/bin/only_doe.py").write_text(FORKED, encoding="utf-8")
    assert mod.run(content_root=doe, engine_bin=eng) == (0, [])


def test_unresolvable_doe_clone_is_a_note_not_a_crash(tmp_path):
    _, eng = _bins(tmp_path)
    rc, lines = mod.run(content_root=tmp_path / "missing", engine_bin=eng)
    assert rc == 0 and len(lines) == 1 and lines[0].startswith("[skip]")
