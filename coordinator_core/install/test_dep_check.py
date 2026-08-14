"""Characterization + parity tests for coordinator_core.install.dep_check.

Port of: dep_check.sh (DoE 6fb5fb37, 2026-07-22, 710 lines).

These tests independently re-derive parity against the bash oracle's OWN
documented contract (read directly from dep_check.sh's function-header
comments and control flow, not from this port's own transcription):

  - `_co_phase_zero_should_run` skips Phase 0 iff ANY READ_ONLY_FLAG_ALLOWLIST
    flag is true -- Test: test_phase_zero_should_run_*.
  - `_co_run_mode_prompt` exits 92 on I_AM_AGENT/COORDINATOR_RUN_MODE=agent
    BEFORE any TTY check; returns 0 immediately on I_AM_HUMAN/RUN_MODE=human;
    skips the prompt entirely (implicit human) when NON_INTERACTIVE or non-TTY
    -- Test: test_run_mode_prompt_*.
  - `_co_dep_probe`'s nested-layout fallback strips a "-claude" suffix and
    retries (the P1 fix) -- Test: test_dep_probe_nested_layout_fallback.
  - `_co_dep_probe`'s python_import probe kind runs from the CALLER's cwd,
    not the sibling dir -- this port never `cd`s at all, which is the
    natural Python equivalent of that guarantee.
  - `_co_dep_probe`'s unknown-probe-kind branch (F9 fix) emits a WARNING
    and returns "present-but-broken", NOT "present"
    -- Test: test_dep_probe_unknown_probe_kind.
  - `_co_visited_set_crash_cleanup` uses a 300s TTL; `_co_visited_set_init`
    uses a 3600s TTL for its OWN stale-sweep -- these are DIFFERENT
    thresholds on purpose (crash-retry window vs. session-start janitor)
    -- Test: test_visited_set_crash_cleanup_ttl_vs_init_ttl.
  - `_co_consent_gate`'s flag-pair-incomplete branch fires BEFORE any dep
    probe, unconditionally -- Test:
    test_consent_gate_flag_pair_incomplete_before_probe.
  - `_co_consent_gate`'s TTY double-confirm protocol: declining EITHER
    prompt is exit 91; accepting both is exit 0 --
    Test: test_consent_gate_double_confirm_*.
"""
from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from typing import List

import pytest

from coordinator_core.install import dep_check


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_manifest(tmp_path: Path, deps: List[dict]) -> Path:
    manifest = {
        "agent_install_contract_version": 2,
        "repo_id": "coordinator-claude",
        "direct_deps": deps,
    }
    manifest_dir = tmp_path / "docs" / "install"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "agent-install-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# _co_phase_zero_should_run
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "flag",
    ["HELP_FLAG", "VERSION_FLAG", "PHASE_LIST", "LAST_STATUS", "I_AM_AGENT", "CHECK_FLAG"],
)
def test_phase_zero_should_run_skips_on_allowlisted_flag(flag):
    assert dep_check.phase_zero_should_run({flag: "true"}) is False


def test_phase_zero_should_run_runs_by_default():
    assert dep_check.phase_zero_should_run({}) is True


def test_phase_zero_should_run_ignores_non_true_string():
    # bash string-compare semantics: only the literal "true" flips the gate.
    assert dep_check.phase_zero_should_run({"HELP_FLAG": "True"}) is True
    assert dep_check.phase_zero_should_run({"HELP_FLAG": "1"}) is True


# ---------------------------------------------------------------------------
# _co_run_mode_prompt
# ---------------------------------------------------------------------------

def test_run_mode_prompt_agent_flag_exits_92():
    stream = io.StringIO()
    rc = dep_check.run_mode_prompt({"I_AM_AGENT": "true"}, stderr=stream)
    assert rc == 92
    assert "AGENT_MANIFEST_PATH=docs/install/AGENT.md" in stream.getvalue()


def test_run_mode_prompt_run_mode_agent_exits_92():
    rc = dep_check.run_mode_prompt({"COORDINATOR_RUN_MODE": "agent"}, stderr=io.StringIO())
    assert rc == 92


def test_run_mode_prompt_human_flag_short_circuits():
    # Even under agent-leaning conditions, I_AM_HUMAN wins if I_AM_AGENT unset.
    rc = dep_check.run_mode_prompt({"I_AM_HUMAN": "true"}, stderr=io.StringIO(), stdin_is_tty=True)
    assert rc == 0


def test_run_mode_prompt_non_interactive_default_skips_prompt():
    # NON_INTERACTIVE defaults to "true" (mirrors bash ${NON_INTERACTIVE:-true})
    # -- absent flags dict means non-interactive by default, no prompt shown.
    stream = io.StringIO()
    rc = dep_check.run_mode_prompt({}, stderr=stream, stdin_is_tty=True)
    assert rc == 0
    assert stream.getvalue() == ""


def test_run_mode_prompt_non_tty_skips_prompt_even_if_interactive_flag_false():
    rc = dep_check.run_mode_prompt({"NON_INTERACTIVE": "false"}, stderr=io.StringIO(), stdin_is_tty=False)
    assert rc == 0


def test_run_mode_prompt_tty_yes_reply_exits_92():
    rc = dep_check.run_mode_prompt(
        {"NON_INTERACTIVE": "false"},
        stderr=io.StringIO(),
        stdin_is_tty=True,
        read_reply=lambda: "y",
    )
    assert rc == 92


def test_run_mode_prompt_tty_no_reply_proceeds():
    rc = dep_check.run_mode_prompt(
        {"NON_INTERACTIVE": "false"},
        stderr=io.StringIO(),
        stdin_is_tty=True,
        read_reply=lambda: "n",
    )
    assert rc == 0


# ---------------------------------------------------------------------------
# _co_dep_probe
# ---------------------------------------------------------------------------

def test_dep_probe_missing_when_dep_id_not_in_manifest(tmp_path):
    manifest_path = _write_manifest(tmp_path, [])
    (tmp_path.parent / "sibling").mkdir(exist_ok=True)
    status = dep_check.dep_probe("nonexistent-dep", manifest_path=str(manifest_path), repo_root=str(tmp_path))
    assert status == "missing"


def test_dep_probe_missing_when_sibling_dir_absent(tmp_path):
    manifest_path = _write_manifest(
        tmp_path,
        [{"id": "dep-a", "severity": "hard", "sibling_dir_name": "dep-a-sibling",
          "functional_probe": {"kind": "sibling_dir_exists"}}],
    )
    status = dep_check.dep_probe("dep-a", manifest_path=str(manifest_path), repo_root=str(tmp_path))
    assert status == "missing"


def test_dep_probe_sibling_dir_exists_present(tmp_path):
    # Isolated per-test sibling root: `tmp_path` is a pytest-session-shared
    # base directory (every test's tmp_path is a sibling under one common
    # session root), so creating "dep-a-sibling" directly under
    # `tmp_path.parent` leaks into any OTHER test in the same session whose
    # own repo_root shares that same parent — including
    # test_dep_probe_missing_when_sibling_dir_absent, order-dependently.
    # Nest repo_root one level under tmp_path so the sibling dir this test
    # creates stays scoped to this test's own subtree.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (tmp_path / "dep-a-sibling").mkdir(exist_ok=True)
    manifest_path = _write_manifest(
        repo_root,
        [{"id": "dep-a", "severity": "hard", "sibling_dir_name": "dep-a-sibling",
          "functional_probe": {"kind": "sibling_dir_exists"}}],
    )
    status = dep_check.dep_probe("dep-a", manifest_path=str(manifest_path), repo_root=str(repo_root))
    assert status == "present"


def test_dep_probe_file_exists_present_and_broken(tmp_path):
    sibling_root = tmp_path.parent
    sibling_dir = sibling_root / "dep-b-sibling"
    sibling_dir.mkdir(exist_ok=True)
    (sibling_dir / "marker.txt").write_text("x", encoding="utf-8")
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"id": "dep-present", "severity": "hard", "sibling_dir_name": "dep-b-sibling",
             "functional_probe": {"kind": "file_exists", "path": "marker.txt"}},
            {"id": "dep-broken", "severity": "hard", "sibling_dir_name": "dep-b-sibling",
             "functional_probe": {"kind": "file_exists", "path": "absent.txt"}},
        ],
    )
    assert dep_check.dep_probe("dep-present", manifest_path=str(manifest_path), repo_root=str(tmp_path)) == "present"
    assert dep_check.dep_probe("dep-broken", manifest_path=str(manifest_path), repo_root=str(tmp_path)) == "present-but-broken"


def test_dep_probe_file_exists_any(tmp_path):
    sibling_root = tmp_path.parent
    sibling_dir = sibling_root / "dep-b2-sibling"
    sibling_dir.mkdir(exist_ok=True)
    (sibling_dir / "second.txt").write_text("x", encoding="utf-8")
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"id": "dep-first-present", "severity": "hard", "sibling_dir_name": "dep-b2-sibling",
             "functional_probe": {"kind": "file_exists_any", "paths": ["first.txt", "second.txt"]}},
            {"id": "dep-second-only", "severity": "hard", "sibling_dir_name": "dep-b2-sibling",
             "functional_probe": {"kind": "file_exists_any", "paths": ["absent.txt", "second.txt"]}},
            {"id": "dep-none-present", "severity": "hard", "sibling_dir_name": "dep-b2-sibling",
             "functional_probe": {"kind": "file_exists_any", "paths": ["absent.txt", "also-absent.txt"]}},
        ],
    )
    stream = io.StringIO()
    assert dep_check.dep_probe("dep-first-present", manifest_path=str(manifest_path), repo_root=str(tmp_path), stderr=stream) == "present"
    assert dep_check.dep_probe("dep-second-only", manifest_path=str(manifest_path), repo_root=str(tmp_path), stderr=stream) == "present"
    assert dep_check.dep_probe("dep-none-present", manifest_path=str(manifest_path), repo_root=str(tmp_path), stderr=stream) == "present-but-broken"
    assert "WARNING" not in stream.getvalue()


def test_dep_probe_command_succeeds(tmp_path):
    sibling_root = tmp_path.parent
    (sibling_root / "dep-c-sibling").mkdir(exist_ok=True)
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"id": "dep-ok", "severity": "hard", "sibling_dir_name": "dep-c-sibling",
             "functional_probe": {"kind": "command_succeeds", "cmd": "true"}},
            {"id": "dep-fail", "severity": "hard", "sibling_dir_name": "dep-c-sibling",
             "functional_probe": {"kind": "command_succeeds", "cmd": "false"}},
        ],
    )
    assert dep_check.dep_probe("dep-ok", manifest_path=str(manifest_path), repo_root=str(tmp_path)) == "present"
    assert dep_check.dep_probe("dep-fail", manifest_path=str(manifest_path), repo_root=str(tmp_path)) == "present-but-broken"


def test_probe_shell_command_never_spawns_bash(monkeypatch):
    """Native reimplementation (2026-07-21 pure-Python-shop cutover):
    `_probe_shell_command` delegates to
    coordinator_core.ops.setup_chain_walker.command_succeeds_native
    (the same `||`-fallback-chain, shlex-tokenized-argv evaluator that
    module's own `command_succeeds` probe kind uses) instead of spawning
    `bash -c "$cmd"`. Asserts no subprocess argv touched here ever contains
    "bash"/"sh" as argv[0]."""
    import subprocess

    real_run = subprocess.run
    spawned: List[List[str]] = []

    def _spy_run(argv, *a, **k):
        spawned.append(list(argv) if isinstance(argv, (list, tuple)) else [argv])
        return real_run(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", _spy_run)

    assert dep_check._probe_shell_command("true") is True
    assert dep_check._probe_shell_command("false") is False
    # `||`-fallback chain: first side fails, second side (a real command) succeeds.
    assert dep_check._probe_shell_command("false || true") is True

    for argv in spawned:
        assert argv[0] not in ("bash", "sh", "/bin/bash", "/bin/sh")


def test_dep_probe_unknown_probe_kind(tmp_path):
    sibling_root = tmp_path.parent
    (sibling_root / "dep-d-sibling").mkdir(exist_ok=True)
    manifest_path = _write_manifest(
        tmp_path,
        [{"id": "dep-d", "severity": "hard", "sibling_dir_name": "dep-d-sibling",
          "functional_probe": {"kind": "some_future_kind"}}],
    )
    stream = io.StringIO()
    status = dep_check.dep_probe("dep-d", manifest_path=str(manifest_path), repo_root=str(tmp_path), stderr=stream)
    assert status == "present-but-broken"
    assert "WARNING: unknown probe kind" in stream.getvalue()


def test_dep_probe_nested_layout_fallback(tmp_path):
    # sibling_dir_name declares "foo-claude"; only "foo" exists on disk.
    sibling_root = tmp_path.parent
    (sibling_root / "foo").mkdir(exist_ok=True)
    manifest_path = _write_manifest(
        tmp_path,
        [{"id": "dep-e", "severity": "hard", "sibling_dir_name": "foo-claude",
          "functional_probe": {"kind": "sibling_dir_exists"}}],
    )
    status = dep_check.dep_probe("dep-e", manifest_path=str(manifest_path), repo_root=str(tmp_path))
    assert status == "present"


def test_dep_probe_all_hints(tmp_path):
    sibling_root = tmp_path.parent
    (sibling_root / "present-sibling").mkdir(exist_ok=True)
    manifest_path = _write_manifest(
        tmp_path,
        [
            {"id": "dep-present", "severity": "hard", "sibling_dir_name": "present-sibling",
             "upstream_url": "https://example.com/present", "functional_probe": {"kind": "sibling_dir_exists"}},
            {"id": "dep-missing", "severity": "hard", "sibling_dir_name": "missing-sibling",
             "upstream_url": "https://example.com/missing", "functional_probe": {"kind": "sibling_dir_exists"}},
        ],
    )
    records = dep_check.dep_probe_all(manifest_path=str(manifest_path), repo_root=str(tmp_path))
    by_id = {r["id"]: r for r in records}
    assert by_id["dep-present"]["status"] == "present"
    assert by_id["dep-present"]["hint"] == ""
    assert by_id["dep-missing"]["status"] == "missing"
    assert by_id["dep-missing"]["hint"] == "Clone from: https://example.com/missing"


# ---------------------------------------------------------------------------
# Visited-set state machine
# ---------------------------------------------------------------------------

def test_visited_set_init_creates_file(tmp_path):
    co_dir = tmp_path / "co"
    visited_file = dep_check.visited_set_init("sess-1", co_dir=co_dir)
    assert visited_file.is_file()
    data = json.loads(visited_file.read_text(encoding="utf-8"))
    assert data == {"session_id": "sess-1", "started_at": data["started_at"], "visited": []}


def test_visited_set_check_false_when_file_absent(tmp_path):
    co_dir = tmp_path / "co"
    assert dep_check.visited_set_check("sess-none", "dep-a", co_dir=co_dir) is False


def test_visited_set_append_then_check(tmp_path):
    co_dir = tmp_path / "co"
    dep_check.visited_set_init("sess-2", co_dir=co_dir)
    assert dep_check.visited_set_check("sess-2", "dep-a", co_dir=co_dir) is False
    dep_check.visited_set_append("sess-2", "dep-a", co_dir=co_dir)
    assert dep_check.visited_set_check("sess-2", "dep-a", co_dir=co_dir) is True


def test_visited_set_append_missing_file_raises(tmp_path):
    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        dep_check.visited_set_append("sess-ghost", "dep-a", co_dir=co_dir)


def test_visited_set_append_is_idempotent(tmp_path):
    co_dir = tmp_path / "co"
    visited_file = dep_check.visited_set_init("sess-3", co_dir=co_dir)
    dep_check.visited_set_append("sess-3", "dep-a", co_dir=co_dir)
    dep_check.visited_set_append("sess-3", "dep-a", co_dir=co_dir)
    data = json.loads(visited_file.read_text(encoding="utf-8"))
    assert data["visited"] == ["dep-a"]


def test_visited_set_crash_cleanup_ttl_vs_init_ttl(tmp_path):
    """crash_cleanup uses a 300s TTL; init's OWN sweep uses 3600s -- a file
    250s old survives crash_cleanup but a file 250s old also survives init's
    sweep (both keep it); a file 400s old is reaped by crash_cleanup but
    NOT by init's sweep (different thresholds, not the same function)."""
    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)
    orphan = co_dir / "chain-walk-orphan.json"
    orphan.write_text(json.dumps({"session_id": "orphan", "started_at": "x", "visited": []}), encoding="utf-8")

    now = 10_000.0
    old_mtime = now - 400  # older than crash_cleanup's 300s TTL, younger than init's 3600s TTL
    os.utime(orphan, (old_mtime, old_mtime))

    dep_check.visited_set_crash_cleanup(co_dir=co_dir, now=now)
    assert not orphan.exists(), "crash_cleanup should reap a 400s-old orphan (>300s TTL)"


def test_visited_set_init_sweep_keeps_recent_reaps_hour_old(tmp_path):
    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)
    stale = co_dir / "chain-walk-stale.json"
    stale.write_text(json.dumps({"session_id": "stale", "started_at": "x", "visited": []}), encoding="utf-8")

    now = 10_000.0
    hour_old = now - 3700  # older than init's 3600s TTL
    os.utime(stale, (hour_old, hour_old))

    dep_check.visited_set_init("sess-fresh", co_dir=co_dir, now=now)
    assert not stale.exists()


# --------------------------------------------------------------------------
# resolution journal wiring (C6)
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _journal_env(tmp_path, monkeypatch):
    from coordinator_core.install import resolution_journal as rj

    journal_path = tmp_path / "journal" / "resolution-journal.jsonl"
    monkeypatch.setenv(rj.RESOLUTION_JOURNAL_ENV_VAR, str(journal_path))
    monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)
    return journal_path


def test_visited_set_init_journals_its_own_session_file(tmp_path):
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "co"
    visited_file = dep_check.visited_set_init("sess-journal", co_dir=co_dir)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]
    write_entries = resolutions[dep_check._VISITED_SET_WRITE_CLAUSE_INDEX].entries
    assert len(write_entries) == 1
    assert write_entries[0].path == str(visited_file)
    assert write_entries[0].kind == "file-path"


def test_visited_set_init_journals_empty_delete_clause_when_nothing_stale(tmp_path):
    """init's own sweep found nothing stale -- a real "looked, nothing was
    stale" fact, journaled as an empty tuple, not skipped."""
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "co"
    dep_check.visited_set_init("sess-clean", co_dir=co_dir)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]
    delete_entries = resolutions[dep_check._VISITED_SET_DELETE_CLAUSE_INDEX].entries
    assert delete_entries == ()


def test_visited_set_init_journals_its_own_stale_sweep_deletions(tmp_path):
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)
    stale = co_dir / "chain-walk-stale.json"
    stale.write_text(json.dumps({"session_id": "stale", "started_at": "x", "visited": []}), encoding="utf-8")
    now = 10_000.0
    hour_old = now - 3700
    os.utime(stale, (hour_old, hour_old))

    dep_check.visited_set_init("sess-fresh", co_dir=co_dir, now=now)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]
    delete_entries = resolutions[dep_check._VISITED_SET_DELETE_CLAUSE_INDEX].entries
    deleted_paths = {e.path for e in delete_entries}
    assert str(stale) in deleted_paths
    assert all(e.effect == "delete" for e in delete_entries)


def test_visited_set_crash_cleanup_absent_dir_never_journals(tmp_path):
    """The directory does not exist -- crash_cleanup never examined
    anything, so no row for either 'looked, found nothing' or a deletion."""
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "does-not-exist"
    dep_check.visited_set_crash_cleanup(co_dir=co_dir)

    assert rj.read_journal() == {}


def test_visited_set_crash_cleanup_journals_empty_when_dir_exists_but_nothing_stale(tmp_path):
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)

    dep_check.visited_set_crash_cleanup(co_dir=co_dir)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]
    assert resolutions[dep_check._VISITED_SET_DELETE_CLAUSE_INDEX].entries == ()


def test_visited_set_crash_cleanup_journals_actual_deletions(tmp_path):
    from coordinator_core.install import resolution_journal as rj

    co_dir = tmp_path / "co"
    co_dir.mkdir(parents=True)
    orphan = co_dir / "chain-walk-orphan.json"
    orphan.write_text(json.dumps({"session_id": "orphan", "started_at": "x", "visited": []}), encoding="utf-8")
    now = 10_000.0
    old_mtime = now - 400
    os.utime(orphan, (old_mtime, old_mtime))

    dep_check.visited_set_crash_cleanup(co_dir=co_dir, now=now)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]
    delete_entries = resolutions[dep_check._VISITED_SET_DELETE_CLAUSE_INDEX].entries
    assert len(delete_entries) == 1
    assert delete_entries[0].path == str(orphan)
    assert delete_entries[0].effect == "delete"


def test_round_trip_via_derive_receipt_entries(tmp_path):
    """Proves read_journal()'s output for this writer is directly
    consumable by derive_receipt_entries for both of dep_check's clause
    indices."""
    from coordinator_core.install import resolution_journal as rj
    from coordinator_core.install.receipt import derive_receipt_entries

    co_dir = tmp_path / "co"
    visited_file = dep_check.visited_set_init("sess-rt", co_dir=co_dir)

    journal = rj.read_journal()
    resolutions = journal["dep-check"]

    receipt_entries = derive_receipt_entries(dep_check.WRITE_SURFACE, resolutions)
    paths = {e.path for e in receipt_entries}
    assert str(visited_file) in paths


# ---------------------------------------------------------------------------
# _co_consent_gate
# ---------------------------------------------------------------------------

def test_consent_gate_flag_pair_incomplete_skip_without_accept():
    rc = dep_check.consent_gate({"SKIP_DEP_CHECK": "true"}, stderr=io.StringIO())
    assert rc == 93


def test_consent_gate_flag_pair_incomplete_accept_without_skip():
    rc = dep_check.consent_gate({"ACCEPT_MISSING_DEPS_RISK": "true"}, stderr=io.StringIO())
    assert rc == 93


def test_consent_gate_flag_pair_incomplete_before_probe(tmp_path, monkeypatch):
    # Even with an unresolvable repo_root (which would blow up dep_probe_all),
    # the incomplete-flag-pair check must fire FIRST and never reach probing.
    rc = dep_check.consent_gate(
        {"SKIP_DEP_CHECK": "true"},
        manifest_path=str(tmp_path / "does-not-exist.json"),
        stderr=io.StringIO(),
    )
    assert rc == 93


def test_consent_gate_no_hard_deps_missing_returns_0(tmp_path):
    manifest_path = _write_manifest(tmp_path, [])
    rc = dep_check.consent_gate({}, manifest_path=str(manifest_path), repo_root=str(tmp_path), stderr=io.StringIO())
    assert rc == 0


def _hard_missing_manifest(tmp_path: Path) -> Path:
    return _write_manifest(
        tmp_path,
        [{"id": "critical-dep", "severity": "hard", "sibling_dir_name": "nope",
          "upstream_url": "https://example.com/critical", "functional_probe": {"kind": "sibling_dir_exists"}}],
    )


def test_consent_gate_non_interactive_missing_hard_dep_exits_90(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    rc = dep_check.consent_gate(
        {"NON_INTERACTIVE": "true"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        stderr=io.StringIO(),
    )
    assert rc == 90


def test_consent_gate_both_flags_accept_bypasses_confirm(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    rc = dep_check.consent_gate(
        {"SKIP_DEP_CHECK": "true", "ACCEPT_MISSING_DEPS_RISK": "true"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        stderr=io.StringIO(),
    )
    assert rc == 0


def test_consent_gate_double_confirm_first_decline_exits_91(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    rc = dep_check.consent_gate(
        {"NON_INTERACTIVE": "false"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        stderr=io.StringIO(),
        stdin_is_tty=True,
        read_reply=lambda: "n",
    )
    assert rc == 91


def test_consent_gate_double_confirm_second_decline_exits_91(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    replies = iter(["y", "n"])
    rc = dep_check.consent_gate(
        {"NON_INTERACTIVE": "false"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        stderr=io.StringIO(),
        stdin_is_tty=True,
        read_reply=lambda: next(replies),
    )
    assert rc == 91


def test_consent_gate_double_confirm_both_accept_exits_0(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    replies = iter(["y", "y"])
    rc = dep_check.consent_gate(
        {"NON_INTERACTIVE": "false"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        stderr=io.StringIO(),
        stdin_is_tty=True,
        read_reply=lambda: next(replies),
    )
    assert rc == 0


def test_consent_gate_banner_substitutes_deps_list(tmp_path):
    manifest_path = _hard_missing_manifest(tmp_path)
    banner_path = tmp_path / "banner.txt"
    banner_path.write_text("HEADER\n<!-- DEPS_LIST_PLACEHOLDER -->\nFOOTER", encoding="utf-8")
    stream = io.StringIO()
    dep_check.consent_gate(
        {"NON_INTERACTIVE": "true"},
        manifest_path=str(manifest_path),
        repo_root=str(tmp_path),
        banner_path=str(banner_path),
        stderr=stream,
    )
    out = stream.getvalue()
    assert "critical-dep [missing]" in out
    assert "HEADER" in out and "FOOTER" in out


# ---------------------------------------------------------------------------
# CLI (main) — subprocess-free dispatch tests via main(argv)
# ---------------------------------------------------------------------------

def test_cli_phase_zero_should_run_exit_codes(monkeypatch):
    monkeypatch.delenv("HELP_FLAG", raising=False)
    assert dep_check.main(["phase-zero-should-run"]) == 0
    monkeypatch.setenv("HELP_FLAG", "true")
    assert dep_check.main(["phase-zero-should-run"]) == 1


def test_cli_unknown_subcommand_returns_2():
    assert dep_check.main(["not-a-real-subcommand"]) == 2


def test_cli_dep_probe_prints_status(tmp_path, capsys):
    manifest_path = _write_manifest(tmp_path, [])
    rc = dep_check.main(["dep-probe", "nope", str(manifest_path)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "missing"


def test_cli_dep_probe_empty_string_manifest_path_falls_back_to_default_resolution(tmp_path, monkeypatch):
    # Review: code-reviewer F1/F2 — bash's "${2:-}" idiom produces a real empty-string
    # positional arg (not an omitted one) when a caller doesn't pass a manifest path.
    # This must take the layout-aware default-resolution path, not raise
    # ManifestCorruptError("manifest not found: .") from Path("").
    _write_manifest(tmp_path, [])
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    rc = dep_check.main(["dep-probe", "nope", ""])
    assert rc == 0


def test_cli_dep_probe_all_empty_string_manifest_path_falls_back_to_default_resolution(tmp_path, monkeypatch, capsys):
    _write_manifest(tmp_path, [])
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    rc = dep_check.main(["dep-probe-all", ""])
    assert rc == 0


def test_cli_consent_gate_empty_string_paths_fall_back_to_default_resolution(tmp_path, monkeypatch):
    _write_manifest(tmp_path, [])
    monkeypatch.setenv("REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("NON_INTERACTIVE", "true")
    rc = dep_check.main(["consent-gate", "", ""])
    assert rc == 0


def test_cli_visited_set_append_missing_file_returns_1(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = dep_check.main(["visited-set-append", "ghost-session", "dep-a"])
    assert rc == 1


def test_cli_crash_reserved_code_does_not_collide_with_business_codes(monkeypatch):
    # Force an internal exception inside a subcommand handler and confirm the
    # CLI maps it to CRASH_RC (3), never to a business code for that
    # subcommand (phase-zero-should-run's own business codes are {0,1}).
    def _boom(flags):
        raise RuntimeError("simulated internal defect")

    monkeypatch.setattr(dep_check, "phase_zero_should_run", _boom)
    rc = dep_check.main(["phase-zero-should-run"])
    assert rc == dep_check.CRASH_RC
    assert dep_check.CRASH_RC not in (0, 1)
