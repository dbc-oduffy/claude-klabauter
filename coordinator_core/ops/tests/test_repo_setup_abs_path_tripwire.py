"""
coordinator_core/ops/tests/test_repo_setup_abs_path_tripwire.py -- repo.setup seeds the absolute-path
tripwire, and the shared `scan_repo` detector it delegates to enforces the abs-path-ok carve-out rule.

Subject: `repo_setup.handle` over a `tmp_path` git repo (default arguments), then `scan_repo` over the
seeded repo with a drive-letter data root committed in code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import repo_setup as mod
from coordinator_core.ops.session.guard_concrete_path_citations import scan_repo
from coordinator_core.ops.tests.test_repo_setup import _git_init, sandbox  # noqa: F401  (fixture)

TRIPWIRE = "test_no_absolute_path_literals.py"
CODE_REL = "src/app.py"
DOC_REL = "docs/notes.md"
ROOT_LINE = 'DATA_ROOT = "D:/data/cache"'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
    )


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x", "--no-verify")


@pytest.fixture
def seeded(tmp_path, sandbox):  # noqa: F811
    plugin = mod._plugin_root()
    (plugin / "tests" / "templates" / TRIPWIRE).write_text("# tripwire\n", encoding="utf-8")
    repo = _git_init(tmp_path / "repo")
    _write(repo, "coordinator.local.md", "---\nfast_test_cmd: pytest tests/\nfull_test_cmd: pytest\n---\n")
    (repo / "tests").mkdir()
    verdict = mod.handle({"root": str(repo)})
    return repo, verdict


def _findings(repo: Path, rel: str):
    return [f for f in scan_repo(repo) if f.file == rel]


def test_repo_setup_seeds_the_absolute_path_tripwire(seeded):
    repo, verdict = seeded
    assert (repo / "tests" / "guards" / TRIPWIRE).is_file()
    assert any(i["step"] == "tripwires" and i["path"] == "tests/guards" for i in verdict["created"])


def test_drive_letter_data_root_in_code_is_flagged(seeded):
    repo, _ = seeded
    _write(repo, CODE_REL, ROOT_LINE + "\n")
    _commit(repo)
    assert [f.rule for f in _findings(repo, CODE_REL)] == ["drive-letter"]


def test_bare_reason_marker_in_code_is_still_flagged(seeded):
    repo, _ = seeded
    _write(repo, CODE_REL, f"{ROOT_LINE}  # abs-path-ok: synthetic\n")
    _commit(repo)
    assert [f.rule for f in _findings(repo, CODE_REL)] == ["drive-letter"]


def test_carve_out_id_listing_the_file_is_clean_and_unlisted_file_is_not(seeded):
    repo, _ = seeded
    line = f"{ROOT_LINE}  # abs-path-ok: carve-out=fixture-x\n"
    _write(repo, CODE_REL, line)
    _write(repo, "src/other.py", line)
    _write(
        repo,
        ".abs-path-carve-outs.toml",
        f'[[carve_out]]\nid = "fixture-x"\npaths = ["{CODE_REL}"]\nreason = "fixture"\n',
    )
    _commit(repo)
    assert _findings(repo, CODE_REL) == []
    assert [f.rule for f in _findings(repo, "src/other.py")] == ["drive-letter"]


def test_listed_registry_key_is_clean_and_unlisted_key_is_not(seeded):
    repo, _ = seeded
    _write(repo, CODE_REL, f"{ROOT_LINE}  # abs-path-ok: key=paths.cache_root\n")
    _write(repo, "src/other.py", f"{ROOT_LINE}  # abs-path-ok: key=paths.unlisted\n")
    _write(repo, ".abs-path-carve-outs.toml", '[registry_keys]\nkeys = ["paths.cache_root"]\n')
    _commit(repo)
    assert _findings(repo, CODE_REL) == []
    assert [f.rule for f in _findings(repo, "src/other.py")] == ["drive-letter"]


def test_markdown_bare_reason_line_is_clean(seeded):
    repo, _ = seeded
    _write(repo, DOC_REL, "Quoted evidence: D:/data/cache  abs-path-ok: quoted evidence\n")
    _commit(repo)
    assert _findings(repo, DOC_REL) == []
