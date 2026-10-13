"""sizing.record_xl_exit with an APM ruling: admitted only in ceo mode, only for exits that run on."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import coordinator_core.ops.sizing_record_xl_exit as mod
from coordinator_core.ops.tests.test_sizing_record_xl_exit import _seed

_RULING = "One deliverable; a single multi-session plan carries it."


def _seed_mode(tmp_path: Path, mode: str) -> Path:
    p = _seed(tmp_path)
    p.write_text(p.read_text(encoding="utf-8") + f"interaction_mode: {mode}\n", encoding="utf-8")
    return p


def _apm(tmp_path: Path, pick: str = "accept_multi_session", **over) -> dict:
    params = {
        "sizing": "state/sizings/20260101-a.yaml",
        "xl_exit": pick,
        "apm_ruling": _RULING,
        "ruling_ref": "ask-1",
        "decided_on": "2026-10-13",
        **over,
    }
    return mod._handler(params, repo_root=tmp_path / ".git")


def test_ceo_mode_records_the_apm_pick_apart_from_the_pms_words(tmp_path):
    p = _seed_mode(tmp_path, "ceo")
    r = _apm(tmp_path)
    assert r["exit_code"] == 0 and r["applied"] is True, r
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert doc["xl_exit"] == "accept_multi_session"
    assert doc["pm_resolution"] == {"decided_on": "2026-10-13", "xl_exit_apm": f"ask-1: {_RULING}"}


@pytest.mark.parametrize("mode", ["pm", "hands-on"])
def test_other_modes_keep_the_exit_the_pms(tmp_path, mode):
    p = _seed_mode(tmp_path, mode)
    before = p.read_text(encoding="utf-8")
    r = _apm(tmp_path)
    assert r["exit_code"] == 1 and "ceo" in r["error"], r
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"xl_exit": "shape"}, "APM ruling picks only"),
        ({"ruling_ref": ""}, "ruling_ref"),
        ({"apm_ruling": "merge to main once done"}, "irreversible"),
        ({"pm_quote": "go"}, "exactly one"),
    ],
)
def test_refusals(tmp_path, over, needle):
    _seed_mode(tmp_path, "ceo")
    pick = over.pop("xl_exit", "roadmap")
    r = _apm(tmp_path, pick, **over)
    assert r["exit_code"] == 1 and needle in r["error"], r
