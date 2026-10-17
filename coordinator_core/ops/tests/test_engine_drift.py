
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from coordinator_core.ops.engine_drift import (
    CAUSE_FLOOR_ABSENT,
    CAUSE_GIT_ERROR,
    CAUSE_MIRROR_UNSTAMPED,
    CAUSE_SHALLOW,
    CAUSE_SOURCE_HISTORY_UNAVAILABLE,
    _diagnose_indeterminate,
    _engine_drift,
    _git_is_behind,
    classify_drift,
    probe_engine_drift,
)
from coordinator_core.engine_version import MIN_KNOWN_GOOD_SHA
from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

FLOOR = MIN_KNOWN_GOOD_SHA
RUNNING = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class TestClassifyDriftBehind:
    def test_behind_floor_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] == "behind"

    def test_behind_floor_offer_present(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert "offer" in result
        assert result["offer"]
        assert RUNNING[:12] in result["offer"]
        assert FLOOR[:12] in result["offer"]

    def test_behind_floor_no_notice_key(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert "notice" not in result

    def test_behind_floor_carries_shas(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: True)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftClean:
    def test_at_or_ahead_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert result["state"] == "clean"

    def test_at_or_ahead_is_silent(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert "offer" not in result
        assert "notice" not in result

    def test_at_or_ahead_carries_shas(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: False)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftIndeterminateUnresolvedSha:
    def test_unknown_sha_state(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] == "indeterminate"

    def test_unknown_sha_distinct_notice_present(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert "notice" in result
        assert result["notice"]
        assert "indeterminate" in result["notice"].lower()

    def test_unknown_sha_not_silent(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] != "clean"
        assert "notice" in result

    def test_unknown_sha_not_behind_alarm(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["state"] != "behind"
        assert "offer" not in result

    def test_unknown_sha_is_behind_never_called(self):
        calls = []

        def _tracking_is_behind(r, f):
            calls.append((r, f))
            return True

        classify_drift(None, FLOOR, is_behind=_tracking_is_behind)
        assert calls == []

    def test_unknown_sha_running_sha_field_is_none(self):
        result = classify_drift(None, FLOOR, is_behind=lambda r, f: True)
        assert result["running_sha"] is None
        assert result["floor_sha"] == FLOOR


class TestClassifyDriftIndeterminateAncestry:
    def test_indeterminate_ancestry_state(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert result["state"] == "indeterminate"

    def test_indeterminate_ancestry_distinct_notice(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert "notice" in result
        assert "offer" not in result

    def test_indeterminate_ancestry_carries_running_sha(self):
        result = classify_drift(RUNNING, FLOOR, is_behind=lambda r, f: None)
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR


# _git_is_behind's merge-base --is-ancestor rc
# mapping was entirely unexercised; a regression flipping the rc0/rc1 branches would
# have passed the full suite silently. Exercised against this repo's own real git
# history rather than a throwaway repo fixture, since MIN_KNOWN_GOOD_SHA is a known
# ancestor of HEAD in this checkout by construction (the floor is always <= HEAD).
class TestGitIsBehindEqualShaShortCircuit:
    def test_equal_sha_returns_false_without_subprocess(self):
        with patch("coordinator_core.ops.engine_drift.git_predicate") as mock_probe:
            result = _git_is_behind(FLOOR, FLOOR, Path("."))
        assert result is False
        mock_probe.assert_not_called()


def _head_and_oldest_reachable():
    """HEAD and the oldest commit reachable from it: an ancestor by construction on any history,
    including one rewritten after `MIN_KNOWN_GOOD_SHA` was pinned (that floor need not be reachable)."""
    def _git_out(*args):
        return subprocess.run(
            ["git", *args], capture_output=True, text=True, cwd="coordinator_core",
            **no_console_creationflags(),
        ).stdout.split()

    head = _git_out("rev-parse", "HEAD")[0]
    oldest = _git_out("rev-list", "--max-parents=0", "HEAD")[-1]
    if oldest == head:
        pytest.skip("single-commit history has no distinct ancestor to probe")
    return head, oldest


REPO = Path(__file__).resolve().parents[2]


class TestGitIsBehindRealAncestry:
    def test_ancestor_sha_returns_true(self):
        head, oldest = _head_and_oldest_reachable()
        assert _git_is_behind(oldest, head, REPO) is True

    def test_descendant_sha_returns_false(self):
        head, oldest = _head_and_oldest_reachable()
        assert _git_is_behind(head, oldest, REPO) is False

    def test_unresolvable_sha_returns_none(self):
        result = _git_is_behind(
            "ffffffffffffffffffffffffffffffffffffff", FLOOR, REPO
        )
        assert result is None


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(cwd), check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit_repo(path, n=2):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    shas = []
    for i in range(n):
        (path / "f.txt").write_text(str(i))
        _git(path, "add", "f.txt")
        _git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(path, "rev-parse", "HEAD"))
    return shas


class TestDiagnoseIndeterminate:
    def test_floor_absent_in_full_clone(self, tmp_path):
        _commit_repo(tmp_path / "full")
        assert _diagnose_indeterminate("b" * 40, tmp_path / "full") == CAUSE_FLOOR_ABSENT

    def test_floor_absent_in_shallow_clone(self, tmp_path):
        shas = _commit_repo(tmp_path / "src")
        _git(tmp_path, "clone", "-q", "--depth", "1", (tmp_path / "src").as_uri(), "shallow")
        assert _diagnose_indeterminate(shas[0], tmp_path / "shallow") == CAUSE_SHALLOW

    def test_floor_present_is_generic_git_error(self, tmp_path):
        shas = _commit_repo(tmp_path / "full")
        assert _diagnose_indeterminate(shas[0], tmp_path / "full") == CAUSE_GIT_ERROR

    def test_not_a_repo_is_generic_git_error(self, tmp_path):
        (tmp_path / "plain").mkdir()
        assert _diagnose_indeterminate("b" * 40, tmp_path / "plain") == CAUSE_GIT_ERROR

    def test_diagnose_only_runs_on_indeterminate_path(self):
        calls = []
        classify_drift(RUNNING, FLOOR, lambda r, f: False, lambda f: calls.append(f))
        classify_drift(RUNNING, FLOOR, lambda r, f: True, lambda f: calls.append(f))
        assert calls == []


class TestClassifyDriftCauseNotices:
    def _indet(self, cause):
        return classify_drift(RUNNING, FLOOR, lambda r, f: None, lambda f: cause)

    def test_floor_absent_names_floor_not_shallow(self):
        result = self._indet(CAUSE_FLOOR_ABSENT)
        assert result["state"] == "indeterminate"
        for text in (result["reason"], result["notice"]):
            assert "floor" in text
            assert "shallow clone" not in text
        assert "absent" in result["notice"]
        assert "Fetch full history" not in result["notice"]

    def test_shallow_keeps_shallow_hint(self):
        result = self._indet(CAUSE_SHALLOW)
        assert "shallow clone" in result["notice"]
        assert "Fetch full history" in result["notice"]

    def test_git_error_gets_neither_hint(self):
        result = self._indet(CAUSE_GIT_ERROR)
        assert "shallow" not in result["notice"]
        assert "absent" not in result["notice"]
        assert "shallow" not in result["reason"]

    def test_no_diagnose_defaults_to_git_error(self):
        result = classify_drift(RUNNING, FLOOR, lambda r, f: None)
        assert "shallow" not in result["notice"]


class TestPinnedFloorReachability:
    def test_pinned_floor_diagnosis_matches_object_presence(self):
        """A floor pin absent from the engine clone must diagnose as absent/shallow, never a generic git error."""
        engine_dir = Path(__file__).resolve().parents[2]
        present = subprocess.run(
            ["git", "-C", str(engine_dir), "cat-file", "-e", f"{FLOOR}^{{commit}}"],
            capture_output=True, **no_console_creationflags(),
        ).returncode == 0
        verdict = _diagnose_indeterminate(FLOOR, engine_dir)
        if present:
            assert verdict == CAUSE_GIT_ERROR
        else:
            assert verdict in (CAUSE_FLOOR_ABSENT, CAUSE_SHALLOW)


def _mirror(path, message):
    shas = _commit_repo(path, n=1)
    (path / "PUBLISH_MIRROR").write_text("")
    _git(path, "add", "PUBLISH_MIRROR")
    _git(path, "commit", "-q", "-m", message)
    return shas


class TestProbeMirrorStamp:
    """Mirror history is distinct from the source's; ancestry runs in the registered source repo."""

    def _setup(self, tmp_path, monkeypatch, floor, stamp_index, register=True):
        src = tmp_path / "src"
        shas = _commit_repo(src, n=3)
        mirror = tmp_path / "mirror"
        _mirror(mirror, f"publish [source-head {shas[stamp_index][:12]}]")
        monkeypatch.setattr("coordinator_core.ops.engine_drift.MIN_KNOWN_GOOD_SHA", floor(shas))
        monkeypatch.setattr(
            "coordinator_core.engine_root.engine_source_root",
            lambda: str(src) if register else None,
        )
        return shas, mirror

    def test_stamp_equals_floor_is_clean_without_ancestry_spawn(self, tmp_path, monkeypatch):
        shas, mirror = self._setup(tmp_path, monkeypatch, lambda s: s[1], 1)
        with patch("coordinator_core.ops.engine_drift._git_is_behind") as m:
            result = probe_engine_drift(mirror)
        m.assert_not_called()
        assert result["state"] == "clean"
        assert result["basis"] == "mirror-stamp"
        assert result["running_sha"].startswith(shas[1][:12])
        assert result["engine_head"] == _git(mirror, "rev-parse", "HEAD")

    def test_floor_older_than_stamp_is_clean(self, tmp_path, monkeypatch):
        _, mirror = self._setup(tmp_path, monkeypatch, lambda s: s[0], 2)
        assert probe_engine_drift(mirror)["state"] == "clean"

    def test_stamp_older_than_floor_is_behind(self, tmp_path, monkeypatch):
        _, mirror = self._setup(tmp_path, monkeypatch, lambda s: s[2], 0)
        result = probe_engine_drift(mirror)
        assert result["state"] == "behind"
        assert "offer" in result

    def test_unregistered_source_is_named_indeterminate(self, tmp_path, monkeypatch):
        shas, mirror = self._setup(tmp_path, monkeypatch, lambda s: s[2], 0, register=False)
        result = probe_engine_drift(mirror)
        assert result["state"] == "indeterminate"
        assert result["cause"] == CAUSE_SOURCE_HISTORY_UNAVAILABLE
        assert shas[0][:12] in result["notice"]

    def test_unstamped_mirror_head_is_named_indeterminate(self, tmp_path, monkeypatch):
        mirror = tmp_path / "mirror"
        _mirror(mirror, "publish with no stamp")
        result = probe_engine_drift(mirror)
        assert result["state"] == "indeterminate"
        assert result["cause"] == CAUSE_MIRROR_UNSTAMPED
        assert result["basis"] == "mirror-stamp"


class TestProbeSourceTree:
    def test_ancestry_runs_in_engine_repo(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        shas = _commit_repo(repo, n=2)
        monkeypatch.setattr("coordinator_core.ops.engine_drift.MIN_KNOWN_GOOD_SHA", shas[0])
        result = probe_engine_drift(repo)
        assert result["state"] == "clean"
        assert result["basis"] == "source-tree"
        assert result["running_sha"] == shas[1]
        monkeypatch.setattr("coordinator_core.ops.engine_drift.MIN_KNOWN_GOOD_SHA", shas[1])
        assert probe_engine_drift(repo)["state"] == "clean"

    def test_floor_absent_from_engine_repo(self, tmp_path):
        repo = tmp_path / "repo"
        _commit_repo(repo, n=2)
        result = probe_engine_drift(repo)
        assert result["state"] == "indeterminate"
        assert result["cause"] == CAUSE_FLOOR_ABSENT

    def test_not_a_repo_is_indeterminate(self, tmp_path):
        (tmp_path / "plain").mkdir()
        result = probe_engine_drift(tmp_path / "plain")
        assert result["state"] == "indeterminate"


class TestEngineDriftHandlerWiring:
    def test_smoke_does_not_raise_and_returns_state(self):
        result = _engine_drift({})
        assert result["state"] in ("clean", "behind", "indeterminate")
        assert "basis" in result and "engine_head" in result

    def test_handler_takes_no_engine_dir_and_calls_probe_with_no_argument(self):
        with patch(
            "coordinator_core.ops.engine_drift.probe_engine_drift", return_value={"state": "clean"}
        ) as mock_probe:
            assert _engine_drift({"engine_dir": "x"}) == {"state": "clean"}
        mock_probe.assert_called_once_with()

    def test_wiring_passes_source_head_and_floor_constant_to_ancestry(self):
        from coordinator_core.engine_version import SourceHead

        with patch(
            "coordinator_core.ops.engine_drift.resolve_engine_source_head",
            return_value=SourceHead(RUNNING, "source-tree", RUNNING),
        ) as mock_resolve, patch(
            "coordinator_core.ops.engine_drift._git_is_behind", return_value=False
        ) as mock_is_behind:
            result = _engine_drift({})

        mock_resolve.assert_called_once()
        assert mock_is_behind.call_args.args[:2] == (RUNNING, FLOOR)
        assert result["state"] == "clean"
        assert result["running_sha"] == RUNNING
        assert result["floor_sha"] == FLOOR
        assert result["basis"] == "source-tree"

    def test_wiring_delegates_unresolved_sha_to_classify_drift(self):
        from coordinator_core.engine_version import SourceHead

        with patch(
            "coordinator_core.ops.engine_drift.resolve_engine_source_head",
            return_value=SourceHead(None, "source-tree", None),
        ), patch("coordinator_core.ops.engine_drift._git_is_behind") as mock_is_behind:
            result = _engine_drift({})

        assert result["state"] == "indeterminate"
        assert result["running_sha"] is None
        mock_is_behind.assert_not_called()
