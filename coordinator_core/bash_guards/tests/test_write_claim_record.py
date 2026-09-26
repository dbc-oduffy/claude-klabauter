"""Tests for coordinator_core.bash_guards.write_claim_record (C1) and its
ONE wire point, dispatch._record_bash_write_claims (called from
evaluate_payload_json).

Spec backlink: docs/plans/2026-08-30-a-bash-write-reaches-the-ledger-that-
decides-what-gets-committed.md, chunk C2. AC1-AC7 below map 1:1 to that
plan's `## Acceptance criteria` table.

Two access paths are used deliberately:

  - AC1/AC4/AC5/AC6/AC7 call `record_write_claims` directly against a
    `tmp_path` fixture repo -- these are about the RECORDER's own
    extraction/filtering behavior (which paths get claimed), not about
    guard-chain wiring, and the plan's own "how it is checked" column asks
    only for "asserts ... against a fixture sink".
  - AC2/AC3 drive the real seam, `dispatch.evaluate_payload_json`, with
    `dispatch._build_guard_chain` monkeypatched to a single controlled
    `GuardEntry` -- the SAME isolation technique
    `test_advisory_fire_counter.py` already uses to test this exact wrapper
    function without depending on the full, order-sensitive real chain.
    AC3's entry wraps the REAL `dispatch_checks.check_cat_heredoc_write_
    advise` (not a constant lambda), so it genuinely fires against a real
    heredoc command -- proving the headline case, not just the plumbing.

No `.git` DIRECTORY CONTENTS are needed anywhere in this file: `git.
repo_root.show_toplevel` (the resolver `_record_bash_write_claims` calls)
is a pure walk for a `.git` marker, never a spawn (see that function's own
docstring) -- so `os.makedirs(root/".git")` is sufficient and this file
spawns no subprocess anywhere, which AC7 itself asserts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry
from coordinator_core.benchmarks.process_time import in_process_time_ms
from coordinator_core.bash_guards.write_claim_record import (
    record_write_claims,
    resolve_read_targets,
)
from coordinator_core.session.touch_record import (
    VERB_TOUCH,
    decode_line,
    iter_complete_lines,
    sink_path,
)

try:
    from coordinator_core.bash_guards._advisory_value import AdvisoryValue
except ImportError:  # pragma: no cover -- mirrors test_advisory_fire_counter.py's own import
    AdvisoryValue = None

_SESSION_ID = "c2-write-claim-record-probe"


def _repo(tmp_path, name="repo") -> str:
    root = tmp_path / name
    os.makedirs(root / ".git")
    return str(root)


def _events(root, session_id=_SESSION_ID):
    sink = sink_path(os.path.join(root, ".git", "coordinator-sessions", session_id))
    if not sink.exists():
        return []
    raw = sink.read_bytes()
    return [decode_line(line) for line in iter_complete_lines(raw)]


def _touched_paths(root, session_id=_SESSION_ID) -> set:
    return {e.path for e in _events(root, session_id) if e.verb == VERB_TOUCH}


def _sink_bytes(root, session_id=_SESSION_ID) -> bytes:
    sink = sink_path(os.path.join(root, ".git", "coordinator-sessions", session_id))
    return sink.read_bytes() if sink.exists() else b""


# AC1 -- one VERB_TOUCH claim per recovered shape.

_AC1_SHAPES = [
    pytest.param("cat > f.py <<'EOF'\nhello\nEOF", "f.py", id="heredoc"),
    pytest.param("echo hi > f.py", "f.py", id="redirect-clobber"),
    pytest.param("echo hi >> f.py", "f.py", id="redirect-append"),
    pytest.param("echo hi | tee f.py", "f.py", id="tee"),
    pytest.param("cp a.py f.py", "f.py", id="cp-destination"),
    pytest.param("mv a.py f.py", "f.py", id="mv-destination"),
    pytest.param(
        "python - <<'PY'\nopen(\"f.py\", \"w\").write(\"hi\")\nPY",
        "f.py",
        id="interpreter-payload",
    ),
]


@pytest.mark.parametrize("cmd, expected_path", _AC1_SHAPES)
def test_ac1_one_touch_claim_per_recovered_shape(tmp_path, cmd, expected_path):
    root = _repo(tmp_path)
    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    events = _events(root)
    touches = [e for e in events if e.verb == VERB_TOUCH]
    assert len(touches) == 1, f"expected exactly one TOUCH for {cmd!r}, got {events}"
    assert touches[0].path == expected_path


def _payload_json(cmd, session_id, cwd):
    return json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": cwd,
        }
    )


_DENY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard deny (fake, isolated chain)",
    }
}


def _fake_hard_deny_entry(name="fake-hard-deny-guard"):
    return GuardEntry(
        name,
        lambda: dict(_DENY_ENVELOPE),
        True,
        GuardBand.CONFINEMENT_DENY,
        AdvisoryValue.NOT_COST_ARGUED,
    )


def test_ac2_denied_command_records_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(
        dispatch, "_build_guard_chain", lambda *a, **k: [_fake_hard_deny_entry()]
    )

    before = _sink_bytes(root)
    assert before == b""

    cmd = "cat > f.py <<'EOF'\nhello\nEOF"
    out = dispatch.evaluate_payload_json(_payload_json(cmd, _SESSION_ID, root))

    assert (
        isinstance(out, dict)
        and out.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    ), f"fake chain must have denied: {out}"
    after = _sink_bytes(root)
    assert after == before == b"", "a denied command must leave the sink byte-unchanged"


def test_ac3_advisory_only_outcome_does_record(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    cmd = "cat > f.py <<'EOF'\nhello\nEOF"

    def _real_advisory_entry():
        return dc.check_cat_heredoc_write_advise(cmd, _SESSION_ID, {}, root)

    entry = GuardEntry(
        "cat-heredoc-write-advise",
        _real_advisory_entry,
        False,
        GuardBand.ADVISORY_REWRITE,
        AdvisoryValue.HOST_INDEPENDENT,
    )
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])

    out = dispatch.evaluate_payload_json(_payload_json(cmd, _SESSION_ID, root))

    assert (
        isinstance(out, dict)
        and out.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"
    ), f"cat-heredoc-write-advise must fire as an advisory ALLOW: {out}"
    assert "f.py" in _touched_paths(root), (
        "an advisory-allow outcome must still record the claim -- this is "
        "the exact shape the recorder exists for"
    )


def test_ac4_out_of_repo_redirect_target_is_never_claimed(tmp_path):
    root = _repo(tmp_path)
    outside = str(tmp_path / "elsewhere" / "f.py")
    cmd = f"echo hi > {outside}"
    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == set()


def test_ac4_unnamed_path_is_never_claimed(tmp_path):
    """A path the command never names must never appear, even when a
    DIFFERENT, real target is also present in the same command."""
    root = _repo(tmp_path)
    cmd = "echo hi > f.py"
    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    touched = _touched_paths(root)
    assert touched == {"f.py"}
    assert "peer-untouched.py" not in touched


def test_ac5_sed_inplace_claims_file_not_script(tmp_path):
    root = _repo(tmp_path)
    record_write_claims("sed -i 's/a/b/' f.py", _SESSION_ID, root, denied=False)
    touches = [e for e in _events(root) if e.verb == VERB_TOUCH]
    assert len(touches) == 1, f"expected exactly one claim, got {touches}"
    assert touches[0].path == "f.py"
    assert "s/a/b/" not in {e.path for e in touches}


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # THE REGRESSION THIS EXISTS FOR. The first shape of
        # `_SED_SCRIPT_RE` and silently dropped any path starting `s`/`y`
        # is INVISIBLE: the file just quietly fails to make the commit,
        # here asserts the CLAIMING direction.
        ("cat >> state/e2e-probe-bash-write.txt", "state/e2e-probe-bash-write.txt"),
        ("cat >> state/x.txt", "state/x.txt"),
        ("echo hi > scripts/s.txt", "scripts/s.txt"),
        ("echo hi > systems/y.txt", "systems/y.txt"),
        ("echo hi > yesterday.txt", "yesterday.txt"),
        ("cat >> s/a/b/c.txt", "s/a/b/c.txt"),
    ],
)
def test_ac5_sed_filter_never_drops_a_real_path(tmp_path, cmd, expected):
    root = _repo(tmp_path)
    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    claimed = {e.path for e in _events(root) if e.verb == VERB_TOUCH}
    assert expected in claimed, (
        f"{cmd!r} lost its claim for {expected!r} -- got {claimed}. "
        "A silently dropped claim is the bug this module fixes."
    )


def test_ac5_sed_file_operand_survives_even_in_the_bad_shape(tmp_path):
    root = _repo(tmp_path)
    target = os.path.join(root, "state")
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "x.txt"), "w", encoding="utf-8") as fh:
        fh.write("a\n")
    record_write_claims("sed -i 's/a/b/' state/x.txt", _SESSION_ID, root, denied=False)
    claimed = {e.path for e in _events(root) if e.verb == VERB_TOUCH}
    assert "state/x.txt" in claimed, claimed
    assert "s/a/b/" not in claimed, claimed


def test_ac6_none_root_never_raises_and_records_nothing():
    record_write_claims("echo hi > f.py", _SESSION_ID, None, denied=False)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_ac6_unwritable_sink_directory_never_raises(tmp_path):
    root = _repo(tmp_path)
    sid_dir = os.path.join(root, ".git", "coordinator-sessions", _SESSION_ID)
    os.makedirs(sid_dir)
    original_mode = os.stat(sid_dir).st_mode
    try:
        os.chmod(sid_dir, 0o000)
        record_write_claims("echo hi > f.py", _SESSION_ID, root, denied=False)
    finally:
        os.chmod(sid_dir, original_mode)


def test_ac6_recorder_never_flips_the_guard_verdict(tmp_path, monkeypatch):
    entry = GuardEntry(
        "fake-allow-guard",
        lambda: None,
        False,
        GuardBand.ADVISORY_REWRITE,
        AdvisoryValue.HOST_INDEPENDENT,
    )
    monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
    cmd = "echo hi > f.py"

    root = _repo(tmp_path, name="healthy")
    baseline = dispatch.evaluate_payload_json(_payload_json(cmd, _SESSION_ID, root))

    no_root_dir = str(tmp_path / "no-repo-here")
    os.makedirs(no_root_dir)
    out_no_root = dispatch.evaluate_payload_json(
        _payload_json(cmd, _SESSION_ID, no_root_dir)
    )
    assert out_no_root == baseline

    if os.name != "nt":
        broken = _repo(tmp_path, name="broken")
        sid_dir = os.path.join(broken, ".git", "coordinator-sessions", _SESSION_ID)
        os.makedirs(sid_dir)
        original_mode = os.stat(sid_dir).st_mode
        try:
            os.chmod(sid_dir, 0o000)
            out_broken = dispatch.evaluate_payload_json(
                _payload_json(cmd, _SESSION_ID, broken)
            )
            assert out_broken == baseline
        finally:
            os.chmod(sid_dir, original_mode)


_AC7_CORPUS = [
    "cat > f1.py <<'EOF'\nhi\nEOF",
    "echo hi > f2.py",
    "echo hi >> f3.py",
    "echo hi | tee f4.py",
    "cp a.py f5.py",
    "mv a.py f6.py",
    "python - <<'PY'\nopen(\"f7.py\", \"w\").write(\"hi\")\nPY",
    "sed -i 's/a/b/' f8.py",
    "sed -i 's/a/b/' f9.py",
    "rsync a.py f10.py",
    "install a.py f11.py",
    "mkdir -p sub12",
    "tar -xf a.tar -C sub13",
    "ls -la",
    "echo just printing, no write",
    "grep -rn foo .",
    "git status",
    "python3 -c \"print('no write here')\"",
    "cat f14.py",
    "echo hi > f15.py",
]


def _scratchpad(tmp_path) -> str:
    scratch = tmp_path / "scratchpad"
    scratch.mkdir()
    return str(scratch)


def _patch_temp_roots(monkeypatch, *roots):
    from coordinator_core.bash_guards import _write_bump_applicability as applicability

    monkeypatch.setattr(applicability, "_all_temp_roots", lambda *a, **k: list(roots))


def test_c1_scratchpad_script_claims_the_target_it_writes(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"python3 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == {"f.py"}


_C1_UNCHANGED_SHAPES = [
    pytest.param('python3 -c "open(\'f.py\', \'w\').write(\'hi\')"', id="inline-c"),
    pytest.param("python3 - <<'PY'\nopen(\"f.py\", \"w\").write(\"hi\")\nPY", id="heredoc"),
    pytest.param("echo x > f.py", id="redirect"),
]


@pytest.mark.parametrize("cmd", _C1_UNCHANGED_SHAPES)
def test_c1_other_three_shapes_unchanged_by_the_new_branch(tmp_path, monkeypatch, cmd):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    record_write_claims(cmd, _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == {"f.py"}


def test_c1_script_outside_scratchpad_claims_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    outside_dir = tmp_path / "not-scratch"
    outside_dir.mkdir()
    script = outside_dir / "fix.py"
    script.write_text("open('f.py', 'w').write('hi')\n", encoding="utf-8")

    record_write_claims(f"python3 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == set()


def test_c1_scratchpad_script_naming_out_of_repo_path_claims_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    outside_target = str(tmp_path / "elsewhere" / "f.py")
    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write(f"open({outside_target!r}, 'w').write('hi')\n")

    record_write_claims(f"python3 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == set()


def test_c1_scratchpad_script_over_size_cap_claims_nothing(tmp_path, monkeypatch):
    from coordinator_core.bash_guards import write_claim_record as wcr

    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)
    monkeypatch.setattr(wcr, "_SCRATCHPAD_SCRIPT_READ_CAP_BYTES", 16)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"python3 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == set()


def test_c1_denied_command_claims_nothing_even_with_eligible_script(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"python3 {script}", _SESSION_ID, root, denied=True)
    assert _touched_paths(root) == set()


def test_c1_unreadable_or_vanished_script_raises_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    missing_script = os.path.join(scratch, "gone.py")
    record_write_claims(f"python3 {missing_script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == set()


# script operand, and a chained invocation naming two DIFFERENT scripts.


@pytest.mark.parametrize(
    "head",
    [
        pytest.param("Python3", id="capitalized"),
        pytest.param("PYTHON3", id="all-caps"),
    ],
)
def test_c1_case_folded_head_verb_still_claims(tmp_path, monkeypatch, head):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"{head} {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == {"f.py"}


def test_c1_version_pinned_interpreter_still_claims(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"python3.11 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == {"f.py"}


def test_c1_value_taking_flag_ahead_of_operand_still_claims(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(
        f"python3 -X faulthandler {script}", _SESSION_ID, root, denied=False
    )
    assert _touched_paths(root) == {"f.py"}


def test_c1_chained_invocation_claims_both_different_targets(tmp_path, monkeypatch):
    """The obvious same-path chained test proves nothing -- both scripts here
    write a DIFFERENT path, so only returning ALL matching operands (not just
    the first) makes this pass."""
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script_a = os.path.join(scratch, "a.py")
    with open(script_a, "w", encoding="utf-8") as fh:
        fh.write("open('a-target.py', 'w').write('hi')\n")
    script_b = os.path.join(scratch, "b.py")
    with open(script_b, "w", encoding="utf-8") as fh:
        fh.write("open('b-target.py', 'w').write('hi')\n")

    record_write_claims(
        f"python3 {script_a} && python3 {script_b}", _SESSION_ID, root, denied=False
    )
    assert _touched_paths(root) == {"a-target.py", "b-target.py"}


def test_c1_env_python3_already_claims_without_a_fix(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    scratch = _scratchpad(tmp_path)
    _patch_temp_roots(monkeypatch, scratch)

    script = os.path.join(scratch, "fix.py")
    with open(script, "w", encoding="utf-8") as fh:
        fh.write("open('f.py', 'w').write('hi')\n")

    record_write_claims(f"env python3 {script}", _SESSION_ID, root, denied=False)
    assert _touched_paths(root) == {"f.py"}


def test_ac7_cost_under_5ms_total_and_no_subprocess(monkeypatch):
    scratch_root = Path(__file__).resolve().parents[3] / ".pytest_ac7_scratch"
    scratch_root.mkdir(exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(dir=str(scratch_root)))
    try:
        root = _repo(work_dir)
        _run_ac7_timing(root, monkeypatch)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            scratch_root.rmdir()
        except OSError:
            pass


def _run_ac7_timing(root, monkeypatch):
    spawned = []
    real_popen_init = subprocess.Popen.__init__

    def _tracking_popen_init(self, *a, **k):
        spawned.append((a, k))
        return real_popen_init(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", _tracking_popen_init)

    record_write_claims("echo warm > warm.py", f"{_SESSION_ID}-warmup", root, denied=False)
    spawned.clear()

    def _corpus_pass() -> None:
        for i, cmd in enumerate(_AC7_CORPUS):
            record_write_claims(cmd, f"{_SESSION_ID}-{i}", root, denied=False)

    timing = in_process_time_ms(_corpus_pass)

    assert not spawned, f"record_write_claims must never spawn a subprocess: {spawned}"
    assert timing["process_time_ms"] < 20.0, (
        f"recorder cost over a {len(_AC7_CORPUS)}-command corpus was "
        f"{timing['process_time_ms']:.3f}ms process time, over the 20ms "
        "budget (reference: 0.639ms/call end-to-end measured on the real "
        "repo)"
    )


# saw.md, chunk C1. TEMPLATE table from that chunk's own body, verbatim.


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("sed -n '1,40p' a.py", ["a.py"]),
        ("head b.py", ["b.py"]),
        ("cat $F", []),
        ("for f in *.py; do cat $f; done", []),
    ],
)
def test_c1_resolve_read_targets_template_table(cmd, expected):
    assert resolve_read_targets(cmd) == expected


@pytest.mark.parametrize(
    "cmd, expected",
    [
        ("cat a.py", ["a.py"]),
        ("tail a.py", ["a.py"]),
        ("less a.py", ["a.py"]),
        ("tail -n 20 a.py", ["a.py"]),
        ("head -c 100 a.py", ["a.py"]),
    ],
)
def test_c1_resolve_read_targets_other_shapes(cmd, expected):
    assert resolve_read_targets(cmd) == expected


def test_c1_resolve_read_targets_sed_inplace_is_not_a_read():
    assert resolve_read_targets("sed -i 's/a/b/' a.py") == []


def test_c1_resolve_read_targets_glob_resolves_nothing():
    assert resolve_read_targets("cat *.py") == []


def test_c1_resolve_read_targets_subshell_resolves_nothing():
    assert resolve_read_targets("cat $(echo a.py)") == []


def test_c1_resolve_read_targets_backtick_substitution_resolves_nothing():
    assert resolve_read_targets("cat `echo a.py`") == []


def test_c1_resolve_read_targets_unrecognized_verb_resolves_nothing():
    assert resolve_read_targets("grep foo a.py") == []


def test_c1_resolve_read_targets_never_raises_on_garbage_input():
    assert resolve_read_targets("") == []
    assert resolve_read_targets("cat 'unterminated") == []
