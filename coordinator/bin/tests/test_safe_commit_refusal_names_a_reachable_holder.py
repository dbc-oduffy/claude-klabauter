
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location(
    "safe_commit_holder_context_under_test", _BIN_DIR / "coordinator-safe-commit.py"
)
assert spec is not None and spec.loader is not None
safe_commit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = safe_commit
spec.loader.exec_module(safe_commit)

_SID = "d12e25cf-3a6b-4614-b269-ff941299e19e"


class _Rec:
    def __init__(self, name):
        self.name = name


def _patch_registry(monkeypatch, mapping):
    from coordinator_core.session import harness_registry

    monkeypatch.setattr(harness_registry, "snapshot", lambda: mapping)


def _session_dir(root: Path) -> Path:
    d = root / ".git" / "coordinator-sessions" / _SID
    d.mkdir(parents=True)
    return d


def test_names_the_holders_baton_title(tmp_path):
    d = _session_dir(tmp_path)
    (d / "baton.json").write_text(
        json.dumps({"title": "Execute the percolate contention flip"}), encoding="utf-8"
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "d12e25cf" in rendered
    assert "Execute the percolate contention flip" in rendered


def test_names_how_long_the_touch_has_gone_unreleased(tmp_path):
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 4 * 3600, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert "held 4.0h" in safe_commit._holder_context(
        str(tmp_path), _SID, "some/file.py"
    )


def test_a_minutes_old_claim_is_not_rendered_as_hours(tmp_path):
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 120, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "held 2m" in rendered
    assert "h" not in rendered.split("held ")[1]


def test_a_touch_on_another_path_is_not_this_paths_age(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    d = _session_dir(tmp_path)
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 4 * 3600, "sid": _SID,
             "path": "other/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
        == "d12e25cf (live, no name in the harness registry)"
    )


def test_degrades_to_the_identifier_alone_rather_than_raising(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    assert (
        safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
        == "d12e25cf (live, no name in the harness registry)"
    )


def test_a_corrupt_baton_does_not_lose_the_age(tmp_path):
    d = _session_dir(tmp_path)
    (d / "baton.json").write_text("{not json", encoding="utf-8")
    (d / "touch-record.jsonl").write_text(
        json.dumps(
            {"v": 1, "verb": "T", "ts": time.time() - 3600, "sid": _SID,
             "path": "some/file.py"}
        )
        + "\n",
        encoding="utf-8",
    )
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert rendered.startswith("d12e25cf") and "held 1.0h" in rendered


def test_resolves_the_sid_to_the_stable_name(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {_SID: _Rec("claude-klabauter-2d")})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert rendered.startswith("claude-klabauter-2d [d12e25cf]")


def test_an_unregistered_sid_is_marked_unnamed_not_printed_as_an_address(
    tmp_path, monkeypatch
):
    _patch_registry(monkeypatch, {})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert "no name in the harness registry" in rendered
    assert "stale" not in rendered
    assert "claude-klabauter" not in rendered


def test_a_registry_failure_still_renders_the_holder(tmp_path, monkeypatch):
    from coordinator_core.session import harness_registry

    def _boom():
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(harness_registry, "snapshot", _boom)
    assert "d12e25cf" in safe_commit._holder_context(
        str(tmp_path), _SID, "some/file.py"
    )


def _patch_liveness_basis(monkeypatch, basis_by_sid):
    from coordinator_core.session import holder_evidence

    monkeypatch.setattr(
        holder_evidence,
        "liveness_basis",
        lambda sid, cwd=None: basis_by_sid.get(sid, "stable-pid"),
    )


def test_a_shared_stable_pid_holder_is_marked_a_possible_ghost(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    _patch_liveness_basis(monkeypatch, {_SID: "stable-pid-shared"})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert safe_commit.SHARED_GHOST_MARKER in rendered


def test_an_ordinary_stable_pid_holder_carries_no_ghost_marker(tmp_path, monkeypatch):
    _patch_registry(monkeypatch, {})
    _patch_liveness_basis(monkeypatch, {_SID: "stable-pid"})
    rendered = safe_commit._holder_context(str(tmp_path), _SID, "some/file.py")
    assert safe_commit.SHARED_GHOST_MARKER not in rendered


def _stub_session(mod, monkeypatch, *, session_id="mine", contested=None):
    def _contested(paths, sid, cwd=None):
        return dict(contested or {})

    cs_core = types.SimpleNamespace(resolve_session_id=lambda: session_id)
    cs_scope = types.SimpleNamespace(contested_by_live_peers=_contested)
    monkeypatch.setattr(
        mod, "_import_session", lambda: (cs_core, object(), cs_scope, object())
    )


def test_refusal_text_qualifies_the_release_promise_on_a_shared_ghost(
    tmp_path, monkeypatch, capsys
):
    _stub_session(
        monkeypatch=monkeypatch,
        mod=safe_commit,
        contested={"some/file.py": [_SID]},
    )
    _patch_registry(monkeypatch, {})
    _patch_liveness_basis(monkeypatch, {_SID: "stable-pid-shared"})

    with pytest.raises(SystemExit):
        safe_commit._refuse_contested_pathspec(["some/file.py"], str(tmp_path))

    text = capsys.readouterr().err
    assert safe_commit.SHARED_GHOST_MARKER in text
    assert "may never do either" in text


def test_refusal_text_keeps_the_unconditional_promise_when_no_holder_is_a_ghost(
    tmp_path, monkeypatch, capsys
):
    _stub_session(
        monkeypatch=monkeypatch,
        mod=safe_commit,
        contested={"some/file.py": [_SID]},
    )
    _patch_registry(monkeypatch, {})
    _patch_liveness_basis(monkeypatch, {_SID: "stable-pid"})

    with pytest.raises(SystemExit):
        safe_commit._refuse_contested_pathspec(["some/file.py"], str(tmp_path))

    text = capsys.readouterr().err
    assert safe_commit.SHARED_GHOST_MARKER not in text
    assert (
        "it frees when that session commits or releases." in text
    ), text
