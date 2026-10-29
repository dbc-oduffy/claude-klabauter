"""A withheld path the halted run never authored a hunk on is not owed by dispatch.record_halted_run_landed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_run_landed as hrl
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RID = "ask-20261029T155740-cec55b"
UNUSED = "pkg/unused.py"
LANDED = "pkg/landed.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(root), check=True,
        capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _setup(tmp_path: Path, monkeypatch, *, unused_bytes: bytes = b"foreign\n") -> tuple[str, str]:
    """A repo where HEAD landed ``LANDED`` and ``UNUSED`` sits dirty in the tree; the run withholds both."""
    monkeypatch.setattr(hrl, "_launch", lambda root, run_id: True)
    monkeypatch.setattr("coordinator_core.machine_resolver.registry_get", lambda key: None)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "core.autocrlf", "false")
    for name in (UNUSED, LANDED):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(b"a\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / LANDED).write_bytes(b"landed\n")
    _git(tmp_path, "commit", "-qam", "land")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / UNUSED).write_bytes(unused_bytes)
    d = tmp_path / "state" / "warp" / RID
    d.mkdir(parents=True)
    (d / "halted.json").write_text(json.dumps({
        "status": "halted", "halted": "x", "sizing_rel": None, "run_base_sha": base,
        "run_artifacts": {"withheld": {UNUSED: "r", LANDED: "r"}},
    }), encoding="utf-8")
    return base, sha


def _manifest(tmp_path: Path, baseline: dict) -> None:
    (tmp_path / "state" / "warp" / RID / "manifest.json").write_text(
        json.dumps({"write_baseline": baseline}), encoding="utf-8")


def _kept(tmp_path: Path, paths: dict) -> None:
    (tmp_path / "state" / "warp" / RID / "kept.json").write_text(
        json.dumps({"chunks": {"C1": {"paths": paths}}, "plan_gate": None}), encoding="utf-8")


def _note(tmp_path: Path, text: str) -> None:
    (tmp_path / "docs" / "delivery").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "delivery" / f"{RID}.md").write_text(text, encoding="utf-8")


def test_delivery_note_naming_the_path_untouched_closes_the_run_landed(tmp_path, monkeypatch):
    _, sha = _setup(tmp_path, monkeypatch)
    _note(tmp_path, f"## Notes\n\n- `{UNUSED}` carries other edits in the tree;\n  they were not authored here.\n"
                    f"- `{LANDED}` was rewritten.\n")
    out = hrl._handler({"run_id": RID, "sha": sha}, tmp_path)
    assert out["ok"] and out["written"] is True
    assert out["voided"] == [UNUSED]
    assert out["covers"] == {sha: [LANDED]}


def test_path_left_at_its_baseline_print_is_foreign_and_not_owed(tmp_path, monkeypatch):
    _, sha = _setup(tmp_path, monkeypatch)
    _manifest(tmp_path, {UNUSED: blob_sha(b"foreign\n")})
    out = hrl._handler({"run_id": RID, "sha": sha}, tmp_path)
    assert out["ok"] and out["voided"] == [UNUSED]


def test_run_edited_uncommitted_path_is_still_refused(tmp_path, monkeypatch):
    _, sha = _setup(tmp_path, monkeypatch, unused_bytes=b"mine\n")
    _manifest(tmp_path, {UNUSED: blob_sha(b"a\n")})
    _kept(tmp_path, {UNUSED: blob_sha(b"mine\n")})
    _note(tmp_path, f"- `{UNUSED}` was left unchanged.\n")
    out = hrl._handler({"run_id": RID, "sha": sha}, tmp_path)
    assert not out["ok"] and UNUSED in out["error"]


def test_no_baseline_and_no_note_stays_owed(tmp_path, monkeypatch):
    _, sha = _setup(tmp_path, monkeypatch)
    out = hrl._handler({"run_id": RID, "sha": sha}, tmp_path)
    assert not out["ok"] and UNUSED in out["error"]


def test_note_bullet_about_a_changed_path_does_not_excuse_it(tmp_path, monkeypatch):
    _, sha = _setup(tmp_path, monkeypatch)
    _note(tmp_path, f"- `{UNUSED}`: new helper added.\n- `{LANDED}` unchanged otherwise.\n")
    out = hrl._handler({"run_id": RID, "sha": sha}, tmp_path)
    assert not out["ok"] and UNUSED in out["error"]
