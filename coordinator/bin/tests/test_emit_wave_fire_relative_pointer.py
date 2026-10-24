"""Pins that emit-wave-fire's repair reader resolves a repo-relative pointer `sidecarPath`."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "emit-wave-fire.py"


def _load():
    spec = importlib.util.spec_from_file_location("emit_wave_fire", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ewf = _load()


def _trail(tmp_path, rel_sidecar):
    trail = tmp_path / "trail"
    slot = trail / "wave-0-aaaa"
    slot.mkdir(parents=True)
    (slot / "b1.planning-report.md").write_text(
        "# Planning report: b1\n\n**Plan:** `docs/plans/p.md` (status: draft)\n", encoding="utf-8"
    )
    (slot / "b1.review-x-pointer.md").write_text(
        json.dumps({"sidecarPath": rel_sidecar, "verdict": "OK", "premiseFailure": None}),
        encoding="utf-8",
    )
    return trail


def test_repair_resolves_a_repo_relative_sidecar_path(tmp_path):
    rel = ".coordinator-local/subagent-share/s/r.md"
    sidecar = tmp_path / rel
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("findings", encoding="utf-8")
    trail = _trail(tmp_path, rel)

    entry, why = ewf._repair_entry(trail, tmp_path, "b1")

    assert why is None, why
    assert len(entry["reviews"]) == 1
    assert entry["unresolvedPointers"] == []


def test_repair_refuses_a_repo_relative_sidecar_path_that_is_gone(tmp_path):
    trail = _trail(tmp_path, ".coordinator-local/subagent-share/s/missing.md")

    entry, why = ewf._repair_entry(trail, tmp_path, "b1")

    assert entry is None
    assert "gone" in why
