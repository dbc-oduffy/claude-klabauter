"""agent_leg_remote: placement, local fallback, diff apply to declared writes only, receipts and the far-side ops."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import run_delta
from coordinator_core.ops import agent_leg_remote as alr
from coordinator_core.ops import test_leg_remote as tlr

SHA = "a" * 40
BOX = {"box_id": "gipsy", "transport": {"kind": "ssh", "host": "gipsy", "invoke": "/x/coordinator-invoke"}}
CHOSEN = {"box": BOX, "box_id": "gipsy", "local": False, "load_pct": 10.0, "local_load_pct": 80.0, "reason": "remote_below_local"}
LOCAL = {"box": None, "box_id": "machine-a", "local": True, "load_pct": 80.0, "local_load_pct": 80.0, "reason": "no_remote_below_local"}


def _git(root: Path, *args: str, data: bytes = b"") -> bytes:
    done = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          input=data, capture_output=True, check=True)
    return done.stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.py").write_text("one\n", encoding="utf-8")
    (root / "b.py").write_text("keep\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _diff_of(root: Path, **edits: str) -> bytes:
    for name, text in edits.items():
        (root / (name + ".py")).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    diff = _git(root, "diff", "--binary", "--cached", "HEAD")
    _git(root, "reset", "-q", "--hard", "HEAD")
    return diff


def _bundle(root=None):
    patch, files = b"", {}
    return {"head_sha": SHA, "patch": patch, "files": files, "digest": run_delta._digest(SHA, patch, files)}


@pytest.fixture
def leg(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(run_delta, "bundle", lambda r, run_id: _bundle())
    prompt = tmp_path / "executor.prompt"
    prompt.write_text("do the row", encoding="utf-8")
    out = tmp_path / "run" / "executor.result.json"
    argv = ["--leg", "executor", "--prompt-file", str(prompt), "--out", str(out), "--repo-root", str(repo),
            "--writes", "a.py", "c.py"]
    return type("Leg", (), {"argv": argv, "out": out, "repo": repo, "prompt": prompt})


def _remote(replies, calls):
    def remote(box, op, stdin_bytes, *, timeout):
        calls.append((op, json.loads(stdin_bytes)))
        step = replies.pop(0)
        return step(op) if callable(step) else step

    return remote


def _ok(reply):
    return {"ok": True, "reply": reply}


def _done(reply, diff=b""):
    return _ok({"ok": True, "state": "done", "rc": 0,
                "verdict": {"reply": reply, "diff_b64": base64.b64encode(diff).decode()}})


def _run(argv, chooser, remote):
    ticks = iter(range(0, 100_000, 1))
    return alr.main(argv, chooser=lambda leg_name: chooser, remote=remote, clock=lambda: next(ticks), sleep=lambda s: None)


def _receipt(leg):
    return json.loads((leg.out.parent / "executor-placement.json").read_text(encoding="utf-8"))


def test_executor_places_remote_and_applies_diff_to_declared_writes_only(leg, capsys):
    diff = _diff_of(leg.repo, a="two\n", b="changed\n")
    calls: list = []
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "running"}), _done("DONE: C1", diff)], calls)
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert (leg.repo / "a.py").read_text(encoding="utf-8") == "two\n"
    assert (leg.repo / "b.py").read_text(encoding="utf-8") == "keep\n"
    body = json.loads(leg.out.read_text(encoding="utf-8"))
    assert (body["placed"], body["reply"], body["box_id"], body["fallback"]) == (True, "DONE: C1", "gipsy", None)
    assert json.loads(capsys.readouterr().out) == body
    rec = _receipt(leg)
    assert (rec["box_id"], rec["load_pct"], rec["local_load_pct"], rec["fallback"]) == ("gipsy", 10.0, 80.0, None)
    assert (rec["head_sha"], rec["delta_digest"], rec["dropped_paths"]) == (SHA, _bundle()["digest"], ["b.py"])
    assert [c[0] for c in calls] == ["agent_leg.remote_run", "agent_leg.remote_poll", "agent_leg.remote_poll"]
    assert calls[0][1]["prompt"] == "do the row" and calls[0][1]["writes"] == ["a.py", "c.py"]


def test_review_places_remote_with_structured_reply_and_no_diff(tmp_path, repo, monkeypatch):
    monkeypatch.setattr(run_delta, "bundle", lambda r, run_id: _bundle())
    prompt = tmp_path / "review.prompt"
    prompt.write_text("review it", encoding="utf-8")
    out = tmp_path / "run" / "review.result.json"
    verdict = {"verdict": "ok"}
    calls: list = []
    remote = _remote([_ok({"ok": True, "job": "j"}), _done(verdict)], calls)
    argv = ["--leg", "review", "--prompt-file", str(prompt), "--out", str(out), "--repo-root", str(repo),
            "--schema-file", str(_schema_file(tmp_path))]
    assert _run(argv, CHOSEN, remote) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["reply"] == verdict
    assert json.loads((out.parent / "review-placement.json").read_text(encoding="utf-8"))["fallback"] is None
    assert calls[0][1]["schema"] == {"type": "object"} and calls[0][1]["leg"] == "review"


def _schema_file(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    return path


def test_local_choice_reports_unplaced_with_receipt_and_no_remote_call(leg, capsys):
    remote = _remote([], [])
    assert _run(leg.argv, LOCAL, remote) == 0
    body = json.loads(leg.out.read_text(encoding="utf-8"))
    assert (body["placed"], body["fallback"], body["reason"]) == (False, "local", "no_remote_below_local")
    rec = _receipt(leg)
    assert (rec["fallback"], rec["reason"], rec["head_sha"]) == ("local", "no_remote_below_local", None)
    assert json.loads(capsys.readouterr().out)["placed"] is False


def test_executor_without_declared_writes_is_never_placed_and_never_asks_the_chooser(leg):
    argv = [a for a in leg.argv[:-3]]
    asked: list = []

    def chooser(leg_name):
        asked.append(leg_name)
        return CHOSEN

    ticks = iter(range(100))
    alr.main(argv, chooser=chooser, remote=_remote([], []), clock=lambda: next(ticks), sleep=lambda s: None)
    assert asked == []
    assert json.loads(leg.out.read_text(encoding="utf-8"))["reason"] == "no_declared_writes"


def test_unreachable_remote_reports_local_fallback_with_the_transport_reason(leg):
    remote = _remote([{"ok": False, "state": "unknown", "reason": "ssh_exit_255"}], [])
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert json.loads(leg.out.read_text(encoding="utf-8"))["placed"] is False
    rec = _receipt(leg)
    assert (rec["fallback"], rec["reason"], rec["box_id"]) == ("local", "ssh_exit_255", "gipsy")


def test_far_box_without_agent_cli_falls_back_with_that_reason(leg):
    remote = _remote([_ok({"ok": False, "reason": "no-agent-cli"})], [])
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert _receipt(leg)["reason"] == "no-agent-cli"


def test_diff_that_does_not_apply_falls_back_local_and_leaves_the_tree_alone(leg):
    diff = _diff_of(leg.repo, a="two\n")
    (leg.repo / "a.py").write_text("peer edit\n", encoding="utf-8")
    remote = _remote([_ok({"ok": True, "job": "j"}), _done("DONE", diff)], [])
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert (leg.repo / "a.py").read_text(encoding="utf-8") == "peer edit\n"
    rec = _receipt(leg)
    assert rec["fallback"] == "local" and rec["reason"].startswith("diff_apply_failed:")
    assert json.loads(leg.out.read_text(encoding="utf-8"))["placed"] is False


def test_sha_absent_ships_a_git_bundle_once(leg, monkeypatch):
    monkeypatch.setattr(tlr, "_git_bundle", lambda root, have: b"packed")
    calls: list = []
    remote = _remote([_ok({"ok": False, "reason": "sha_absent", "have": None}), _ok({"ok": True, "job": "j"}),
                      _done("DONE", b"")], calls)
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert "git_bundle_b64" not in calls[0][1] and base64.b64decode(calls[1][1]["git_bundle_b64"]) == b"packed"
    assert json.loads(leg.out.read_text(encoding="utf-8"))["placed"] is True


def test_lost_remote_job_falls_back(leg):
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "lost"})], [])
    assert _run(leg.argv, CHOSEN, remote) == 0
    assert _receipt(leg)["reason"] == "remote_job_lost"


def test_missing_required_argument_is_a_usage_error(tmp_path):
    assert alr.main(["--leg", "executor"]) == alr.EXIT_USAGE
    assert alr.main(["--leg", "bogus", "--prompt-file", "p", "--out", str(tmp_path / "o.json")]) == alr.EXIT_USAGE


def test_apply_diff_keeps_only_declared_paths(repo):
    diff = _diff_of(repo, a="two\n", b="changed\n")
    applied, dropped, error = alr.apply_diff(repo, diff, ["a.py"])
    assert (applied, dropped, error) == (["a.py"], ["b.py"], "")
    assert (repo / "b.py").read_text(encoding="utf-8") == "keep\n"
    assert alr.apply_diff(repo, diff, ["zzz.py"])[0] == []


def test_reply_of_prefers_structured_output_and_tails_text():
    assert alr._reply_of({"structured_output": {"v": 1}, "result": "x"}, "review", {"type": "object"}) == {"v": 1}
    assert alr._reply_of({"result": json.dumps({"v": 2})}, "review", {"type": "object"}) == {"v": 2}
    assert alr._reply_of({"result": "not json"}, "review", {"type": "object"}) is None
    assert alr._reply_of({"result": "z" * 20_000}, "executor", None) == "z" * alr.REPLY_TAIL_CHARS


@pytest.fixture
def far(tmp_path, repo, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    spawned: list = []
    monkeypatch.setattr(tlr, "_scratch_root", lambda: scratch)
    monkeypatch.setattr(alr, "_agent_cli", lambda: "/far/claude")
    monkeypatch.setattr(tlr, "_spawn_detached", lambda argv, jdir: spawned.append((argv, jdir)) or 4321)
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    bundle = {"head_sha": head, "patch": b"", "files": {}, "digest": run_delta._digest(head, b"", {})}
    params = {"leg": "executor", "prompt": "p", "schema": None, "model": None, "budget_s": 60,
              "head_sha": head, "digest": bundle["digest"], "patch_b64": "", "files_b64": {}}
    return type("Far", (), {"scratch": scratch, "spawned": spawned, "params": params, "repo": repo})


def test_remote_run_starts_a_detached_job_and_poll_reads_its_result(far, monkeypatch):
    monkeypatch.setattr(tlr, "_alive", lambda pid: True)
    reply = alr.remote_run(far.params, repo_root=far.repo)
    assert reply["ok"] is True and far.spawned[0][0][-2] == "--far-wrap"
    job = reply["job"]
    assert alr.remote_poll({"job": job})["state"] == "running"
    jdir = tlr._job_dir(far.scratch, job)
    (jdir / "result.json").write_text(json.dumps({"reply": "DONE", "diff_b64": ""}), encoding="utf-8")
    (jdir / "exit.json").write_text(json.dumps({"rc": 0, "stage": "done"}), encoding="utf-8")
    done = alr.remote_poll({"job": job})
    assert (done["state"], done["verdict"]["reply"]) == ("done", "DONE")


def test_remote_run_refuses_a_missing_agent_cli_a_bad_digest_and_an_absent_sha(far, monkeypatch):
    monkeypatch.setattr(alr, "_agent_cli", lambda: None)
    assert alr.remote_run(far.params, repo_root=far.repo) == {"ok": False, "reason": "no-agent-cli"}
    monkeypatch.setattr(alr, "_agent_cli", lambda: "/far/claude")
    assert alr.remote_run({**far.params, "digest": "0" * 64}, repo_root=far.repo)["reason"] == "digest_mismatch"
    gone = "b" * 40
    absent = {**far.params, "head_sha": gone, "digest": run_delta._digest(gone, b"", {})}
    reply = alr.remote_run(absent, repo_root=far.repo)
    assert (reply["ok"], reply["reason"]) == (False, "sha_absent")
    assert alr.remote_run({"leg": "bogus"}, repo_root=far.repo)["reason"] == "bad_params"
    assert far.spawned == []


def test_remote_run_without_the_scratch_root_key_is_a_typed_refusal_naming_it(far, monkeypatch):
    monkeypatch.setattr(tlr, "_scratch_root", lambda: None)
    reply = alr.remote_run(far.params, repo_root=far.repo)
    assert reply == {"ok": False, "reason": "settings-key-missing: paths.scratch_root"}
    assert alr.remote_poll({"job": "0123456789abcdef-01234567"})["reason"].startswith("settings-key-missing")


def test_remote_poll_reports_a_dead_job_lost(far, monkeypatch):
    job = alr.remote_run(far.params, repo_root=far.repo)["job"]
    monkeypatch.setattr(tlr, "_alive", lambda pid: False)
    assert alr.remote_poll({"job": job})["state"] == "lost"
    assert alr.remote_poll({"job": "../x"})["reason"] == "bad_params"
