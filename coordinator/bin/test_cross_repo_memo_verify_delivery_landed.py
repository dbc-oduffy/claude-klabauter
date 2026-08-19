"""
test_cross_repo_memo_verify_delivery_landed.py — regression fixture for the
_verify_delivery_landed oracle's HEAD: revspec path-separator handling.

Spec backlink: coordinator/bin/cross-repo-memo.py `_verify_delivery_landed`
(the function containing "is not committed at HEAD in").

Purpose: on Windows, `os.path.relpath` returns backslash-separated paths, but
git's `HEAD:<path>` revspec requires forward slashes. Before the fix, the
oracle built `HEAD:cross-repo\\inbox\\<memo>.md`, which git cannot resolve --
a 100% false negative on every successful Windows send, whose own
remediation text ("Re-send with --supersedes <path>") would duplicate-deliver
the memo into the receiver's inbox. A single-path-component fixture (a
top-level committed file) cannot exercise this: relpath has no separator to
mangle in that case and the test would pass even with the bug present. This
fixture commits the file at a NESTED path (cross-repo/inbox/<name>.md,
matching the real delivery layout) so the regression is reachable.

Negative-spec: does not re-test the on-disk-but-uncommitted degraded path or
the missing-file path -- those are unaffected by the separator bug and are
already covered by the docstring's stated contract in cross-repo-memo.py.

`test_verify_delivery_landed_true_for_windows_shaped_relpath_on_any_host`
drives the oracle with a hardcoded backslash-separated relpath (monkeypatched
onto `os.path.relpath`) rather than relying on the HOST's `os.sep` to produce
one -- the production normalization is a hardcoded `"\\"` replace (not
`os.sep`-keyed) precisely so a git `HEAD:<path>` revspec, whose tree
namespace is always forward-slash-separated, gets fixed regardless of which
OS built the path string. That test fails against pre-fix code
(`.replace(os.sep, "/")`) on ANY host, POSIX included -- os.sep is "/" on
POSIX, so the pre-fix replace is a no-op against a hardcoded "\\"-laden
string there too. It complements, not replaces, the OS-native test below,
which still exercises the real end-to-end os.path.relpath output.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _bin_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _script_path() -> str:
    return os.path.join(_bin_dir(), "cross-repo-memo.py")


def _load_dispatcher_module():
    """Import the extensionless cross-repo-memo script as a module.

    Mirrors test_cross_repo_memo_roundtrip.py's _load_dispatcher_module --
    the script has no .py extension and is not directly executable on
    Windows, but importlib loads it fine by path.
    """
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cross_repo_memo_verify_fixture", _script_path())
    spec = importlib.util.spec_from_loader("cross_repo_memo_verify_fixture", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _git_init(path: str) -> None:
    # popup-safe-env-suppressed: test-harness fixture subprocess, not hot-path
    subprocess.run(["git", "init", path], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.email", "t@t.com"], capture_output=True, check=False)
    subprocess.run(["git", "-C", path, "config", "user.name", "T"], capture_output=True, check=False)


def test_verify_delivery_landed_true_for_committed_nested_path(tmp_path) -> None:
    """AC: a memo committed at HEAD under a nested path (cross-repo/inbox/...)
    is reported as verified, not as a false-negative "not committed at HEAD".

    Before the fix this failed on Windows: os.path.relpath produced
    "cross-repo\\inbox\\memo.md", `git cat-file -e HEAD:cross-repo\\inbox\\memo.md`
    exited 128 (git cannot resolve backslash-separated HEAD: revspecs), and the
    oracle returned False for a memo that WAS correctly committed.

    This exercises the real end-to-end os.path.relpath output on the host
    it runs on -- real protection on Windows, and a sanity check everywhere
    else that the happy path still works. The host-independent regression
    coverage lives in
    `test_verify_delivery_landed_true_for_windows_shaped_relpath_on_any_host`
    below, which fails against pre-fix code on any OS.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-14-fixture-memo.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")

    add = subprocess.run(
        ["git", "-C", str(receiver), "add", "--", "cross-repo/inbox/2026-08-14-fixture-memo.md"],
        capture_output=True, text=True, check=False,
    )
    assert add.returncode == 0, add.stderr
    commit = subprocess.run(
        ["git", "-C", str(receiver), "-c", "commit.gpgsign=false", "commit", "-m", "deliver fixture memo"],
        capture_output=True, text=True, check=False,
    )
    assert commit.returncode == 0, commit.stderr

    mod = _load_dispatcher_module()
    verified = mod._verify_delivery_landed(str(receiver), str(memo_path))
    assert verified is True


def test_verify_delivery_landed_true_for_windows_shaped_relpath_on_any_host(
    tmp_path, monkeypatch
) -> None:
    """AC: the oracle resolves a memo committed at a nested HEAD path even
    when os.path.relpath returns a Windows-shaped (backslash-separated)
    string -- on ANY host, not just Windows.

    Drives the regression directly by monkeypatching os.path.relpath to
    return a hardcoded backslash-laden string, rather than relying on the
    host's own os.sep to produce one (which only happens on Windows). This
    is sound because the production fix is a hardcoded "\\" -> "/" replace,
    not an os.sep-keyed one: a git HEAD:<path> revspec addresses a tree
    path, and tree paths are always forward-slash-separated regardless of
    host OS, so the normalization must run unconditionally. Fails against
    pre-fix code (`.replace(os.sep, "/")`) on every host, including POSIX,
    where os.sep is already "/" and the pre-fix replace is a no-op against
    this hardcoded backslash string too.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-14-fixture-memo-winshaped.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")

    add = subprocess.run(
        ["git", "-C", str(receiver), "add", "--", "cross-repo/inbox/2026-08-14-fixture-memo-winshaped.md"],
        capture_output=True, text=True, check=False,
    )
    assert add.returncode == 0, add.stderr
    commit = subprocess.run(
        ["git", "-C", str(receiver), "-c", "commit.gpgsign=false", "commit", "-m", "deliver fixture memo"],
        capture_output=True, text=True, check=False,
    )
    assert commit.returncode == 0, commit.stderr

    mod = _load_dispatcher_module()
    windows_shaped_relpath = "cross-repo\\inbox\\2026-08-14-fixture-memo-winshaped.md"
    monkeypatch.setattr(
        mod.os.path, "relpath", lambda *_a, **_kw: windows_shaped_relpath
    )

    verified = mod._verify_delivery_landed(str(receiver), str(memo_path))
    assert verified is True


def test_verify_delivery_landed_false_for_uncommitted_nested_path(tmp_path) -> None:
    """Sanity/negative-spec: an on-disk-but-uncommitted nested file must still
    read as NOT verified -- the fix normalizes separators, it does not change
    the oracle's degraded-verdict semantics for a genuinely uncommitted memo.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))
    subprocess.run(
        ["git", "-C", str(receiver), "commit", "--allow-empty", "-m", "seed"],
        capture_output=True, check=False,
    )

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-14-fixture-memo-uncommitted.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")
    # deliberately not `git add`ed/committed

    mod = _load_dispatcher_module()
    verified = mod._verify_delivery_landed(str(receiver), str(memo_path))
    assert verified is False


def test_verify_delivery_landed_true_via_expected_sha_when_head_read_races(
    tmp_path, monkeypatch
) -> None:
    """AC: a real, provably-landed commit is verified via `expected_sha`
    (the engine's own `CommitOutcome.committed_sha`) even when the HEAD-based
    oracle would transiently miss it — the false-warning-on-proven-delivery
    defect (2026-08-15, team-lead dispatch): a concurrent sibling session's
    own commit interleaving in the read window can make a bare `HEAD:<path>`
    read a false negative for a commit that unquestionably landed. The
    `<sha>:<path>` check is anchored to a fixed, immutable commit object, so
    it must still report verified even when the HEAD path is made to always
    fail.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-15-fixture-memo-sha-anchored.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")

    rel = "cross-repo/inbox/2026-08-15-fixture-memo-sha-anchored.md"
    add = subprocess.run(
        ["git", "-C", str(receiver), "add", "--", rel],
        capture_output=True, text=True, check=False,
    )
    assert add.returncode == 0, add.stderr
    commit = subprocess.run(
        ["git", "-C", str(receiver), "-c", "commit.gpgsign=false", "commit", "-m", "deliver fixture memo"],
        capture_output=True, text=True, check=False,
    )
    assert commit.returncode == 0, commit.stderr
    sha = subprocess.run(
        ["git", "-C", str(receiver), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert sha

    mod = _load_dispatcher_module()

    # Force the HEAD-based fallback to always fail (simulates the race) --
    # if expected_sha's fast path weren't checked first, this would report
    # False.
    real_run = mod.subprocess.run

    def _run_forcing_head_miss(argv, *a, **kw):
        if "cat-file" in argv and any(str(x).startswith("HEAD:") for x in argv):
            result = real_run(["git", "rev-parse", "--verify", "does-not-exist"], *a, **kw)
            return result
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", _run_forcing_head_miss)

    verified = mod._verify_delivery_landed(str(receiver), str(memo_path), expected_sha=sha)
    assert verified is True


def test_verify_delivery_landed_retries_before_reporting_uncommitted(
    tmp_path, monkeypatch
) -> None:
    """AC: the HEAD-based fallback (no expected_sha) settles across a bounded
    number of retries before printing the duplicate-delivery remediation --
    it must not fail loud on a single transient miss, but a GENUINELY
    uncommitted delivery (every attempt misses) must still be caught.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-15-fixture-memo-settle.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")
    rel = "cross-repo/inbox/2026-08-15-fixture-memo-settle.md"
    subprocess.run(["git", "-C", str(receiver), "add", "--", rel], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", str(receiver), "-c", "commit.gpgsign=false", "commit", "-m", "deliver fixture memo"],
        capture_output=True, check=False,
    )

    mod = _load_dispatcher_module()
    monkeypatch.setattr(mod, "_VERIFY_HEAD_RETRY_DELAY_SECONDS", 0.01)

    real_run = mod.subprocess.run
    calls = {"n": 0}

    def _run_flaky_then_ok(argv, *a, **kw):
        if "cat-file" in argv and any(str(x).startswith("HEAD:") for x in argv):
            calls["n"] += 1
            if calls["n"] < 2:
                return real_run(["git", "rev-parse", "--verify", "does-not-exist"], *a, **kw)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", _run_flaky_then_ok)

    verified = mod._verify_delivery_landed(str(receiver), str(memo_path))
    assert verified is True
    assert calls["n"] >= 2  # confirms the retry path actually fired, not a first-try pass


def test_verify_delivery_landed_false_when_expected_sha_never_settles(tmp_path) -> None:
    """Negative-spec: a genuinely uncommitted delivery, with no expected_sha
    given, still reports False (and prints the --supersedes remediation)
    after exhausting the bounded retry budget -- the retry/settle fix must
    not weaken the oracle into never warning.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))
    subprocess.run(
        ["git", "-C", str(receiver), "commit", "--allow-empty", "-m", "seed"],
        capture_output=True, check=False,
    )

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-15-fixture-memo-never-settles.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")
    # deliberately not `git add`ed/committed

    mod = _load_dispatcher_module()
    verified = mod._verify_delivery_landed(str(receiver), str(memo_path))
    assert verified is False


def test_verify_delivery_landed_false_when_expected_sha_is_real_but_mismatched(
    tmp_path,
) -> None:
    """P3 (2026-08-15, coordinator:code-reviewer): `expected_sha` given and a
    REAL commit (e.g. a stale sha from a superseded send, or another repo's
    sha) but one that does not contain `memo_relpath` -- the sha fast path's
    `cat-file -e <sha>:<path>` must fail and the check must fall through to
    the HEAD-based oracle rather than trusting the sha blindly, and that
    oracle must still catch a genuinely-uncommitted delivery. Closes the gap
    the docstring's "a real mismatch, not a guess to paper over" claim had
    no direct test for.
    """
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    _git_init(str(receiver))
    # A real commit exists at HEAD, but it never touched the memo path --
    # this is the "real commit, doesn't contain memo_relpath" mismatch shape.
    stale_sha = subprocess.run(
        ["git", "-C", str(receiver), "commit", "--allow-empty", "-m", "unrelated stale commit"],
        capture_output=True, text=True, check=False,
    )
    assert stale_sha.returncode == 0, stale_sha.stderr
    sha = subprocess.run(
        ["git", "-C", str(receiver), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    assert sha

    inbox_dir = receiver / "cross-repo" / "inbox"
    inbox_dir.mkdir(parents=True)
    memo_path = inbox_dir / "2026-08-15-fixture-memo-stale-sha.md"
    memo_path.write_text("---\ntitle: fixture\n---\n\nbody\n", encoding="utf-8")
    # deliberately not `git add`ed/committed -- genuinely uncommitted

    mod = _load_dispatcher_module()
    verified = mod._verify_delivery_landed(str(receiver), str(memo_path), expected_sha=sha)
    assert verified is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
