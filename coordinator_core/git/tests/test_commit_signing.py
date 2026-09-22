"""`commit.gpgsign` is porcelain-only -- these pin that every native
commit-object seam replays it, and that resolving the answer never spawns."""

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit_signing import (
    SigningFailed,
    _read_config_bool,
    _strip_inline_comment,
    commit_signing_enabled,
    sign_flag_args,
    write_signed_commit_object,
)


def _write_config(root: Path, body: str) -> None:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "config").write_text(body, encoding="utf-8")


@pytest.fixture
def no_global_config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))


def test_unset_gpgsign_reads_false(tmp_path, no_global_config):
    _write_config(tmp_path, "[core]\n\trepositoryformatversion = 0\n")
    assert commit_signing_enabled(tmp_path) is False
    assert sign_flag_args(tmp_path) == []


def test_local_true_enables_signing(tmp_path, no_global_config):
    _write_config(tmp_path, "[commit]\n\tgpgsign = true\n")
    assert commit_signing_enabled(tmp_path) is True
    assert sign_flag_args(tmp_path) == ["-S"]


def test_local_false_beats_global_true(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_config(tmp_path, "[commit]\n\tgpgsign = false\n")
    assert commit_signing_enabled(tmp_path) is False


def test_global_true_applies_when_local_silent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_config(tmp_path, "[core]\n\tbare = false\n")
    assert commit_signing_enabled(tmp_path) is True


@pytest.mark.parametrize(
    "value,expected",
    [("true", True), ("yes", True), ("on", True), ("1", True),
     ("false", False), ("no", False), ("off", False), ("0", False)],
)
def test_boolean_spellings(tmp_path, no_global_config, value, expected):
    _write_config(tmp_path, f"[commit]\n\tgpgsign = {value}\n")
    assert commit_signing_enabled(tmp_path) is expected


def test_inline_comment_does_not_swallow_the_value(tmp_path, no_global_config):
    """`gpgsign = true # note` is a form git accepts. Reading it as the token
    `true # note` reports a signing repo as unsigned, which ships silently."""
    for body in (
        "[commit]\n\tgpgsign = true # fleet policy\n",
        "[commit]\n\tgpgsign = true ; fleet policy\n",
        "[commit] # section comment\n\tgpgsign = true\n",
    ):
        _write_config(tmp_path, body)
        assert commit_signing_enabled(tmp_path) is True, body


def test_quoted_hash_is_part_of_the_value(tmp_path, no_global_config):
    """A `#` inside quotes is literal, so it must not start a comment."""
    assert _strip_inline_comment('\tprogram = "/usr/bin/sign#1" # note').strip() == (
        'program = "/usr/bin/sign#1"'
    )


def test_unrecognised_value_is_unknown_not_false(tmp_path, monkeypatch):
    """The one wrong answer that ships silently is "signing is off". An
    unparseable local value defers to the global config rather than
    reporting a signing repo as unsigned."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[commit]\n\tgpgsign = true\n", encoding="utf-8")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write_config(tmp_path, "[commit]\n\tgpgsign = maybe\n")
    assert _read_config_bool(tmp_path / ".git" / "config", "commit", "gpgsign") is None
    assert commit_signing_enabled(tmp_path) is True


def test_valueless_key_is_true(tmp_path, no_global_config):
    """git reads a bare `[commit] gpgsign` as true -- so does this."""
    _write_config(tmp_path, "[commit]\n\tgpgsign\n")
    assert _read_config_bool(tmp_path / ".git" / "config", "commit", "gpgsign") is True


def test_resolution_spawns_nothing(tmp_path, no_global_config, monkeypatch):
    """The resolver sits on the brightline path; the signature does not."""
    _write_config(tmp_path, "[commit]\n\tgpgsign = true\n")

    def _refuse(*args, **kwargs):
        raise AssertionError("commit_signing_enabled must not spawn")

    monkeypatch.setattr(subprocess, "run", _refuse)
    monkeypatch.setattr(subprocess, "Popen", _refuse)
    assert commit_signing_enabled(tmp_path) is True


def test_signing_failure_warns_rather_than_raising(tmp_path, monkeypatch):
    """DR-308: a signature that cannot be produced must never block the
    commit. `write_signed_commit_object` reports `(None, warning)` so its
    caller falls through to the same zero-spawn unsigned write it would
    have used with `commit.gpgsign` off -- never a raise, which is how the
    old contract (`SigningFailed`) could abort a commit outright."""
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, b"", b"no signing key"),
    )
    sha, warning = write_signed_commit_object(
        tmp_path, "0" * 40, None, "msg", "C", "c@x", "1700000000 +0000",
    )
    assert sha is None
    assert warning is not None
    assert "no signing key" in warning
    assert "committed unsigned" in warning


def test_message_bytes_reach_commit_tree_verbatim(tmp_path, no_global_config, monkeypatch):
    """The native writers this stands in for write their message bytes
    verbatim. A decode/re-encode here would make a non-UTF-8 body differ
    between a signed and an unsigned commit of the same input."""
    captured = {}

    def _capture(args, **kwargs):
        captured["input"] = kwargs.get("input")
        captured["text"] = kwargs.get("text")
        return subprocess.CompletedProcess(args, 0, b"a" * 40 + b"\n", b"")

    monkeypatch.setattr(subprocess, "run", _capture)
    raw = b"subject\n\nbody with \xff a non-utf8 byte\n"
    sha, warning = write_signed_commit_object(
        tmp_path, "0" * 40, None, raw, "C", "c@x", "1700000000 +0000",
    )
    assert warning is None
    assert sha == "a" * 40
    assert captured["input"] == raw
    assert not captured["text"]


def test_identity_is_pinned_for_author_and_committer(tmp_path, no_global_config, monkeypatch):
    """A signed commit carries the same author and committer lines the native
    path would have stamped -- same name, email and timestamp."""
    captured = {}

    def _capture(args, **kwargs):
        captured["env"] = kwargs.get("env")
        captured["args"] = list(args)
        return subprocess.CompletedProcess(args, 0, b"b" * 40 + b"\n", b"")

    monkeypatch.setattr(subprocess, "run", _capture)
    sha, warning = write_signed_commit_object(
        tmp_path, "0" * 40, "f" * 40, "msg", "Ada", "ada@x", "1700000000 +0000",
    )
    assert warning is None
    assert sha == "b" * 40
    env = captured["env"]
    for role in ("AUTHOR", "COMMITTER"):
        assert env[f"GIT_{role}_NAME"] == "Ada"
        assert env[f"GIT_{role}_EMAIL"] == "ada@x"
        assert env[f"GIT_{role}_DATE"] == "1700000000 +0000"
    assert "-S" in captured["args"]
    assert captured["args"][captured["args"].index("-p") + 1] == "f" * 40
