"""test_leg_remote: placement, local fallback, receipts, and the far-side ops."""
from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.group_em import run_delta
from coordinator_core.ops import test_leg_remote as tlr
from coordinator_core.ops import test_leg_run

SHA = "a" * 40
BOX = {"box_id": "gipsy", "transport": {"kind": "ssh", "host": "gipsy", "invoke": "/x/coordinator-invoke"}}
CHOSEN = {"box": BOX, "box_id": "gipsy", "local": False, "load_pct": 10.0, "local_load_pct": 80.0, "reason": "remote_below_local"}
LOCAL = {"box": None, "box_id": "machine-a", "local": True, "load_pct": 80.0, "local_load_pct": 80.0, "reason": "no_remote_below_local"}
VERDICT = {"status": "pass", "tests_run": 3, "over_budget": False}


def _bundle():
    patch, files = b"diff", {"a.py": b"x"}
    return {"head_sha": SHA, "patch": patch, "files": files, "digest": run_delta._digest(SHA, patch, files)}


@pytest.fixture
def leg(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    out = tmp_path / "run" / "tests.json"
    local_calls: list = []

    def local_main(argv):
        local_calls.append(list(argv))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"status": "local"}) + "\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(test_leg_run, "main", local_main)
    monkeypatch.setattr(run_delta, "bundle", lambda r, run_id: _bundle())
    argv = ["--repo-root", str(root), "--out", str(out), "--", "pkg/test_a.py"]
    return type("Leg", (), {"argv": argv, "out": out, "local": local_calls, "root": root})


def _receipt(leg):
    return json.loads((leg.out.parent / tlr.RECEIPT_NAME).read_text(encoding="utf-8"))


def _remote(replies, calls):
    def remote(box, op, stdin_bytes, *, timeout):
        calls.append((op, json.loads(stdin_bytes)))
        step = replies.pop(0)
        return step(op) if callable(step) else step

    return remote


def _ok(reply):
    return {"ok": True, "reply": reply}


def _run(leg, chooser, remote, ticks=None):
    clock = iter(ticks) if ticks else iter(range(0, 10_000, 1))
    return tlr.main(leg.argv, chooser=lambda leg_name: chooser, remote=remote, clock=lambda: next(clock), sleep=lambda s: None)


def test_remote_run_writes_verdict_and_receipt_without_running_local(leg, capsys):
    calls: list = []
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "running"}),
                      _ok({"ok": True, "state": "done", "rc": 0, "verdict": VERDICT})], calls)
    assert _run(leg, CHOSEN, remote) == 0
    assert leg.local == []
    assert json.loads(leg.out.read_text(encoding="utf-8")) == VERDICT
    rec = _receipt(leg)
    assert (rec["box_id"], rec["load_pct"], rec["local_load_pct"], rec["fallback"]) == ("gipsy", 10.0, 80.0, None)
    assert (rec["head_sha"], rec["delta_digest"]) == (SHA, _bundle()["digest"])
    assert [c[0] for c in calls] == ["test_leg.remote_run", "test_leg.remote_poll", "test_leg.remote_poll"]
    assert calls[0][1]["targets"] == ["pkg/test_a.py"] and calls[0][1]["files_b64"] == {"a.py": base64.b64encode(b"x").decode()}
    assert json.loads(capsys.readouterr().out) == VERDICT


def test_over_budget_verdict_exits_timeout(leg):
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "done", "verdict": {**VERDICT, "over_budget": True}})], [])
    assert _run(leg, CHOSEN, remote) == test_leg_run.EXIT_TIMEOUT


def test_unreachable_remote_falls_back_to_local_once_with_reason(leg):
    calls: list = []
    remote = _remote([{"ok": False, "state": "unknown", "reason": "ssh_exit_255"}], calls)
    assert _run(leg, CHOSEN, remote) == 0
    assert len(calls) == 1 and len(leg.local) == 1
    rec = _receipt(leg)
    assert (rec["fallback"], rec["reason"], rec["box_id"]) == ("local", "ssh_exit_255", "gipsy")
    assert "--budget-s" in leg.local[0] and leg.local[0].count("--budget-s") == 1


def test_fallback_rebudgets_an_explicit_budget(leg):
    leg.argv[:0] = ["--budget-s", "500"]
    remote = _remote([{"ok": False, "state": "unknown", "reason": "ssh_timeout"}], [])
    _run(leg, CHOSEN, remote, ticks=[0, 100, 100, 100, 100, 100])
    argv = leg.local[0]
    assert argv.count("--budget-s") == 1 and argv[argv.index("--budget-s") + 1] == "400"


def test_chooser_local_runs_local_without_a_remote_call(leg):
    calls: list = []
    assert _run(leg, LOCAL, _remote([], calls)) == 0
    assert calls == [] and leg.local == [leg.argv]
    rec = _receipt(leg)
    assert (rec["fallback"], rec["reason"], rec["box_id"]) == ("local", "no_remote_below_local", "machine-a")


@pytest.mark.parametrize("flag", ["--base-sha", "--head-sha", "--touched", "--measured", "--measured-tree", "--base-export-out"])
def test_history_dependent_flags_run_local_and_never_ask_the_chooser(leg, flag):
    leg.argv[:0] = [flag] if flag in ("--measured", "--measured-tree") else [flag, "v"]

    def chooser(name):
        raise AssertionError("chooser must not run")

    assert tlr.main(leg.argv, chooser=chooser, remote=_remote([], []), clock=lambda: 0.0, sleep=lambda s: None) == 0
    assert _receipt(leg)["reason"] == f"remote_ineligible:{flag}"


def test_unknown_flag_runs_local(leg):
    leg.argv[:0] = ["--surprise"]
    assert _run(leg, CHOSEN, _remote([], [])) == 0
    assert _receipt(leg)["reason"] == "remote_ineligible:unsupported_args"


def test_missing_out_delegates_usage_to_test_leg_run(leg):
    assert tlr.main(["--", "t.py"], chooser=lambda n: pytest.fail("chooser"), remote=_remote([], [])) == 0
    assert not (leg.out.parent / tlr.RECEIPT_NAME).exists()


def test_absent_sha_ships_a_git_bundle_then_runs_remote(leg, monkeypatch):
    monkeypatch.setattr(tlr, "_git_bundle", lambda root, have: b"PACK")
    calls: list = []
    remote = _remote([_ok({"ok": False, "reason": "sha_absent", "have": SHA}), _ok({"ok": True, "job": "j"}),
                      _ok({"ok": True, "state": "done", "verdict": VERDICT})], calls)
    assert _run(leg, CHOSEN, remote) == 0
    assert "git_bundle_b64" not in calls[0][1]
    assert base64.b64decode(calls[1][1]["git_bundle_b64"]) == b"PACK"
    assert _receipt(leg)["fallback"] is None


def test_unbuildable_git_bundle_falls_back(leg, monkeypatch):
    monkeypatch.setattr(tlr, "_git_bundle", lambda root, have: None)
    assert _run(leg, CHOSEN, _remote([_ok({"ok": False, "reason": "sha_absent"})], [])) == 0
    assert _receipt(leg)["reason"] == "sha_absent_bundle_failed"


def test_far_refusal_falls_back_with_its_reason(leg):
    assert _run(leg, CHOSEN, _remote([_ok({"ok": False, "reason": "digest_mismatch"})], [])) == 0
    assert _receipt(leg)["reason"] == "digest_mismatch"


def test_lost_job_falls_back(leg):
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "lost"})], [])
    assert _run(leg, CHOSEN, remote) == 0
    assert _receipt(leg)["reason"] == "remote_job_lost"


def test_repeated_poll_failures_fall_back(leg):
    bad = {"ok": False, "state": "unknown", "reason": "rate_limited"}
    remote = _remote([_ok({"ok": True, "job": "j"}), bad, bad, bad], [])
    assert _run(leg, CHOSEN, remote) == 0
    assert _receipt(leg)["reason"] == "poll_failed:rate_limited"


def test_one_failed_poll_is_retried(leg):
    bad = {"ok": False, "state": "unknown", "reason": "ssh_timeout"}
    remote = _remote([_ok({"ok": True, "job": "j"}), bad, _ok({"ok": True, "state": "done", "verdict": VERDICT})], [])
    assert _run(leg, CHOSEN, remote) == 0 and leg.local == []


def test_poll_deadline_falls_back(leg):
    remote = _remote([_ok({"ok": True, "job": "j"})], [])
    ticks = [0, 10_000, 10_000, 10_000, 10_000]
    assert _run(leg, CHOSEN, remote, ticks=ticks) == 0
    assert _receipt(leg)["reason"] == "remote_timeout"


def test_done_without_verdict_falls_back(leg):
    remote = _remote([_ok({"ok": True, "job": "j"}), _ok({"ok": True, "state": "done", "stage": "apply", "verdict": None})], [])
    assert _run(leg, CHOSEN, remote) == 0
    assert _receipt(leg)["reason"] == "no_verdict:apply"


def test_bundle_failure_falls_back(leg, monkeypatch):
    def boom(root, run_id):
        raise RuntimeError("git diff failed")

    monkeypatch.setattr(run_delta, "bundle", boom)
    assert _run(leg, CHOSEN, _remote([], [])) == 0
    assert _receipt(leg)["reason"].startswith("bundle_failed:RuntimeError")


def test_chooser_exception_runs_local(leg):
    def chooser(name):
        raise RuntimeError("x")

    assert tlr.main(leg.argv, chooser=chooser, remote=_remote([], []), clock=lambda: 0.0, sleep=lambda s: None) == 0
    assert _receipt(leg)["reason"] == "chooser_failed:RuntimeError"


def test_non_git_repo_root_falls_back(leg):
    (leg.root / ".git").rmdir()
    assert _run(leg, CHOSEN, _remote([], [])) == 0
    assert "is not a git checkout" in _receipt(leg)["reason"]


@pytest.fixture
def far(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tlr, "_scratch_root", lambda: scratch)
    repo = tmp_path / "far-repo"
    repo.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "f.txt").write_text("1", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), *ident, "commit", "-q", "-m", "c"], check=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return type("Far", (), {"scratch": scratch, "repo": repo, "head": head})


def _params(head, **over):
    b = {**_bundle(), "head_sha": head}
    b["digest"] = run_delta._digest(head, b["patch"], b["files"])
    params = {"head_sha": head, "digest": b["digest"], "patch_b64": base64.b64encode(b["patch"]).decode(),
              "files_b64": {p: base64.b64encode(v).decode() for p, v in b["files"].items()},
              "targets": ["pkg/test_a.py"], "budget_s": 60}
    return {**params, **over}


def test_remote_run_starts_a_detached_job(far, monkeypatch):
    spawned: list = []
    monkeypatch.setattr(tlr, "_spawn_detached", lambda argv, jdir: spawned.append(argv) or 4242)
    reply = tlr.remote_run(_params(far.head), far.repo)
    assert reply["ok"] is True
    jdir = far.scratch / f"leg-{reply['job']}"
    assert json.loads((jdir / "job.json").read_text(encoding="utf-8"))["targets"] == ["pkg/test_a.py"]
    assert (jdir / "pid").read_text(encoding="utf-8") == "4242"
    assert spawned[0][-2:] == ["--far-wrap", str(jdir)]


def test_remote_run_refuses_a_tampered_digest(far):
    assert tlr.remote_run(_params(far.head, digest="0" * 64), far.repo) == {"ok": False, "reason": "digest_mismatch"}


@pytest.mark.parametrize("over", [{"targets": []}, {"head_sha": "zz"}, {"budget_s": "x"}, {"patch_b64": None}])
def test_remote_run_refuses_bad_params(far, over):
    assert tlr.remote_run(_params(far.head, **over), far.repo) == {"ok": False, "reason": "bad_params"}


def test_remote_run_names_a_missing_scratch_key(far, monkeypatch):
    monkeypatch.setattr(tlr, "_scratch_root", lambda: None)
    assert tlr.remote_run(_params(far.head), far.repo) == {"ok": False, "reason": "settings-key-missing: paths.scratch_root"}


def test_remote_run_asks_for_a_bundle_when_the_sha_is_absent(far):
    reply = tlr.remote_run(_params("b" * 40), far.repo)
    assert reply == {"ok": False, "reason": "sha_absent", "have": far.head}


def test_remote_run_refuses_an_unusable_bundle(far):
    params = _params("b" * 40, git_bundle_b64=base64.b64encode(b"not a bundle").decode())
    assert tlr.remote_run(params, far.repo) == {"ok": False, "reason": "bundle_unusable"}


def _job(far, name="0123456789abcdef-01234567"):
    jdir = far.scratch / f"leg-{name}"
    jdir.mkdir()
    return name, jdir


def test_poll_reports_running_lost_done_and_unknown(far, monkeypatch):
    job, jdir = _job(far)
    assert tlr.remote_poll({"job": job}) == {"ok": True, "state": "running"}
    (jdir / "pid").write_text("77", encoding="utf-8")
    monkeypatch.setattr(tlr, "_alive", lambda pid: True)
    assert tlr.remote_poll({"job": job})["state"] == "running"
    monkeypatch.setattr(tlr, "_alive", lambda pid: False)
    assert tlr.remote_poll({"job": job})["state"] == "lost"
    (jdir / "tests.json").write_text(json.dumps(VERDICT), encoding="utf-8")
    (jdir / "exit.json").write_text(json.dumps({"rc": 0, "stage": "run"}), encoding="utf-8")
    assert tlr.remote_poll({"job": job}) == {"ok": True, "state": "done", "rc": 0, "stage": "run", "verdict": VERDICT}
    assert tlr.remote_poll({"job": "ffffffffffffffff-ffffffff"}) == {"ok": False, "reason": "unknown_job"}


@pytest.mark.parametrize("job", ["../../etc", "x", None, "0123456789abcdef-0123456/"])
def test_poll_refuses_a_job_id_outside_the_shape(far, job):
    assert tlr.remote_poll({"job": job}) == {"ok": False, "reason": "bad_params"}


def test_done_without_a_verdict_file_reports_none(far):
    job, jdir = _job(far)
    (jdir / "exit.json").write_text(json.dumps({"rc": -1, "stage": "apply"}), encoding="utf-8")
    assert tlr.remote_poll({"job": job})["verdict"] is None


def test_wrap_records_the_failed_stage(far):
    _, jdir = _job(far)
    (jdir / "job.json").write_text("{}", encoding="utf-8")
    assert tlr._wrap(jdir) == 0
    assert json.loads((jdir / "exit.json").read_text(encoding="utf-8")) == {"rc": -1, "stage": "apply"}
    assert (jdir / "wrap.err").is_file()


def test_entry_routes_far_wrap(far, monkeypatch):
    seen: list = []
    monkeypatch.setattr(tlr, "_wrap", lambda p: seen.append(p) or 0)
    assert tlr._entry(["--far-wrap", str(far.scratch)]) == 0 and seen == [Path(far.scratch)]


def test_far_ops_are_allowlisted_transport_ops():
    from coordinator_core.group_em import box_transport

    assert {"test_leg.remote_run", "test_leg.remote_poll"} <= box_transport.ALLOWED_OPS
