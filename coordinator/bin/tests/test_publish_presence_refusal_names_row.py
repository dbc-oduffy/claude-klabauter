"""`dispatch_mirror_like` names the governing portable row when the orphan
sweep refuses with `SystemExit(3)`, on the stream the FATAL went to (stderr).

The presence-preflight site runs the real `sync_mirror`. The mass-deletion
site is shadowed by the preflight in the real engine (both act on the same
non-overridden orphans, preflight raises first), so it is driven with a stub
that raises that site's exact FATAL.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BIN_DIR.parent / "lib"))

from percolate import publish_sync  # noqa: E402


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_presence_refusal_names_row_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW = "claude-klabauter-publish-repo-toplevel"


def _target(tmp_path: Path, name: str = _ROW):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return publish.ResolvedTarget(name=name, mode="mirror", source_dir=src, dest_dir=dst)


def _assert_names_row(err: str) -> None:
    assert f"Skipping {_ROW}." in err
    assert f"governing row: {_ROW}" in err
    assert "publish-mirror:claude_klabauter/" in err
    assert "publish-targets.portable" in err


def test_presence_preflight_refusal_names_row(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_ORPHAN_SWEEP", raising=False)
    target = _target(tmp_path)
    (target.source_dir / "kept").mkdir()
    (target.dest_dir / "kept").mkdir()
    (target.dest_dir / "gone").mkdir()
    (target.dest_dir / "gone" / "f.txt").write_text("x", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        publish.dispatch_mirror_like(
            publish_sync, target, target.source_dir, publish.RunTotals(), dry_run=False
        )

    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "top-level presence check" in err
    _assert_names_row(err)


def test_mass_deletion_refusal_names_row(tmp_path, capsys):
    target = _target(tmp_path)

    def _mass_deletion(src_dir, dst_dir, ignore, dry_run, **kwargs):
        print(
            "FATAL: orphan sweep would remove 2 of 3 destination plugin dirs (a, b) "
            "— refusing as a likely src_dir misconfiguration.",
            file=sys.stderr,
        )
        raise SystemExit(3)

    stub = types.ModuleType("stub_mass_deletion_sync")
    stub.sync_mirror = _mass_deletion
    stub.load_ignore = publish_sync.load_ignore

    with pytest.raises(SystemExit) as exc:
        publish.dispatch_mirror_like(
            stub, target, target.source_dir, publish.RunTotals(), dry_run=False
        )

    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "orphan sweep would remove" in err
    _assert_names_row(err)


def test_other_exit_codes_pass_through_without_row_line(tmp_path, capsys):
    target = _target(tmp_path)

    def _other(src_dir, dst_dir, ignore, dry_run, **kwargs):
        raise SystemExit(2)

    stub = types.ModuleType("stub_other_exit_sync")
    stub.sync_mirror = _other
    stub.load_ignore = publish_sync.load_ignore

    with pytest.raises(SystemExit) as exc:
        publish.dispatch_mirror_like(
            stub, target, target.source_dir, publish.RunTotals(), dry_run=False
        )

    assert exc.value.code == 2
    assert "governing row" not in capsys.readouterr().err
