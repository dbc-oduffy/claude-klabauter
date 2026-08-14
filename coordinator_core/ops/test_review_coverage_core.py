"""
test_review_coverage_core.py — pytest unit tests for
coordinator_core.ops.review_coverage_core.

Independent parity derivation: builds real git fixtures (bare origin + working
clone) and asserts against ground truth computed directly from `git rev-list`
output in each test, rather than mirroring the bash oracle's assertions
verbatim.

Port of: review-coverage-core.test.sh (DoE c6d97219, 2026-07-22) — the bash
oracle's assertions on the CLI's byte-shape (WARN/ERROR text, exit codes).

Spec backlink: docs/plans/2026-06-23-chain-end-review-coverage-gate.md § C2
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.review_coverage_core import main

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Fixture helpers — bare origin + working clone (mirrors the bash oracle's
# _make_fixture / _add_commit so a real `git rev-list origin/main..HEAD`
# range is resolvable).
# ---------------------------------------------------------------------------


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_fixture(base: Path) -> Path:
    origin_dir = base / "origin.git"
    origin_dir.mkdir(parents=True)
    _git("init", "-q", "--bare", cwd=origin_dir)

    seed_dir = base / "seed"
    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(seed_dir)],
        check=True, capture_output=True, text=True,
    )
    _git("config", "user.email", "test@test.local", cwd=seed_dir)
    _git("config", "user.name", "Test", cwd=seed_dir)
    _git("config", "commit.gpgsign", "false", cwd=seed_dir)
    subprocess.run(["git", "-C", str(seed_dir), "checkout", "-q", "-b", "main"],
                    capture_output=True, text=True)
    (seed_dir / ".gitkeep").write_text("")
    _git("add", "--", ".gitkeep", cwd=seed_dir)
    _git("commit", "-q", "-m", "root", cwd=seed_dir)
    _git("push", "-q", "origin", "main", cwd=seed_dir)

    repo_dir = base / "repo"
    subprocess.run(
        ["git", "clone", "-q", str(origin_dir), str(repo_dir)],
        check=True, capture_output=True, text=True,
    )
    _git("config", "user.email", "test@test.local", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)
    _git("config", "commit.gpgsign", "false", cwd=repo_dir)
    subprocess.run(["git", "-C", str(repo_dir), "checkout", "-q", "main"],
                    capture_output=True, text=True)

    (repo_dir / "state" / "review-trail").mkdir(parents=True)
    return repo_dir


def _add_commit(repo: Path, *files: str) -> str:
    for f in files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(f"content of {f}\n")
        _git("add", "--", f, cwd=repo)
    _git("commit", "-q", "-m", f"touch {' '.join(files)}", cwd=repo)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _origin_main(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _write_trail(repo: Path, name: str, obj: dict) -> Path:
    p = repo / "state" / "review-trail" / name
    p.write_text(json.dumps(obj))
    return p


# ---------------------------------------------------------------------------
# --reviewed-set mode
# ---------------------------------------------------------------------------


def test_reviewed_set_injection_shaped_range_rejected(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "src/thing.py")
    trail = _write_trail(
        repo, "unsafe.json",
        {"scope_kind": "diff", "sha_range": "--output=/tmp/evil..HEAD", "artifact": "evil"},
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.strip() == ""
    assert "unsafe sha_range" in err or "failed rev-range validation" in err


def test_reviewed_set_json_single_object(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path)
    sha1 = _add_commit(repo, "src/alpha.py")
    sha2 = _add_commit(repo, "src/beta.py")
    origin = _origin_main(repo)
    trail = _write_trail(
        repo, "single-obj.json",
        {"scope_kind": "diff", "sha_range": f"{origin}..{sha2}", "verdict": "ok", "artifact": "ws"},
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)], cwd=str(repo))
    assert rc == 0

    # independent ground truth
    expected = set(
        subprocess.run(
            ["git", "-C", str(repo), "rev-list", f"{origin}..{sha2}"],
            check=True, capture_output=True, text=True,
        ).stdout.split()
    )
    assert {sha1, sha2} <= expected


def test_reviewed_set_jsonl_dual_shape(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha1 = _add_commit(repo, "src/a.py")
    sha2 = _add_commit(repo, "src/b.py")
    origin = _origin_main(repo)
    trail = repo / "state" / "review-trail" / "jsonl.json"
    trail.write_text(
        json.dumps({"scope_kind": "diff", "sha_range": f"{origin}..{sha2}", "verdict": "warn", "artifact": "ws"})
        + "\n"
        + json.dumps({"scope_kind": "integration", "artifact": "integrator"})
        + "\n"
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    lines = set(out.splitlines())
    assert sha1 in lines and sha2 in lines


@pytest.mark.parametrize("scope_kind", ["plan", "integration"])
def test_reviewed_set_non_diff_scope_kinds_skipped_silently(tmp_path, monkeypatch, capsys, scope_kind):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "docs/x.md")
    trail = _write_trail(repo, "nd.json", {"scope_kind": scope_kind, "artifact": "x"})
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)], cwd=str(repo))
    out, err = capsys.readouterr()
    assert rc == 0
    assert out.strip() == ""
    assert "WARN" not in err


@pytest.mark.parametrize(
    "verdict,included",
    [("pending", False), ("ok", True), ("warn", True), ("blocked", True), ("waived", True), (None, True)],
)
def test_reviewed_set_verdict_filter(tmp_path, monkeypatch, capsys, verdict, included):
    repo = _make_fixture(tmp_path)
    sha = _add_commit(repo, "src/v.py")
    origin = _origin_main(repo)
    obj = {"scope_kind": "diff", "sha_range": f"{origin}..{sha}", "artifact": "v"}
    if verdict is not None:
        obj["verdict"] = verdict
    trail = _write_trail(repo, "v.json", obj)
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    assert (sha in out.splitlines()) == included


def test_reviewed_set_empty_trail_no_crash(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "src/x.py")
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set"], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    assert out == ""


def test_reviewed_set_union_across_records(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha1 = _add_commit(repo, "src/u1.py")
    sha2 = _add_commit(repo, "src/u2.py")
    sha3 = _add_commit(repo, "src/u3.py")
    origin = _origin_main(repo)
    trail_a = _write_trail(
        repo, "a.json", {"scope_kind": "diff", "sha_range": f"{origin}..{sha2}", "verdict": "ok", "artifact": "a"}
    )
    trail_b = _write_trail(
        repo, "b.json", {"scope_kind": "diff", "sha_range": f"{sha2}..{sha3}", "verdict": "ok", "artifact": "b"}
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail_a), str(trail_b)], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    lines = set(out.splitlines())
    assert {sha1, sha2, sha3} <= lines


def test_reviewed_set_unrecognized_scope_kind_degrades_not_fatal(tmp_path, monkeypatch, capsys):
    """2026-08-10 coverage-gate wedge (cross-repo/inbox/2026-08-10-project-
    rag-ue-addon-em-coverage-gate-crashes-on-chunk-and-inline-dispatch-
    kinds.md): a trail record with an unrecognized scope_kind ("inline-
    dispatch") must not take the whole --reviewed-set run down. It earns zero
    credit (its sha is absent from the output) and a WARN naming the kind is
    emitted; a sibling "diff" record in the same run is credited normally."""
    repo = _make_fixture(tmp_path)
    unrecognized_sha = _add_commit(repo, "src/inline.py")
    diff_sha = _add_commit(repo, "src/normal.py")
    trail_bad = _write_trail(
        repo, "bad.json",
        {
            "scope_kind": "inline-dispatch",
            "sha_range": f"{unrecognized_sha}^..{unrecognized_sha}",
            "verdict": "ok",
            "artifact": "bad",
        },
    )
    trail_good = _write_trail(
        repo, "good.json",
        {"scope_kind": "diff", "sha_range": f"{diff_sha}^..{diff_sha}", "verdict": "ok", "artifact": "good"},
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail_bad), str(trail_good)], cwd=str(repo))
    out, err = capsys.readouterr()
    assert rc == 0
    lines = set(out.splitlines())
    assert diff_sha in lines, "the sibling diff record must still be credited normally"
    assert unrecognized_sha not in lines, (
        "an unrecognized scope_kind must credit nothing — fail-closed, not fatal"
    )
    assert "WARN" in err and "inline-dispatch" in err, (
        "the unrecognized kind must be named in a loud WARN to stderr"
    )


def test_reviewed_set_garbage_file_default_fail(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "src/g.py")
    trail = repo / "state" / "review-trail" / "garbage.json"
    trail.write_text('{"a":1}{"b":2}\n')
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", str(trail)], cwd=str(repo))
    assert rc == 1


def test_reviewed_set_garbage_file_skip_processes_sibling(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha = _add_commit(repo, "src/sib.py")
    origin = _origin_main(repo)
    bad = repo / "state" / "review-trail" / "garbage.json"
    bad.write_text('{"a":1}{"b":2}\n')
    good = _write_trail(
        repo, "good.json", {"scope_kind": "diff", "sha_range": f"{origin}..{sha}", "verdict": "ok", "artifact": "g"}
    )
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", "--on-record-error", "skip", str(bad), str(good)], cwd=str(repo))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "WARN" in err
    assert sha in out.splitlines()


def test_reviewed_set_unresolvable_ref_skip_vs_fail(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha = _add_commit(repo, "src/r.py")
    bad_range = f"{sha}..WORKING"

    trail = _write_trail(
        repo, "bad.json", {"scope_kind": "diff", "sha_range": bad_range, "verdict": "ok", "artifact": "bad"}
    )
    monkeypatch.chdir(repo)

    rc_fail = main(["--reviewed-set", "--on-record-error", "fail", str(trail)], cwd=str(repo))
    assert rc_fail == 1

    rc_skip = main(["--reviewed-set", "--on-record-error", "skip", str(trail)], cwd=str(repo))
    out, err = capsys.readouterr()
    assert rc_skip == 0
    assert "WARN" in err


def test_reviewed_set_intersect_filters_output(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha1 = _add_commit(repo, "src/i1.py")
    sha2 = _add_commit(repo, "src/i2.py")
    origin = _origin_main(repo)
    trail = _write_trail(
        repo, "i.json", {"scope_kind": "diff", "sha_range": f"{origin}..{sha2}", "verdict": "ok", "artifact": "i"}
    )
    intersect_file = repo / "intersect.txt"
    intersect_file.write_text(sha1 + "\n")
    monkeypatch.chdir(repo)
    rc = main(["--reviewed-set", "--intersect", str(intersect_file), str(trail)], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    lines = set(out.splitlines())
    assert sha1 in lines
    assert sha2 not in lines


# ---------------------------------------------------------------------------
# --segments-json mode
# ---------------------------------------------------------------------------


def test_segments_json_shape_and_file_attribution(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha1 = _add_commit(repo, "src/seg-a.py")
    sha2 = _add_commit(repo, "src/seg-b.py")
    origin = _origin_main(repo)
    trail = _write_trail(
        repo, "seg.json",
        {"scope_kind": "diff", "sha_range": f"{origin}..{sha2}", "verdict": "ok", "artifact": "seg"},
    )
    monkeypatch.chdir(repo)
    rc = main(["--segments-json", str(trail)], cwd=str(repo))
    out, _err = capsys.readouterr()
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 1
    seg = data[0]
    all_shas = seg["shas"]
    assert sha1 in all_shas and sha2 in all_shas
    assert "src/seg-a.py" in seg["files"]
    assert "src/seg-b.py" in seg["files"]
    # deterministic ordering
    assert seg["shas"] == sorted(seg["shas"])
    assert seg["files"] == sorted(seg["files"])


def test_segments_json_unresolvable_ref_skip_keeps_sibling(tmp_path, monkeypatch, capsys):
    repo = _make_fixture(tmp_path)
    sha_valid = _add_commit(repo, "src/valid.py")
    origin = _origin_main(repo)
    bad_range = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef..HEAD"

    bad = _write_trail(
        repo, "bad.json", {"scope_kind": "diff", "sha_range": bad_range, "verdict": "ok", "artifact": "bad"}
    )
    good = _write_trail(
        repo, "good.json",
        {"scope_kind": "diff", "sha_range": f"{origin}..{sha_valid}", "verdict": "ok", "artifact": "good"},
    )
    monkeypatch.chdir(repo)
    rc = main(["--segments-json", "--on-unresolvable-ref", "skip", str(bad), str(good)], cwd=str(repo))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "WARN" in err
    data = json.loads(out)
    all_shas = [s for seg in data for s in seg["shas"]]
    assert sha_valid in all_shas


def test_segments_json_unresolvable_ref_default_fails(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "src/x.py")
    bad_range = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef..HEAD"
    trail = _write_trail(
        repo, "bad.json", {"scope_kind": "diff", "sha_range": bad_range, "verdict": "ok", "artifact": "bad"}
    )
    monkeypatch.chdir(repo)
    rc = main(["--segments-json", str(trail)], cwd=str(repo))
    assert rc == 1


def test_segments_json_independence_parse_failure_fails_loud_despite_ref_skip(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path)
    _add_commit(repo, "src/x.py")
    trail = repo / "state" / "review-trail" / "malformed.json"
    trail.write_text('{"a":1}{"b":2}\n')
    monkeypatch.chdir(repo)
    rc = main(["--segments-json", "--on-unresolvable-ref", "skip", str(trail)], cwd=str(repo))
    assert rc == 1


# ---------------------------------------------------------------------------
# Usage / flag validation
# ---------------------------------------------------------------------------


def test_no_args_prints_usage_and_fails(capsys):
    rc = main([])
    assert rc == 1
    assert "Usage:" in capsys.readouterr().err


def test_unknown_mode_prints_usage_and_fails(capsys):
    rc = main(["--bogus-mode"])
    assert rc == 1
    assert "Usage:" in capsys.readouterr().err


def test_invalid_on_record_error_value_rejected(capsys):
    rc = main(["--reviewed-set", "--on-record-error", "maybe"])
    assert rc == 1
    assert "must be 'skip' or 'fail'" in capsys.readouterr().err


def test_on_unresolvable_ref_inherits_on_record_error(tmp_path, monkeypatch):
    repo = _make_fixture(tmp_path)
    sha_valid = _add_commit(repo, "src/inh.py")
    origin = _origin_main(repo)
    bad_range = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef..HEAD"

    bad = _write_trail(
        repo, "bad.json", {"scope_kind": "diff", "sha_range": bad_range, "verdict": "ok", "artifact": "bad"}
    )
    good = _write_trail(
        repo, "good.json",
        {"scope_kind": "diff", "sha_range": f"{origin}..{sha_valid}", "verdict": "ok", "artifact": "good"},
    )
    monkeypatch.chdir(repo)
    # --on-record-error skip, no --on-unresolvable-ref → inherits skip.
    rc = main(["--reviewed-set", "--on-record-error", "skip", str(bad), str(good)], cwd=str(repo))
    assert rc == 0
