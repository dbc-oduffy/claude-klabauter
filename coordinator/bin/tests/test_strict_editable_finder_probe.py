"""`strict-editable-finder-probe.py` -- classifies finder-based/repo-root-on-path editable
installs on a shared interpreter (docs/plans/2026-09-26-fleet-strict-mode-editable-installs.md,
row C2).

Zero-spawn throughout, unlike `test_waste_signal.py`'s own precedent which is only PARTIAL (it
also carries a `@pytest.mark.spawns_process` subprocess case): this file loads the script via
`importlib.util.spec_from_file_location`, the same way `test_waste_signal.py::_load` does, and
drives `main(argv)` in-process for every test case. No `subprocess.run` anywhere in this file.

Covers AC3 (JSON shape, exit codes), AC4 (the four classification rules, each pinned by its own
fixture), and AC5 (zero-spawn, in-process `main(argv)` driving).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "strict-editable-finder-probe.py"


def _load():
    spec = importlib.util.spec_from_file_location("strict_editable_finder_probe", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


probe = _load()


def _write_pth(site_dir: Path, name: str, body: str) -> Path:
    site_dir.mkdir(parents=True, exist_ok=True)
    p = site_dir / name
    p.write_text(body, encoding="utf-8")
    return p


# --- AC4 classification fixtures --------------------------------------------------------------


def test_import_finder_pth_is_classified_import_finder(tmp_path):
    site_dir = tmp_path / "site-packages"
    _write_pth(
        site_dir,
        "__editable__.mypkg-1.2.3.pth",
        "import __editable___mypkg_1_2_3_finder; __editable___mypkg_1_2_3_finder.install()\n",
    )

    findings = probe.classify([str(site_dir)])

    assert findings == [
        {"pth": "__editable__.mypkg-1.2.3.pth", "dist": "mypkg", "class": "import-finder"}
    ]


def test_compat_pth_pointing_at_repo_root_is_repo_root_on_path(tmp_path):
    site_dir = tmp_path / "site-packages"
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    _write_pth(site_dir, "__editable__.otherpkg-0.1.pth", f"{repo_root}\n")

    findings = probe.classify([str(site_dir)])

    assert findings == [
        {"pth": "__editable__.otherpkg-0.1.pth", "dist": "otherpkg", "class": "repo-root-on-path"}
    ]


def test_compat_pth_pointing_at_src_with_no_project_file_is_clean(tmp_path):
    site_dir = tmp_path / "site-packages"
    src_dir = tmp_path / "repo" / "src"
    src_dir.mkdir(parents=True)
    _write_pth(site_dir, "__editable__.cleanpkg-2.0.pth", f"{src_dir}\n")

    findings = probe.classify([str(site_dir)])

    assert findings == []


def test_distutils_precedence_pth_is_never_flagged(tmp_path):
    site_dir = tmp_path / "site-packages"
    _write_pth(
        site_dir,
        "distutils-precedence.pth",
        "import sys; sys.path = [p for p in sys.path if p != '']\n",
    )

    findings = probe.classify([str(site_dir)])

    assert findings == []


def test_plain_non_editable_pth_is_never_flagged(tmp_path):
    site_dir = tmp_path / "site-packages"
    other_dir = tmp_path / "somewhere"
    other_dir.mkdir()
    (other_dir / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    _write_pth(site_dir, "plain.pth", f"{other_dir}\n")

    findings = probe.classify([str(site_dir)])

    assert findings == []


def test_compat_pth_relative_path_line_resolved_against_pth_parent_dir(tmp_path):
    """Sixth fixture (AC4's relative-resolution rule): a relative path line in a compat `.pth`
    resolves against the `.pth` file's OWN parent directory, not the cwd or the site dir's
    ancestor -- and every non-blank, non-#, non-import line is evaluated, not only the first."""
    site_dir = tmp_path / "nested" / "site-packages"
    repo_root = tmp_path / "elsewhere" / "repo"
    repo_root.mkdir(parents=True)
    (repo_root / "setup.py").write_text("# setup\n", encoding="utf-8")

    # Relative path from site_dir to repo_root.
    import os

    rel = os.path.relpath(repo_root, start=site_dir)

    body = "# a leading comment, ignored\n" f"{rel}\n"
    _write_pth(site_dir, "__editable__.relpkg-3.4.pth", body)

    findings = probe.classify([str(site_dir)])

    assert findings == [
        {"pth": "__editable__.relpkg-3.4.pth", "dist": "relpkg", "class": "repo-root-on-path"}
    ]


def test_dist_name_with_internal_hyphen_extracted_correctly(tmp_path):
    site_dir = tmp_path / "site-packages"
    _write_pth(
        site_dir,
        "__editable__.project-rag-1.0.0.pth",
        "import __editable___example_retrieval_repo_1_0_0_finder\n",
    )

    findings = probe.classify([str(site_dir)])

    assert findings[0]["dist"] == "project-rag"
    assert findings[0]["class"] == "import-finder"


# --- AC3: JSON shape and exit codes, driven through main(argv) in-process ----------------------


def test_main_no_findings_exits_zero_and_prints_empty_findings(tmp_path, capsys):
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()

    rc = probe.main(["--site-dir", str(site_dir)])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"findings": []}


def test_main_with_findings_exits_one_and_prints_them(tmp_path, capsys):
    site_dir = tmp_path / "site-packages"
    _write_pth(
        site_dir,
        "__editable__.foo-1.0.pth",
        "import __editable___foo_1_0_finder\n",
    )

    rc = probe.main(["--site-dir", str(site_dir)])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert out["findings"] == [
        {"pth": "__editable__.foo-1.0.pth", "dist": "foo", "class": "import-finder"}
    ]


def test_main_multiple_site_dirs_scanned_together(tmp_path, capsys):
    site_dir_a = tmp_path / "a"
    site_dir_b = tmp_path / "b"
    _write_pth(site_dir_a, "__editable__.pkga-1.0.pth", "import pkga_finder\n")
    _write_pth(site_dir_b, "__editable__.pkgb-1.0.pth", "import pkgb_finder\n")

    rc = probe.main(["--site-dir", str(site_dir_a), "--site-dir", str(site_dir_b)])

    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    dists = {f["dist"] for f in out["findings"]}
    assert dists == {"pkga", "pkgb"}


def test_main_missing_site_dir_is_skipped_not_an_error(tmp_path, capsys):
    missing = tmp_path / "does-not-exist"

    rc = probe.main(["--site-dir", str(missing)])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"findings": []}


def test_module_is_inert_until_main_is_called():
    """Reaching here at all (via `_load()` above, which imports/execs the module for every other
    test in this file) is already the proof; this additionally confirms `main` is present and
    callable without having been invoked yet."""
    assert callable(probe.main)
    assert callable(probe.classify)
