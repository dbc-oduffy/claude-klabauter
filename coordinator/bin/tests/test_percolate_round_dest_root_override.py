"""percolate-round --dest-root: the clone is validated, dest is relocated into it,
publish.py is handed the override flags, and the live checkout is never written.

Hermetic: a bare origin, a "live" clone and a scratch clone, all under tmp_path.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_dest_root_override.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_LIB_DIR = _BIN_DIR.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

from percolate.targets import (  # noqa: E402
    PUBLISH_DEST_ROOT_LIVE_FLAG,
    PUBLISH_DEST_ROOT_OVERRIDE_FLAG,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_dest_root", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(cwd, *args) -> str:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        env={**os.environ, **_GIT_ENV}, check=True,
    )
    return out.stdout.strip()


class _Repos:
    def __init__(self, root: Path):
        self.origin = root / "origin.git"
        self.live = root / "live"
        self.clone = root / "scratch"
        seed = root / "seed"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "coordinator_core").mkdir()
        (seed / "coordinator_core" / "a.txt").write_text("a\n")
        _git(seed, "add", ".")
        _git(seed, "commit", "-q", "-m", "seed")
        _git(seed, "push", "-q", str(self.origin), "HEAD:refs/heads/candidate")
        for dest in (self.live, self.clone):
            subprocess.run(["git", "clone", "-q", str(self.origin), str(dest)], check=True)
            _git(dest, "checkout", "-q", "candidate")

    @property
    def live_dest(self) -> str:
        return str(self.live / "coordinator_core")


@pytest.fixture
def repos(tmp_path):
    return _Repos(tmp_path)


def _live_state(live: Path):
    return (
        _git(live, "rev-parse", "HEAD"),
        _git(live, "--no-optional-locks", "status", "--porcelain"),
        (live / ".git" / "index").stat().st_mtime_ns,
    )


def test_valid_clone_relocates_dest_under_the_clone(repos):
    dest, override = _mod._resolve_dest_override(repos.live_dest, str(repos.clone))
    assert override is not None
    assert Path(dest) == Path(os.path.realpath(repos.clone)) / "coordinator_core"
    assert override.live_root == Path(os.path.realpath(repos.live))
    assert override.clone_root == Path(os.path.realpath(repos.clone))


def test_valid_clone_with_same_local_url_does_not_touch_live(repos):
    before = _live_state(repos.live)
    _mod._resolve_dest_override(repos.live_dest, str(repos.clone))
    assert _live_state(repos.live) == before


def _refused(repos, clone, capsys):
    dest, override = _mod._resolve_dest_override(repos.live_dest, str(clone))
    assert (dest, override) == (None, None)
    return capsys.readouterr().err


def test_refuses_the_live_checkout(repos, capsys):
    assert "percolate-round: refused:" in _refused(repos, repos.live, capsys)


def test_refuses_a_non_repo_dir(repos, tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "percolate-round: refused:" in _refused(repos, plain, capsys)


def test_refuses_a_subdir_of_the_clone(repos, capsys):
    err = _refused(repos, repos.clone / "coordinator_core", capsys)
    assert "not the root" in err


def test_refuses_a_shallow_clone(repos, tmp_path, capsys):
    shallow = tmp_path / "shallow"
    _git(tmp_path, "-c", "protocol.file.allow=always", "clone", "-q", "--depth", "1",
         f"file://{repos.origin}", str(shallow))
    # A file:// URL differs textually from the live path origin; shallow is checked first.
    assert "shallow" in _refused(repos, shallow, capsys)


def test_refuses_a_clone_whose_origin_is_the_live_checkout(repos, tmp_path, capsys):
    pushes_to_live = tmp_path / "from-live"
    subprocess.run(["git", "clone", "-q", str(repos.live), str(pushes_to_live)], check=True)
    assert "different origin" in _refused(repos, pushes_to_live, capsys)


def test_refuses_a_clone_with_a_different_origin(repos, tmp_path, capsys):
    other = tmp_path / "other.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(repos.origin), str(other)], check=True)
    foreign = tmp_path / "foreign"
    subprocess.run(["git", "clone", "-q", str(other), str(foreign)], check=True)
    assert "different origin" in _refused(repos, foreign, capsys)


def _drive_cmd_round(monkeypatch, repos, *, dest_root, extra=()):
    """`_cmd_round` with every collaborator but the override resolution stubbed;
    returns the (dest, override-or-None) `_cmd_round_default` received."""
    import coordinator_core.percolate.source_preflight as preflight

    seen = {}
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda root: str(repos.live))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda t, r: str(repos.live))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda t, r: repos.live_dest)
    monkeypatch.setattr(_mod, "_field7_preflight", lambda *a, **k: None)
    monkeypatch.setattr(preflight, "check_source_published", lambda *a, **k: None)

    def fake_default(args, target, percolate_root, source_dir, dest, tmp, **kwargs):
        seen["dest"] = dest
        seen["override"] = kwargs.get("override")
        seen["kwargs"] = kwargs
        # The stubbed round: write and commit one file in the dest.
        root = Path(_mod._resolve_repo_root(dest))
        (root / "coordinator_core" / "new.txt").write_text("n\n")
        _git(root, "add", ".")
        _git(root, "commit", "-q", "-m", "round")
        return 0

    monkeypatch.setattr(_mod, "_cmd_round_default", fake_default)
    argv = ["claude-klabauter", *extra]
    if dest_root is not None:
        argv += ["--dest-root", str(dest_root)]
    return _mod._cmd_round(_mod._build_parser().parse_args(argv)), seen


def test_cmd_round_commits_in_the_clone_and_leaves_live_untouched(monkeypatch, repos):
    before = _live_state(repos.live)
    clone_head = _git(repos.clone, "rev-parse", "HEAD")
    rc, seen = _drive_cmd_round(monkeypatch, repos, dest_root=repos.clone)
    assert rc == 0
    assert Path(seen["dest"]).is_relative_to(Path(os.path.realpath(repos.clone)))
    assert seen["override"] is not None
    assert _live_state(repos.live) == before
    assert _git(repos.clone, "rev-list", "--count", f"{clone_head}..HEAD") == "1"


def test_cmd_round_without_dest_root_passes_no_override(monkeypatch, repos):
    rc, seen = _drive_cmd_round(monkeypatch, repos, dest_root=None)
    assert rc == 0
    assert seen["dest"] == repos.live_dest
    assert seen["override"] is None


def test_cmd_round_refusal_returns_usage_and_never_reaches_the_round(monkeypatch, repos):
    reached = []
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: reached.append(1) or 0)
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda root: str(repos.live))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda t, r: str(repos.live))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda t, r: repos.live_dest)
    args = _mod._build_parser().parse_args(["claude-klabauter", "--dest-root", str(repos.live)])
    assert _mod._cmd_round(args) == _mod._EXIT_USAGE
    assert reached == []


def _capture_publish_argv(monkeypatch, tmp_path, dest, override):
    cmds = []

    def fake_run(cmd, *, timeout, **kwargs):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "stop")

    monkeypatch.setattr(_mod, "_run", fake_run)
    monkeypatch.setattr(_mod, "_round_held_lock", lambda *a, **k: contextlib.nullcontext(),
                        raising=False)
    args = _mod._build_parser().parse_args(["claude-klabauter", "--no-publish"])
    kwargs = {"override": override} if override is not None else {}
    _mod._cmd_round_default(args, "claude-klabauter", str(tmp_path), str(tmp_path), dest,
                            tmp_path, **kwargs)
    return next(c for c in cmds if str(_mod._PUBLISH) in c)


def test_publish_argv_carries_both_flags_with_realpaths(monkeypatch, repos, tmp_path):
    dest, override = _mod._resolve_dest_override(repos.live_dest, str(repos.clone))
    argv = _capture_publish_argv(monkeypatch, tmp_path, dest, override)
    assert argv[argv.index(PUBLISH_DEST_ROOT_OVERRIDE_FLAG) + 1] == os.path.realpath(repos.clone)
    assert argv[argv.index(PUBLISH_DEST_ROOT_LIVE_FLAG) + 1] == os.path.realpath(repos.live)


def test_publish_argv_without_override_has_neither_flag(monkeypatch, repos, tmp_path):
    argv = _capture_publish_argv(monkeypatch, tmp_path, repos.live_dest, None)
    assert PUBLISH_DEST_ROOT_OVERRIDE_FLAG not in argv
    assert PUBLISH_DEST_ROOT_LIVE_FLAG not in argv


def test_notice_under_override_names_the_clone_push(capsys):
    _mod._print_push_notice("t", clone_root="/c/clone")
    out = capsys.readouterr().out
    assert "git -C /c/clone -c credential.interactive=never push" in out
    assert "percolate-push" not in out


def test_notice_under_override_refusal_names_no_push(capsys):
    _mod._print_push_notice("t", refusal_reason="review warnings", clone_root="/c/clone")
    out = capsys.readouterr().out
    assert "uncertified commit" in out
    assert "push\n" not in out and "percolate-push" not in out


def test_notice_without_override_is_unchanged(capsys):
    _mod._print_push_notice("t")
    assert capsys.readouterr().out == (
        "\nPublish committed. Push is the operator's step:\n\n    percolate-push t\n"
    )
