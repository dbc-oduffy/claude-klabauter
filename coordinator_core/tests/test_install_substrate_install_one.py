"""
Regression coverage for ``coordinator_core.install.substrate._install_one``'s
overwrite policy, specifically the ``force_overwrite`` caller-forced class
added to repair the percolate-hook-files bug (a stale
``percolate-store.yaml``/README/`.gitkeep`/allowlist destination was silently
preserved as "operator customization" on re-install, because those files
carry non-code suffixes/names and never satisfied the pre-existing
suffix/name classification alone).

Pins three overwrite-policy legs:
  1. A hook-file-shaped destination that differs from source IS overwritten
     when the call site passes ``force_overwrite=True`` — the regression fix.
  2. A non-hook, non-code config file (``.toml``) is STILL preserved on diff
     when ``force_overwrite`` is left at its default — the operator-
     protection path must not regress.
  3. Destination-absent still copies; destination-identical is a no-op,
     regardless of ``force_overwrite``.

Spec backlink: dispatch brief "percolate-hook-files overwrite-policy repair",
2026-07-24 (bug confirmed against
Example-doctrine-repo coordinator/tests/test_percolate_store_copies_parity.py).
"""

from __future__ import annotations

from coordinator_core.install import substrate


def test_force_overwrite_repairs_stale_hook_file_destination(tmp_path, capsys):
    """The regression this fixes: a percolate-hook-shaped destination (e.g.
    percolate-store.yaml) that has drifted from source must be repaired on
    re-install when the call site marks it force_overwrite=True — it is a
    doctrine-tracked template, not operator config, even though its .yaml
    suffix/name would otherwise fall into the preserve-on-diff bucket."""
    src = tmp_path / "percolate-store.yaml"
    src.write_text("concerns: [fresh]\n", encoding="utf-8")
    dst = tmp_path / "dst" / "percolate-store.yaml"
    dst.parent.mkdir()
    dst.write_text("concerns: [stale]\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "concerns: [fresh]\n"
    out = capsys.readouterr().out
    assert "updated percolate-store.yaml" in out
    assert "re-install overwrites" in out


def test_non_hook_config_file_still_preserved_on_diff(tmp_path, capsys):
    """Operator-protection path must not regress: a plain .toml (no
    force_overwrite passed, default False) with a differing destination is
    preserved, not overwritten."""
    src = tmp_path / "registry.toml"
    src.write_text("concerns = []\n", encoding="utf-8")
    dst = tmp_path / "dst" / "registry.toml"
    dst.parent.mkdir()
    dst.write_text("concerns = [\"operator-added\"]\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "concerns = [\"operator-added\"]\n"
    out = capsys.readouterr().out
    assert "operator-customized" in out
    assert "preserved" in out


def test_destination_absent_copies_regardless_of_force_overwrite(tmp_path):
    src = tmp_path / "README.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "dst" / "README.md"
    dst.parent.mkdir()

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "hello\n"


def test_destination_identical_is_noop_with_force_overwrite(tmp_path, capsys):
    """Byte-identical destination is a no-op even under force_overwrite —
    no spurious "updated" print, no unnecessary copy."""
    src = tmp_path / "README.md"
    src.write_text("hello\n", encoding="utf-8")
    dst = tmp_path / "dst" / "README.md"
    dst.parent.mkdir()
    dst.write_text("hello\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False, force_overwrite=True)

    assert dst.read_text(encoding="utf-8") == "hello\n"
    out = capsys.readouterr().out
    assert "updated" not in out


def test_code_file_force_overwrite_reason_unchanged(tmp_path, capsys):
    """Pre-existing code-file classification (suffix/name) still reports
    'code file' as the reason, independent of the new caller-forced class."""
    src = tmp_path / "foo.py"
    src.write_text("print('fresh')\n", encoding="utf-8")
    dst = tmp_path / "dst" / "foo.py"
    dst.parent.mkdir()
    dst.write_text("print('stale')\n", encoding="utf-8")

    substrate._install_one(src, dst, False, "machine-local", False)

    assert dst.read_text(encoding="utf-8") == "print('fresh')\n"
    out = capsys.readouterr().out
    assert "updated foo.py (code file; re-install overwrites)" in out
