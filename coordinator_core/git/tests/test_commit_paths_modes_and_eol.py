"""C4 -- the two correctness gaps `ceremony.commit` v2's zero-spawn parts
still carried: the exec bit, and the `eol=crlf` fallback.

EXEC BIT: `stage_paths_in_process` derived every mode from `_mode_for`'s own
stat, unconditionally -- on `core.fileMode=false` (this box's own config,
because Windows reports the exec bit set on every file) `_mode_for` always
answers `100644`, so re-staging an already-executable tracked file silently
dropped its bit. The fix reads the INDEX's existing entry for a tracked path
first, exactly as `commit_paths` already did -- these tests exercise it
without depending on a real POSIX exec bit, so they discriminate identically
on every OS this box runs on.

EOL FALLBACK: an attribute-pinned path (`text`/`eol=`/`-text`) was refused
UNCONDITIONALLY before this chunk, even when nothing needed converting
(`-text` always, or a `text`/`eol=` pin over CR-free content) -- that put the
common, zero-cost case on the same refusal path as the one real gap: CR
bytes under a `text`/`eol=` pin, which is refused to the batched
`blob_fallback` because this module's normalizer is not proven against real
git for that shape.

NEGATIVE SPEC: no suppression flag, no widening to "any attributed path" --
`-text` and CR-free `text`/`eol=` content must stay off the fallback path
(cost zero, asserted by an empty `blob_fallback`-call list), and CR-bearing
`text`/`eol=` content must still refuse loudly with no `blob_fallback`
supplied.
"""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=check, **_NOWIN
    )


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "work/z")
    _git(repo, "config", "user.email", "t@local")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8", newline="\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo


def _ls_files_mode(repo, path):
    out = _git(repo, "ls-files", "-s", "--", path).stdout.strip()
    assert out, f"{path} not found in index: {out!r}"
    return out.split()[0]


# ---------------------------------------------------------------------------
# EXEC BIT -- stage_paths_in_process must read the index's existing mode for
# a tracked path, never re-derive it from a stat.


def test_stage_paths_in_process_preserves_existing_exec_mode(tmp_path):
    repo = _repo(tmp_path)
    (repo / "run.sh").write_bytes(b"#!/bin/sh\necho one\n")
    blob = _git(repo, "hash-object", "-w", "--", "run.sh").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100755,{blob},run.sh")
    _git(repo, "commit", "-q", "-m", "seed exec")
    assert _ls_files_mode(repo, "run.sh") == "100755"

    # Edit the worktree content and re-stage -- the mode must survive.
    (repo / "run.sh").write_bytes(b"#!/bin/sh\necho two\n")
    staged = gcommit.stage_paths_in_process(repo, ["run.sh"])

    assert staged == ("run.sh",)
    assert _ls_files_mode(repo, "run.sh") == "100755", (
        "re-staging a tracked executable through stage_paths_in_process must "
        "not drop its mode back to 100644"
    )


def test_stage_paths_in_process_new_file_still_defaults_to_100644(tmp_path):
    """A genuinely new (untracked) path has no index entry to read, so the
    stat-based `_mode_for` default still applies -- the fix narrows to
    TRACKED paths, it does not remove the fallback for new ones."""
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("new\n", encoding="utf-8", newline="\n")

    staged = gcommit.stage_paths_in_process(repo, ["new.txt"])

    assert staged == ("new.txt",)
    assert _ls_files_mode(repo, "new.txt") == "100644"


def test_stage_paths_in_process_preserves_exec_mode_via_fallback_leg(tmp_path):
    """The same invariant on the `blob_fallback` resolution leg (a CR-bearing
    attribute-pinned path routed through the fallback) -- mode still comes
    from the index, not `_mode_for`."""
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.cmd text eol=crlf\n", encoding="utf-8", newline="\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")

    (repo / "run.cmd").write_bytes(b"echo one\r\n")
    blob = _git(repo, "hash-object", "-w", "--path=run.cmd", "--", "run.cmd").stdout.strip()
    _git(repo, "update-index", "--add", "--cacheinfo", f"100755,{blob},run.cmd")
    _git(repo, "commit", "-q", "-m", "seed exec cmd")
    assert _ls_files_mode(repo, "run.cmd") == "100755"

    (repo / "run.cmd").write_bytes(b"echo two\r\n")

    def fallback(paths):
        out = {}
        for rel in paths:
            out[rel] = _git(
                repo, "hash-object", "-w", f"--path={rel}", "--", rel
            ).stdout.strip()
        return out

    staged = gcommit.stage_paths_in_process(repo, ["run.cmd"], blob_fallback=fallback)

    assert staged == ("run.cmd",)
    assert _ls_files_mode(repo, "run.cmd") == "100755"


# ---------------------------------------------------------------------------
# EOL FALLBACK -- only CR bytes under a text/eol attribute pin reach the
# fallback; `-text` and CR-free text/eol content cost zero.


def _attrs_repo(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text(
        "*.cmd text eol=crlf\n*.sh text eol=lf\n_goldens/** -text\n",
        encoding="utf-8", newline="\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "attrs")
    return repo


def test_cr_bearing_eol_crlf_pin_refuses_without_fallback(tmp_path):
    repo = _attrs_repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"echo one\r\necho two\r\n")
    with pytest.raises(gcommit.FilterUnsupported, match="run.cmd"):
        gcommit.commit_paths(repo, ["run.cmd"], "no fallback")


def test_cr_bearing_eol_crlf_pin_routes_through_fallback(tmp_path):
    repo = _attrs_repo(tmp_path)
    (repo / "run.cmd").write_bytes(b"echo one\r\necho two\r\n")

    calls = []

    def fallback(paths):
        calls.append(list(paths))
        out = {}
        for rel in paths:
            out[rel] = _git(
                repo, "hash-object", "-w", f"--path={rel}", "--", rel
            ).stdout.strip()
        return out

    gcommit.commit_paths(repo, ["run.cmd"], "via fallback", blob_fallback=fallback)

    assert calls == [["run.cmd"]]
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    expected_blob = _git(
        repo, "hash-object", "--path=run.cmd", "--", "run.cmd"
    ).stdout.strip()
    assert _git(repo, "rev-parse", "HEAD:run.cmd").stdout.strip() == expected_blob


def test_text_eol_lf_cr_free_content_never_calls_fallback(tmp_path):
    """`*.sh text eol=lf` with LF-only content: nothing to normalize, so it
    must cost zero -- the fallback must never be called for it."""
    repo = _attrs_repo(tmp_path)
    (repo / "run.sh").write_text("echo one\necho two\n", encoding="utf-8", newline="\n")

    calls = []
    gcommit.commit_paths(
        repo, ["run.sh"], "cr free text",
        blob_fallback=lambda paths: calls.append(list(paths)) or {},
    )

    assert calls == [], f"CR-free text/eol content must not reach the fallback: {calls}"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""


def test_binary_pin_never_calls_fallback_even_with_cr_bytes(tmp_path):
    """`-text` never converts, so CR bytes are irrelevant to it -- always
    zero cost, raw bytes committed verbatim."""
    repo = _attrs_repo(tmp_path)
    (repo / "_goldens").mkdir()
    (repo / "_goldens" / "fixture.bin").write_bytes(b"line one\r\nline two\r\n")

    calls = []
    gcommit.commit_paths(
        repo, ["_goldens/fixture.bin"], "binary golden",
        blob_fallback=lambda paths: calls.append(list(paths)) or {},
    )

    assert calls == [], f"-text content must not reach the fallback: {calls}"
    committed = _git(repo, "rev-parse", "HEAD:_goldens/fixture.bin").stdout.strip()
    expected = _git(
        repo, "hash-object", "--path=_goldens/fixture.bin", "--", "_goldens/fixture.bin"
    ).stdout.strip()
    assert committed == expected


# ---------------------------------------------------------------------------
# UNSET-ATTRIBUTE `core.autocrlf` SURFACE -- `767079e6e` deleted ~10 lines of
# code that had been unconditionally shadowed by an earlier, unconditional
# `return write_object(...)` in `_worktree_blob`'s pre-image. Both call sites
# of `_autocrlf_checkin_normalize` sat beneath that dead return, so neither
# had ever executed in production: every CR-bearing, unattributed path was
# refused to the spawning fallback regardless of `core.autocrlf`. Removing
# the shadow made the `_repo_autocrlf_true` branch live, and
# `core.autocrlf=true` -- the majority Windows shape on this box -- is
# exactly the path whose behaviour flipped from REFUSE to
# NORMALIZE-IN-PROCESS. Nothing before this group asserted that flip, or
# proved the normalizer's output against real git for the shapes most likely
# to defeat a hand-rolled CRLF->LF pass: a lone CR that is not part of a line
# ending, a CR sitting at EOF with nothing after it, and a CRLF pair living
# inside otherwise-binary (NUL-bearing) content.
#
# The oracle is `git hash-object -w --path <p> -- <p>` itself -- the exact
# invocation `git add` uses to decide a blob's checkin-converted sha. An
# expected-sha constant would only re-encode this module's own assumption
# about what git does; asking real git is the only differential check that
# can catch this module disagreeing with it.


def _autocrlf_repo(tmp_path, value):
    repo = _repo(tmp_path)
    if value is None:
        _git(repo, "config", "--unset", "core.autocrlf", check=False)
    else:
        _git(repo, "config", "core.autocrlf", value)
    return repo


def _oracle_blob(repo, rel):
    return _git(repo, "hash-object", "-w", f"--path={rel}", "--", rel).stdout.strip()


@pytest.mark.parametrize(
    "name,content",
    [
        ("crlf_lines", b"line one\r\nline two\r\n"),
        ("lone_cr_not_eol", b"col1\rcol2\r\ncol3\n"),
        ("cr_at_eof", b"line one\nline two\r"),
        ("mixed_crlf_and_lf", b"crlf\r\nlf\ncrlf again\r\n"),
        ("crlf_inside_nul_content", b"bin\x00head\r\nmore\x00\r\ntail\r\n"),
    ],
)
def test_unattributed_cr_content_autocrlf_true_matches_oracle(tmp_path, name, content):
    """No `.gitattributes` pin applies to `<name>.dat` -- disposition is
    UNSET, so `core.autocrlf` alone decides, and with it `true` this is the
    branch `767079e6e` made reachable for the first time. Every shape here
    must land the SAME blob sha `git hash-object -w --path=<p>` would
    produce, not merely "no exception"."""
    repo = _autocrlf_repo(tmp_path, "true")
    rel = f"{name}.dat"
    (repo / rel).write_bytes(content)

    def fallback(paths):
        raise AssertionError(f"unattributed autocrlf=true content must not reach fallback: {paths}")

    gcommit.commit_paths(repo, [rel], "autocrlf true", blob_fallback=fallback)

    expected = _oracle_blob(repo, rel)
    committed = _git(repo, "rev-parse", f"HEAD:{rel}").stdout.strip()
    assert committed == expected


@pytest.mark.parametrize("value", ["input", "false"])
def test_unattributed_cr_content_autocrlf_non_true_refuses_without_fallback(tmp_path, value):
    """`_repo_autocrlf_true` answers False for both `input` and `false` (and
    for unset -- see below), so this module refuses rather than guesses,
    even though `input`'s checkin conversion is byte-identical to `true`'s
    (see the module-level TODO note below) -- this test locks the CURRENT
    refusal, not the theoretically-reachable optimization."""
    repo = _autocrlf_repo(tmp_path, value)
    rel = "crlf.dat"
    (repo / rel).write_bytes(b"line one\r\nline two\r\n")

    with pytest.raises(gcommit.FilterUnsupported, match=rel):
        gcommit.commit_paths(repo, [rel], "no fallback")


def test_unattributed_cr_content_autocrlf_unset_refuses_without_fallback(tmp_path, monkeypatch):
    """No `core.autocrlf` set at all -- `_repo_autocrlf_true` must answer
    False, and the CR-bearing unattributed path refuses exactly as the
    `false` case does.

    `GIT_CONFIG_NOSYSTEM` is set for the duration of this test: this box's
    own Git-for-Windows install carries `core.autocrlf=true` at the SYSTEM
    layer (`_system_gitconfig_paths`'s own reason for existing), so a repo
    with no local/global override would otherwise resolve `true` through
    that layer -- exercising the `true` case again rather than the
    genuinely-unset one this test targets. `_repo_autocrlf_true` documents
    honouring this variable itself, so setting it is exercising the
    function's own declared config surface, not bypassing it.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = _autocrlf_repo(tmp_path, None)
    rel = "crlf.dat"
    (repo / rel).write_bytes(b"line one\r\nline two\r\n")

    with pytest.raises(gcommit.FilterUnsupported, match=rel):
        gcommit.commit_paths(repo, [rel], "no fallback")


# TODO (do not implement here): `core.autocrlf=input`'s CHECKIN conversion
# is byte-identical to `true`'s -- the peer's corpus measured both producing
# the same blob sha (`814f4a422927...`) over the same content set. The two
# settings differ only on CHECKOUT (whether the working tree gets CRLF back),
# which `_worktree_blob` never performs. That means the `input` refusal above
# pays a `blob_fallback` spawn for a conversion this module could already
# compute for free by routing `input` through the same
# `_autocrlf_checkin_normalize` branch as `true`. Left unimplemented here:
# this dispatch is scoped to test coverage only, and `commit.py` is
# explicitly out of scope for this change.


# ---------------------------------------------------------------------------
# POST-REF SPLICE FAILURE -- the commit LANDED and must not be retried.
#
# `commit_paths` splices the index AFTER the ref swap by design (invariant 3:
# "an index that matches a commit which never landed is the same lie in the
# other direction"). A peer holding `.git/index.lock` for the width of that
# splice therefore lands with real work in history -- routine at the
# ~50-session load norm, not exotic.
#
# It escaped as a bare `IndexWriteLockBusy`, whose own docstring promises the
# OPPOSITE of what is true at that line: "raised BEFORE any bytes reach
# `.git/index` and before the ref moves, so retrying is correct there". Every
# `commit_paths` caller in the tree catches `(CommitRefused, FilterUnsupported)`
# and nothing else, so it surfaced as an internal error for a landed commit --
# and the honest response to an internal error is a retry, which commits the
# same work twice.
#
# `IndexStaleAfterCommit` was written for this outcome and had no raise site.
# Source: cross-repo/inbox/2026-09-01-example-retrieval-repo-em-ceremony-engine-defects-
# second-repo-confirmation.md (the memo.send face of it).


def test_lock_held_during_splice_raises_stale_not_lock_busy(tmp_path):
    from coordinator_core.git.index_write import (
        IndexStaleAfterCommit,
        IndexWriteLockBusy,
    )

    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Stand in for a peer mid-write. The lock is taken before the call, so it
    # is held across the post-ref splice.
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    with pytest.raises(IndexStaleAfterCommit) as caught:
        gcommit.commit_paths(repo, ["seed.txt"], "second\n")

    exc = caught.value
    assert not isinstance(exc, IndexWriteLockBusy), (
        "the type must say the commit LANDED -- IndexWriteLockBusy's docstring "
        "promises the ref had not moved, which is false past the cas_ref"
    )

    head_after = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head_after != head_before, "the commit really did land"
    assert exc.outcome is not None, (
        "the caller needs the sha it just created -- naming the outcome without "
        "carrying it still strands them"
    )
    assert exc.outcome.sha == head_after


def test_stale_after_commit_message_forbids_the_retry(tmp_path):
    """The message is the whole remedy here: a caller that retries commits the
    same work twice, which is the one thing this outcome must prevent."""
    from coordinator_core.git.index_write import IndexStaleAfterCommit

    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")
    (repo / ".git" / "index.lock").write_text("", encoding="utf-8")

    with pytest.raises(IndexStaleAfterCommit) as caught:
        gcommit.commit_paths(repo, ["seed.txt"], "second\n")

    text = str(caught.value)
    assert "LANDED" in text
    assert "Do not retry" in text


def test_ordinary_commit_still_splices_and_raises_nothing(tmp_path):
    """The unchanged path: no lock, so the splice lands and the index is
    current. Asserted so the wrap above cannot quietly swallow the success."""
    repo = _repo(tmp_path)
    (repo / "seed.txt").write_text("changed\n", encoding="utf-8", newline="\n")

    outcome = gcommit.commit_paths(repo, ["seed.txt"], "second\n")

    assert outcome.sha == _git(repo, "rev-parse", "HEAD").stdout.strip()
    # A current index reports nothing staged and nothing dirty for this path.
    assert _git(repo, "status", "--porcelain", "--", "seed.txt").stdout.strip() == ""
