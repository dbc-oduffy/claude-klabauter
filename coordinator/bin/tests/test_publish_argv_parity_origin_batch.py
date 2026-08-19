"""Direct multi-item coverage for `_argv_parity_pairing_origin_batch`,
`_porcelain_touched_paths`, and `_git_head` (`coordinator/bin/publish.py`),
added per review finding amp-s1 #5.

`_argv_parity_pairing_origin_batch`/`_porcelain_touched_paths` are only
meaningfully exercised with >=2 `rel_modules` sharing one batched `git
status --porcelain -z` spawn -- a single-path test is indistinguishable
from the pre-batch `_argv_parity_pairing_origin` it replaced and would pass
identically before and after batching landed (finding #5's named
non-test). The tests below instead cover: a 3-item partial-miss batch
spanning all three origin verdicts in one spawn, a rename record (only a
`-z` multi-field parse produces two attributed paths from one record),
special-character path attribution (review finding #4: default
`core.quotePath=true` text porcelain would C-quote this and miss an
exact-string match -- `-z` never quotes, which is why the fix moved to
`-z`), and the amp-s1 #3 fix itself: a failed/errored batch spawn must fall
back to PER-ITEM resolution rather than blinding the whole batch to
"unknown-origin".

`_git_head` gets its own pin for review finding #6: a corrupt (non-ref,
non-sha) `HEAD` file must fail closed to `""`, not return the garbage text
verbatim.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_argv_parity_origin_batch_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: bytes) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


# ---------------------------------------------------------------------------
# _porcelain_touched_paths -- NUL-delimited `-z` parsing
# ---------------------------------------------------------------------------


def test_porcelain_touched_paths_parses_ordinary_and_rename_records():
    unicode_path = "café/módule.py"
    stream = (
        b" M plain/touched.py\0"
        + ("R  new/" + unicode_path).encode("utf-8") + b"\0"
        + ("old/" + unicode_path).encode("utf-8") + b"\0"
    )

    touched = publish._porcelain_touched_paths(stream)

    assert touched == {
        "plain/touched.py",
        "new/café/módule.py",
        "old/café/módule.py",
    }


def test_porcelain_touched_paths_empty_stream():
    assert publish._porcelain_touched_paths(b"") == set()


# ---------------------------------------------------------------------------
# _argv_parity_pairing_origin_batch
# ---------------------------------------------------------------------------


def test_batch_resolves_all_three_origins_from_one_spawn(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    touched = repo_root / "mod" / "touched.py"
    touched.parent.mkdir(parents=True)
    touched.write_text("x", encoding="utf-8")
    published = repo_root / "mod" / "published_clean.py"
    published.write_text("y", encoding="utf-8")
    # "mod/missing.py" deliberately does not exist on disk.

    stdout = b" M mod/touched.py\0"

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _FakeCompleted(0, stdout)

    monkeypatch.setattr(publish.subprocess, "run", _fake_run)

    origins = publish._argv_parity_pairing_origin_batch(
        repo_root, ["mod/touched.py", "mod/published_clean.py", "mod/missing.py"],
    )

    assert len(calls) == 1, "must be exactly one spawn for the whole batch"
    assert origins == {
        "mod/touched.py": "destination-only (locally modified in the mirror working tree)",
        "mod/published_clean.py": "published-by-this-round",
        "mod/missing.py": "unknown-origin",
    }


def test_special_character_path_is_attributed_via_z_not_missed(tmp_path, monkeypatch):
    """Review finding #4: default text porcelain output would C-quote a
    path like this under `core.quotePath=true`, and an exact-string match
    against the plain rel_module would silently miss it (falling to
    unknown-origin). `-z` never quotes, so the batch must attribute it
    correctly."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    rel = "mod/nön-ascii ⚡ file.py"
    stdout = (" M " + rel).encode("utf-8") + b"\0"

    monkeypatch.setattr(
        publish.subprocess, "run", lambda argv, **kwargs: _FakeCompleted(0, stdout),
    )

    origins = publish._argv_parity_pairing_origin_batch(repo_root, [rel])

    assert origins[rel] == "destination-only (locally modified in the mirror working tree)"


def test_batch_spawn_failure_falls_back_to_per_item_isolation(tmp_path, monkeypatch):
    """Regression pin for review finding amp-s1 #3: a failed batch spawn
    must not blind every rel_module to unknown-origin when a per-item call
    would still resolve some of them. Before the fix, a non-zero batch
    returncode mapped the WHOLE set to unknown-origin unconditionally --
    this test fails against that shape because it asserts one entry
    resolves as published-by-this-round."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    resolvable = repo_root / "mod" / "resolvable.py"
    resolvable.parent.mkdir(parents=True)
    resolvable.write_text("x", encoding="utf-8")
    # "mod/unresolvable.py" does not exist and every per-item spawn for it
    # is also made to fail below, so it stays unknown-origin honestly.

    call_n = {"i": 0}

    def _fake_run(argv, **kwargs):
        call_n["i"] += 1
        if call_n["i"] == 1:
            # The batched call (contains both rel_modules as trailing argv).
            return _FakeCompleted(1, b"")
        # Per-item fallback calls: one rel_module per spawn.
        rel_module = argv[-1]
        if rel_module == "mod/resolvable.py":
            return _FakeCompleted(0, "")
        return _FakeCompleted(1, "")

    monkeypatch.setattr(publish.subprocess, "run", _fake_run)

    origins = publish._argv_parity_pairing_origin_batch(
        repo_root, ["mod/resolvable.py", "mod/unresolvable.py"],
    )

    assert call_n["i"] == 3, "one batch spawn plus one per-item fallback spawn each"
    assert origins["mod/resolvable.py"] == "published-by-this-round"
    assert origins["mod/unresolvable.py"] == "unknown-origin"


def test_batch_empty_input_short_circuits_without_spawning(monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("must not spawn for an empty rel_modules list")

    monkeypatch.setattr(publish.subprocess, "run", _fail)
    assert publish._argv_parity_pairing_origin_batch(Path("."), []) == {}


# ---------------------------------------------------------------------------
# _git_head -- corrupt HEAD fails closed (review finding #6)
# ---------------------------------------------------------------------------


def _init_bare_git_dir(root: Path) -> Path:
    gitdir = root / ".git"
    (gitdir / "refs" / "heads").mkdir(parents=True)
    return gitdir


def test_git_head_corrupt_detached_text_fails_closed(tmp_path):
    root = tmp_path / "repo"
    gitdir = _init_bare_git_dir(root)
    (gitdir / "HEAD").write_text("not-a-ref-and-not-a-sha\n", encoding="utf-8")

    assert publish._git_head(root) == ""


def test_git_head_valid_detached_sha_is_returned(tmp_path):
    root = tmp_path / "repo"
    gitdir = _init_bare_git_dir(root)
    sha = "a" * 40
    (gitdir / "HEAD").write_text(sha + "\n", encoding="utf-8")

    assert publish._git_head(root) == sha
