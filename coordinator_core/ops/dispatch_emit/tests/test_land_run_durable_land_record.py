"""A keyed land_run / terminal_commit call leaves a durable land record that a dead worker's
reconcile resolves: committed once the commit is on HEAD, never-ran only before the commit spawn."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_intent, land_run, run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope
from coordinator_core.session import touch_record
from coordinator_core.session_hub_path import session_dir_for
from coordinator_core.warm import dispatch_ack, dispatch_fence
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SELF = "5e1f0000-0000-4000-8000-0000000000a1"
RUN = "plan-20261021T100000-aaaaaa"
SCRIPT = "run-a.mjs"
PLAN = "docs/plans/p.md"
KEY = "123-456"
TC_RUN = "ask-1"
TC_SCRIPT = "run.mjs"
_TC_REVIEWED = {
    "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
    "wave_reviews": [{"verdict": "PASS", "sidecar_path": "../wave-review.md"}],
}


class _Death(BaseException):
    """A worker dying at this point: nothing after it runs, nothing is settled."""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgsign", "false")


@pytest.fixture
def keyed(tmp_path):
    """Run inside a keyed dispatch: `(claim_dir, journal_dir)`; released after the test."""
    claim_dir = tmp_path / "svc" / "dispatch-claims"
    claim_dir.mkdir(parents=True)
    dispatch_fence.hold(str(claim_dir), KEY)
    yield claim_dir, dispatch_ack.journal_dir_beside(claim_dir)
    dispatch_fence.release()
    land_intent._held = None


@pytest.fixture
def land_repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    _init(root)
    _write(root, "README.md", "seed\n")
    _write(root, PLAN, "---\ntitle: p\n---\nbody\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("src/a.py",)),), plan_path=PLAN)
    _write(root, SCRIPT, "// emitted\n" + render_marker(request) + "\n")
    _write(root, "src/a.py", "a = 1\n")
    session_scope.touch(SELF, "src/a.py", cwd=str(root))
    sink = touch_record.sink_path(session_dir_for(root / ".git", SELF))
    touch_record.append_event(
        sink, session_id=SELF, agent_id=None, verb="T", path="src/a.py",
        content_hash=touch_record.compute_content_hash(root / "src/a.py"), kind="w",
    )
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/run.json", json.dumps({"run_id": RUN}))
    _write(root, f"{RECORD_DIR_ROOT}/{RUN}/ask.md", "ask\n")
    return root


@pytest.fixture
def tc_repo(tmp_path):
    root = tmp_path / "tcrepo"
    _init(root)
    (tmp_path / "wave-review.md").write_text("# review\n", encoding="utf-8")
    _write(root, "README.md", "seed\n")
    sizing = "state/sizings/20260101-a.yaml"
    _write(root, sizing, "\n".join([
        "schema: sizing-object", "intent: Test intent, verbatim.", "estimate:", "  tshirt: XS",
        "  provisional: true", "route: dispatch", "detents: []", "fork: null", "xl_exit: null",
        "status: routed", "premise:", "  provenance: read", "  evidence: test fixture", "",
    ]))
    _write(root, PLAN, f"---\nsizing_object: {sizing}\n---\n# p\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=PLAN)
    _write(root, TC_SCRIPT, render_marker(request) + "\n")
    _write(root, "a.py", "a\n")
    return root


def _fire_land(root: Path, token: str) -> dict:
    params = {
        "script_path": SCRIPT, "incomplete_chunks": [], "inline_review": dict(reviewed()),
        "session_id": SELF, "run_id": RUN, "run_token": token,
    }
    return land_run._handler(params, repo_root=root / ".git")


def _fire_terminal(root: Path, token: str) -> dict:
    params = {
        "script_path": TC_SCRIPT, "incomplete_chunks": [], "inline_review": _TC_REVIEWED,
        "run_id": TC_RUN, "run_token": token,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


# Each path: (repo fixture, mint(root) -> token, fire(root, token), the module owning _settle_token, run id, script).
_PATHS = {
    "land_run": ("land_repo", lambda r: run_token.mint(r, RUN, SCRIPT, session_id=SELF), _fire_land, land_run, RUN, SCRIPT),
    "terminal_commit": ("tc_repo", lambda r: run_token.mint(r, TC_RUN, TC_SCRIPT), _fire_terminal, terminal_commit, TC_RUN, TC_SCRIPT),
}


@pytest.fixture(params=sorted(_PATHS))
def path(request):
    return request.getfixturevalue(_PATHS[request.param][0]), _PATHS[request.param], request.param


def _phases(monkeypatch) -> list:
    seen: list = []
    real = land_intent.mark
    monkeypatch.setattr(land_intent, "mark", lambda phase, sha=None, **fields: (seen.append(phase), real(phase, sha, **fields))[1])
    return seen


def _record(journal) -> dict:
    record = dispatch_ack.read_land_intent(journal, KEY)
    assert record is not None
    return record


def test_a_landed_call_walks_claimed_committing_committed_with_the_sha(path, keyed, monkeypatch):
    root, (_f, mint, fire, _mod, run_id, script), kind = path
    _claim, journal = keyed
    seen = _phases(monkeypatch)
    token = mint(root)

    out = fire(root, token)

    assert out["committed"] is True, out
    assert seen == ["committing", "committed"]
    record = _record(journal)
    assert record["phase"] == "committed" and record["sha"] == out["sha"]
    assert (record["kind"], record["run_id"], record["token"], record["script_path"]) == (kind, run_id, token, script)
    assert record["repo_root"] == str(root)


def test_a_death_before_the_commit_spawn_resolves_never_ran(path, keyed, monkeypatch):
    root, (_f, mint, fire, _mod, _r, _s), _k = path
    _claim, journal = keyed
    real = land_intent.mark

    def _die_at_committing(phase, sha=None, **fields):
        if phase == land_intent.PHASE_COMMITTING:
            raise _Death()
        real(phase, sha, **fields)

    monkeypatch.setattr(land_intent, "mark", _die_at_committing)
    head = _git(root, "rev-parse", "HEAD")
    token = mint(root)

    with pytest.raises(_Death):
        fire(root, token)

    assert _git(root, "rev-parse", "HEAD") == head
    record = _record(journal)
    assert record["phase"] == "claimed" and "sha" not in record
    assert land_intent.resolve(record) == {"verdict": "never-ran"}


def test_a_death_after_the_commit_before_the_receipt_is_spent_resolves_committed(path, keyed, monkeypatch):
    root, (_f, mint, fire, mod, run_id, script), _k = path
    _claim, journal = keyed

    def _die_when_landed(token_state, *, landed, receipt=None):
        if landed:
            raise _Death()

    monkeypatch.setattr(mod, "_settle_token", _die_when_landed)
    token = mint(root)

    with pytest.raises(_Death):
        fire(root, token)

    head = _git(root, "rev-parse", "HEAD").strip()
    assert run_token.spent_receipt(root, run_id, token, script) is None  # the receipt alone proves nothing
    record = _record(journal)
    assert record["phase"] == "committed"
    assert land_intent.resolve(record) == {"verdict": "committed", "sha": head}


def test_a_death_inside_the_commit_is_unresolved_never_never_ran(path, keyed, monkeypatch):
    root, (_f, mint, fire, _mod, _r, _s), _k = path
    _claim, journal = keyed
    real = land_intent.mark

    def _die_after_committing(phase, sha=None, **fields):
        real(phase, sha, **fields)
        if phase == land_intent.PHASE_COMMITTING:
            raise _Death()

    monkeypatch.setattr(land_intent, "mark", _die_after_committing)
    token = mint(root)

    with pytest.raises(_Death):
        fire(root, token)

    record = _record(journal)
    assert record["phase"] == "committing"
    assert land_intent.resolve(record) is None


def test_a_call_outside_a_keyed_dispatch_writes_no_record(path, tmp_path):
    root, (_f, mint, fire, _mod, _r, _s), _k = path
    token = mint(root)

    out = fire(root, token)

    assert out["committed"] is True, out
    assert not list(tmp_path.rglob("*.land.json"))


def test_a_call_that_lost_the_claim_writes_no_record(path, keyed):
    root, (_f, mint, fire, _mod, _r, _s), _k = path
    _claim, journal = keyed
    token = mint(root)
    first = fire(root, token)
    assert first["committed"] is True, first
    Path(journal, KEY + ".land.json").unlink()

    repeat = fire(root, token)

    assert repeat["committed"] is True and repeat["already_landed"] is True
    assert dispatch_ack.read_land_intent(journal, KEY) is None


def test_a_record_with_a_sha_not_on_head_is_unresolved(path, keyed):
    root, _p, _k = path
    record = {"repo_root": str(root), "run_id": "x", "token": "y", "script_path": "z",
              "phase": "committed", "sha": "0" * 40}
    assert land_intent.resolve(record) is None


def test_a_record_without_a_repo_root_is_unresolved():
    assert land_intent.resolve({"phase": "claimed"}) is None
