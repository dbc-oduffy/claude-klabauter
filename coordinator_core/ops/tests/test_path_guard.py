"""
coordinator_core.ops.tests.test_path_guard

Unit tests for coordinator_core.ops._path_guard (safe_id / contained_path) —
the shared helper lifted and generalized from handoff_lineage_ancestry.py's
reference containment guard (op-family path-containment sweep, 2026-07-08).

Coverage:
  safe_id:
    (a) accepts a plain alphanumeric id
    (b) accepts dots/underscores/hyphens (a realistic filename-segment shape)
    (c) rejects a value containing '/'
    (d) rejects a value containing '\\'
    (e) rejects the bare traversal token '..'
    (f) rejects the bare traversal token '.'
    (g) rejects empty string
  contained_path:
    (h) accepts a candidate under an allowed root, returns resolved Path
    (i) rejects a candidate outside all allowed roots (returns None)
    (j) accepts a candidate under the SECOND of multiple allowed roots
    (k) symlink escape is caught (resolves outside every allowed root -> None)
    (l) macOS /tmp -> /private/tmp resolve mismatch does not spuriously reject
        when the expected root is itself passed through .resolve()
    (m) Windows extended-length-prefix asymmetry (2026-08-03 residual close,
        PM-authorized): an internal .resolve() adding the prefix to ONE
        operand and not the other must not desync the containment
        comparison, and the RETURNED path must keep the prefix intact
        (load-bearing on a genuine >MAX_PATH Windows path).

Spec backlink: docs/problems/2026-07-08-op-family-path-containment-investigation.md § 4
Spec backlink (m): state/audits/2026-08-03-extended-length-prefix-call-site-audit.md
"""

from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.ops._path_guard import contained_path, safe_id


# ---------------------------------------------------------------------------
# safe_id
# ---------------------------------------------------------------------------


def test_safe_id_accepts_plain_alphanumeric():
    assert safe_id("abc123") is True


def test_safe_id_accepts_dots_underscores_hyphens():
    assert safe_id("2026-07-08_test.file-name") is True


def test_safe_id_rejects_forward_slash():
    assert safe_id("../secret") is False
    assert safe_id("a/b") is False


def test_safe_id_rejects_backslash():
    assert safe_id("a\\b") is False


def test_safe_id_rejects_bare_double_dot():
    """The regex alone admits '..' (all dots); the explicit not-in check rejects it."""
    assert safe_id("..") is False


def test_safe_id_rejects_bare_single_dot():
    assert safe_id(".") is False


def test_safe_id_rejects_empty_string():
    assert safe_id("") is False


# ---------------------------------------------------------------------------
# contained_path
# ---------------------------------------------------------------------------


def test_contained_path_accepts_candidate_under_root(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    candidate = root / "file.md"
    candidate.write_text("x", encoding="utf-8")

    result = contained_path(candidate, [root])
    assert result == candidate.resolve()


def test_contained_path_rejects_candidate_outside_all_roots(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "secret.md"
    outside.parent.mkdir()
    outside.write_text("x", encoding="utf-8")

    result = contained_path(outside, [root])
    assert result is None


def test_contained_path_accepts_candidate_under_second_of_multiple_roots(tmp_path):
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    root_b = tmp_path / "root_b"
    root_b.mkdir()
    candidate = root_b / "file.md"
    candidate.write_text("x", encoding="utf-8")

    result = contained_path(candidate, [root_a, root_b])
    assert result == candidate.resolve()


def test_contained_path_symlink_escape_is_rejected(tmp_path):
    """A symlink inside the allowed root pointing outside it must be rejected —
    .resolve() follows the symlink, and the resolved target is not under any
    allowed root."""
    root = tmp_path / "allowed"
    root.mkdir()
    outside_target = tmp_path / "elsewhere" / "secret.md"
    outside_target.parent.mkdir()
    outside_target.write_text("secret", encoding="utf-8")

    symlink_path = root / "escape.md"
    try:
        symlink_path.symlink_to(outside_target)
    except OSError:
        # Symlink creation can fail on some platforms/permissions; skip rather
        # than fail the suite over an environment limitation.
        import pytest
        pytest.skip("symlink creation not permitted in this environment")

    result = contained_path(symlink_path, [root])
    assert result is None, "symlink escaping the allowed root must be rejected"


def test_contained_path_macos_tmp_resolve_symmetry(tmp_path):
    """Both candidate and allowed-root must be passed through .resolve() so a
    macOS /tmp -> /private/tmp symlink mismatch does not spuriously reject.

    tmp_path fixtures live under a tmp dir that .resolve() may remap (e.g.
    macOS /tmp -> /private/tmp). contained_path resolves the root internally
    (root.resolve()), so passing an UN-resolved root here still succeeds.
    """
    root = tmp_path / "state" / "handoffs"
    root.mkdir(parents=True)
    candidate = root / "h.md"
    candidate.write_text("x", encoding="utf-8")

    # Pass the allowed root WITHOUT pre-resolving it — contained_path must
    # resolve it internally before the relative_to check.
    result = contained_path(candidate, [root])
    assert result == candidate.resolve()
    assert str(result) == os.path.realpath(candidate)


# ---------------------------------------------------------------------------
# Windows extended-length-prefix asymmetry (2026-08-03 residual close).
# `Path.resolve()` is monkeypatched, keyed on the specific operand, to
# reproduce the exact length-triggered shape a real Windows host produces:
# ONE operand's internal resolve gains the `\\?\` (or UNC `\\?\UNC\`) prefix,
# the other does not. This is a macOS box; live resolve() never produces the
# prefix here (docs/problems/2026-07-08-op-family-path-containment-
# investigation.md Anti-scope: "do not assume macOS behaviour generalizes"),
# so the asymmetry must be injected rather than reproduced live.
# ---------------------------------------------------------------------------


def test_contained_path_extended_length_prefix_on_candidate_only_still_contained(tmp_path, monkeypatch):
    r"""Pre-fix, this failed: `resolved.relative_to(root.resolve())` compared
    a `\\?\`-prefixed candidate Path against a bare root Path, raised
    ValueError on every root, and contained_path returned None for a
    candidate genuinely inside the allowed root. Red assertion pre-fix:
    `assert result is not None` -> AssertionError (result was None)."""
    root = tmp_path / "allowed"
    root.mkdir()
    candidate = root / "file.md"
    candidate.write_text("x", encoding="utf-8")

    real_resolve = Path.resolve

    def fake_resolve(self, *a, **kw):
        result = real_resolve(self, *a, **kw)
        if self == candidate:
            return Path("\\\\?\\" + str(result))
        return result

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = contained_path(candidate, [root])

    assert result is not None
    # Load-bearing negative: the RETURNED path is the real resolved path,
    # prefix intact — normalization is comparison-only, never returned.
    assert str(result).startswith("\\\\?\\")


def test_contained_path_extended_length_prefix_on_root_only_still_contained(tmp_path, monkeypatch):
    """Mirror of the above with the prefix on the ROOT side instead of the
    candidate — the asymmetry is directionless; either operand can be the
    one whose internal resolve() happens to grow the prefix. Red assertion
    pre-fix: `assert result is not None` -> AssertionError (result was
    None)."""
    root = tmp_path / "allowed"
    root.mkdir()
    candidate = root / "file.md"
    candidate.write_text("x", encoding="utf-8")

    real_resolve = Path.resolve

    def fake_resolve(self, *a, **kw):
        result = real_resolve(self, *a, **kw)
        if self == root:
            return Path("\\\\?\\" + str(result))
        return result

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = contained_path(candidate, [root])

    assert result is not None
    # Candidate side untouched here — return value carries no prefix.
    assert not str(result).startswith("\\\\?\\")


def _has_extended_length_prefix(path: Path) -> bool:
    """True when `path` still carries a Windows extended-length prefix, in
    either spelling.

    `\\\\?\\` is the raw form straight off Windows; `//?/` is the
    forward-slash-normalized form `strip_extended_length_prefix` also accepts
    (its own docstring names both). A test that hard-codes one spelling is
    asserting which host it runs on, so the prefix-survives negative is
    checked through this instead.
    """
    text = str(path)
    return text.startswith("\\\\?\\") or text.startswith("//?/")


def test_contained_path_unc_prefix_asymmetry_also_still_contained(monkeypatch):
    r"""UNC form (`\\?\UNC\<server>\<share>\...`) is the other prefix shape
    `strip_extended_length_prefix` recognizes — collapses to the bare UNC
    double-separator rather than to nothing, so it is exercised separately
    from the plain-prefix tests above. `Path.resolve()` is faked entirely
    (no real filesystem backing) since UNC path decomposition is
    Windows-`WindowsPath`-specific (backslash-delimited parts do not
    decompose under this macOS suite's `PosixPath`, which only splits on
    `/`) — the forward-slash UNC spelling is used instead, a shape
    `strip_extended_length_prefix` explicitly also accepts (its own
    docstring: "raw backslash form straight off Windows, or already
    forward-slash normalized"), so `relative_to` can actually decompose
    the parts on this host. `Path.resolve()` is faked entirely (no real
    filesystem backing): the fake resolves both operands directly to their
    post-`.resolve()` UNC-shaped strings, one prefixed, one bare, exactly
    as `strip_extended_length_prefix`'s own unit tests
    (`write_guards/tests/test__case_fold_path.py`) probe the UNC branch.
    Red assertion pre-fix: `assert result is not None` -> AssertionError
    (result was None)."""
    root = Path("root-marker")
    candidate = Path("candidate-marker")
    bare_unc = "//server/share/dir"
    prefixed_unc_candidate = "//?/unc/server/share/dir/file.md"

    def fake_resolve(self, *a, **kw):
        if self == root:
            return Path(bare_unc)
        if self == candidate:
            return Path(prefixed_unc_candidate)
        raise AssertionError(f"unexpected resolve() call: {self!r}")

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = contained_path(candidate, [root])

    assert result is not None
    # Compared as PATHS, not as strings. The invariant under test is "the
    # returned path is the caller's resolved path with its extended-length
    # prefix intact" -- not any particular separator spelling. `WindowsPath`
    # canonicalizes `/` to `\\` at construction, so a `str(...) ==` against
    # this test's forward-slash literal can never hold on Windows no matter
    # what `contained_path` returns; it was asserting the host's separator
    # convention, not the guard's behaviour. `Path.__eq__` normalizes per
    # platform, so this reads the same on both.
    assert result == Path(prefixed_unc_candidate)
    # Load-bearing negative, kept explicit and separator-agnostic: the
    # extended-length prefix must SURVIVE. Normalization inside
    # `contained_path` is comparison-only; a stripped return value would
    # still fail here, which is the whole point of this assertion.
    assert _has_extended_length_prefix(result)


def test_contained_path_genuinely_long_windows_path_return_value_keeps_prefix(tmp_path, monkeypatch):
    """The load-bearing negative named in the brief: a real >MAX_PATH
    Windows path where the extended-length prefix is NOT cosmetic. The
    returned Path must retain it — a caller doing real filesystem work with
    a prefix-stripped return value would silently target the wrong (or a
    non-existent, truncated) name on a genuine long-path host."""
    root = tmp_path / "allowed"
    root.mkdir()
    candidate = root / "file.md"
    candidate.write_text("x", encoding="utf-8")
    long_form = "\\\\?\\" + str(root.resolve()) + "/" + ("x" * 300) + "/file.md"

    real_resolve = Path.resolve

    def fake_resolve(self, *a, **kw):
        if self == candidate:
            return Path(long_form)
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    result = contained_path(candidate, [root])

    assert result is not None
    # Path-compared for the same reason as the UNC test above: `long_form` is
    # built with a `/` separator before the final component, which
    # `WindowsPath` rewrites to `\\` on construction.
    assert result == Path(long_form)
    assert _has_extended_length_prefix(result)
