"""Tests for the doctor release-age layer: health-first, bounded gh fallback, `unknown` degradation."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import doctor_release_age as dra

NOW = 1_800_000_000.0
REGISTRY = {"repos.alpha.publish_repo": "owner/alpha", "repos.alpha": "/x/alpha", "repos.flag": True}
_IMPERATIVE = re.compile(r"\b(run|fix|re-?run|update|pull|publish|please|should|must|try)\b", re.I)


def _rag_home(tmp_path: Path, slug: str = "alpha", index_age_s: int | None = 86400 * 2) -> Path:
    home = tmp_path / "project-rag"
    marker = home / "indexes" / slug / "root" / ".index_job.json"
    marker.parent.mkdir(parents=True)
    if index_age_s is not None:
        marker.write_text("{}")
        os.utime(marker, (NOW - index_age_s, NOW - index_age_s))
    return home


def _messages(layer) -> list[str]:
    return [f.message for f in layer.findings]


def test_health_field_is_used_verbatim_and_gh_is_not_called(tmp_path: Path):
    home = _rag_home(tmp_path)
    (home / "corpus-state.json").write_text(
        json.dumps({"repos": {"alpha": {"release_staleness": "release 4h newer than live index"}}})
    )

    def boom(*_a):
        raise AssertionError("gh called although health carries release staleness")

    layer = dra.release_age_layer(REGISTRY, home, NOW, gh_age=boom)
    assert _messages(layer) == ["alpha: release 4h newer than live index; live index 2d old"]


def test_gh_fallback_reports_release_and_index_age(tmp_path: Path):
    home = _rag_home(tmp_path)
    layer = dra.release_age_layer(REGISTRY, home, NOW, gh_age=lambda repo, now: 3600 * 5)
    assert _messages(layer) == ["alpha: latest release 5h old; live index 2d old"]
    assert layer.status == "ok"


def test_gh_failure_degrades_to_unknown(tmp_path: Path):
    home = _rag_home(tmp_path)
    layer = dra.release_age_layer(REGISTRY, home, NOW, gh_age=lambda repo, now: None)
    assert _messages(layer) == ["alpha: latest release unknown; live index 2d old"]
    assert layer.status == "ok"


def test_missing_index_marker_reads_unknown(tmp_path: Path):
    home = _rag_home(tmp_path, index_age_s=None)
    layer = dra.release_age_layer(REGISTRY, home, NOW, gh_age=lambda repo, now: 60)
    assert "live index unknown" in _messages(layer)[0]


def test_registry_without_publish_repo_yields_no_lines(tmp_path: Path):
    layer = dra.release_age_layer({"repos.alpha": "/x/alpha"}, tmp_path, NOW)
    assert layer.findings == []
    assert layer.status == "ok"


def test_gh_missing_on_path_is_none(monkeypatch):
    monkeypatch.setattr(dra.shutil, "which", lambda _name: None)
    assert dra.gh_release_age_s("owner/alpha", NOW) is None


def test_gh_timeout_is_none(monkeypatch):
    def slow(*_a, **kw):
        assert kw["timeout"] == dra.GH_TIMEOUT_S
        raise subprocess.TimeoutExpired("gh", kw["timeout"])

    monkeypatch.setattr(dra.subprocess, "run", slow)
    assert dra.gh_release_age_s("owner/alpha", NOW, gh="gh") is None


def test_gh_success_parses_published_at(monkeypatch):
    payload = json.dumps([{"tagName": "scip-1", "publishedAt": "2027-01-15T07:00:00Z"}])
    monkeypatch.setattr(
        dra.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=payload, stderr=""),
    )
    from datetime import datetime, timezone

    published = datetime(2027, 1, 15, 7, tzinfo=timezone.utc).timestamp()
    assert dra.gh_release_age_s("owner/alpha", published + 7200, gh="gh") == pytest.approx(7200)


def test_gh_nonzero_exit_and_garbage_are_none(monkeypatch):
    monkeypatch.setattr(
        dra.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="auth"),
    )
    assert dra.gh_release_age_s("o/r", NOW, gh="gh") is None
    monkeypatch.setattr(
        dra.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="[]", stderr=""),
    )
    assert dra.gh_release_age_s("o/r", NOW, gh="gh") is None


def test_status_lines_carry_no_imperative_wording(tmp_path: Path):
    home = _rag_home(tmp_path)
    for gh_age in (lambda r, n: 99, lambda r, n: None):
        for line in _messages(dra.release_age_layer(REGISTRY, home, NOW, gh_age=gh_age)):
            assert not _IMPERATIVE.search(line), line


def test_run_doctor_appends_the_layer(monkeypatch, tmp_path: Path):
    from coordinator_core.ops import doctor

    monkeypatch.setattr(dra, "_check_release_age", lambda: dra.release_age_layer({}, tmp_path, NOW))
    for name in [n for n in dir(doctor) if n.startswith("_check_") and n != "_check_box_identity"]:
        monkeypatch.setattr(doctor, name, lambda: doctor.Layer("stub", "ok", []))
    monkeypatch.setattr("coordinator_core.ops.doctor_boot_banners.boot_banner_layers", lambda _d: [])
    report, _ = doctor.run_doctor()
    assert dra.LAYER_NAME in [layer.name for layer in report.layers]
