"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_chain_ancestry_waiver_bookkeeping

AC12/C7b (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md):
`_stage_chain_ancestry_waivers` (coverage_gate.py, landed C7) only `git add`s a
just-minted chain-ancestry waiver directory on the invocation that HALTs -- that
invocation aborts the ceremony before `wsc_tail`'s own bookkeeping commit ever
runs, so the waiver files sit STAGED, never committed, at that point. This suite
covers the remainder: `wsc_tail._pending_chain_ancestry_waiver_stage_path` names
that same directory on a LATER invocation (the one where the gate finally passes,
after the EM has reviewed) so it rides into `_run_precommit_tail`'s
`extra_stage_paths` -> `full_stage_paths` -> `run_commit_pipeline`'s explicit
commit pathspec.

Two things are pinned, deliberately kept separate:

  1. `_pending_chain_ancestry_waiver_stage_path` itself -- pure path-resolution
     logic, no git involved.
  2. The underlying git fact this whole fix leans on -- a scoped
     `git commit -- <pathspec>` commits ONLY paths named in that pathspec, so a
     path that is merely `git add`-ed (staged) on an earlier, aborted pass does
     NOT ride into a later commit unless it is named again. This is proven with
     real git (not mocked -- a mock has no index, so "staged but not committed"
     is not a distinction a mock can even represent), using a small local git
     helper rather than importing `fixtures/real_git.py` (this suite is not on
     that module's reviewed-crossing allowlist and has no need to be -- the
     setup here is simpler than the divergence fixtures that module exists for).

`_run_precommit_tail` itself is NOT driven end-to-end here: doing so would
require mocking every other pre-commit sub-step (render-handoff-tracker,
refresh-roadmap-callout, coverage.gate, review_trail.write) plus the AC18
sentinel/locking machinery in `_handler`, none of which this fix touches --
excessive scaffolding for a fact already provable at the two boundaries above.
The call site itself (`wsc_tail.py`, `_run_precommit_tail`'s
"AC12/C7b: fold in this session's chain-ancestry waiver subdirectory" block)
is a three-line, directly-readable append of test 1's return value into the
same `extra_stage_paths` list the two adjacent, already-tested renders append
to -- see that function's own tests file for the sibling coverage this mirrors.

Spec backlink: pln-teach-the-review-trail-foreign-fa3c96
§ C7 / AC12.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from coordinator_core import chain_ancestry_waivers
from coordinator_core.ops.ceremony.git_native import add_paths, commit_scoped

_SID = "22222222-2222-2222-2222-222222222222"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _seeded_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "wsc-tail-waiver-test@example.invalid"], root)
    _git(["config", "user.name", "wsc-tail-waiver-test"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _committed_at_head(root: Path, rel: str) -> bool:
    """True iff `rel` is present in the HEAD tree -- i.e. actually committed,
    not merely staged in the index."""
    result = subprocess.run(
        ["git", "show", f"HEAD:{rel}"],
        cwd=str(root),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# 1. _pending_chain_ancestry_waiver_stage_path -- pure path resolution.
# ---------------------------------------------------------------------------


def test_pending_waiver_stage_path_empty_when_no_waivers_minted(tmp_path):
    root = _seeded_repo(tmp_path)
    assert wsc_tail_mod._pending_chain_ancestry_waiver_stage_path(root, _SID) == []


def test_pending_waiver_stage_path_returns_files_never_the_directory(tmp_path):
    """The stage paths must be the waiver FILES, never their containing
    directory.

    `commit_scoped` categorically rejects a directory pathspec -- a directory
    matches whatever is inside it AT COMMIT TIME, including a file a peer
    session dropped in after the list was built. Returning the directory here
    made the entire bookkeeping commit fail, which meant AC12 was not
    discharged at all: the waivers could never reach a commit by this path.
    Caught only by dogfooding the ceremony, because the original test proved
    the ordering with raw `git commit -- <dir>` (which git accepts) rather
    than through the production commit path (which does not).
    """
    root = _seeded_repo(tmp_path)
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(root), frozenset({"a" * 40, "c" * 40}), _SID,
    )

    result = wsc_tail_mod._pending_chain_ancestry_waiver_stage_path(root, _SID)

    base = f"state/review-trail/chain-ancestry-waivers/{_SID}"
    assert result == [f"{base}/{'a' * 40}.json", f"{base}/{'c' * 40}.json"]
    assert base not in result, "the bare directory must never be a stage path"
    for entry in result:
        assert not (root / entry).is_dir(), (
            f"stage path {entry!r} resolves to a directory; commit_scoped "
            "rejects directory pathspecs outright"
        )


def test_pending_waiver_stage_path_empty_for_unsafe_sid(tmp_path):
    """A `chain_id` failing `chain_waiver_dir`'s own directory-name-safety
    shape check (e.g. containing a path separator) must never resolve to a
    stage path -- mirrors that function's own None contract."""
    root = _seeded_repo(tmp_path)
    assert wsc_tail_mod._pending_chain_ancestry_waiver_stage_path(root, "../escape") == []


def test_pending_waiver_stage_path_empty_when_directory_unreadable(tmp_path, monkeypatch):
    """TOCTOU guard (review finding, 2026-07-31): the directory can be removed
    or become unreadable between the `is_dir()` check and the `iterdir()` call
    (or genuinely lack read permission). This must degrade to an empty list,
    never propagate an uncaught OSError -- an unguarded exception here would
    fail the entire ceremony commit, reproducing the exact "one bad path
    aborts the whole bookkeeping commit" failure mode AC12/C7b exists to fix.

    `Path.iterdir` is monkeypatched to raise rather than manipulating real
    filesystem permissions, which is unreliable across platforms and when
    running as root.
    """
    root = _seeded_repo(tmp_path)
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(root), frozenset({"e" * 40}), _SID,
    )

    def _raise_iterdir(self):
        raise OSError("simulated unreadable directory")

    monkeypatch.setattr(Path, "iterdir", _raise_iterdir)

    assert wsc_tail_mod._pending_chain_ancestry_waiver_stage_path(root, _SID) == []


# ---------------------------------------------------------------------------
# 2. The real-git fact this fix leans on: a path merely `git add`-ed on an
#    earlier pass is NOT carried into a later scoped commit unless it is
#    named again in that commit's own pathspec.
# ---------------------------------------------------------------------------


def test_waiver_minted_and_staged_on_aborted_pass_is_not_committed_until_named(tmp_path):
    """End-to-end proof of the ordering described in this op's docstring:

      pass 1 (HALT, aborts before wsc_tail's own commit runs):
        - coverage_gate.record_chain_ancestry_waiver mints the waiver file
        - coverage_gate._stage_chain_ancestry_waivers `git add`s the dir
        - <ceremony aborts here -- no commit>

      pass 2 (gate passes, EM has reviewed):
        - `_pending_chain_ancestry_waiver_stage_path` names the SAME
          directory again
        - THAT name is what must appear in the scoped commit's pathspec for
          the file to actually land in history.
    """
    root = _seeded_repo(tmp_path)

    # --- Pass 1 (simulates coverage_gate's mint + best-effort stage at HALT) ---
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(root), frozenset({"b" * 40}), _SID,
    )
    rel_paths = wsc_tail_mod._pending_chain_ancestry_waiver_stage_path(root, _SID)
    assert rel_paths, "the mint must resolve at least one waiver file to stage"
    _git(["add", "--", *rel_paths], root)  # mirrors coverage_gate._stage_chain_ancestry_waivers

    rel_dir = f"state/review-trail/chain-ancestry-waivers/{_SID}"
    waiver_file_rel = f"{rel_dir}/{'b' * 40}.json"
    # Staged (index has it) but nothing has been committed for it yet --
    # pass 1's ceremony aborted before any commit ran.
    porcelain = _git(["status", "--porcelain"], root).stdout
    assert "A  " + waiver_file_rel in porcelain or waiver_file_rel in porcelain
    assert not _committed_at_head(root, waiver_file_rel)

    # --- RED PROOF: a pass-2 commit that does NOT name the waiver dir in its
    # own pathspec leaves the file staged-forever, never committed -- exactly
    # the "staging is not committing" gap AC12 flags. Demonstrated with an
    # unrelated same-pass edit standing in for pass 2's own real stage_paths. ---
    (root / "other.txt").write_text("pass 2 content\n", encoding="utf-8")
    add_paths(root, ["other.txt"])
    _git(["commit", "-q", "-m", "pass 2 (waiver dir NOT named)", "--", "other.txt"], root)
    assert not _committed_at_head(root, waiver_file_rel), (
        "the waiver file must NOT be committed by a pathspec that never "
        "named its directory -- proves the pre-fix gap is real, not assumed"
    )

    # --- GREEN: pass 2's commit DOES name the same directory
    # `_pending_chain_ancestry_waiver_stage_path` resolves -- exactly what
    # `_run_precommit_tail`'s new AC12/C7b block folds into `extra_stage_paths`. ---
    # Driven through the PRODUCTION commit function, not raw `git commit`.
    # This is the fidelity gap that let the directory-pathspec bug ship: raw
    # git happily accepts `commit -- <dir>`, so a raw-git proof says nothing
    # about what `commit_scoped` -- which rejects directory pathspecs outright
    # -- will actually do on the real close path.
    (root / "other2.txt").write_text("pass 3 content\n", encoding="utf-8")
    stage_paths = ["other2.txt", *rel_paths]
    add_paths(root, stage_paths)
    msg_file = root / ".commit-msg.txt"
    msg_file.write_text("pass 3 (waiver files named)\n", encoding="utf-8")
    res = commit_scoped(stage_paths, msg_file, root)
    assert res.ok, f"commit_scoped refused the file pathspec: {res!r}"

    assert _committed_at_head(root, waiver_file_rel), (
        "naming the waiver files in the commit's own pathspec must carry the "
        "file minted on the earlier, aborted pass into history"
    )
    assert _committed_at_head(root, "other2.txt")


def test_commit_scoped_rejects_the_bare_waiver_directory_as_a_pathspec(tmp_path):
    """The production commit function refuses a directory pathspec.

    Regression for the shipped-inert bug: `_pending_chain_ancestry_waiver_
    stage_path` returned the containing directory, `commit_scoped` rejected
    it, and the ENTIRE bookkeeping commit failed -- so AC12 was not
    discharged, the waivers never reached history, and the ceremony could not
    close. Pinning the rejection here means any future change that reverts to
    naming the directory fails at this test rather than at a live ceremony.
    """
    root = _seeded_repo(tmp_path)
    chain_ancestry_waivers.record_chain_ancestry_waiver(
        str(root), frozenset({"d" * 40}), _SID,
    )
    rel_dir = f"state/review-trail/chain-ancestry-waivers/{_SID}"
    add_paths(root, [rel_dir])
    msg_file = root / ".commit-msg.txt"
    msg_file.write_text("should refuse a directory pathspec\n", encoding="utf-8")

    res = commit_scoped([rel_dir], msg_file, root)

    assert not res.ok, "commit_scoped must refuse a bare directory pathspec"
    assert "directory pathspec" in f"{res.stderr} {res.stdout}", (
        f"expected a directory-pathspec refusal, got: {res!r}"
    )
