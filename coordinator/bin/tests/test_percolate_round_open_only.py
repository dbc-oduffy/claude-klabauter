"""`percolate-round --open-only` hands its push to percolate-push's
`open_only_publish` in-process, under the still-held round lock.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_open_only.py -q
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_percolate_round as base  # noqa: E402  same-directory harness

_mod = base._mod


class _FakePush:
    """Stands in for the percolate-push module the round loads via `_sibling_cli`."""

    def __init__(self, rc: int = 0, marker: Path | None = None):
        self.rc = rc
        self.marker = marker
        self.calls: list = []
        self.lock_was_held: list = []
        self.marker_present_during: list = []

    def open_only_publish(self, dest, target, **kwargs):
        self.calls.append((dest, target, kwargs))
        try:
            with _mod._round_held_lock(Path(dest), holder_label="probe", timeout=0):
                self.lock_was_held.append(False)
        except _mod._RoundLockTimeout:
            self.lock_was_held.append(True)
        if self.marker is not None:
            self.marker_present_during.append(self.marker.exists())
        return self.rc


def _install_fake_push(monkeypatch, fake: _FakePush) -> None:
    monkeypatch.setattr(_mod, "_load_open_only_publish", lambda: fake.open_only_publish)


def _force_flag(monkeypatch, *flags: str) -> None:
    real_build = _mod._build_parser

    class _Parser:
        def __init__(self):
            self._p = real_build()

        def parse_args(self, argv):
            return self._p.parse_args(list(argv) + list(flags))

    monkeypatch.setattr(_mod, "_build_parser", _Parser)


def _fake_exists(self):
    return self.name in ("added-file.md", "changed-file.md") or _ORIG_EXISTS(self)


_ORIG_EXISTS = Path.exists


def _push_spawns(spy) -> list:
    return [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]


def test_open_only_calls_open_only_publish_once_under_lock_and_never_push_dest(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    fake = _FakePush(rc=0, marker=base._marker_path(percolate_root))
    _install_fake_push(monkeypatch, fake)
    _force_flag(monkeypatch, "--open-only")
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", _fake_exists)
    monkeypatch.setattr(
        _mod, "_push_dest", lambda d: pytest.fail("_push_dest called under --open-only")
    )

    rc, out, spy, dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == 0
    assert len(fake.calls) == 1
    assert fake.calls[0][0] == str(dest)
    assert fake.calls[0][2]["lock_held"] is True
    assert fake.lock_was_held == [True]
    assert not [c for c in spy.calls if "percolate-gate.py" in " ".join(str(x) for x in c)]
    assert _push_spawns(spy) == []


@pytest.mark.parametrize("rc_in", [1, 2, 3, 75])
def test_open_only_forwards_exit_code(tmp_path, monkeypatch, rc_in):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    fake = _FakePush(rc=rc_in)
    _install_fake_push(monkeypatch, fake)
    _force_flag(monkeypatch, "--open-only")
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", _fake_exists)

    rc, _out, _spy, _dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == rc_in


def test_refused_round_calls_neither_push_path(tmp_path, monkeypatch):
    fake = _FakePush()
    _install_fake_push(monkeypatch, fake)
    _force_flag(monkeypatch, "--open-only")
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: self.name != "changed-file.md")
    monkeypatch.setattr(
        _mod, "_push_dest", lambda d: pytest.fail("_push_dest called on a refused round")
    )
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    rc, _out, spy, _dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == _mod._EXIT_FAIL
    assert fake.calls == []
    assert _push_spawns(spy) == []


def test_open_only_with_no_publish_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        _mod.main(["alpha", "--open-only", "--no-publish"])
    assert exc.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_without_flag_push_dest_is_called_as_today(tmp_path, monkeypatch):
    fake = _FakePush()
    _install_fake_push(monkeypatch, fake)
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", _fake_exists)
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    rc, out, spy, dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == 0
    assert fake.calls == []
    assert len(_push_spawns(spy)) == 1
    assert f"Published: pushed to {dest}." in out


def test_open_only_with_zero_ahead_count_still_delegates(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = base._SubprocessSpy(
        dryrun_stdout="",
        real_stdout="",
        parse2_stdout=base._parse2_stdout(),
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    base._install_manifest_stub(monkeypatch, spy)
    base._install_sibling_cli_stub(monkeypatch, spy)
    fake = _FakePush()
    _install_fake_push(monkeypatch, fake)
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    args = _mod._build_parser().parse_args(
        ["alpha", "--percolate-root", str(percolate_root), "--yes", "--open-only"]
    )
    with contextlib.redirect_stdout(io.StringIO()):
        rc = _mod._cmd_round(args)

    assert rc == 0
    assert len(fake.calls) == 1
    assert fake.lock_was_held == [True]


def test_marker_cleared_only_after_successful_push(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    marker = base._marker_path(percolate_root)
    fake = _FakePush(rc=0, marker=marker)
    _install_fake_push(monkeypatch, fake)
    _force_flag(monkeypatch, "--open-only")
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", _fake_exists)

    rc, _out, _spy, _dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == 0
    assert fake.marker_present_during == [True]
    assert not marker.exists()


def test_marker_survives_a_failed_push(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    marker = base._marker_path(percolate_root)
    fake = _FakePush(rc=1, marker=marker)
    _install_fake_push(monkeypatch, fake)
    _force_flag(monkeypatch, "--open-only")
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", _fake_exists)

    rc, _out, _spy, _dest = base._run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == 1
    assert marker.is_file()
    assert json.loads(marker.read_text(encoding="utf-8"))["reason"] == "uncommitted-verdict"
