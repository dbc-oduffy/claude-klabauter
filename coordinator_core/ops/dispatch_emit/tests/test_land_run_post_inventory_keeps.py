"""dispatch.land_run carries the KEEP behaviours the post-inventory table (C25 P-lines) names.

One section per line id: P3 the fresh test verdict, P11 a held chunk's sole path, P12 a review fix-pass
agent's write, P13/P14 comment-only drift (`.py` arm, repo-verifier arm), P15 the review-anchor arm
for a review that applied fixes.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time

import pytest

from coordinator_core.benchmarks import terminal_commit_full_path as driver
from coordinator_core.ops.dispatch_emit import land_run, review_edit_source
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import (  # noqa: F401 -- fixture
    RUN,
    SELF,
    _author,
    _declare,
    _dirty,
    _git,
    _land,
    _tree,
    _write,
    repo,
)
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

AGENT = "fixpass-agent-1"
_RECORDED_FAIL = {"status": "fail", "tests_failed": 1, "baseline": None}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _session_sink(root):
    return touch_record.sink_path(root / ".git" / "coordinator-sessions" / SELF)


def _agent_write(root, rel: str, text: str, at: float) -> None:
    hub = root / ".git" / "coordinator-sessions"
    (hub / SELF).mkdir(parents=True, exist_ok=True)
    (hub / SELF / "dispatched-agents.txt").write_text(
        f"{AGENT}\thost\tcoordinator:code-reviewer\t1\n", encoding="utf-8"
    )
    agent_dir = hub / ".agents" / AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(agent_dir), session_id=SELF, agent_id=AGENT, verb="T", path=rel,
        content_hash=_sha(text), kind="w", timestamp=at,
    )


# ---- P3: the test verdict reads through landing_test_gate -----------------------------------------


def _fresh_verdict(root, *, head: str) -> None:
    path = li.fresh_verdict_path(root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "run_id": RUN, "head": head, "tests": ["a.py"], "covered": li.landing_fingerprint(root, ["a.py"]),
            "status": "pass",
        }),
        encoding="utf-8",
    )


def test_p3_a_fresh_retest_pass_clears_a_recorded_failure(repo):
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    _fresh_verdict(repo, head=_git(["rev-parse", "HEAD"], repo).strip())

    out = _land(repo, targeted_tests=_RECORDED_FAIL)

    assert out["committed"] is True, out
    assert "a.py" in _tree(repo)


def test_p3_control_a_stale_retest_leaves_the_recorded_failure_refusing(repo):
    _author(repo, "a.py", "a = 1\n")
    _declare(repo, [("C1", ["a.py"])])
    _fresh_verdict(repo, head="0" * 40)

    out = _land(repo, targeted_tests=_RECORDED_FAIL)

    assert out["committed"] is False and out["refused"] == li.TARGETED_TESTS_FAILED, out


# ---- P11: a held chunk's sole-declared path is never staged ---------------------------------------


def test_p11_a_held_sole_path_a_review_agent_wrote_is_not_staged(repo, monkeypatch):
    _author(repo, "a.py", "a = 1\n")
    _author(repo, "contract/held.json", "{}\n")
    _declare(repo, [("C1", ["a.py"]), ("H1", ["contract/held.json"])])
    monkeypatch.setattr(
        review_edit_source, "session_review_writes", lambda root, sid, since: {"contract/held.json": AGENT}
    )

    out = _land(repo, held=[{"chunk": "H1", "reason": "halted"}])

    assert out["committed"] is True, out
    assert "contract/held.json" not in _tree(repo) and "a.py" in _tree(repo)
    assert "contract/held.json" in _dirty(repo)


# ---- P12: a review fix-pass agent's own write is the expectation ----------------------------------


def test_p12_review_agent_write_events_name_the_agents_own_bytes(repo, monkeypatch):
    now = time.time()
    _agent_write(repo, "src/x.py", "fixed\n", now)
    monkeypatch.setattr(
        review_edit_source, "session_review_writes", lambda root, sid, since: {"src/x.py": AGENT, "gone.py": "nobody"}
    )

    events = land_run._review_write_events(repo, SELF, RUN, {})

    assert events["src/x.py"] == (_sha("fixed\n"), pytest.approx(now))
    assert "gone.py" not in events


def _fingerprints(root, review_events):
    return land_run._recorded_fingerprints(
        root, SELF, ["src/x.py"], ["src/x.py"], excluded=set(), reviewed=None, reviewed_at=None,
        fixes_integrated_at=None, wave_paths=frozenset(), review_events=review_events,
    )


def _original_event(root, at: float, text: str = "original\n") -> None:
    touch_record.append_event(
        _session_sink(root), session_id=SELF, agent_id=None, verb="T", path="src/x.py",
        content_hash=_sha(text), kind="w", timestamp=at,
    )


def test_p12_a_fixpass_write_is_declared_at_the_agents_hash(repo):
    _write(repo, "src/x.py", "fixed\n")
    base = time.time() - 100
    _original_event(repo, base)

    assert _fingerprints(repo, {"src/x.py": (_sha("fixed\n"), base + 50)}) == {"src/x.py": _sha("fixed\n")}


def test_p12_a_foreign_edit_after_the_fixpass_keeps_the_agents_hash_so_it_refuses(repo):
    _write(repo, "src/x.py", "foreign\n")
    base = time.time() - 100
    _original_event(repo, base)

    expected = _fingerprints(repo, {"src/x.py": (_sha("fixed\n"), base + 50)})

    assert expected["src/x.py"] == _sha("fixed\n") != _sha("foreign\n")


def test_p12_a_newer_session_write_outranks_an_older_fixpass_event(repo):
    _write(repo, "src/x.py", "later\n")
    base = time.time() - 100
    _original_event(repo, base + 80, "later\n")

    assert _fingerprints(repo, {"src/x.py": (_sha("fixed\n"), base + 50)}) == {"src/x.py": _sha("later\n")}


def _fixpass_landing(repo, monkeypatch, *, agent_recorded: bool) -> dict:
    _write(repo, "src/x.py", "original\n")
    base = time.time() - 100
    _original_event(repo, base)
    _write(repo, "src/x.py", "fixed\n" if agent_recorded else "foreign\n")
    if agent_recorded:
        _agent_write(repo, "src/x.py", "fixed\n", base + 50)
    monkeypatch.setattr(
        review_edit_source, "session_review_writes",
        lambda root, sid, since: {"src/x.py": AGENT} if agent_recorded else {},
    )
    _declare(repo, [("C1", ["src/x.py"])])
    return _land(repo)


def test_p12_the_fixpass_write_lands_without_blob_drift(repo, monkeypatch):
    out = _fixpass_landing(repo, monkeypatch, agent_recorded=True)

    assert out["committed"] is True, out
    assert _tree(repo) == ["src/x.py"]


def test_p12_control_an_unrecorded_outside_edit_still_refuses_as_blob_drift(repo, monkeypatch):
    out = _fixpass_landing(repo, monkeypatch, agent_recorded=False)

    assert out["committed"] is False and out["refused"] == "blob-drift", out


# ---- P13 / P14: comment-only drift ----------------------------------------------------------------

_PREFIX = "manifest-run-1-prep"
_SIDECAR_REL = f".coordinator-local/subagent-share/{SELF}/{_PREFIX}.review-prep.md"
_REVIEWED = {"integration_stem": "rev-stem", "slices": 1, "fixes": 0, "prep_sidecar": _SIDECAR_REL}
_PY_SOURCE = "# Reviewed-by: kira\nx = 1\n"


def _snapshot(root, blobs: dict) -> None:
    snap = root / ".coordinator-local" / "subagent-share" / SELF / f"{_PREFIX}.pre-review"
    snap.mkdir(parents=True, exist_ok=True)
    files = {}
    for path, text in blobs.items():
        raw = _sha(text)
        (snap / raw).write_bytes(text.encode("utf-8"))
        files[path] = raw
    (snap / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")


def _drifted(root, reviewed: dict, current: dict) -> None:
    """Each path recorded at its reviewed bytes, then rewritten to its current bytes."""
    for path, text in reviewed.items():
        _author(root, path, text)
    for path, text in current.items():
        _write(root, path, text)
    _snapshot(root, reviewed)
    _declare(root, [("C1", list(reviewed))])


class _CountingCommit:
    def __init__(self, monkeypatch):
        from coordinator_core.git import commit as git_commit

        self.calls = 0
        real = git_commit.commit_paths

        def spy(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(git_commit, "commit_paths", spy)


def test_p13_a_comment_only_edit_after_review_lands_with_a_trailer(repo, monkeypatch):
    _drifted(repo, {"a.py": _PY_SOURCE}, {"a.py": "x = 1\n"})
    counter = _CountingCommit(monkeypatch)

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["committed"] is True, out
    assert counter.calls == 2
    assert f"Comment-Only-Drift: a.py {_sha(_PY_SOURCE)[:12]}->{_sha('x = 1' + chr(10))[:12]}" in _git(
        ["log", "-1", "--format=%B"], repo
    )
    assert "comment_only_drift" not in out


def test_p13_a_one_token_code_edit_refuses_after_one_attempt(repo, monkeypatch):
    _drifted(repo, {"a.py": _PY_SOURCE}, {"a.py": "# Reviewed-by: kira\nx = 2\n"})
    head = _git(["rev-parse", "HEAD"], repo)
    counter = _CountingCommit(monkeypatch)

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert counter.calls == 1
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_p13_mixed_drift_refuses_and_the_remedy_names_only_the_real_path(repo):
    _drifted(
        repo, {"a.py": _PY_SOURCE, "b.py": "y = 1\n"}, {"a.py": "x = 1\n", "b.py": "y = 2\n"},
    )

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["refused"] == "blob-drift", out
    withhold_call = out["error"].split("coordinator-invoke", 1)[1]
    assert "b.py" in withhold_call and "a.py" not in withhold_call


def test_p13_a_deleted_snapshot_is_unprovable_and_refuses(repo):
    import shutil

    _drifted(repo, {"a.py": _PY_SOURCE}, {"a.py": "x = 1\n"})
    shutil.rmtree(repo / ".coordinator-local" / "subagent-share" / SELF / f"{_PREFIX}.pre-review")

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["refused"] == "blob-drift", out


def test_p14_the_landing_set_is_passed_as_untrusted_paths(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import comment_only_drift

    seen: dict = {}
    real_fn = comment_only_drift.tolerated_drift

    def spy(root, sidecar, drift, **kw):
        seen.update(kw)
        return real_fn(root, sidecar, drift, **kw)

    monkeypatch.setattr(comment_only_drift, "tolerated_drift", spy)
    _drifted(repo, {"a.py": _PY_SOURCE}, {"a.py": "x = 1\n"})

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["committed"] is True, out
    assert "a.py" in seen["untrusted_paths"]


_TS_SOURCE = "// Reviewed-by: kira\nconst x = 1;\n"
_TS_CURRENT = "const x = 1;\n"


def _declare_verifier(root, tmp_path, exit_code: int, declared: bool = True):
    marker = tmp_path / "v-ran.marker"
    _write(
        root, "tools/v.py",
        f"import sys\nopen({str(marker)!r}, 'w').write('ran')\nsys.exit({exit_code})\n",
    )
    _git(["add", "tools/v.py"], root)
    _git(["commit", "-q", "-m", "verifier"], root)
    if declared:
        (root / "coordinator.local.md").write_text(
            "---\n"
            f'comment_only_verifier: ["{sys.executable}", "tools/v.py"]\n'
            'comment_only_verifier_suffixes: [".ts"]\n'
            "---\n",
            encoding="utf-8",
        )
    return marker


def test_p14_a_verifier_exit_zero_tolerates_ts_drift_with_a_trailer(repo, tmp_path):
    marker = _declare_verifier(repo, tmp_path, 0)
    _drifted(repo, {"a.ts": _TS_SOURCE}, {"a.ts": _TS_CURRENT})

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["committed"] is True, out
    assert f"Comment-Only-Drift: a.ts {_sha(_TS_SOURCE)[:12]}->{_sha(_TS_CURRENT)[:12]}" in _git(
        ["log", "-1", "--format=%B"], repo
    )
    assert marker.exists()


def test_p14_a_verifier_exit_one_is_real_drift(repo, tmp_path):
    marker = _declare_verifier(repo, tmp_path, 1)
    _drifted(repo, {"a.ts": _TS_SOURCE}, {"a.ts": _TS_CURRENT})
    head = _git(["rev-parse", "HEAD"], repo)

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["committed"] is False and out["refused"] == "blob-drift", out
    assert marker.exists()
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_p14_no_declaration_refuses_without_running_a_verifier(repo, tmp_path):
    marker = _declare_verifier(repo, tmp_path, 0, declared=False)
    _drifted(repo, {"a.ts": _TS_SOURCE}, {"a.ts": _TS_CURRENT})

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["refused"] == "blob-drift", out
    assert not marker.exists()


def test_p14_a_verifier_in_the_landing_set_refuses_without_running(repo, tmp_path):
    marker = _declare_verifier(repo, tmp_path, 0)
    edited = (repo / "tools" / "v.py").read_text(encoding="utf-8") + "# edited\n"
    _drifted(
        repo, {"a.ts": _TS_SOURCE, "tools/v.py": edited}, {"a.ts": _TS_CURRENT, "tools/v.py": edited + "# more\n"},
    )

    out = _land(repo, inline_review=dict(_REVIEWED))

    assert out["refused"] == "blob-drift", out
    assert not marker.exists()


# ---- P15: a review that applied fixes anchors its trailer when the product matches HEAD ------------


@pytest.fixture
def anchored(tmp_path, monkeypatch):
    """A run whose declared write (a.py) is already committed at HEAD; returns (repo, script)."""
    root, script = driver.build_fixture(tmp_path)
    _git(["add", "a.py"], root)
    _git(["commit", "-q", "-m", "hand-committed delivery"], root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", driver.SESSION_ID)
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    return root, script


def _anchor_land(root, script, *, fixes: int) -> dict:
    review = {**driver.INLINE_REVIEW, "fixes": fixes}
    return land_run._handler(
        {"script_path": script, "incomplete_chunks": [], "session_id": driver.SESSION_ID, "inline_review": review},
        repo_root=root / ".git",
    )


def test_p15_a_review_with_applied_fixes_lands_its_trailer_over_a_tree_equal_product(anchored):
    root, script = anchored
    before = _git(["rev-parse", "HEAD"], root).strip()

    out = _anchor_land(root, script, fixes=2)

    assert out["committed"] is True, out
    assert out["sha"] != before
    body = _git(["log", "-1", "--format=%B"], root)
    assert "Review: " in body and "Inline-Review: applies" in body


def test_p15_control_a_review_without_fixes_has_nothing_to_commit(anchored):
    root, script = anchored

    out = _anchor_land(root, script, fixes=0)

    assert out["committed"] is False and out.get("nothing_to_commit") is True, out


def test_p15_the_machine_local_review_record_is_never_in_the_commit(anchored):
    root, script = anchored

    out = _anchor_land(root, script, fixes=2)

    assert out["committed"] is True, out
    assert not any(".coordinator-local" in p for p in _git(["show", "--name-only", "--format=", "HEAD"], root).split())
