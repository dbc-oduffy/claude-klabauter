"""dispatch.terminal_commit refuses, before commit_v2, a landing set carrying an unparseable
``.py`` or a blob that drops lines a foreign commit added since the run base."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity, terminal_commit
from coordinator_core.ops.dispatch_emit import landing_targeted_tests
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    render_marker,
)
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REVIEWED = reviewed()


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


@pytest.fixture(autouse=True)
def _review_verdict_present(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_floor

    monkeypatch.setattr(review_floor, "verdict_count", lambda *_a, **_k: 1)


def _fire(repo: Path, path: str, **extra) -> dict:
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=(path,)),))
    (repo / "run.mjs").write_text(render_marker(request) + "\n", encoding="utf-8")
    params = {"inline_review": _REVIEWED, "script_path": "run.mjs", "incomplete_chunks": []}
    params.update(extra)
    return terminal_commit._handler(params, repo_root=repo / ".git")


def test_an_unparseable_py_is_refused_with_the_named_reason(repo):
    (repo / "m.py").write_text('x = "a\nb"\n', encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "m.py")

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.PY_UNPARSEABLE
    assert out["integrity"][0]["path"] == "m.py"
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_stubbed_overlap_finding_is_refused_and_the_token_is_released(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo)
    seen = {}

    def stub(root, paths, base, *, run_id, plan=None):
        seen["base"] = base
        return [landing_targeted_tests.Finding("ok.py", landing_integrity.SINCE_BASE_OVERLAP, "drops abc1234")]

    monkeypatch.setattr(landing_integrity, "since_base_findings", stub)
    token = run_token.mint(repo, "r1", "run.mjs")

    out = _fire(repo, "ok.py", run_base_sha=head, run_id="r1", run_token=token)

    assert out["committed"] is False
    assert out["refused"] == landing_integrity.SINCE_BASE_OVERLAP
    assert seen["base"] == head
    assert _git(["rev-parse", "HEAD"], repo) == head
    assert run_token.admits(repo, "r1", token, "run.mjs") is True


def test_a_caller_reading_a_name_only_the_worktree_callee_binds_is_refused(repo):
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "li.py").write_text("def old():\n    return 1\n", encoding="utf-8")
    (pkg / "caller.py").write_text(
        "def run():\n    from pkg import li\n    return li.old()\n", encoding="utf-8"
    )
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "seed pkg"], repo)
    (pkg / "li.py").write_text(
        "def old():\n    return 1\n\n\ndef new_fn():\n    return 2\n", encoding="utf-8"
    )
    (pkg / "caller.py").write_text(
        "def run():\n    from pkg import li\n    return li.old() + li.new_fn()\n", encoding="utf-8"
    )
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "pkg/caller.py")

    assert out["committed"] is False, out
    text = str(out)
    assert "import_closure" in text and "new_fn" in text, out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_clean_landing_without_a_base_commits_and_reports_no_base(repo):
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8")

    out = _fire(repo, "ok.py")

    assert out["committed"] is True, out
    assert out["since_base_check"] == "no-base"


def _closure_repo(repo: Path, monkeypatch) -> str:
    from types import SimpleNamespace

    from coordinator_core.authoring_leaks import import_closure

    (repo / "caller.py").write_text("x = 1\n", encoding="utf-8")
    hit = SimpleNamespace(path="caller.py", line=1, detail="imports a deleted module")
    monkeypatch.setattr(import_closure, "residue_findings", lambda *_a, **_k: [hit])
    return "caller.py"


def _fake_waiver(monkeypatch, covered):
    from coordinator_core.authz import landing_waiver

    consumed = []

    def match(root, guard, hits, **_kw):
        if guard == "import-closure-untracked" and set(hits) <= set(covered):
            return landing_waiver.Waiver(Path("w.json"), {})
        return None

    monkeypatch.setattr(landing_waiver, "matching_waiver", match)
    monkeypatch.setattr(landing_waiver, "consume", lambda w: consumed.append(w) or True)
    return consumed


def test_a_covering_waiver_drops_the_import_closure_finding_and_is_consumed_once(repo, monkeypatch):
    path = _closure_repo(repo, monkeypatch)
    consumed = _fake_waiver(monkeypatch, {path})

    out = _fire(repo, path)

    assert out["committed"] is True, out
    assert len(consumed) == 1


def test_a_waiver_for_other_paths_still_refuses_and_names_the_g_em(repo, monkeypatch):
    path = _closure_repo(repo, monkeypatch)
    consumed = _fake_waiver(monkeypatch, {"other.py"})
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, path)

    assert out["committed"] is False
    assert out.get("refused") == landing_integrity.IMPORT_CLOSURE_UNTRACKED, out
    text = str(out)
    assert "ask the G-EM" in text and "landing_waiver record" in text and "ask the PM" not in text
    assert consumed == []
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_one_waiver_listing_both_guards_is_consumed_once_for_one_landing(tmp_path, monkeypatch):
    from coordinator_core.authz import landing_waiver
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    shared = landing_waiver.Waiver(Path("w.json"), {})
    consumed = []
    monkeypatch.setattr(landing_waiver, "matching_waiver", lambda root, guard, hits, **_k: shared)
    monkeypatch.setattr(landing_waiver, "consume", lambda w: consumed.append(w) or True)
    findings = [
        landing_targeted_tests.Finding("a.py", "import-closure-untracked", "d"),
        landing_targeted_tests.Finding("b.py", "peer-hunks-co-declared", "d"),
    ]

    out = refusals._apply_landing_waiver(tmp_path, findings, landing_targeted_tests.Finding)

    assert out == []
    assert consumed == [shared]


def test_hands_on_mode_reaches_the_matcher_and_a_covered_finding_still_refuses(tmp_path, monkeypatch):
    from coordinator_core.authz import landing_waiver
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
    from coordinator_core.session import mode_resolution

    monkeypatch.setattr(mode_resolution, "resolve_mode", lambda *_a, **_k: "hands-on")
    seen = []

    def match(root, guard, hits, **kw):
        seen.append(kw.get("hands_on"))
        return None if kw.get("hands_on") else landing_waiver.Waiver(Path("w.json"), {})

    monkeypatch.setattr(landing_waiver, "matching_waiver", match)
    monkeypatch.setattr(landing_waiver, "consume", lambda w: True)
    findings = [landing_targeted_tests.Finding("a.py", "import-closure-untracked", "d")]

    out = refusals._apply_landing_waiver(tmp_path, findings, landing_targeted_tests.Finding)

    assert seen == [True] and len(out) == 1


def test_a_lost_consume_race_keeps_the_findings(tmp_path, monkeypatch):
    from coordinator_core.authz import landing_waiver
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    shared = landing_waiver.Waiver(Path("w.json"), {})
    monkeypatch.setattr(landing_waiver, "matching_waiver", lambda *_a, **_k: shared)
    monkeypatch.setattr(landing_waiver, "consume", lambda w: False)
    findings = [landing_targeted_tests.Finding("a.py", "import-closure-untracked", "d")]

    out = refusals._apply_landing_waiver(tmp_path, findings, landing_targeted_tests.Finding)

    assert len(out) == 1 and "ask the G-EM" in out[0].detail


def test_an_uncovered_peer_hunks_finding_stays_and_spends_nothing(tmp_path, monkeypatch):
    from coordinator_core.authz import landing_waiver
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals

    consumed = []
    monkeypatch.setattr(landing_waiver, "matching_waiver", lambda *_a, **_k: None)
    monkeypatch.setattr(landing_waiver, "consume", consumed.append)
    findings = [landing_targeted_tests.Finding("b.py", "peer-hunks-co-declared", "d")]

    out = refusals._apply_landing_waiver(tmp_path, findings, landing_targeted_tests.Finding)

    assert len(out) == 1 and "ask the G-EM" in out[0].detail and "ask the PM" not in out[0].detail
    assert consumed == []
