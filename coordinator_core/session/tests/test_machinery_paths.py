"""Tests for `session.machinery_paths` -- path shapes only, per its own
negative-spec (owns paths, never creates a directory, never does I/O).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.session import machinery_paths

REPO_ROOT = os.path.join("X:", os.sep, "fake-repo")

_REPO_ROOT_ON_DISK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..")
)
_ALLOWLIST_PATH = os.path.join(
    _REPO_ROOT_ON_DISK, "docs", "reference", "state-corpus-allowlist.txt"
)


def _read_allowlist() -> list[str]:
    with open(_ALLOWLIST_PATH, "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def _parse_first_segments(ls_files_stdout: str) -> set[str]:
    segments = set()
    for line in ls_files_stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("/", 2)
        if len(parts) >= 2:
            segments.add(parts[1])
    return segments


def test_machinery_root_is_repo_root_dotdir():
    assert machinery_paths.machinery_root(REPO_ROOT) == os.path.join(
        REPO_ROOT, ".coordinator-local"
    )


def test_share_dir_lives_under_machinery_root():
    got = machinery_paths.share_dir(REPO_ROOT, "sid-1")
    assert got == os.path.join(
        machinery_paths.machinery_root(REPO_ROOT), "subagent-share", "sid-1"
    )


def test_ledger_intake_send_log_paths_join_onto_share_dir():
    sid = "sid-2"
    share = machinery_paths.share_dir(REPO_ROOT, sid)
    assert machinery_paths.ledger_path(REPO_ROOT, sid) == os.path.join(
        share, machinery_paths.LEDGER_FILENAME
    )
    assert machinery_paths.intake_path(REPO_ROOT, sid) == os.path.join(
        share, machinery_paths.INTAKE_FILENAME
    )
    assert machinery_paths.send_log_path(REPO_ROOT, sid) == os.path.join(
        share, machinery_paths.SEND_LOG_FILENAME
    )


def test_safe_session_id_rejects_unsafe_components():
    assert machinery_paths.safe_session_id("abc-123") is True
    assert machinery_paths.safe_session_id("..") is False
    assert machinery_paths.safe_session_id(".") is False
    assert machinery_paths.safe_session_id("../etc") is False
    assert machinery_paths.safe_session_id("") is False
    assert machinery_paths.safe_session_id(None) is False


def test_relocated_bucket_accessors_join_onto_machinery_root():
    root = machinery_paths.machinery_root(REPO_ROOT)
    assert machinery_paths.review_trail_dir(REPO_ROOT) == os.path.join(root, "review-trail")
    assert machinery_paths.ceremony_dir(REPO_ROOT) == os.path.join(root, "ceremony")
    assert machinery_paths.dispatch_briefs_dir(REPO_ROOT) == os.path.join(
        root, "dispatch-briefs"
    )
    assert machinery_paths.plan_sidecars_dir(REPO_ROOT) == os.path.join(root, "plan-sidecars")
    assert machinery_paths.cache_dir(REPO_ROOT) == os.path.join(root, "cache")
    assert machinery_paths.orientation_cache_path(REPO_ROOT) == os.path.join(
        root, "orientation_cache.md"
    )
    assert machinery_paths.cockpit_emission_path(REPO_ROOT) == os.path.join(
        root, "cockpit-emission.json"
    )
    assert machinery_paths.ledgers_dir(REPO_ROOT) == os.path.join(root, "ledgers")
    assert machinery_paths.kill_ledger_path(REPO_ROOT) == os.path.join(
        root, "kill-ledger.md"
    )
    assert machinery_paths.memo_outbox_sent_ledger_path(REPO_ROOT) == os.path.join(
        root, "memo-outbox", "sent-ledger.jsonl"
    )


def test_memo_outbox_accessors_join_onto_machinery_root():
    root = machinery_paths.machinery_root(REPO_ROOT)
    assert machinery_paths.memo_outbox_dir(REPO_ROOT) == os.path.join(root, "memo-outbox")
    assert machinery_paths.memo_outbox_sent_dir(REPO_ROOT) == os.path.join(
        root, "memo-outbox", "sent"
    )
    assert machinery_paths.memo_outbox_sent_ledger_path(REPO_ROOT) == os.path.join(
        machinery_paths.memo_outbox_dir(REPO_ROOT), "sent-ledger.jsonl"
    )


def test_legacy_memo_outbox_accessors_are_repo_root_relative_not_machinery_root():
    assert machinery_paths.legacy_memo_outbox_dir(REPO_ROOT) == os.path.join(
        REPO_ROOT, "state", "memo-outbox"
    )
    assert machinery_paths.legacy_memo_outbox_sent_dir(REPO_ROOT) == os.path.join(
        REPO_ROOT, "state", "memo-outbox", "sent"
    )


def test_module_never_creates_a_directory(tmp_path):
    repo_root = str(tmp_path)
    machinery_paths.share_dir(repo_root, "sid-3")
    machinery_paths.review_trail_dir(repo_root)
    machinery_paths.ledgers_dir(repo_root)
    assert not os.path.exists(os.path.join(repo_root, ".coordinator-local"))


def test_the_back_compat_alias_is_gone_after_c2():
    """C1 leaves `session/subagent_share.py` as a re-exporting alias so the engine has no
    broken window between C1's rename and C2's repoint; C2 deletes it as its LAST step.
    Asserting the alias is ABSENT is what pins that deletion -- the earlier version of this
    test asserted the alias EXISTS, which C2 then falsified by doing its job.
    """
    import importlib
    try:
        importlib.import_module("coordinator_core.session.subagent_share")
    except ImportError:
        return
    raise AssertionError(
        "session/subagent_share.py still exists -- C2 deletes the alias as its last step"
    )

def test_every_allowlist_entry_resolves_to_a_real_path_under_state():
    """A typo in the allowlist fails loudly rather than silently widening
    the untrack -- exit criterion 2's write-side half. Checked against
    `git ls-files`, not the filesystem: a tracked path can be momentarily
    absent from a live working tree (mid-edit, mid-move) on a box with ~50
    concurrent sessions, and that is not what a typo test is for."""
    out = subprocess.run(
        ["git", "ls-files", "state/"],
        cwd=_REPO_ROOT_ON_DISK,
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    tracked = _parse_first_segments(out.stdout)
    for entry in _read_allowlist():
        assert entry in tracked, (
            f"allowlist entry {entry!r} does not resolve to a tracked state/ path"
        )


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_every_tracked_state_first_segment_is_on_the_allowlist():
    """The CONVERSE assertion exit criterion 2 actually depends on: every
    first segment of a tracked `state/` path is either on the allowlist or
    is one of the C6/C7 relocation-set buckets this allowlist deliberately
    excludes. Without this test, close-out is a manual read of ~90
    segments."""
    out = subprocess.run(
        ["git", "ls-files", "state/"],
        cwd=_REPO_ROOT_ON_DISK,
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    allowlist = set(_read_allowlist())
    relocation_set = {
        "subagent-share",
        "review-trail",
        "ceremony",
        "dispatch-briefs",
        "plan-sidecars",
        "memo-outbox",
        "ledgers",
        "kill-ledger.md",
    }
    tracked = _parse_first_segments(out.stdout)
    unaccounted = tracked - allowlist - relocation_set
    assert not unaccounted, (
        f"tracked state/ segments missing from the allowlist and the "
        f"relocation set: {sorted(unaccounted)}"
    )


def test_subagent_share_id_pattern_captures_the_id_under_either_root():
    """The read-side accessor C4 added, tested directly rather than only
    through `artifact_owner`'s use of it. Both roots and both separator
    spellings, because a pattern that matches only the spelling its author
    happened to type is the defect at `2acd5ca032` -- it reported "no owner"
    for every live sidecar and said nothing while doing it.
    """
    pat = machinery_paths.subagent_share_id_pattern()
    for path in (
        ".coordinator-local/subagent-share/sid-9/report.md",
        r".coordinator-local\subagent-share\sid-9\report.md",
        "state/subagent-share/sid-9/report.md",
        r"state\subagent-share\sid-9\report.md",
        "/repo/.coordinator-local/subagent-share/sid-9/report.md",
    ):
        m = pat.search(path)
        assert m is not None, path
        assert m.group(1) == "sid-9", path


def test_subagent_share_id_pattern_does_not_match_a_foreign_bucket():
    """A directory that merely SITS beside the bucket is not the bucket.
    Asserted because the failure mode this accessor exists to prevent is a
    silent one: an over-broad pattern captures a wrong id and every caller
    downstream believes it.
    """
    pat = machinery_paths.subagent_share_id_pattern()
    for path in (
        ".coordinator-local/review-trail/sid-9/report.md",
        ".coordinator-local/ceremony/sid-9.md",
        "subagent-share-notes/sid-9/report.md",
    ):
        assert pat.search(path) is None, path


def test_subagent_share_id_pattern_is_cached():
    """Compiled once per process, not per call: this module is on the
    per-turn Stop-family hook path its own docstring names.
    """
    assert (
        machinery_paths.subagent_share_id_pattern()
        is machinery_paths.subagent_share_id_pattern()
    )
