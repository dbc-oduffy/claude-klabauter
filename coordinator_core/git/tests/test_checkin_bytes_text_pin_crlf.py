"""`_checkin_bytes` under a text/eol pin: bytes it returns hash to the sha real git prints."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import checkin_attrs
from coordinator_core.git.commit import FilterUnsupported, _checkin_bytes

pytestmark = pytest.mark.spawns_process

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_CORPUS = {
    "crlf": b"a\r\nb\r\nc\r\n",
    "lone_cr": b"a\rb\r\n",
    "trailing_lone_cr": b"a\r\nb\r",
    "mixed": b"a\r\nb\nc\r\n",
    "nul_with_crlf": b"a\r\n\x00b\r\n",
    "many_controls": b"\x01\x02\x03\x04\x05\x06\x07\x0b\x0e\x0f x\r\n",
    "ctrl_z_tail": b"ab\r\n" + b"\x01" * 2 + b"\x1a",
    "long_crlf": b"line of text\r\n" * 2000,
    "empty_lines": b"\r\n\r\n\r\n",
}

_PINS = {
    "text_auto_eol_lf": ("* text=auto eol=lf\n", "f.txt"),
    "text_auto": ("* text=auto\n", "f.txt"),
    "text_set": ("* text\n", "f.txt"),
    "text_set_eol_lf": ("* text eol=lf\n", "f.txt"),
    "eol_lf_only": ("*.x eol=lf\n", "f.x"),
    "auto_then_eol_override": ("* text=auto\n*.x eol=lf\n", "f.x"),
}


def _git(root: Path, *args: str, stdin: bytes = b"") -> bytes:
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    done = subprocess.run(
        ["git", *args], cwd=root, input=stdin, capture_output=True, env=env,
        creationflags=_NO_WINDOW, check=True,
    )
    return done.stdout


def _repo(tmp_path: Path, attrs: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    (root / ".gitattributes").write_text(attrs, encoding="ascii", newline="\n")
    return root


def _blob_sha(body: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(body) + body).hexdigest()


def _real(root: Path, rel: str, data: bytes) -> str:
    return _git(root, "hash-object", f"--path={rel}", "--stdin", stdin=data).decode().strip()


@pytest.mark.parametrize("pin", sorted(_PINS))
@pytest.mark.parametrize("shape", sorted(_CORPUS))
def test_returned_bytes_hash_like_real_git_or_refuse(tmp_path, pin, shape):
    attrs, rel = _PINS[pin]
    root = _repo(tmp_path, attrs)
    data = _CORPUS[shape]
    try:
        got = _checkin_bytes(root, rel, data)
    except FilterUnsupported:
        return
    assert _blob_sha(got) == _real(root, rel, data)


@pytest.mark.parametrize("pin", ["text_auto_eol_lf", "text_set", "eol_lf_only"])
def test_plain_crlf_is_converted_in_process(tmp_path, pin):
    attrs, rel = _PINS[pin]
    root = _repo(tmp_path, attrs)
    assert _checkin_bytes(root, rel, b"a\r\nb\r\n") == b"a\nb\n"


@pytest.mark.parametrize("pin", ["text_auto_eol_lf", "text_auto"])
@pytest.mark.parametrize("index_blob", [b"x\r\ny\r\n", b"x\ny\n"])
def test_text_auto_honours_cr_in_index_blob(tmp_path, pin, index_blob):
    attrs, rel = _PINS[pin]
    root = _repo(tmp_path, attrs)
    sha = _git(root, "hash-object", "-w", "--no-filters", "--stdin", stdin=index_blob).decode().strip()
    _git(root, "update-index", "--add", "--cacheinfo", f"100644,{sha},{rel}")
    data = b"a\r\nb\r\n"
    got = _checkin_bytes(root, rel, data)
    (root / rel).write_bytes(data)
    _git(root, "add", "--", rel)
    staged = _git(root, "ls-files", "-s", "--", rel).split()[1].decode()
    assert _blob_sha(got) == staged
    assert (got == data) == (b"\r" in index_blob)


def test_text_auto_binary_content_passes_through_raw(tmp_path):
    root = _repo(tmp_path, "* text=auto eol=lf\n")
    data = b"a\r\n\x00b\r\n"
    assert _checkin_bytes(root, "f.bin", data) == data


@pytest.mark.parametrize(
    "attrs,rel",
    [
        ("* eol=crlf\n", "f.txt"),
        ("* text eol=crlf\n", "f.txt"),
        ("* text=auto eol=crlf\n", "f.txt"),
        ("* -text eol=lf\n", "f.txt"),
        ("[attr]mac text\n* mac\n", "f.txt"),
        ("* text=weird\n", "f.txt"),
        ("* text binary\n", "f.txt"),
    ],
)
def test_unproven_shapes_still_refuse(tmp_path, attrs, rel):
    root = _repo(tmp_path, attrs)
    with pytest.raises(FilterUnsupported):
        _checkin_bytes(root, rel, b"a\r\nb\r\n")


def test_unreadable_index_blob_refuses(tmp_path):
    root = _repo(tmp_path, "* text=auto eol=lf\n")
    sha = _git(root, "hash-object", "-w", "--no-filters", "--stdin", stdin=b"x\r\n").decode().strip()
    _git(root, "update-index", "--add", "--cacheinfo", f"100644,{sha},f.txt")
    obj = root / ".git" / "objects" / sha[:2] / sha[2:]
    obj.chmod(0o666)
    obj.unlink()
    with pytest.raises(FilterUnsupported):
        _checkin_bytes(root, "f.txt", b"a\r\n")


def test_resolve_text_pin_tracks_text_and_eol_separately(tmp_path):
    root = _repo(tmp_path, "* text=auto\n*.x eol=lf\n")
    assert checkin_attrs.resolve_text_pin(root, "f.x") == checkin_attrs.TextPin("auto", "lf")
    assert checkin_attrs.resolve_text_pin(root, "f.y") == checkin_attrs.TextPin("auto", None)
    assert checkin_attrs.checkin_disposition(root, "f.x") == checkin_attrs.TEXT
