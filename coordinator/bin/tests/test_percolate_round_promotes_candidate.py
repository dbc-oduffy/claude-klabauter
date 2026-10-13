"""The percolate round's promote leg: at the terminals where a channel dest ends
level with its upstream, `_promote_if_channel` calls klabauter-promote's
`promote_candidate(unattended=True)`.

The first cases load the real klabauter-promote against a real-git fixture
(bare origin + clone); the wiring cases stub the round's own steps in
test_percolate_round.py's fixture idiom and spy on the leg.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
from pathlib import Path

import pytest

from . import test_percolate_round as base
from .test_klabauter_promote import _Rig, _g

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_mod = base._mod


def _leg(rig: _Rig, capsys):
    rc = _mod._promote_if_channel(str(rig.dest), "alpha", str(rig.root))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_promote_when_ready_promotes_the_soaked_state_not_the_tip(tmp_path, capsys):
    rig = _Rig(tmp_path)
    soaked = rig.commit(80)
    rig.push(80)
    tip = rig.commit(0)
    rig.push(0)
    rc, out, _err = _leg(rig, capsys)
    assert rc == 0
    assert rig.origin_ref("refs/heads/main") == soaked != tip
    assert "promoted" in out and soaked in out


def test_refuse_when_no_state_is_72h_old(tmp_path, capsys):
    rig = _Rig(tmp_path)
    rig.commit(1)
    rig.push(1)
    rc, out, _err = _leg(rig, capsys)
    assert rc == 0
    assert "predicate 4" in out and "72h" in out and "promotion refused" in out
    assert rig.origin_ref("refs/heads/main") == rig.base


def test_nothing_to_promote_reads_as_neither_refusal_nor_predicate(tmp_path, capsys):
    rig = _Rig(tmp_path)
    rig.commit(80)
    rig.push(80)
    assert _leg(rig, capsys)[0] == 0
    main_after_first = rig.origin_ref("refs/heads/main")
    rc, out, err = _leg(rig, capsys)
    assert rc == 0
    assert "nothing to promote" in out
    assert "refused" not in out + err and "predicate" not in out + err
    assert rig.origin_ref("refs/heads/main") == main_after_first


def test_diverged_main_refuses_predicate_3_and_pushes_nothing(tmp_path, capsys):
    rig = _Rig(tmp_path)
    rig.commit(80)
    rig.push(80)
    other = rig.origin.parent / "other"
    other.mkdir()
    _g(other, "clone", str(rig.origin), ".")
    (other / "diverge.txt").write_text("d")
    _g(other, "add", ".")
    _g(other, "commit", "-m", "diverge")
    _g(other, "push", "origin", "HEAD:main")
    _g(rig.dest, "fetch", "origin")
    diverged = rig.origin_ref("refs/heads/main")
    rc, out, _err = _leg(rig, capsys)
    assert rc == 0
    assert "predicate 3" in out and "promotion refused" in out
    assert rig.origin_ref("refs/heads/main") == diverged


def test_main_push_failure_names_the_failure_and_fails_the_round(tmp_path, capsys):
    rig = _Rig(tmp_path)
    rig.commit(80)
    rig.push(80)
    _g(rig.dest, "remote", "set-url", "--push", "origin", str(tmp_path / "gone.git"))
    rc, out, err = _leg(rig, capsys)
    assert rc == _mod._EXIT_FAIL
    assert "promotion push failed" in err
    assert rig.origin_ref("refs/heads/main") == rig.base


def test_leg_never_passes_a_waiver(tmp_path, monkeypatch):
    rig = _Rig(tmp_path)
    rig.commit(80)
    rig.push(80)
    promote = _mod._sibling_cli(_mod._KLABAUTER_PROMOTE)
    seen = []
    real = promote.promote_candidate

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(promote, "promote_candidate", spy)
    _mod._promote_if_channel(str(rig.dest), "alpha", str(rig.root))
    assert len(seen) == 1
    assert seen[0]["unattended"] is True and seen[0]["confirm"] is True
    assert "pm_soak_override" not in seen[0] and "soak_waived_by" not in seen[0]


@pytest.mark.parametrize("branch", ["main", "work/feature"])
def test_non_channel_dest_never_reaches_promote_and_spawns_nothing(tmp_path, monkeypatch, branch):
    rig = _Rig(tmp_path)
    _g(rig.dest, "checkout", "-B", branch)
    spawns = []
    monkeypatch.setattr(_mod, "_run", lambda *a, **k: spawns.append(a))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawns.append(a))

    def boom(script):
        raise AssertionError("klabauter-promote loaded for a non-channel dest")

    monkeypatch.setattr(_mod, "_sibling_cli", boom)
    assert _mod._promote_if_channel(str(rig.dest), "alpha", str(rig.root)) == 0
    assert spawns == []


def test_dest_without_a_git_dir_skips_the_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_sibling_cli", lambda s: pytest.fail("loaded"))
    assert _mod._promote_if_channel(str(tmp_path), "alpha", str(tmp_path)) == 0


def test_unreadable_head_still_evaluates(tmp_path, monkeypatch):
    (tmp_path / ".git").write_text("gitdir: elsewhere\n")
    calls = []

    class _Promote:
        @staticmethod
        def promote_candidate(*a, **k):
            calls.append(k)
            return _outcome(main_branch=None, message="x")

    monkeypatch.setattr(_mod, "_sibling_cli", lambda s: _Promote)
    _mod._promote_if_channel(str(tmp_path), "alpha", str(tmp_path))
    assert len(calls) == 1


def _outcome(**kw):
    fields = dict(
        promoted=False, refusals=(), nothing_to_promote=False, push_failed=False,
        main_branch="main", promoted_sha=None, message="", followup_warning="",
    )
    fields.update(kw)
    return type("O", (), fields)()


# --- wiring: where in the round the leg runs --------------------------------

def _spy_leg(monkeypatch, exit_override=None):
    calls = []

    def leg(dest, target, percolate_root):
        calls.append((dest, target))
        return 0 if exit_override is None else exit_override

    monkeypatch.setattr(base._mod, "_promote_if_channel", leg)
    return calls


def _noop_round(tmp_path, monkeypatch, ahead_stdout, no_publish=False):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    root = tmp_path / "percolate-root"
    (root / "setup").mkdir(parents=True)
    spy = base._SubprocessSpy(
        dryrun_stdout="", real_stdout="", parse2_stdout=base._parse2_stdout(),
        dest_ahead_stdout=ahead_stdout,
    )
    base._install_manifest_stub(monkeypatch, spy)
    base._install_sibling_cli_stub(monkeypatch, spy)
    monkeypatch.setattr(base._mod.subprocess, "run", spy)
    monkeypatch.setattr(base._mod, "_branch0_gate", lambda t, r: str(source_dir))
    monkeypatch.setattr(base._mod, "_resolve_dest", lambda t, r: str(dest))
    monkeypatch.setattr(base._mod, "_resolve_central_state", lambda: None)
    argv = ["alpha", "--percolate-root", str(root), "--yes"] + (["--no-publish"] if no_publish else [])
    args = base._mod._build_parser().parse_args(argv)
    with contextlib.redirect_stdout(io.StringIO()):
        return base._mod._cmd_round(args), str(dest)


def test_noop_in_sync_round_reaches_the_leg(tmp_path, monkeypatch):
    calls = _spy_leg(monkeypatch)
    rc, dest = _noop_round(tmp_path, monkeypatch, "# branch.ab +0 -0\n")
    assert rc == 0 and calls == [(dest, "alpha")]


def test_noop_round_that_pushed_earlier_commits_reaches_the_leg(tmp_path, monkeypatch):
    calls = _spy_leg(monkeypatch)
    rc, _dest = _noop_round(tmp_path, monkeypatch, "# branch.ab +1 -0\n")
    assert rc == 0 and len(calls) == 1


def test_no_publish_noop_round_never_reaches_the_leg(tmp_path, monkeypatch):
    calls = _spy_leg(monkeypatch)
    _noop_round(tmp_path, monkeypatch, "# branch.ab +0 -0\n", no_publish=True)
    assert calls == []


def test_clean_round_push_reaches_the_leg_and_its_exit_code_is_the_rounds(tmp_path, monkeypatch):
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    calls = _spy_leg(monkeypatch, exit_override=1)
    rc, out, _spy, _dest = base._run_round(tmp_path, monkeypatch)
    assert len(calls) == 1 and rc == 1
    assert "Published: pushed to" in out


def test_no_publish_and_failed_push_never_reach_the_leg(tmp_path, monkeypatch):
    base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    calls = _spy_leg(monkeypatch)
    rc, _out, _spy, _dest = base._run_round(tmp_path, monkeypatch, no_publish=True)
    assert rc == 0 and calls == []
    second = tmp_path / "b"
    second.mkdir()
    rc, _out, _spy, _dest = base._run_round(second, monkeypatch, push_returncode=1)
    assert rc == base._mod._EXIT_FAIL and calls == []
