"""Unit tests for coordinator_core.search.sources_powershell.

Covers parse-and-produce for `Get-Content`/aliases and `Get-ChildItem`/aliases,
and, more importantly, the decline set: the PowerShell-specific escapes this
module must refuse rather than approximate. No subprocess -- a differential
oracle against a real PowerShell host, if ever built, is a later chunk's job.
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.search import sources_powershell as sp
from coordinator_core.search.engine import MAX_RENDER_BYTES, Unanswerable


@pytest.fixture()
def workdir(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", newline="")
    (tmp_path / "b.txt").write_text("four\nfive", newline="")  # no trailing newline
    (tmp_path / "empty.txt").write_text("", newline="")
    return str(tmp_path)


def _produce(tokens, cwd, newline="\r\n"):
    return sp.parse_content_segment(tokens).produce(cwd, newline=newline)


# --------------------------------------------------------------------------- Get-Content


def test_get_content_single_file(workdir):
    assert _produce(["Get-Content", "a.txt"], workdir) == "one\r\ntwo\r\nthree\r\n"


@pytest.mark.parametrize("alias", ["cat", "gc", "type", "Get-Content", "get-content"])
def test_get_content_aliases_recognized(workdir, alias):
    assert _produce([alias, "a.txt"], workdir) == "one\r\ntwo\r\nthree\r\n"


def test_get_content_line_object_stream_always_ends_with_newline(workdir):
    # Unlike `cat`, Get-Content emits one line-object per source line, so a
    # file with no trailing newline still renders with one after the last
    # line -- this is the fidelity divergence the module docstring names.
    assert _produce(["Get-Content", "b.txt"], workdir) == "four\r\nfive\r\n"


def test_get_content_empty_file_is_empty(workdir):
    assert _produce(["Get-Content", "empty.txt"], workdir) == ""


def test_get_content_custom_newline_parameter(workdir):
    assert _produce(["Get-Content", "a.txt"], workdir, newline="\n") == "one\ntwo\nthree\n"


def test_get_content_absolute_operand_passthrough(workdir):
    abs_path = os.path.join(workdir, "a.txt")
    assert _produce(["Get-Content", abs_path], workdir) == "one\r\ntwo\r\nthree\r\n"


def test_get_content_relative_operand_joins_cwd(workdir):
    sub = os.path.join(workdir, "sub")
    os.mkdir(sub)
    with open(os.path.join(sub, "c.txt"), "w", newline="") as fh:
        fh.write("x")
    assert _produce(["Get-Content", "c.txt"], sub) == "x\r\n"


# --------------------------------------------------------------------------- Get-Content: -TotalCount/-First/-Tail/-Last


def test_get_content_totalcount(workdir):
    with open(os.path.join(workdir, "many.txt"), "w", newline="") as fh:
        fh.write("\n".join(str(i) for i in range(1, 21)) + "\n")
    out = _produce(["Get-Content", "-TotalCount", "3", "many.txt"], workdir)
    assert out == "1\r\n2\r\n3\r\n"


def test_get_content_first_alias(workdir):
    out = _produce(["Get-Content", "-First", "2", "a.txt"], workdir)
    assert out == "one\r\ntwo\r\n"


def test_get_content_tail(workdir):
    out = _produce(["Get-Content", "-Tail", "1", "a.txt"], workdir)
    assert out == "three\r\n"


def test_get_content_last_alias(workdir):
    out = _produce(["Get-Content", "-Last", "2", "a.txt"], workdir)
    assert out == "two\r\nthree\r\n"


def test_get_content_tail_zero_is_empty(workdir):
    assert _produce(["Get-Content", "-Tail", "0", "a.txt"], workdir) == ""


def test_get_content_totalcount_missing_value_declines():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", "-TotalCount"])


def test_get_content_totalcount_non_numeric_declines():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", "-TotalCount", "x", "a.txt"])


def test_get_content_duplicate_totalcount_declines():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", "-TotalCount", "1", "-First", "2", "a.txt"])


# --------------------------------------------------------------------------- Get-Content: decline set


@pytest.mark.parametrize("flag", ["-Raw", "-Encoding", "-Stream", "-Force", "-Wait"])
def test_get_content_declines_bytes_changing_flags(flag):
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", flag, "a.txt"])


def test_get_content_declines_absent_operand():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content"])


def test_get_content_declines_multiple_operands():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", "a.txt", "b.txt"])


def test_get_content_declines_unsupported_verb():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Item", "a.txt"])


def test_get_content_declines_empty_segment():
    with pytest.raises(Unanswerable):
        sp.parse_content_segment([])


@pytest.mark.parametrize("tokens", [
    ["Get-Content", "a.txt", ">", "out.txt"],
    ["Get-Content", "a.txt", ">>", "out.txt"],
    ["Get-Content", "a.txt", "2>", "err.txt"],
    ["Get-Content", "$(whoami)"],
    ["Get-Content", "`whoami`"],
    ["Get-Content", "<(cat f)"],
    ["Get-Content", "$env:TEMP"],
    ["Get-Content", "&"],
])
def test_get_content_declines_redirection_and_substitution(tokens):
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(tokens)


@pytest.mark.parametrize("operand", [
    "Env:TEMP",
    "env:TEMP",
    "Registry::HKEY_LOCAL_MACHINE\\SOFTWARE",
    "Function:\\prompt",
    "Cert:\\LocalMachine\\My",
    "WSMan:\\localhost",
    "Variable:\\Home",
    "Alias:\\gci",
])
def test_get_content_declines_non_filesystem_providers(operand):
    with pytest.raises(Unanswerable):
        sp.parse_content_segment(["Get-Content", operand])


def test_get_content_declines_glob_operand(workdir):
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "*.txt"], workdir)


def test_get_content_declines_nonexistent_path(workdir):
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "does-not-exist.txt"], workdir)


def test_get_content_declines_directory(workdir):
    sub = os.path.join(workdir, "sub")
    os.mkdir(sub)
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "sub"], workdir)


def test_get_content_declines_nul_byte(workdir):
    path = os.path.join(workdir, "mostly-text.dat")
    with open(path, "wb") as fh:
        fh.write(b"line\n" * 4000)
        fh.write(b"\x00")
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "mostly-text.dat"], workdir)


def test_get_content_declines_non_utf8_bytes(workdir):
    path = os.path.join(workdir, "latin1.txt")
    with open(path, "wb") as fh:
        fh.write("café".encode("latin-1"))
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "latin1.txt"], workdir)


def test_get_content_declines_oversized_file_without_reading(workdir):
    path = os.path.join(workdir, "big.txt")
    with open(path, "w") as fh:
        fh.write("x" * (MAX_RENDER_BYTES + 1))
    with pytest.raises(Unanswerable):
        _produce(["Get-Content", "big.txt"], workdir)


def test_get_content_at_cap_file_is_still_served(workdir):
    path = os.path.join(workdir, "exact.txt")
    with open(path, "w") as fh:
        fh.write("x" * MAX_RENDER_BYTES)
    assert _produce(["Get-Content", "exact.txt"], workdir) == "x" * MAX_RENDER_BYTES + "\r\n"


# --------------------------------------------------------------------------- Get-ChildItem: parsing


def test_parse_bare_get_childitem():
    spec = sp.parse_childitem_segment(["Get-ChildItem"])
    assert spec == sp.ChildItemSpec(directory=".")


@pytest.mark.parametrize("alias", ["gci", "ls", "dir", "Get-ChildItem", "get-childitem"])
def test_parse_get_childitem_aliases(alias):
    spec = sp.parse_childitem_segment([alias, "subdir"])
    assert spec == sp.ChildItemSpec(directory="subdir")


@pytest.mark.parametrize("flag", ["-Force", "-Recurse", "-Filter", "-Include", "-Hidden"])
def test_get_childitem_declines_any_flag(flag):
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(["Get-ChildItem", flag])


def test_get_childitem_declines_multiple_operands():
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(["Get-ChildItem", "dir1", "dir2"])


def test_get_childitem_declines_glob_operand():
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(["Get-ChildItem", "*.py"])


def test_get_childitem_declines_not_a_listing_verb():
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(["Get-Content", "a.txt"])


def test_get_childitem_declines_empty_segment():
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment([])


@pytest.mark.parametrize("tokens", [
    ["Get-ChildItem", ">", "out.txt"],
    ["Get-ChildItem", "subdir", ">>", "out.txt"],
    ["Get-ChildItem", "2>", "err.txt"],
    ["Get-ChildItem", "$(whoami)"],
    ["Get-ChildItem", "`whoami`"],
    ["Get-ChildItem", "<(cat f)"],
    ["Get-ChildItem", "$env:TEMP"],
    ["Get-ChildItem", "&"],
])
def test_get_childitem_declines_redirection_and_substitution(tokens):
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(tokens)


def test_get_childitem_declines_non_filesystem_provider():
    with pytest.raises(Unanswerable):
        sp.parse_childitem_segment(["Get-ChildItem", "Env:"])


# --------------------------------------------------------------------------- Get-ChildItem: run


@pytest.mark.skipif(sys.platform != "win32", reason="enumeration-order equivalence is win32-only")
def test_run_childitem_lists_visible_entries(tmp_path):
    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    spec = sp.ChildItemSpec(directory=".")
    result = sp.run_childitem(spec, cwd=str(tmp_path))
    assert "visible.txt" in result


@pytest.mark.skipif(sys.platform != "win32", reason="enumeration-order equivalence is win32-only")
def test_run_childitem_excludes_hidden_attribute(tmp_path):
    import ctypes

    hidden_path = tmp_path / "hidden.txt"
    hidden_path.write_text("x", encoding="utf-8")
    FILE_ATTRIBUTE_HIDDEN = 0x2
    ctypes.windll.kernel32.SetFileAttributesW(str(hidden_path), FILE_ATTRIBUTE_HIDDEN)

    (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
    spec = sp.ChildItemSpec(directory=".")
    result = sp.run_childitem(spec, cwd=str(tmp_path))
    assert "visible.txt" in result
    assert "hidden.txt" not in result


def test_run_childitem_declines_nonexistent_directory(tmp_path):
    spec = sp.ChildItemSpec(directory="does-not-exist")
    with pytest.raises(Unanswerable):
        sp.run_childitem(spec, cwd=str(tmp_path))


def test_run_childitem_declines_file_operand(tmp_path):
    (tmp_path / "plain.txt").write_text("hi", encoding="utf-8")
    spec = sp.ChildItemSpec(directory="plain.txt")
    with pytest.raises(Unanswerable):
        sp.run_childitem(spec, cwd=str(tmp_path))


def test_run_childitem_declines_off_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sp.sys, "platform", "linux")
    spec = sp.ChildItemSpec(directory=".")
    with pytest.raises(Unanswerable):
        sp.run_childitem(spec, cwd=str(tmp_path))


# --------------------------------------------------------------------------- dispatch


def test_parse_powershell_segment_dispatches_content():
    spec = sp.parse_powershell_segment(["Get-Content", "a.txt"])
    assert isinstance(spec, sp.ContentSpec)


def test_parse_powershell_segment_dispatches_childitem():
    spec = sp.parse_powershell_segment(["ls"])
    assert isinstance(spec, sp.ChildItemSpec)


def test_parse_powershell_segment_declines_unrecognized_verb():
    with pytest.raises(Unanswerable):
        sp.parse_powershell_segment(["Remove-Item", "a.txt"])
