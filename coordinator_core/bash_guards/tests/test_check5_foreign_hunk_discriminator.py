"""D2 (docs/plans/2026-09-26-commit-emit-plane-engine-findings.md) --
characterization matrix for the C11 foreign-hunk arm's pass/fail
discriminator (finding R). Diagnosis only -- this file asserts what the
arm does TODAY, at HEAD, and changes no guard code.

Fast-tier: no real git process is spawned. ``dispatch_checks._run_git`` is
stubbed with a canned table keyed by its argv, and
``coordinator_core.session.scope.compute_scope`` (imported lazily inside
``check_validate_commit``, so patching the module attribute is sufficient)
is stubbed to return a canned ``ScopeResult`` -- both are inputs the arm
consumes, neither is what it is characterizing. The touch-record sink
itself is real (written through the module's own public writer), and
``compute_content_hash`` reads real files on disk under ``tmp_path``, so
the arm's own hash comparison runs unstubbed.

Findings recorded in ``docs/research/2026-09-26-foreign-hunk-discriminator.md``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pytest

from coordinator_core.bash_guards import dispatch_checks
from coordinator_core.session import touch_record
from coordinator_core.session.scope import ScopeResult

pytestmark = [pytest.mark.cadence]


def _deny_entries_present(result: Optional[Dict]) -> bool:
    if result is None:
        return False
    reason = result["hookSpecificOutput"].get("permissionDecisionReason", "")
    return "foreign hunk" in reason


def _stub_run_git(table: Dict[Tuple[str, ...], Tuple[int, str]]):
    def _fake(args: List[str], cwd: Optional[str] = None, timeout: float = 2.0,
               extra_env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        key = tuple(args)
        if key in table:
            return table[key]
        # Unregistered probe -- fail closed to "not found" so a missing
        # stub entry is loud (empty output, rc 1) rather than silently
        # matching some other branch's assumption.
        return 1, ""

    return _fake


def _status_lines(staged: List[str]) -> str:
    return "\n".join("M\t%s" % p for p in staged)


def _write_session_touch(
    git_root: str,
    session_id: str,
    path: str,
    content_hash: Optional[str],
) -> None:
    sdir = Path(git_root) / ".git" / "coordinator-sessions" / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(sdir),
        session_id=session_id,
        agent_id=None,
        verb=touch_record.VERB_TOUCH,
        path=path,
        content_hash=content_hash,
    )


def _write_agent_touch(
    git_root: str,
    em_session_id: str,
    agent_id: str,
    path: str,
    content_hash: Optional[str],
) -> None:
    adir = Path(git_root) / ".git" / "coordinator-sessions" / ".agents" / agent_id
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(em_session_id, encoding="utf-8")
    touch_record.append_event(
        touch_record.sink_path(adir),
        session_id=agent_id,
        agent_id=agent_id,
        verb=touch_record.VERB_TOUCH,
        path=path,
        content_hash=content_hash,
    )
    # `project_live_claims` (touch_record.py) filters every claim through
    # `liveness.session_live(event.session_id, cwd)`, which resolves the
    # SID's own session dir via `core.session_dir` -- `.git/coordinator-
    # sessions/<sid>`, never the `.agents/<agent_id>` sink dir the claim
    # itself lives under. A freshly-created dir with no meta.json reads
    # live via the recency-mtime fallback (Layer 2), so this mirrors real
    # dispatch provisioning closely enough for the excusal path to be
    # reachable at all.
    (Path(git_root) / ".git" / "coordinator-sessions" / agent_id).mkdir(
        parents=True, exist_ok=True
    )


@pytest.fixture()
def git_root(tmp_path: Path) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return str(root)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    git_root: str,
    session_id: str,
    staged: List[str],
    *,
    command: str = "git commit -m x",
    pathspec: Optional[List[str]] = None,
) -> Optional[Dict]:
    table: Dict[Tuple[str, ...], Tuple[int, str]] = {
        ("rev-parse", "--show-toplevel"): (0, git_root + "\n"),
        ("diff", "--cached", "--name-status", "-M"): (0, _status_lines(staged)),
    }
    if pathspec is not None:
        table[("diff", "--cached", "--name-only", "--", *pathspec)] = (
            0,
            "\n".join(pathspec),
        )
    monkeypatch.setattr(dispatch_checks, "_run_git", _stub_run_git(table))
    return dispatch_checks.check_validate_commit(command, session_id, git_root)


def _scope(monkeypatch: pytest.MonkeyPatch, my_scope: List[str]) -> None:
    import coordinator_core.session.scope as scope_mod

    monkeypatch.setattr(
        scope_mod,
        "compute_scope",
        lambda session_id, cwd: ScopeResult(my_scope=my_scope, skipped=[], orphans=[]),
    )


# --- H1: the write channel -----------------------------------------------
#
# A path touched through Edit/Write (`hooks.track_touched_files`) records a
# hash. A path touched through a Bash write goes through
# `write_claim_record.record_write_claims` -> `touch_record.append_touch_claims`,
# which is called with `content_hashes=None` (verified by reading
# `bash_guards/write_claim_record.py::record_write_claims`'s own call site --
# it passes no `content_hashes` kwarg) -- so a Bash-recorded TOUCH always
# carries `content_hash=None`. The arm's own gate,
# `if _own_hash is not None:`, means such a path can never reach the
# hash-comparison branch at all: the arm cannot fire for it, in either
# direction.


def test_h1_bash_recorded_touch_carries_no_hash_and_never_fires(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h1"
    path = "app.py"
    (Path(git_root) / path).write_text("changed on disk\n", encoding="utf-8")
    # Mirrors the Bash write channel: a TOUCH with no content_hash, exactly
    # what `record_write_claims` -> `append_touch_claims` produces today.
    _write_session_touch(git_root, session_id, path, content_hash=None)
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert not _deny_entries_present(result), (
        "a Bash-recorded touch (content_hash=None) must never trigger the "
        "foreign-hunk deny -- TODAY it cannot, because `_own_hash is not "
        "None` gates the whole comparison branch"
    )


# --- H2: a same-session re-edit -------------------------------------------
#
# An Edit records hash A, then this session's own subsequent Bash write (or
# formatter) re-touches the path with no hash. `project_live_claims` merges
# per-path to the winning (most recent) EVENT wholesale -- not per-field --
# so the later, hash-less event supersedes the earlier hash-bearing one for
# that path. `_own_content_hashes` filters to `content_hash is not None` on
# the winning event, so this path drops out of the map entirely. The
# session's own later write, far from causing a false "foreign hunk", SILENCES
# the arm for that path (a false negative, not a false positive).


def test_h2_later_hashless_touch_supersedes_and_silences_the_arm(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h2"
    path = "app.py"
    (Path(git_root) / path).write_text("edited then bash-rewritten\n", encoding="utf-8")
    # First: an Edit-shaped TOUCH with a hash that will disagree with disk.
    _write_session_touch(git_root, session_id, path, content_hash="stale-hash-from-edit")
    # Then: this session's own later Bash write, hash-less (H1's channel),
    # for the SAME path -- last-verb-wins supersedes the whole event.
    _write_session_touch(git_root, session_id, path, content_hash=None)
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert not _deny_entries_present(result), (
        "the winning (most recent) touch event for a path carries no hash "
        "once a hash-less re-touch supersedes an earlier hash-bearing one -- "
        "the arm cannot fire, because the path is absent from "
        "_own_content_hashes entirely (a false-negative silencing, not the "
        "'fires on own work' shape H2 originally hypothesized)"
    )


# --- H3: scoped vs unscoped commit_scope narrowing -------------------------
#
# `commit_scope` narrows to a commit's own explicit pathspec (`_bt_commit_own_
# pathspec` + a re-run `git diff --cached --name-only -- <pathspec>`). The
# C11 loop iterates `for staged_file in commit_scope`, so a staged, hash-
# disagreeing, owned path that sits OUTSIDE an explicit pathspec is simply
# never visited by the loop for THIS commit attempt -- it is not part of
# what is being committed, so this is the pathspec correctly bounding what
# gets checked, not the arm skipping a check it should have made. The two
# 2026-09-02 example-store-repo memos describing a scoped commit silently sweeping
# a peer's work are about a DIFFERENT check (the `commit-scope-events`
# peer-ownership advisory/deny, not the C11 foreign-hunk arm) and both carry
# `decision_note: REFUTED` -- routed to the 2026-09-11 inbox blitz. This case
# still pins C11's own scoped-vs-unscoped behavior directly, rather than
# relying on that refuted memo pair.


def test_h3_pathspec_narrows_commit_scope_so_an_out_of_scope_hunk_is_not_visited(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h3"
    in_scope = "included.py"
    out_of_scope = "excluded.py"
    (Path(git_root) / in_scope).write_text("in scope, unchanged\n", encoding="utf-8")
    (Path(git_root) / out_of_scope).write_text("out of scope, disagrees\n", encoding="utf-8")
    _write_session_touch(git_root, session_id, in_scope, content_hash=touch_record.compute_content_hash(
        Path(git_root) / in_scope
    ))
    # `out_of_scope`'s recorded hash disagrees with disk-now -- would fire
    # the arm if visited.
    _write_session_touch(git_root, session_id, out_of_scope, content_hash="stale-and-would-disagree")
    _scope(monkeypatch, [in_scope, out_of_scope])

    result = _run(
        monkeypatch,
        git_root,
        session_id,
        [in_scope, out_of_scope],
        command="git commit -m x -- %s" % in_scope,
        pathspec=[in_scope],
    )

    assert not _deny_entries_present(result), (
        "an explicit pathspec narrows commit_scope to the named path(s); a "
        "hash-disagreeing owned file OUTSIDE that pathspec is not part of "
        "this commit attempt and the C11 loop never visits it -- pinning "
        "TODAY's scoped-commit behavior for this arm specifically"
    )


def test_h3_the_same_disagreeing_path_denies_when_actually_in_scope(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    """Control for the case above: the SAME disagreeing path, named by its
    own pathspec, still denies -- confirming H3's narrowing is about which
    paths the loop visits, not about the comparison itself going silent."""
    session_id = "sess-h3b"
    path = "included.py"
    (Path(git_root) / path).write_text("disagrees with recorded hash\n", encoding="utf-8")
    _write_session_touch(git_root, session_id, path, content_hash="stale-and-disagrees")
    _scope(monkeypatch, [path])

    result = _run(
        monkeypatch,
        git_root,
        session_id,
        [path],
        command="git commit -m x -- %s" % path,
        pathspec=[path],
    )

    assert _deny_entries_present(result), (
        "a hash-disagreeing path NAMED by its own pathspec still denies -- "
        "the pathspec narrows the visited set, it does not disable the "
        "comparison for paths inside it"
    )


# --- H4: EOL / on-disk rewrite after the hash was recorded ----------------
#
# B4 (docs/plans/2026-09-26-commit-emit-plane-engine-findings.md) flipped this
# case per D2's fix-locus finding: the arm now re-hashes disk-now content with
# CRLF/CR normalized to LF and excuses the disagreement if THAT matches the
# recorded hash. Recording a hash for one line ending and then rewriting the
# SAME file's bytes to a different line ending (a CRLF checkout, an EOL-repair
# pass) no longer denies -- a genuinely foreign edit (different content, not
# just line endings) still falls through to the ordinary comparison below.


def test_h4_an_eol_only_rewrite_after_recording_no_longer_denies(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h4"
    path = "app.py"
    original = Path(git_root) / path
    original.write_bytes(b"line one\nline two\n")
    recorded_hash = touch_record.compute_content_hash(original)
    # An EOL-repair pass (or a CRLF checkout) rewrites the SAME logical
    # content to different bytes on disk, after the hash was recorded.
    original.write_bytes(b"line one\r\nline two\r\n")
    _write_session_touch(git_root, session_id, path, content_hash=recorded_hash)
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert not _deny_entries_present(result), (
        "an EOL-only rewrite (recorded content and disk-now content agree "
        "once CRLF/CR is normalized to LF) must no longer reproduce a "
        "'foreign hunk' deny -- B4 flipped this false-positive case per "
        "D2's diagnosis"
    )


def test_h4_a_genuinely_different_content_change_with_crlf_still_denies(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h4-real"
    path = "app.py"
    original = Path(git_root) / path
    original.write_bytes(b"line one\nline two\n")
    recorded_hash = touch_record.compute_content_hash(original)
    # A genuinely different edit, not just an EOL rewrite -- normalizing EOL
    # does not make this match the recorded hash, so it must still deny.
    original.write_bytes(b"line one\r\nline THREE\r\n")
    _write_session_touch(git_root, session_id, path, content_hash=recorded_hash)
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert _deny_entries_present(result), (
        "EOL normalization must only excuse a byte-for-byte-after-"
        "normalization match -- a genuinely foreign content change "
        "(not just line endings) must still deny"
    )


# --- H5: agent excusal ------------------------------------------------------
#
# A dispatched subagent's own touch record, back-pointed at the dispatching
# session via `.agents/<agent_id>/em-session-id.txt`, excuses a disagreement
# when the AGENT's own recorded hash equals disk-now
# (`_agent_owned_content_hashes`, consulted lazily on first disagreement).
# The excusal is keyed on the agent hash equaling CURRENT disk content, not
# on recency -- an agent hash recorded before a LATER EM edit does not equal
# the EM's own newer disk-now content, so that later edit is NOT excused by
# the agent's stale hash; it falls through to the ordinary comparison
# against the session's own recorded hash instead.


def test_h5_agent_hash_matching_disk_now_excuses_the_disagreement(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h5"
    agent_id = "agent-h5"
    path = "app.py"
    disk_file = Path(git_root) / path
    disk_file.write_text("written by the dispatched agent\n", encoding="utf-8")
    agent_hash = touch_record.compute_content_hash(disk_file)
    # The EM's own recorded hash is stale (pre-dispatch content).
    _write_session_touch(git_root, session_id, path, content_hash="pre-dispatch-hash")
    # The agent's own hash matches disk-now, and the agent back-points at
    # this session.
    _write_agent_touch(git_root, session_id, agent_id, path, content_hash=agent_hash)
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert not _deny_entries_present(result), (
        "a self-back-pointed dispatched agent's own recorded hash, when it "
        "equals disk-now, excuses the disagreement against the session's "
        "own stale recorded hash -- this is the EM-dispatch-vs-foreign "
        "distinction the guard already draws"
    )


def test_h5_a_stale_agent_hash_recorded_before_a_later_em_edit_does_not_excuse(
    monkeypatch: pytest.MonkeyPatch, git_root: str
) -> None:
    session_id = "sess-h5b"
    agent_id = "agent-h5b"
    path = "app.py"
    disk_file = Path(git_root) / path
    # The agent wrote this content first...
    disk_file.write_text("agent's original content\n", encoding="utf-8")
    agent_hash_now_stale = touch_record.compute_content_hash(disk_file)
    _write_agent_touch(git_root, session_id, agent_id, path, content_hash=agent_hash_now_stale)
    # ...then the EM itself rewrote the file to something newer, recording
    # its own (now also stale relative to disk) hash for the OLD content --
    # neither recorded hash matches disk-now.
    _write_session_touch(git_root, session_id, path, content_hash="em-recorded-hash-also-stale")
    disk_file.write_text("content changed again after both hashes were recorded\n", encoding="utf-8")
    _scope(monkeypatch, [path])

    result = _run(monkeypatch, git_root, session_id, [path])

    assert _deny_entries_present(result), (
        "the agent excusal is keyed on the agent's OWN hash matching "
        "disk-now, not on the agent having touched the path at all -- a "
        "stale agent hash that no longer matches disk-now does not excuse "
        "a disagreement against the session's own (also-stale) recorded "
        "hash, and the arm denies"
    )
