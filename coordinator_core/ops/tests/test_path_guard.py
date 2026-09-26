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
    assert safe_id("..") is False


def test_safe_id_rejects_bare_single_dot():
    assert safe_id(".") is False


def test_safe_id_rejects_empty_string():
    assert safe_id("") is False


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
    root = tmp_path / "allowed"
    root.mkdir()
    outside_target = tmp_path / "elsewhere" / "secret.md"
    outside_target.parent.mkdir()
    outside_target.write_text("secret", encoding="utf-8")

    symlink_path = root / "escape.md"
    try:
        symlink_path.symlink_to(outside_target)
    except OSError:
        import pytest
        pytest.skip("symlink creation not permitted in this environment")

    result = contained_path(symlink_path, [root])
    assert result is None, "symlink escaping the allowed root must be rejected"


def test_contained_path_macos_tmp_resolve_symmetry(tmp_path):
    root = tmp_path / "state" / "handoffs"
    root.mkdir(parents=True)
    candidate = root / "h.md"
    candidate.write_text("x", encoding="utf-8")

    result = contained_path(candidate, [root])
    assert result == candidate.resolve()
    assert str(result) == os.path.realpath(candidate)


def test_contained_path_extended_length_prefix_on_candidate_only_still_contained(tmp_path, monkeypatch):
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
    assert str(result).startswith("\\\\?\\")


def test_contained_path_extended_length_prefix_on_root_only_still_contained(tmp_path, monkeypatch):
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
    assert not str(result).startswith("\\\\?\\")


def _has_extended_length_prefix(path: Path) -> bool:
    text = str(path)
    return text.startswith("\\\\?\\") or text.startswith("//?/")


def test_contained_path_unc_prefix_asymmetry_also_still_contained(monkeypatch):
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
    assert result == Path(prefixed_unc_candidate)
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
    assert result == Path(long_form)
    assert _has_extended_length_prefix(result)
