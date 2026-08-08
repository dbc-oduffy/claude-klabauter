"""Unit tests for coordinator_core.install.substrate._install_one's
overwrite-vs-preserve policy.

Spec backlink: this closes a genuinely-dropped coverage gap left by example-doctrine-repo's
deletion of ``coordinator/lib/tests/test-install-substrate-overwrite.sh``
(191 lines) in the `6fb5fb37` de-bash wave, which had no successor. The
subject under test is `_install_one` itself (`coordinator_core/install/
substrate.py:206-256`) — NOT `coordinator/lib/install-substrate.py`, which is
only a thin CLI trampoline.

Matrix covered: {extension/name class} x {destination state}.

Extension/name classes:
  - force-overwrite-on-diff: ``.py``, ``.sh``, ``.cmd``, and the three
    extension-less wrapper names (``machine-local``, ``resolve-coordinator-
    clone``, ``claude-home``) that force-overwrite regardless of extension.
  - preserve-on-diff (operator-surface): ``.toml`` and an arbitrary other
    extension (``.md``), representing "everything else."

Destination states: absent (fresh install), present-and-identical (no-op,
exec-bit still applied if requested), present-and-diverging (force-overwrite
or preserve, per class).

Deliberately skipped cell: `check_only=True` behaviour is not part of this
matrix — it's a distinct early-return branch (`substrate.py:215-217`) that
doesn't reach the overwrite/preserve decision this coverage gap is about,
and reading `check_only`'s dry-run print is a separate, already-legible
one-liner rather than a matrix cell.
"""
from __future__ import annotations

import os
import stat

import pytest

from coordinator_core.install.substrate import _install_one

# NTFS has no POSIX exec bit, and `os.chmod` cannot fabricate one on Windows
# (`os.stat(...).st_mode`'s S_IXUSR/S_IXGRP/S_IXOTH bits are unaffected by
# any chmod call there — verified empirically: chmod(mode | 0o111) is a
# silent no-op on the reported mode). `_install_one`'s `exec_bit` request is
# consequently a POSIX-only mechanism with no Windows-side equivalent to
# assert instead — Windows resolves executability via file extension/
# PATHEXT and the separate `.cmd` forwarder companion (see
# test_forwarder_trust_guard.py), never via the stat mode bits this file's
# exec-bit-only tests probe.
_EXEC_BIT_SKIP = pytest.mark.skipif(
    os.name == "nt",
    reason="NTFS has no POSIX exec bit; os.chmod cannot set S_IXUSR/GRP/OTH "
    "on Windows, so this exec-bit-only assertion has no Windows analogue",
)

# (src filename, name asserted force_overwrite=True) — force-overwrite classes.
_FORCE_OVERWRITE_CASES = [
    pytest.param("helper.py", id="py-ext"),
    pytest.param("helper.sh", id="sh-ext"),
    pytest.param("wrapper.cmd", id="cmd-ext"),
    pytest.param("machine-local", id="name-machine-local"),
    pytest.param("resolve-coordinator-clone", id="name-resolve-coordinator-clone"),
    pytest.param("claude-home", id="name-claude-home"),
]

# preserve-on-diff (operator-surface) classes.
_PRESERVE_CASES = [
    pytest.param("registry.toml", id="toml-ext"),
    pytest.param("README.md", id="other-ext"),
]


def _make_src(tmp_path, filename: str, content: str = "template content\n"):
    src = tmp_path / "src" / filename
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(content, encoding="utf-8")
    return src


# --- absent destination: install regardless of class -----------------------


@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES + _PRESERVE_CASES)
def test_install_one_absent_dst_installs_for_every_class(tmp_path, filename):
    src = _make_src(tmp_path, filename)
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)

    _install_one(src, dst, False, "machine-local", False)

    assert dst.is_file()
    assert dst.read_text(encoding="utf-8") == "template content\n"


@_EXEC_BIT_SKIP
@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES + _PRESERVE_CASES)
def test_install_one_absent_dst_applies_exec_bit_when_requested(tmp_path, filename):
    src = _make_src(tmp_path, filename)
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)

    _install_one(src, dst, True, "machine-local", False)

    mode = dst.stat().st_mode
    assert mode & stat.S_IXUSR
    assert mode & stat.S_IXGRP
    assert mode & stat.S_IXOTH


# --- present-and-identical: no-op (content untouched either way) -----------


@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES + _PRESERVE_CASES)
def test_install_one_identical_dst_is_a_noop_for_every_class(tmp_path, filename):
    src = _make_src(tmp_path, filename)
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("template content\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "template content\n"


@_EXEC_BIT_SKIP
@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES + _PRESERVE_CASES)
def test_install_one_identical_dst_still_applies_exec_bit(tmp_path, filename):
    # Both the identical-content preserve branch (substrate.py:236-238) and
    # the identical-content force-overwrite branch (substrate.py:230-235,
    # `elif exec_bit:` on the `filecmp.cmp` match) re-apply the exec bit even
    # though content isn't rewritten — a fresh dst without the bit set must
    # still end up executable regardless of class.
    #
    # Uniform across both classes as of the substrate.py fix: an exec bit can
    # be lost out-of-band (Windows checkout, core.fileMode=false, an archive
    # extraction) without content changing, and re-install is the documented
    # repair path — that repair path only works if content-identical dst
    # still gets its exec bit reapplied. Previously this coverage was split
    # into two tests because the force-overwrite class silently skipped the
    # reapply (a real asymmetry, now fixed); the split no longer serves a
    # purpose now that both classes behave the same way, so it was merged
    # back into one parametrized test covering all 8 classes.
    src = _make_src(tmp_path, filename)
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("template content\n", encoding="utf-8")
    dst.chmod(0o644)

    _install_one(src, dst, True, "machine-local", False)

    assert dst.stat().st_mode & stat.S_IXUSR


# --- present-and-diverging: force-overwrite classes overwrite --------------


@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES)
def test_install_one_diverging_dst_force_overwrites_code_and_wrapper_classes(
    tmp_path, filename, capsys
):
    src = _make_src(tmp_path, filename, content="new template content\n")
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "new template content\n"
    out = capsys.readouterr().out
    assert "updated" in out
    assert "re-install overwrites" in out


@_EXEC_BIT_SKIP
@pytest.mark.parametrize("filename", _FORCE_OVERWRITE_CASES)
def test_install_one_diverging_dst_force_overwrite_applies_exec_bit(tmp_path, filename):
    src = _make_src(tmp_path, filename, content="new template content\n")
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, True, "machine-local", False)

    assert dst.stat().st_mode & stat.S_IXUSR


# --- present-and-diverging: preserve classes protect operator content ------


@pytest.mark.parametrize("filename", _PRESERVE_CASES)
def test_install_one_diverging_dst_preserves_operator_surface_classes(
    tmp_path, filename, capsys
):
    src = _make_src(tmp_path, filename, content="new template content\n")
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "operator-modified content\n"
    out = capsys.readouterr().out
    assert "operator-customized" in out
    assert "preserved" in out


@pytest.mark.parametrize("filename", _PRESERVE_CASES)
def test_install_one_diverging_dst_preserve_uses_claude_home_warning_variant(
    tmp_path, filename, capsys
):
    # warn_prefix is caller-supplied and independent of the src name/suffix
    # that drives force_overwrite — the "claude-home" cross-repo-contract
    # warning fires purely off warn_prefix == "claude-home".
    src = _make_src(tmp_path, filename, content="new template content\n")
    dst = tmp_path / "dst" / filename
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, False, "claude-home", False)

    assert dst.read_text(encoding="utf-8") == "operator-modified content\n"
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "cross-repo contract surface" in out


# --- C1 regression: extensionless name NOT in the 3-name allowlist ---------
#
# `_install_one`'s own suffix/name classification only force-overwrites the
# three allowlisted extension-less wrapper names (machine-local,
# resolve-coordinator-clone, claude-home). An extensionless name outside that
# allowlist — e.g. `coordinator-settings-home`, the C1 bug class — is NOT
# code by construction to `_install_one` itself; it only force-overwrites
# when the CALL SITE passes `force_overwrite=True` (the post-C1 contract).
# Pre-C1, no call site did, so a diverging `coordinator-settings-home`
# destination was silently preserved forever as "operator customization."

_NON_ALLOWLISTED_EXTENSIONLESS_NAME = "coordinator-settings-home"


def test_install_one_non_allowlisted_extensionless_diverging_default_is_preserved(
    tmp_path, capsys
):
    """Without a caller-forced force_overwrite, this name falls to
    preserve-on-diff like any other non-code file — `_install_one` alone
    never overwrote it, which is exactly the gap the C1 bug fell through."""
    src = _make_src(
        tmp_path, _NON_ALLOWLISTED_EXTENSIONLESS_NAME, content="new template content\n"
    )
    dst = tmp_path / "dst" / _NON_ALLOWLISTED_EXTENSIONLESS_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "operator-modified content\n"
    out = capsys.readouterr().out
    assert "operator-customized" in out
    assert "preserved" in out


def test_install_one_non_allowlisted_extensionless_diverging_force_overwrite_true_overwrites(
    tmp_path, capsys
):
    """The C1 regression cell: post-C1, the `coordinator-settings-home` call
    site passes `force_overwrite=True` — this is the cell that would have
    caught the pre-C1 bug (a stale destination surviving re-install)."""
    src = _make_src(
        tmp_path, _NON_ALLOWLISTED_EXTENSIONLESS_NAME, content="new template content\n"
    )
    dst = tmp_path / "dst" / _NON_ALLOWLISTED_EXTENSIONLESS_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("operator-modified content\n", encoding="utf-8")

    _install_one(src, dst, True, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "new template content\n"
    if os.name != "nt":
        # See `_EXEC_BIT_SKIP`'s docstring: NTFS has no POSIX exec bit and
        # os.chmod cannot fabricate one, so this half of the assertion has
        # no Windows analogue — the content-overwrite half above still
        # applies on every platform.
        assert dst.stat().st_mode & stat.S_IXUSR
    out = capsys.readouterr().out
    assert "updated" in out
    assert "re-install overwrites" in out


# --- .ps1 regression: exec_bit=False by construction (Review: eng-director, F1/F10) ---


def test_install_one_ps1_exec_bit_false_by_construction_force_overwrite_true_overwrites(
    tmp_path, capsys
):
    """A `.ps1` has no POSIX exec bit to set — Windows resolves executability
    by extension, not the POSIX mode bit, so callers pass exec_bit=False for
    it by construction. `.ps1` is also not in `_install_one`'s suffix/name
    force-overwrite classification, so pre-fix a diverging `.ps1` destination
    (e.g. `claude-machine-local.ps1`) was preserved forever. Post-fix the
    call site passes force_overwrite=True; pin that exec_bit=False does not
    also suppress the overwrite."""
    src = _make_src(tmp_path, "claude-machine-local.ps1", content="Write-Host 'fresh'\n")
    dst = tmp_path / "dst" / "claude-machine-local.ps1"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("Write-Host 'stale'\n", encoding="utf-8")

    _install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "Write-Host 'fresh'\n"
    out = capsys.readouterr().out
    assert "updated" in out
    assert "re-install overwrites" in out


# --- AC17: a missing POSIX mode bit is not content drift --------------------


def test_install_one_identical_content_missing_mode_bit_is_not_reported_as_drift(
    tmp_path, capsys
):
    """A destination byte-identical to source but missing its POSIX mode bit
    (e.g. 0644 where exec_bit=True expects 0755) must not be reported as
    content drift: `_install_one` compares bytes via
    `filecmp.cmp(src, dst, shallow=False)`, which never inspects mode, so the
    identical-content branch (silent exec-bit reapply, no "updated" print) is
    taken rather than the diverging/overwrite branch."""
    src = _make_src(tmp_path, "helper.py", content="print('same')\n")
    dst = tmp_path / "dst" / "helper.py"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("print('same')\n", encoding="utf-8")
    dst.chmod(0o644)

    _install_one(src, dst, True, "machine-local", False)

    out = capsys.readouterr().out
    assert "updated" not in out
    if os.name != "nt":
        # See `_EXEC_BIT_SKIP`'s docstring: no Windows analogue for a
        # POSIX exec-bit reapply; the "not reported as drift" half above
        # (the actual AC17 subject) still applies on every platform.
        assert dst.stat().st_mode & stat.S_IXUSR
