
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"


def _load_probe_module() -> Optional[ModuleType]:
    if not _BIN_PROBE.exists():
        return None
    _KEY = "claude_klabauter_doctor_probe_publish_provenance_unit"
    spec = importlib.util.spec_from_file_location(_KEY, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_KEY] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(_KEY, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


def _is_parseable_probe_result(r: object) -> bool:
    return (
        hasattr(r, "probe")
        and hasattr(r, "status")
        and hasattr(r, "detail")
        and hasattr(r, "remediation")
        and isinstance(r.probe, str) and len(r.probe) > 0  # type: ignore[union-attr]
        and isinstance(r.status, str) and len(r.status) > 0  # type: ignore[union-attr]
    )


def _git(*args, cwd):
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_all(path: Path, message: str) -> str:
    _git("add", "-A", cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path)


def _toplevel(path: Path) -> str:
    return _git("rev-parse", "--show-toplevel", cwd=path)


def _write_record(settings_home: Path, rows: dict) -> Path:
    record_path = settings_home / "machine-local" / "publish-provenance.json"
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(
        json.dumps({"completed_at": "2026-08-19T14:26:00+00:00", "rows": rows}),
        encoding="utf-8",
    )
    return record_path


class TestPublishProvenanceProbe:

    def test_current_engine_is_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        head = _commit_all(claude_klabauter_root, "v1")
        toplevel = _toplevel(claude_klabauter_root)

        _write_record(
            tmp_path / "settings_home",
            {"klabauter": {"published": True, "toplevels": {toplevel: head}}},
        )

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.probe == "claude-klabauter.publish.provenance"
        assert result.status == mod._PASS
        assert result.data["commits_behind"] == 0

    def test_behind_engine_is_degraded_with_distance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        published_head = _commit_all(claude_klabauter_root, "v1")
        toplevel = _toplevel(claude_klabauter_root)

        (claude_klabauter_root / "f.txt").write_text("v2", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v2")

        _write_record(
            tmp_path / "settings_home",
            {"klabauter": {"published": True, "toplevels": {toplevel: published_head}}},
        )

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status == mod._DEGRADED
        assert result.status != mod._PASS
        assert result.data["commits_behind"] == 1

        assert "coordinator-publish" in result.remediation
        assert "percolate-round" not in result.remediation

    def test_absent_record_is_not_recorded_never_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v1")

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._PASS
        assert result.status == mod._INFO
        assert mod._sz_status(result) == mod._SZ_WARN, (
            "a never-published box is the normal state of a fresh install — it must not "
            "render with the same `fail` token as a real fault"
        )
        assert "not recorded" in result.detail.lower()
        assert result.required is False

    def test_unreadable_record_is_unknown_never_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        settings_home = tmp_path / "settings_home"
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v1")

        record_path = settings_home / "machine-local" / "publish-provenance.json"
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text("{not valid json", encoding="utf-8")

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._PASS
        assert "unknown" in result.detail.lower()

    def test_unknown_sha_not_in_history_is_unknown_never_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v1")
        toplevel = _toplevel(claude_klabauter_root)

        bogus_sha = "0" * 40
        _write_record(
            tmp_path / "settings_home",
            {"klabauter": {"published": True, "toplevels": {toplevel: bogus_sha}}},
        )

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._PASS
        assert "unknown" in result.detail.lower()

    def test_no_row_for_this_toplevel_is_unknown_never_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v1")

        other_repo = tmp_path / "other"
        _init_repo(other_repo)
        (other_repo / "g.txt").write_text("v1", encoding="utf-8")
        other_head = _commit_all(other_repo, "v1")
        other_toplevel = _toplevel(other_repo)

        _write_record(
            tmp_path / "settings_home",
            {"other-row": {"published": True, "toplevels": {other_toplevel: other_head}}},
        )

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._PASS
        assert result.status == mod._INFO
        assert mod._sz_status(result) == mod._SZ_WARN, (
            "a checkout that has not published yet is normal, not a fault"
        )
        assert "not recorded" in result.detail.lower()

    def test_failed_row_never_read_as_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings_home"))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v1")

        _write_record(
            tmp_path / "settings_home",
            {"broken-row": {"published": False}},
        )

        result = mod._run_probe_publish_provenance(claude_klabauter_root)

        assert _is_parseable_probe_result(result)
        assert result.status != mod._PASS
        assert "not recorded" in result.detail.lower()

    def test_drift_and_corruption_still_render_as_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = _require_module()
        settings_home = tmp_path / "settings_home"
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        claude_klabauter_root = tmp_path / "claude-klabauter"
        _init_repo(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v1", encoding="utf-8")
        published_head = _commit_all(claude_klabauter_root, "v1")
        toplevel = _toplevel(claude_klabauter_root)
        (claude_klabauter_root / "f.txt").write_text("v2", encoding="utf-8")
        _commit_all(claude_klabauter_root, "v2")

        _write_record(
            settings_home,
            {"klabauter": {"published": True, "toplevels": {toplevel: published_head}}},
        )
        behind = mod._run_probe_publish_provenance(claude_klabauter_root)
        assert mod._sz_status(behind) == mod._SZ_FAIL

        record_path = settings_home / "machine-local" / "publish-provenance.json"
        record_path.write_text("{not valid json", encoding="utf-8")
        corrupt = mod._run_probe_publish_provenance(claude_klabauter_root)
        assert mod._sz_status(corrupt) == mod._SZ_FAIL

    def test_none_root_emits_skip(self) -> None:
        mod = _require_module()
        result = mod._run_probe_publish_provenance(None)
        assert _is_parseable_probe_result(result)
        assert result.status == mod._INFO
        assert result.skipped is True
        assert result.required is False
