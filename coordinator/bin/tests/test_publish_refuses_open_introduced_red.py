"""Pins the publish pre-sync gate's refusal while an introduced-red fix row is open.

`publish.py` never imports coordinator_core, so the row-record names it reads are duplicated; this
file pins them equal to `checkpoint_reds`' own constants and drives `run_pre_sync_gates`.

Run: python -m pytest coordinator/bin/tests/test_publish_refuses_open_introduced_red.py -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

from coordinator_core.ops.dispatch_emit import checkpoint_reds
from coordinator_core.ops.dispatch_emit.bug_close import BUG_HOME

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_open_red_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _gates(monkeypatch, tmp_path, shadow: Path):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    target = publish.ResolvedTarget(name="t", mode="mirror", source_dir=raw, dest_dir=dest)
    monkeypatch.setattr(publish, "check_live_install_clobber", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_marketplace_version_regression", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_version_consistency", lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_round_pin_source_sha", lambda root, pinned, *, out=None, late=False: "SHA")
    monkeypatch.setattr(publish, "_git_materialize_ref", lambda root, ref="HEAD", withheld=None: shadow)
    err = io.StringIO()
    result = publish.run_pre_sync_gates(
        target,
        setup_dir=tmp_path,
        identity_file_exists=False,
        identity=None,
        totals=publish.RunTotals(),
        dry_run=True,
        out=io.StringIO(),
        err=err,
    )
    return result, err.getvalue()


def _mint(shadow: Path, status: str = "open") -> str:
    row = shadow / BUG_HOME / f"2026-11-10{checkpoint_reds._NAME_MARK}some-test-abc123def456.yaml"
    row.parent.mkdir(parents=True, exist_ok=True)
    row.write_text(f"status: {status}\nintroduced_red: true\ntest_id: x\n", encoding="utf-8")
    return row.name


def test_mirrored_names_equal_checkpoint_reds_constants():
    assert publish._INTRODUCED_RED_HOME == BUG_HOME
    assert publish._INTRODUCED_RED_NAME_MARK == checkpoint_reds._NAME_MARK
    assert publish._INTRODUCED_RED_KEY == "introduced_red"
    assert publish._INTRODUCED_RED_STATUS_KEY == "status"


def test_open_row_refuses_naming_the_row_and_closing_it_clears(monkeypatch, tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    name = _mint(shadow)
    result, err = _gates(monkeypatch, tmp_path, shadow)
    assert result.proceed is False
    assert "abc123def456" in err

    (shadow / BUG_HOME / name).write_text("status: closed\nintroduced_red: true\n", encoding="utf-8")
    result, err = _gates(monkeypatch, tmp_path, shadow)
    assert result.proceed is True
    assert "introduced-red" not in err


def test_absent_bug_backlog_does_not_refuse(monkeypatch, tmp_path):
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    result, _ = _gates(monkeypatch, tmp_path, shadow)
    assert result.proceed is True


def test_ordinary_open_bug_row_does_not_refuse(monkeypatch, tmp_path):
    shadow = tmp_path / "shadow"
    (shadow / BUG_HOME).mkdir(parents=True)
    (shadow / BUG_HOME / "2026-11-10-plain-bug.yaml").write_text("status: open\n", encoding="utf-8")
    result, _ = _gates(monkeypatch, tmp_path, shadow)
    assert result.proceed is True
