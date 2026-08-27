"""The fixture corpus that carries THIS repo's real checkin surface, and
proves it discriminates -- the row every downstream green claim in the
`ceremony.commit` v2 plan depends on landing first.

WHY A DEDICATED FIXTURE CORPUS, NOT `tmp_path` PER TEST. This repo's real
checkin surface is three ingredients acting together: the `.gitattributes`
pins it ships (`*.cmd`/`*.ps1` -> `eol=crlf`, `*.sha`/`*.diff`/`*.patch`/
`*.sh` -> `eol=lf`, `**/_goldens/**` -> `-text`), `core.autocrlf=true`, and
`core.fileMode=false`. A suite that only ever tests plain LF content never
exercises the attribute-matching code path at all -- which is exactly how
an LF-only fixture corpus let a normalizer misclassify 81% of this box's
real files while a 68/68 suite stayed green (this plan's own anti-scope).
So every positive shape below runs against `checkin_repo_factory`, which
reproduces the real `.gitattributes`, and the corpus's power to DETECT a
misclassification is itself pinned by
`test_lf_only_fixture_fails_a_shape_the_real_corpus_passes` against the
deliberately attribute-blind `lf_only_repo_factory` twin.

WHY REAL GIT, NOT A SYNTHESISED ORACLE. Every assertion here is "does this
fixture's committed blob match what git itself would write for this path" --
a hand-rolled normalizer re-asserted against itself proves nothing about
real git's checkin conversion. `git/tests/test_git_state_against_real_git.py`
took the identical position for `git_state.py` and split onto its own file
for the identical reason: every test here spawns `git`, so the whole file
carries `pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]`
rather than a per-function mark that pytest's fixture-injection would leave
unreachable to the spawn ratchet's static call-walk in the fixture-factory
case (`test_no_new_spawning_tests.py`'s `test_ensure_meta.py` note).

THE THREE ASSERTIONS, NEVER COLLAPSED, per the chunk's own body: for every
shape, (1) the committed blob sha matches what `git hash-object` itself
computes for that path's checkin-normalized content, (2) `git status
--porcelain` is empty immediately after the commit, and (3) `git fsck
--strict` reports nothing. A suite that only checked (1) would pass on a
blob that FSCK could still flag as unreachable or malformed; one that only
checked (2) would pass on a committed-but-wrong blob so long as the index
and worktree agree with each other while both disagree with git.

NEGATIVE SPEC: this file never calls `coordinator_core.git.commit` or
`index_write` -- that is the v2 op's own surface (C3), gated on this row
existing first (`depends_on: C2, gate_kind: output-consumption-runtime`).
This file establishes ground truth about what real git does with each
shape; C3's own tests assert the in-process implementation against that
ground truth, not the other way round.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from coordinator_core.git.tests.conftest import REAL_GITATTRIBUTES  # noqa: E402
from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, *, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        **no_console_creationflags(),
    )


def _init_common(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@t.example"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)
    _git(["config", "core.autocrlf", "true"], cwd=repo)
    _git(["config", "core.fileMode", "false"], cwd=repo)


@pytest.fixture
def checkin_repo_factory(tmp_path: Path) -> Callable[[str], Path]:
    """`(name) -> repo_root` seeded with this repo's real `.gitattributes`
    pins, `core.autocrlf=true`, `core.fileMode=false`, and one committed
    seed (`.gitattributes` itself) so `HEAD` exists before a shape test
    commits its own file."""

    def _make(name: str = "repo") -> Path:
        repo = tmp_path / name
        _init_common(repo)
        (repo / ".gitattributes").write_bytes(REAL_GITATTRIBUTES.encode("utf-8"))
        _git(["add", "--", ".gitattributes"], cwd=repo)
        _git(["commit", "-q", "-m", "seed attributes"], cwd=repo)
        return repo

    return _make


@pytest.fixture
def lf_only_repo_factory(tmp_path: Path) -> Callable[[str], Path]:
    """`(name) -> repo_root` for the NEGATIVE fixture: identical
    `core.autocrlf`/`core.fileMode` config, but deliberately no
    `.gitattributes` at all -- an LF-only fixture is exactly the shape that
    let a normalizer misclassify 81% of this box's real files under a
    green 68/68 (this plan's anti-scope)."""

    def _make(name: str = "lf_only_repo") -> Path:
        repo = tmp_path / name
        _init_common(repo)
        return repo

    return _make


# ---------------------------------------------------------------------------
# Shared assertions -- THREE things, never collapsed.


def _assert_status_clean(repo: Path) -> None:
    out = _git(["status", "--porcelain"], cwd=repo).stdout
    assert out == "", f"git status --porcelain not empty:\n{out}"


def _assert_fsck_clean(repo: Path) -> None:
    result = _git(["fsck", "--strict"], cwd=repo, check=False)
    assert result.returncode == 0, f"git fsck --strict failed:\n{result.stdout}{result.stderr}"
    assert result.stdout.strip() == "", f"git fsck --strict reported:\n{result.stdout}"


def _committed_blob_sha(repo: Path, rel_path: str) -> str:
    return _git(["rev-parse", f"HEAD:{rel_path}"], cwd=repo).stdout.strip()


def _hash_object_sha(repo: Path, rel_path: str) -> str:
    """The blob sha real git computes for the WORKTREE file at `rel_path`,
    through its own checkin filters (`--path=` makes `git hash-object`
    apply attribute-driven conversion without requiring the path to sit at
    that name on disk), without writing anything to the object store."""
    return _git(
        ["hash-object", f"--path={rel_path}", "--", rel_path], cwd=repo
    ).stdout.strip()


def _write_worktree(repo: Path, rel_path: str, content: bytes) -> Path:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _commit_shape(repo: Path, rel_path: str, content: bytes, message: str) -> None:
    """Write, `hash-object` (BEFORE `add`, so the oracle reads the same
    worktree bytes the commit is about to convert), `add`, `commit` -- the
    sequence every shape test below shares."""
    _write_worktree(repo, rel_path, content)
    expected = _hash_object_sha(repo, rel_path)
    _git(["add", "--", rel_path], cwd=repo)
    _git(["commit", "-q", "-m", message], cwd=repo)
    actual = _committed_blob_sha(repo, rel_path)
    assert actual == expected, (
        f"{rel_path}: committed blob {actual} does not match "
        f"git hash-object's own checkin-normalized {expected}"
    )


LF_CONTENT = b"line one\nline two\nline three\n"
CRLF_CONTENT = b"line one\r\nline two\r\nline three\r\n"


# ---------------------------------------------------------------------------
# The six measured shapes, plus the mixed batch.


def test_shape_plain_lf(checkin_repo_factory):
    repo = checkin_repo_factory("plain_lf")
    _commit_shape(repo, "src/plain_lf.txt", LF_CONTENT, "plain LF")
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)


def test_shape_plain_crlf(checkin_repo_factory):
    repo = checkin_repo_factory("plain_crlf")
    _commit_shape(repo, "src/plain_crlf.txt", CRLF_CONTENT, "plain CRLF")
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)
    # The point of this shape: unpinned CRLF content is NORMALIZED to LF on
    # checkin under core.autocrlf=true, so the committed blob must differ
    # from a raw hash of the CRLF bytes as written.
    committed = _committed_blob_sha(repo, "src/plain_crlf.txt")
    raw_header = f"blob {len(CRLF_CONTENT)}\0".encode("ascii")
    import hashlib

    raw_sha = hashlib.sha1(raw_header + CRLF_CONTENT).hexdigest()
    assert committed != raw_sha, "plain CRLF content was not normalized on checkin"


def test_shape_text_eol_lf(checkin_repo_factory):
    repo = checkin_repo_factory("text_eol_lf")
    _commit_shape(repo, "scripts/launcher.sh", CRLF_CONTENT, "text eol=lf")
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)


def test_shape_binary_pin(checkin_repo_factory):
    repo = checkin_repo_factory("binary_pin")
    rel = "coordinator_core/x/_goldens/fixture.bin"
    _commit_shape(repo, rel, CRLF_CONTENT, "-text golden")
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)
    # The point of this shape: `-text` stores the RAW bytes, unconverted.
    committed = _committed_blob_sha(repo, rel)
    raw_header = f"blob {len(CRLF_CONTENT)}\0".encode("ascii")
    import hashlib

    raw_sha = hashlib.sha1(raw_header + CRLF_CONTENT).hexdigest()
    assert committed == raw_sha, "-text path was normalized when it must not be"


def test_shape_eol_crlf_pin(checkin_repo_factory):
    repo = checkin_repo_factory("eol_crlf_pin")
    _commit_shape(repo, "coordinator/bin/launcher.cmd", CRLF_CONTENT, "eol=crlf")
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)


def test_shape_deletion(checkin_repo_factory):
    repo = checkin_repo_factory("deletion")
    rel = "src/to_delete.txt"
    _commit_shape(repo, rel, LF_CONTENT, "add file to delete")
    _git(["rm", "-q", "--", rel], cwd=repo)
    _git(["commit", "-q", "-m", "delete"], cwd=repo)
    _assert_status_clean(repo)
    _assert_fsck_clean(repo)
    result = _git(["cat-file", "-e", f"HEAD:{rel}"], cwd=repo, check=False)
    assert result.returncode != 0, f"{rel} still present at HEAD after deletion"


def test_shape_mixed_batch_pinned_and_plain(checkin_repo_factory):
    """Two pinned paths and two plain paths, staged and committed together --
    the mixed-batch shape the chunk's own body names."""
    repo = checkin_repo_factory("mixed_batch")
    pinned_cmd = "coordinator/bin/mixed.cmd"
    pinned_sh = "scripts/mixed.sh"
    plain_a = "src/mixed_plain_a.txt"
    plain_b = "src/mixed_plain_b.txt"

    specs = {
        pinned_cmd: CRLF_CONTENT,
        pinned_sh: CRLF_CONTENT,
        plain_a: LF_CONTENT,
        plain_b: CRLF_CONTENT,
    }
    expected = {}
    for rel, content in specs.items():
        _write_worktree(repo, rel, content)
        expected[rel] = _hash_object_sha(repo, rel)

    _git(["add", "--", *specs.keys()], cwd=repo)
    _git(["commit", "-q", "-m", "mixed batch"], cwd=repo)

    for rel in specs:
        actual = _committed_blob_sha(repo, rel)
        assert actual == expected[rel], f"{rel}: mixed-batch commit diverged from git's own hash-object"

    _assert_status_clean(repo)
    _assert_fsck_clean(repo)


# ---------------------------------------------------------------------------
# The negative test -- the point of the row.


def test_lf_only_fixture_fails_a_shape_the_real_corpus_passes(lf_only_repo_factory):
    """A deliberately LF-only fixture (no `.gitattributes` at all) MUST
    misclassify the `-text`-shaped golden path that the real corpus (see
    `test_shape_binary_pin`) passes -- without `.gitattributes` pinning
    `**/_goldens/**` as binary, `core.autocrlf=true`'s default `text=auto`
    heuristic normalizes the CRLF content instead of storing it raw, which
    is exactly the misclassification this plan's anti-scope names (81% of
    this box's real files, under a green 68/68). If this test ever passes
    on an UNCHANGED assertion, the corpus has stopped being able to detect
    the thing it exists to detect."""
    repo = lf_only_repo_factory()
    rel = "coordinator_core/x/_goldens/fixture.bin"
    _write_worktree(repo, rel, CRLF_CONTENT)
    _git(["add", "--", rel], cwd=repo)
    _git(["commit", "-q", "-m", "lf-only golden"], cwd=repo)

    committed = _committed_blob_sha(repo, rel)
    raw_header = f"blob {len(CRLF_CONTENT)}\0".encode("ascii")
    import hashlib

    raw_sha = hashlib.sha1(raw_header + CRLF_CONTENT).hexdigest()

    # The real corpus (test_shape_binary_pin) asserts committed == raw_sha.
    # Under the LF-only fixture, with no -text pin, that equality FAILS --
    # the content was normalized instead of stored raw.
    assert committed != raw_sha, (
        "LF-only fixture unexpectedly preserved raw CRLF bytes for a "
        "goldens-shaped path -- the negative fixture stopped discriminating"
    )
