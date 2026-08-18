"""coordinator/bin/tests/test_publish_git_archive_eol_regimes.py — owed
regression pin for `publish.py::_extract_git_archive` (D6,
state/handoffs/2026-08-16-ceremony-cli-surface-defects.md). This call was
CRLF-corrupting every text file it materialized on Windows before ecb252b92
(comment corrected 60a9e00c9); no test guarded it before this file.

Per `_extract_git_archive`'s own docstring, the fix is two `git -c` flags —
`core.autocrlf=false` and `core.eol=lf` — each load-bearing in a DIFFERENT
`.gitattributes` regime:

  - attribute-free path (e.g. the repo's vendored cockpit-contract/LICENSE):
    `core.autocrlf` alone drives conversion. `autocrlf=false` is
    load-bearing; `eol=lf` is inert.
  - explicit `text=auto` path (this repo sets `* text=auto` repo-wide):
    the ATTRIBUTE drives conversion; the target ending comes from
    `core.eol`, which defaults to `native` = CRLF on Windows. `eol=lf` is
    load-bearing; `autocrlf=false` is inert.

A pin covering only one regime stays green if the other regime's flag is
later dropped as "redundant" — the exact cleanup the pre-fix comment
invited. This file therefore builds a throwaway git repo per regime (never
depends on this repo's own live `.gitattributes` state, which can drift)
with the toplevel's OWN config set to `core.eol=native` always, plus a
regime-specific `core.autocrlf` baseline (see `_init_risky_repo` for why the
two regimes need different baselines — empirically, a single baseline does
not reproduce both flags' independent load-bearing-ness) so a bare `git
archive` — no `-c` overrides — really would corrupt, and asserts
`_extract_git_archive`'s output bytes equal the committed blob bytes in
both regimes. `test_git_archive_flags_are_independently_load_bearing`
separately proves each flag's removal flips the result, by driving `git
archive` directly with each flag combination and checking against the
docstring's own measured byte/CRLF-count claims.

Run: python -m pytest coordinator/bin/tests/test_publish_git_archive_eol_regimes.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Multi-line LF body — a single line with no trailing newline is too short
# for autocrlf/eol conversion to have anything to act on.
_LF_BODY = b"line one\nline two\nline three\n"


def _git(*args: str, cwd: Path, capture: bool = False):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
        text=not capture,
    )


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_git_archive_eol_regimes_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _init_risky_repo(root: Path, *, autocrlf: str) -> None:
    """A repo whose OWN config mimics an invoking-repo state the docstring
    warns about, `core.eol=native` (= CRLF on Windows) always, plus a
    caller-chosen `core.autocrlf` — the two regimes need DIFFERENT
    baselines to exercise the flag each one is sensitive to (empirically
    verified: with `core.autocrlf=true` at the repo, an attribute-free path
    is corrupted by autocrlf regardless of `core.eol`, so `eol=lf` alone
    cannot fix it and only `autocrlf=false` does; with `core.autocrlf=false`,
    a `text=auto` path is corrupted by `core.eol=native` alone, so
    `autocrlf=false` is already the baseline — redundant, hence inert — and
    only `eol=lf` fixes it). A bare `git archive` against either — no `-c`
    overrides — really does corrupt LF blobs; only `_extract_git_archive`'s
    pinned pair of `-c` flags brings it back to the committed bytes in
    both."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "publish-git-archive-eol-regimes-test@claude-klabauter.test", cwd=root)
    _git("config", "user.name", "Publish Git Archive Eol Regimes Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    _git("config", "core.autocrlf", autocrlf, cwd=root)
    _git("config", "core.eol", "native", cwd=root)


def _commit_lf_file(root: Path, rel_path: str, *, gitattributes: str | None = None) -> str:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_LF_BODY)
    if gitattributes is not None:
        (root / ".gitattributes").write_text(gitattributes, encoding="utf-8", newline="\n")
        _git("add", ".gitattributes", cwd=root)
    _git("add", rel_path, cwd=root)
    _git("commit", "-q", "-m", f"add {rel_path}", cwd=root)
    return _git("rev-parse", "HEAD", cwd=root).stdout.strip()


def _blob_bytes(root: Path, sha: str, rel_path: str) -> bytes:
    result = _git("cat-file", "blob", f"{sha}:{rel_path}", cwd=root, capture=True)
    return result.stdout


# ---------------------------------------------------------------------------
# 1. Attribute-free regime — `core.autocrlf=false` is the load-bearing flag.
# ---------------------------------------------------------------------------


def test_extract_git_archive_matches_blob_bytes_attribute_free_regime(tmp_path):
    root = tmp_path / "repo-attr-free"
    _init_risky_repo(root, autocrlf="true")
    rel_path = "vendored/LICENSE"
    sha = _commit_lf_file(root, rel_path)
    expected = _blob_bytes(root, sha, rel_path)
    assert b"\r\n" not in expected  # sanity: committed blob is genuinely LF

    shadow_dir = publish._extract_git_archive(root, sha)
    try:
        actual = (shadow_dir / rel_path).read_bytes()
        assert actual == expected
    finally:
        import shutil

        shutil.rmtree(shadow_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. Explicit `text=auto` regime — `core.eol=lf` is the load-bearing flag.
# ---------------------------------------------------------------------------


def test_extract_git_archive_matches_blob_bytes_text_auto_regime(tmp_path):
    root = tmp_path / "repo-text-auto"
    _init_risky_repo(root, autocrlf="false")
    rel_path = "dist/payload.txt"
    sha = _commit_lf_file(root, rel_path, gitattributes="dist/payload.txt text=auto\n")
    expected = _blob_bytes(root, sha, rel_path)
    assert b"\r\n" not in expected  # sanity: committed blob is genuinely LF

    shadow_dir = publish._extract_git_archive(root, sha)
    try:
        actual = (shadow_dir / rel_path).read_bytes()
        assert actual == expected
    finally:
        import shutil

        shutil.rmtree(shadow_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3. Empirical proof each flag is independently load-bearing, per the
# docstring's own measured claims: attribute-free path is fixed by
# `autocrlf=false` alone and untouched by `eol=lf` alone; text=auto path is
# fixed by `eol=lf` alone and untouched by `autocrlf=false` alone. Drives
# `git archive` directly (bypassing `_extract_git_archive`) with each flag
# combination so a future edit that silently drops one flag from the real
# function is caught by tests 1/2 above, while this test independently
# confirms the flags are not redundant with each other.
# ---------------------------------------------------------------------------


def _archive_member_bytes(root: Path, sha: str, rel_path: str, git_c_args: list[str]) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *git_c_args, "archive", "--format=tar", sha, "--", rel_path],
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
        member = archive.extractfile(rel_path)
        assert member is not None
        return member.read()


def test_git_archive_flags_are_independently_load_bearing(tmp_path):
    # --- attribute-free regime: autocrlf=false matters, eol=lf is inert ---
    root_a = tmp_path / "repo-a"
    _init_risky_repo(root_a, autocrlf="true")
    rel_a = "vendored/LICENSE"
    sha_a = _commit_lf_file(root_a, rel_a)
    expected_a = _blob_bytes(root_a, sha_a, rel_a)

    bare_a = _archive_member_bytes(root_a, sha_a, rel_a, [])
    assert b"\r\n" in bare_a  # corrupted without any -c override

    eol_only_a = _archive_member_bytes(root_a, sha_a, rel_a, ["-c", "core.eol=lf"])
    assert b"\r\n" in eol_only_a  # eol=lf alone does NOT fix this regime

    autocrlf_only_a = _archive_member_bytes(
        root_a, sha_a, rel_a, ["-c", "core.autocrlf=false"]
    )
    assert autocrlf_only_a == expected_a  # autocrlf=false alone DOES fix it

    both_a = _archive_member_bytes(
        root_a, sha_a, rel_a, ["-c", "core.autocrlf=false", "-c", "core.eol=lf"]
    )
    assert both_a == expected_a

    # --- text=auto regime: eol=lf matters, autocrlf=false is inert ---
    root_b = tmp_path / "repo-b"
    _init_risky_repo(root_b, autocrlf="false")
    rel_b = "dist/payload.txt"
    sha_b = _commit_lf_file(root_b, rel_b, gitattributes="dist/payload.txt text=auto\n")
    expected_b = _blob_bytes(root_b, sha_b, rel_b)

    bare_b = _archive_member_bytes(root_b, sha_b, rel_b, [])
    assert b"\r\n" in bare_b  # corrupted without any -c override

    autocrlf_only_b = _archive_member_bytes(
        root_b, sha_b, rel_b, ["-c", "core.autocrlf=false"]
    )
    assert b"\r\n" in autocrlf_only_b  # autocrlf=false alone does NOT fix this regime

    eol_only_b = _archive_member_bytes(root_b, sha_b, rel_b, ["-c", "core.eol=lf"])
    assert eol_only_b == expected_b  # eol=lf alone DOES fix it

    both_b = _archive_member_bytes(
        root_b, sha_b, rel_b, ["-c", "core.autocrlf=false", "-c", "core.eol=lf"]
    )
    assert both_b == expected_b
