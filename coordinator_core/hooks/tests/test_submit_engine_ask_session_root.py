"""submit_engine_ask._repo_root: the session anchor's repo wins over a nested-clone shell cwd."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.bash_guards import _write_bump_applicability as wba
from coordinator_core.hooks import submit_engine_ask as sea
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "sess-root-anchor-test"


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q"], cwd=str(path), check=True, capture_output=True, **no_console_creationflags()
    )


@pytest.fixture
def repos(tmp_path):
    a = tmp_path / "A"
    _init(a)
    clone = a / "scratch" / "clone"
    _init(clone)
    return a.resolve(), clone


def _anchor(monkeypatch, value):
    seen = []

    def fake(session_id, cwd=None, env=None):
        seen.append(session_id)
        return value

    monkeypatch.setattr(wba, "resolve_launch_anchor", fake)
    return seen


def test_session_anchor_beats_nested_clone_cwd(repos, monkeypatch):
    a, clone = repos
    _anchor(monkeypatch, str(a))
    assert sea._repo_root(None, {"session_id": SID, "cwd": str(clone)}).resolve() == a


def test_no_anchor_falls_back_to_cwd(repos, monkeypatch):
    a, clone = repos
    _anchor(monkeypatch, None)
    assert sea._repo_root(None, {"session_id": SID, "cwd": str(clone)}).resolve() == clone.resolve()


def test_no_session_id_never_consults_anchor(repos, monkeypatch):
    a, clone = repos
    seen = _anchor(monkeypatch, str(a))
    assert sea._repo_root(None, {"cwd": str(clone)}).resolve() == clone.resolve()
    assert seen == []


def test_relative_repo_arg_resolves_against_anchor(repos, monkeypatch):
    a, clone = repos
    _anchor(monkeypatch, str(a))
    got = sea._repo_root("scratch/clone", {"session_id": SID, "cwd": str(a)})
    assert got.resolve() == clone.resolve()
    other = sea._repo_root("scratch/clone", {"session_id": SID, "cwd": str(clone)})
    assert other.resolve() == clone.resolve()


def test_absolute_repo_arg_wins(repos, monkeypatch):
    a, clone = repos
    seen = _anchor(monkeypatch, str(a))
    got = sea._repo_root(str(clone), {"session_id": SID, "cwd": str(a)})
    assert got.resolve() == clone.resolve()
    assert seen == []


def _child_emit_params(root):
    run = type("R", (), {"slice_sizing": "state/sizings/x.yaml", "run_id": "ask-1"})()
    return sea._child_params(root, {"args": {}}, "sid", run, None, False)


def test_child_emit_finds_its_sizing_in_the_submitting_repo_not_the_engine_root(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.op import _sizing_root
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing

    sub = tmp_path / "submitter"
    engine = tmp_path / "engine"
    _init(sub)
    _init(engine)
    (sub / "state" / "sizings").mkdir(parents=True)
    (sub / "state" / "sizings" / "x.yaml").write_text("schema: sizing-object\nname: x\n", encoding="utf-8")
    monkeypatch.setattr(sea, "_load_sizing_doc", lambda *a: {})
    monkeypatch.chdir(engine)

    params = _child_emit_params(sub)
    root = _sizing_root(params, None, params["sizing_path"])
    assert root == sub
    load_sizing(root, params["sizing_path"])

    bare = {k: v for k, v in params.items() if k != "target_root"}
    with pytest.raises(SizingFireRefused):
        load_sizing(_sizing_root(bare, None, bare["sizing_path"]), bare["sizing_path"])


def test_claude_project_dir_from_payload_env_anchors(repos, monkeypatch):
    a, clone = repos
    monkeypatch.setattr(wba, "read_session_start_record", lambda *args, **kw: None)
    payload = {"session_id": SID, "cwd": str(clone), "env": {"CLAUDE_PROJECT_DIR": str(a)}}
    assert sea._repo_root(None, payload).resolve() == a
