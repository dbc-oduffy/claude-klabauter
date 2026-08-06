"""test_reap_orphaned_in_flight_handoffs.py — regression suite for
reap-orphaned-in-flight-handoffs.py.

Consolidates three test suites, covering:
  - _fm_field quote stripping (B-F4)
  - _shipped_orphan_sha ship-check predicate (P1-P4)
  - end-to-end dead-holder claim-release vs live-holder no-op, dry-run coverage

The helpers (`_fm_field`, `_shipped_orphan_sha`) are imported and called
directly — no subprocess indirection needed for the unit-level cases; the
end-to-end cases still drive the real script as a subprocess against a
hermetic git sandbox.

Run:
    pytest coordinator/bin/tests/test_reap_orphaned_in_flight_handoffs.py -v

Spec backlink: docs/plans/2026-07-13-reaper-ship-not-abandon-shipped-orphans.md
Spec backlink: state/handoffs/2026-07-20_114653_revive-lost-capabilities-triage.md § Part 1
Port of: test-reap-orphaned-in-flight-handoffs-quote-strip.sh, test-reap-orphaned-in-flight-ship-not-abandon.sh,
    test-reap-orphaned-dead-holder-releases-claim.sh (example-doctrine-repo e991362e, 2026-07-21)
"""
from __future__ import annotations

import datetime
import importlib.util
import os
import subprocess
import sys
import time

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "reap-orphaned-in-flight-handoffs.py")
_QUERY_CLI = os.path.join(_REPO_ROOT, "coordinator", "bin", "query-completions.py")

# Windows: suppresses the console popup a subprocess.run(...) would otherwise
# trigger under the headless Claude Code Bash-tool parent. No-op (0) elsewhere.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_module():
    spec = importlib.util.spec_from_file_location("reap_orphaned_in_flight_handoffs", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _git(cwd, *args):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root):
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "commit.gpgsign", "false")


def _commit(root, relpath):
    _git(root, "add", "--", relpath)
    _git(root, "commit", "-q", "-m", f"fixture commit: {relpath}")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _write_handoff(path, status, dstate, consumed_by, kind=None, deliverable_id=None):
    lines = [
        "---",
        f"status: {status}",
        f"deployment_state: {dstate}",
        f"consumed_by: {consumed_by}",
    ]
    if kind:
        lines.append(f"kind: {kind}")
    if deliverable_id:
        lines.append(f"deliverable_id: {deliverable_id}")
    lines += ["---", "body", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _write_valid_handoff(path, title, consumed_by):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
schema: handoff
title: "{title}"
status: consumed
deployment_state: in_flight
consumed_at: 2026-07-20T10:00:00Z
consumed_by: {consumed_by}
predecessor: none
kind: session-handoff
category: infra
summary: "fixture handoff for dead-holder claim-release regression test"
pickup_ready: true
created: 2026-07-20
created_at: 2026-07-20
branch: work/machine-a/2026-07-20
---
# Fixture Handoff — {title}
body
""")


def _write_new_vocab_handoff(path, title, claimed_by):
    """DR-084 new-vocabulary fixture — status: claimed + claimed_by: (no
    consumed_by: at all), mirroring `_write_valid_handoff` but for the
    post-P2 field/value names. See test_claimed_by_dead_holder_released.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
schema: handoff
title: "{title}"
status: claimed
deployment_state: in_flight
claimed_at: 2026-07-22T10:00:00Z
claimed_by: {claimed_by}
predecessor: none
kind: session-handoff
category: infra
summary: "fixture handoff for DR-084 dual-read dead-holder claim-release regression test"
pickup_ready: true
created: 2026-07-22
created_at: 2026-07-22
branch: work/machine-a/2026-07-22
---
# Fixture Handoff — {title}
body
""")


def _write_no_holder_claimed_handoff(path, title):
    """status: claimed + deployment_state: in_flight with NEITHER
    claimed_by: NOR consumed_by: recorded — must hit the fail-closed
    skipped_no_holder path (see test_claimed_status_no_holder_fails_closed).
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
schema: handoff
title: "{title}"
status: claimed
deployment_state: in_flight
predecessor: none
kind: session-handoff
category: infra
summary: "fixture handoff for DR-084 no-holder-recorded fail-closed regression test"
pickup_ready: true
created: 2026-07-22
created_at: 2026-07-22
branch: work/machine-a/2026-07-22
---
# Fixture Handoff — {title}
body
""")


def _write_completion(path, authored_by, commits_block):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
title: "fixture completion entry"
created: 2026-07-13
nature: infra
nature_inferred: false
chain: null
{commits_block}
status: released
chain_terminal: true
authored_by: {authored_by}
---
fixture body
""")


def _write_live_session(sandbox, sid):
    sdir = os.path.join(sandbox, ".git", "coordinator-sessions", sid)
    os.makedirs(sdir, exist_ok=True)
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(sdir, "meta.json"), "w", encoding="utf-8") as fh:
        fh.write('{"session_id": "%s", "last_activity": "%s"}' % (sid, now_iso))


def _query_shim(sandbox):
    shim = os.path.join(sandbox, "query-completions-shim.sh")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write(f'#!/usr/bin/env bash\nexec python3 "{_QUERY_CLI}" "$@" --root "{sandbox}"\n')
    os.chmod(shim, 0o755)
    return shim


def _sandbox(tmp_path, extra_dirs=("state/handoffs", "archive/handoffs", "archive/completed/2026-07")):
    root = str(tmp_path)
    _init_repo(root)
    for d in extra_dirs:
        os.makedirs(os.path.join(root, d), exist_ok=True)
    return root


# ---------------------------------------------------------------------------
# _fm_field — quote-stripping
# ---------------------------------------------------------------------------

def test_fm_field_unquoted_unchanged(mod, tmp_path):
    f = tmp_path / "unquoted.md"
    f.write_text("---\nconsumed_by: session-abc-123\n---\nbody\n")
    assert mod._fm_field(str(f), "consumed_by") == "session-abc-123"


def test_fm_field_single_quoted_all_digit_stripped(mod, tmp_path):
    f = tmp_path / "single-quoted.md"
    f.write_text("---\nconsumed_by: '123456'\n---\nbody\n")
    assert mod._fm_field(str(f), "consumed_by") == "123456"


def test_fm_field_double_quoted_stripped(mod, tmp_path):
    f = tmp_path / "double-quoted.md"
    f.write_text('---\nconsumed_by: "789012"\n---\nbody\n')
    assert mod._fm_field(str(f), "consumed_by") == "789012"


def test_fm_field_empty_value_stays_empty(mod, tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("---\nconsumed_by:\n---\nbody\n")
    assert mod._fm_field(str(f), "consumed_by") == ""


# ---------------------------------------------------------------------------
# _shipped_orphan_sha — ship-check predicate P1-P4
# ---------------------------------------------------------------------------

def test_ac1_ship_path_picks_max_committer_timestamp_sha(tmp_path, mod):
    sandbox = _sandbox(tmp_path)
    session_id = "sess-ac1-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan.md")
    _write_handoff(orphan, "consumed", "in_flight", session_id)

    (tmp_path / "f1.txt").write_text("one\n")
    sha1 = _commit(sandbox, "f1.txt")
    time.sleep(1.1)
    (tmp_path / "f2.txt").write_text("two\n")
    sha2 = _commit(sandbox, "f2.txt")

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    # Newer commit (sha2) listed FIRST — regression guard against "first entry
    # wins" instead of true max-committer-timestamp selection.
    _write_completion(completion, session_id, f"commits:\n  - {sha2}\n  - {sha1}")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == sha2


def test_ac2_no_matching_completion_entry_is_empty(tmp_path, mod):
    sandbox = _sandbox(tmp_path)
    session_id = "sess-ac2-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan.md")
    _write_handoff(orphan, "consumed", "in_flight", session_id)

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == ""


def test_ac3_empty_commits_array_fails_closed(tmp_path, mod):
    sandbox = _sandbox(tmp_path)
    session_id = "sess-ac3-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan.md")
    _write_handoff(orphan, "consumed", "in_flight", session_id)

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(completion, session_id, "commits: []")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == ""


def test_ac3b_bare_scaffold_placeholder_authored_by_does_not_ship(tmp_path, mod):
    sandbox = _sandbox(tmp_path)
    session_id = "PLACEHOLDER-session-id"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan.md")
    _write_handoff(orphan, "consumed", "in_flight", session_id)

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(completion, session_id, "commits: []")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == ""


def test_finding0_regression_two_consumed_handoffs_fails_closed(tmp_path, mod):
    """LOAD-BEARING: session S consumed TWO handoffs (A, B); both carry
    consumed_by: S. P2's bounded-scan ambiguity gate must fail closed for B —
    a completion entry cannot be unambiguously bound to a specific
    consumption when the session consumed more than one handoff.
    """
    sandbox = _sandbox(tmp_path)
    session_id = "sess-finding0-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    handoff_a = os.path.join(handoffs_dir, "handoff-a.md")
    handoff_b = os.path.join(handoffs_dir, "handoff-b.md")
    _write_handoff(handoff_a, "consumed", "shipped", session_id)
    _write_handoff(handoff_b, "consumed", "in_flight", session_id)

    (tmp_path / "f1.txt").write_text("one\n")
    sha1 = _commit(sandbox, "f1.txt")

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(completion, session_id, f"commits:\n  - {sha1}")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, handoff_b, handoffs_dir, sandbox, query_cli=shim)
    assert got == "", "P2 ambiguity gate must fail closed when consumed_by appears on >1 handoff"


def test_archive_handoffs_scan_branch_ambiguity_across_directories(tmp_path, mod):
    """P2's archive/handoffs/** globstar-equivalent scan branch must also
    trigger the ambiguity fail-closed, not just the state/handoffs/ branch.
    """
    sandbox = _sandbox(tmp_path)
    session_id = "sess-archivescan-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    os.makedirs(os.path.join(sandbox, "archive/handoffs/2026-07"), exist_ok=True)
    archived_sibling = os.path.join(sandbox, "archive/handoffs/2026-07/handoff-archived.md")
    orphan_b = os.path.join(handoffs_dir, "orphan-b.md")

    _write_handoff(archived_sibling, "consumed", "shipped", session_id)
    _write_handoff(orphan_b, "consumed", "in_flight", session_id)

    (tmp_path / "f1.txt").write_text("one\n")
    sha1 = _commit(sandbox, "f1.txt")

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(completion, session_id, f"commits:\n  - {sha1}")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan_b, handoffs_dir, sandbox, query_cli=shim)
    assert got == "", "archive/handoffs/** scan branch must also fail closed on ambiguity"


def test_p4_all_unresolvable_commits_fail_closed(tmp_path, mod):
    """A non-empty commits[] array whose entries are ALL unresolvable must
    fail closed (empty), not echo a garbage SHA — regression guard for the
    `continue`-vs-`echo 0` P4 loop bug.
    """
    sandbox = _sandbox(tmp_path)
    session_id = "sess-garbage-commits-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan.md")
    _write_handoff(orphan, "consumed", "in_flight", session_id)

    fake_sha = "deadbeef00112233445566778899aabbccddeeff"
    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(
        completion, session_id,
        f'commits:\n  - "{fake_sha}"\n  - "<pending: some-note>"',
    )
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == ""


def test_claimed_by_ship_path_reclaims_new_vocab_orphan(tmp_path, mod):
    """DR-084 regression (code-reviewer Finding 1/2): `_shipped_orphan_sha`'s
    P2 bounded scan must dual-read via `_claim_holder` (claimed_by preferred,
    fall back to consumed_by) — before the fix it read raw `consumed_by`
    only, so a claimed_by-only (new-vocabulary) orphan's own scan entry never
    matched its dead holder's session id, match_count came back 0, P2 failed,
    and the ship-reclaim path silently stopped firing. Mirrors
    test_ac1_ship_path_picks_max_committer_timestamp_sha but built on a
    `_write_new_vocab_handoff` (claimed_by-only) fixture plus a matching
    completion-log entry, so the ship-check actually reaches — and exercises
    — P2 on migrated-vocabulary input. Must fail against the pre-fix code
    (asserted red-before/green-after in the completion report) and pass now.
    """
    sandbox = _sandbox(tmp_path)
    session_id = "sess-claimedvocab-ship-0001"
    handoffs_dir = os.path.join(sandbox, "state/handoffs")
    orphan = os.path.join(handoffs_dir, "orphan-claimedvocab.md")
    _write_new_vocab_handoff(orphan, "claimed-vocab ship-path orphan", session_id)

    (tmp_path / "f1.txt").write_text("one\n")
    sha1 = _commit(sandbox, "f1.txt")

    completion = os.path.join(sandbox, "archive/completed/2026-07/entry.md")
    _write_completion(completion, session_id, f"commits:\n  - {sha1}")
    _commit(sandbox, "archive/completed/2026-07/entry.md")

    shim = _query_shim(sandbox)
    got = mod._shipped_orphan_sha(session_id, orphan, handoffs_dir, sandbox, query_cli=shim)
    assert got == sha1


# ---------------------------------------------------------------------------
# End-to-end — dead-holder claim-release vs live-holder no-op
# ---------------------------------------------------------------------------

def _assert_released_status(text):
    """DR-084 dual-read assertion helper for the RELEASED-state OUTPUT.

    The writer (claude-klabauter's archive-stamp-cli, reached via
    reap-orphaned-in-flight-handoffs.py's unconsume-handoff dispatch) has
    already migrated the status-axis half of DR-084: a released claim now
    reads `status: open`, not the pre-DR-084 `status: active`. Accept both
    spellings for the migration window so this suite doesn't pin to a
    vocabulary the writer no longer emits. Collapses to asserting
    `status: open` alone once the window closes and the old-vocab path is
    retired from the writer. NOT for fixture-input assertions (those stay
    vocabulary-specific by design — see `_write_valid_handoff` /
    `_write_new_vocab_handoff`).
    """
    assert "status: open" in text or "status: active" in text, (
        f"released handoff must read status: open (or legacy status: active "
        f"during the DR-084 migration window); got neither in:\n{text}"
    )


def _run_target(sandbox, *extra_args):
    return subprocess.run(
        [sys.executable, _TARGET] + list(extra_args),
        cwd=sandbox, capture_output=True, text=True, creationflags=_NO_WINDOW,
    )


def test_dry_run_never_mutates_and_says_would_release(tmp_path):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-dead-holder-dryrun-0001"
    dead_handoff = os.path.join(sandbox, "state/handoffs/orphan-dead-holder-dryrun.md")
    _write_valid_handoff(dead_handoff, "dead-holder orphan (dry-run, should be untouched)", dead_sid)

    before = open(dead_handoff, encoding="utf-8").read()
    result = _run_target(sandbox, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "would release" in result.stdout
    assert "would reap" not in result.stdout
    assert "reaped" not in result.stdout
    assert "abandon" not in result.stdout
    assert open(dead_handoff, encoding="utf-8").read() == before, "dry-run must never mutate"


def test_dead_holder_released_live_holder_untouched(tmp_path):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-dead-holder-0001"
    live_sid = "sess-live-holder-0001"

    dead_handoff = os.path.join(sandbox, "state/handoffs/orphan-dead-holder.md")
    live_handoff = os.path.join(sandbox, "state/handoffs/orphan-live-holder.md")
    _write_valid_handoff(dead_handoff, "dead-holder orphan (should be released)", dead_sid)
    _write_valid_handoff(live_handoff, "live-holder orphan (should be untouched)", live_sid)

    # dead_sid deliberately gets NO session directory — absent session dir is
    # the simplest "dead" fixture: session_live() fails closed to not-live
    # immediately, with no meta.json to fabricate liveness from.
    _write_live_session(sandbox, live_sid)

    live_before = open(live_handoff, encoding="utf-8").read()

    result = _run_target(sandbox)
    assert result.returncode == 0, result.stderr

    dead_after = open(dead_handoff, encoding="utf-8").read()
    _assert_released_status(dead_after)
    assert "deployment_state: ready_to_fire" in dead_after
    assert "consumed_by:" not in dead_after
    assert "consumed_at:" not in dead_after
    assert "park_note:" in dead_after
    assert dead_sid in dead_after
    assert os.path.isfile(dead_handoff)
    assert "abandoned" not in dead_after

    assert "released" in result.stdout
    assert "reaped" not in result.stdout
    assert "abandoned" not in result.stdout

    live_after = open(live_handoff, encoding="utf-8").read()
    assert live_after == live_before, "live-holder handoff must be byte-for-byte untouched"
    assert "status: consumed" in live_after
    assert "deployment_state: in_flight" in live_after


# ---------------------------------------------------------------------------
# DR-084 dual-read — status:claimed + claimed_by: (new vocabulary). The
# corpus is mixed during the P1..P4 migration window (P0 schema-widen
# landed; on-disk records may carry either vocabulary) — these pin the
# regression that a single-vocabulary orphan filter strands new-vocabulary
# dead-holder claims forever (never reaped, never released to the pool).
# ---------------------------------------------------------------------------

def test_claimed_by_dead_holder_released(tmp_path):
    """Core regression: status: claimed + claimed_by: <dead session> +
    deployment_state: in_flight must be reaped exactly like the old
    consumed/consumed_by vocabulary — this FAILS against the unfixed
    single-vocabulary orphan-candidate filter.
    """
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-claimed-dead-holder-0001"

    dead_handoff = os.path.join(sandbox, "state/handoffs/orphan-claimed-dead-holder.md")
    _write_new_vocab_handoff(dead_handoff, "claimed-vocab dead-holder orphan (should be released)", dead_sid)

    # dead_sid deliberately gets NO session directory — absent session dir is
    # the simplest "dead" fixture: session_live() fails closed to not-live
    # immediately, with no meta.json to fabricate liveness from.

    result = _run_target(sandbox)
    assert result.returncode == 0, result.stderr

    dead_after = open(dead_handoff, encoding="utf-8").read()
    _assert_released_status(dead_after)
    assert "deployment_state: ready_to_fire" in dead_after
    assert "claimed_by:" not in dead_after
    assert "claimed_at:" not in dead_after
    assert "park_note:" in dead_after
    assert dead_sid in dead_after
    assert os.path.isfile(dead_handoff)
    assert "abandoned" not in dead_after

    assert "released" in result.stdout
    assert "reaped" not in result.stdout
    assert "abandoned" not in result.stdout


def test_claimed_by_live_holder_untouched(tmp_path):
    """status: claimed + claimed_by: <LIVE session> must NOT be reaped —
    the new-vocabulary path must respect the same liveness gate as the old
    consumed/consumed_by vocabulary.
    """
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    live_sid = "sess-claimed-live-holder-0001"

    live_handoff = os.path.join(sandbox, "state/handoffs/orphan-claimed-live-holder.md")
    _write_new_vocab_handoff(live_handoff, "claimed-vocab live-holder orphan (should be untouched)", live_sid)
    _write_live_session(sandbox, live_sid)

    live_before = open(live_handoff, encoding="utf-8").read()

    result = _run_target(sandbox)
    assert result.returncode == 0, result.stderr

    live_after = open(live_handoff, encoding="utf-8").read()
    assert live_after == live_before, "live-holder handoff must be byte-for-byte untouched"
    assert "status: claimed" in live_after
    assert "deployment_state: in_flight" in live_after
    assert "no orphaned in_flight handoffs released" in result.stdout


def test_claimed_by_e2e_ship_path_dispatches_stamp_shipped_in(tmp_path, mod, monkeypatch, capsys):
    """Regression for the reaper ship-path TOCTOU status-vocab defect
    (cross-repo/inbox/2026-07-22-claude-central-em-dr084-reaper-toctou-status-vocab.md):
    the TOCTOU re-assert at the ship-path gate used to compare
    `now_status == "consumed"` only (single old-vocab), while the candidate
    filter accepted both `consumed` and `claimed`. A `status: claimed`
    dead-holder orphan that legitimately earned a ship SHA therefore took
    NEITHER exit — not shipped (this gate `continue`d it), and never falling
    through to claim-release either (the `continue` happens before that code
    is reached) — permanently stranding it status:claimed +
    deployment_state:in_flight, re-failing identically on every subsequent
    run.

    This is the uncovered cell the existing suite missed: e2e (drives the
    real `main()`, unlike `test_claimed_by_ship_path_reclaims_new_vocab_orphan`
    which calls `_shipped_orphan_sha` directly) x new vocabulary (status:
    claimed + claimed_by:, via `_write_new_vocab_handoff`) x ship path
    (`_shipped_orphan_sha` monkeypatched truthy, unlike
    `test_claimed_by_dead_holder_released` which writes no completion entry
    and so exercises claim-release instead). Fails against the unfixed
    `now_status == "consumed"` gate (asserted red-before/green-after in the
    completion report); passes now that the gate uses the shared
    `is_claimed_status` accessor.
    """
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-claimedvocab-e2e-ship-0001"
    orphan = os.path.join(sandbox, "state/handoffs/orphan-claimedvocab-e2e-ship.md")
    _write_new_vocab_handoff(orphan, "claimed-vocab e2e ship-path orphan", dead_sid)

    # dead_sid deliberately gets NO session directory — absent session dir is
    # the simplest "dead" fixture (see test_claimed_by_dead_holder_released).

    fake_sha = "deadbeef00112233445566778899aabbccddeeff"
    monkeypatch.setattr(mod, "_shipped_orphan_sha", lambda *a, **kw: fake_sha)

    stamp_calls = []

    def _fake_archive_stamp_cli(args):
        stamp_calls.append(args)
        if args[0] == "stamp-shipped-in":
            # Simulate the stamp landing: write shipped_in into frontmatter
            # so the module's post-stamp TOCTOU re-read sees it populated —
            # mirrors test_warn_degraded_ship_path_never_consults_guard's
            # fixture-mutation idiom.
            text = open(orphan, encoding="utf-8").read()
            text = text.replace("---\n", f"---\nshipped_in: {fake_sha}\n", 1)
            with open(orphan, "w", encoding="utf-8") as fh:
                fh.write(text)
            return True
        if args[0] == "ship-handoff":
            return True
        raise AssertionError(f"unexpected archive-stamp-cli verb: {args[0]}")

    monkeypatch.setattr(mod, "_run_archive_stamp_cli", _fake_archive_stamp_cli)

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, _err = capsys.readouterr()

    assert rc == 0
    assert [c[0] for c in stamp_calls] == ["stamp-shipped-in", "ship-handoff"], (
        "a status:claimed dead-holder orphan with a genuine ship-check hit must "
        "dispatch stamp-shipped-in then ship-handoff via the ship path — the "
        "unfixed TOCTOU re-assert (`now_status == \"consumed\"` only) silently "
        "`continue`d before either verb dispatched, taking neither the ship exit "
        "nor the claim-release fall-through and permanently stranding the handoff"
    )
    assert "reclaimed (shipped)" in out
    assert "reclaimed as shipped" in out


def test_claimed_status_no_holder_fails_closed(tmp_path, mod, capsys, monkeypatch):
    """status: claimed + deployment_state: in_flight with NEITHER
    claimed_by: NOR consumed_by: recorded must hit the fail-closed
    skipped_no_holder path — cannot evaluate liveness without a holder.
    """
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    orphan = os.path.join(sandbox, "state/handoffs/orphan-claimed-no-holder.md")
    _write_no_holder_claimed_handoff(orphan, "claimed-vocab no-holder orphan (should be retained)")

    before = open(orphan, encoding="utf-8").read()

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, _err = capsys.readouterr()

    assert rc == 0
    assert open(orphan, encoding="utf-8").read() == before, "no-holder-recorded orphan must be untouched"
    # Review: code-reviewer — Finding 3. Message now reflects that the
    # skipped_no_holder branch fires when NEITHER claimed_by: nor
    # consumed_by: is populated (DR-084 dual-read), not consumed_by alone.
    assert "consumed+in_flight handoffs retained (no claimed_by/consumed_by recorded)" in out
    assert "returned to pool" not in out
    assert "no orphaned in_flight handoffs released" in out


# ---------------------------------------------------------------------------
# Reverse-membership (live-children) guard on the clean release fall-through
# (_has_live_children_exit_code) — dispatched in-process via mod.main() with
# the guard seam and _run_archive_stamp_cli stubbed, per the module's own
# clean-fallthrough contract: reachable only when the ship-check (P1-P4)
# returns "" — a dead-holder orphan with no completion-log entry.
# ---------------------------------------------------------------------------

def _dead_orphan_no_completion(sandbox, sid, name):
    """A consumed+in_flight orphan with a dead holder and no completion entry
    anywhere — the ship-check (P3) fails closed, so sha stays "" and the
    clean fall-through (and its live-children guard) is reached.
    """
    handoff = os.path.join(sandbox, "state/handoffs", name)
    _write_valid_handoff(handoff, f"dead-holder orphan ({name})", sid)
    return handoff


def test_guard_exit_0_has_live_children_skips_act_mode(tmp_path, mod, monkeypatch, capsys):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-guard-haschildren-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-has-children.md")
    before = open(orphan, encoding="utf-8").read()

    monkeypatch.setattr(mod, "_has_live_children_exit_code", lambda path: 0)
    calls = []
    monkeypatch.setattr(mod, "_run_archive_stamp_cli", lambda args: (calls.append(args), True)[1])

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, err = capsys.readouterr()

    assert rc == 0
    assert calls == [], "unconsume-handoff must NOT be dispatched when the guard reports live children"
    assert open(orphan, encoding="utf-8").read() == before, "frontmatter must be untouched on a guard skip"
    assert "1 orphaned in_flight handoffs skipped (live-children guard)" in out
    assert orphan in err, "the skip log line must name the file"
    assert "has a live succession child" in err


def test_guard_exit_1_childless_release_unchanged(tmp_path, mod, monkeypatch):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-guard-childless-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-childless.md")

    monkeypatch.setattr(mod, "_has_live_children_exit_code", lambda path: 1)
    calls = []
    monkeypatch.setattr(mod, "_run_archive_stamp_cli", lambda args: (calls.append(args), True)[1])

    monkeypatch.chdir(sandbox)
    rc = mod.main([])

    assert rc == 0
    assert len(calls) == 1, "unconsume-handoff must still be dispatched when the guard reports childless"
    assert calls[0][0] == "unconsume-handoff"
    assert calls[0][1] == orphan


def test_guard_exit_2_indeterminate_fails_closed(tmp_path, mod, monkeypatch, capsys):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-guard-indeterminate-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-indeterminate.md")
    before = open(orphan, encoding="utf-8").read()

    monkeypatch.setattr(mod, "_has_live_children_exit_code", lambda path: 2)
    calls = []
    monkeypatch.setattr(mod, "_run_archive_stamp_cli", lambda args: (calls.append(args), True)[1])

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, err = capsys.readouterr()

    assert rc == 0
    assert calls == [], "an indeterminate guard result must fail closed to skip, not release"
    assert open(orphan, encoding="utf-8").read() == before
    assert "1 orphaned in_flight handoffs skipped (live-children guard)" in out
    assert "live-children guard indeterminate; fail-closed" in err


def test_guard_spawn_oserror_fails_closed(tmp_path, mod, monkeypatch, capsys):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-guard-oserror-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-oserror.md")
    before = open(orphan, encoding="utf-8").read()

    real_run = mod.subprocess.run

    def _raising_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and mod._HAS_LIVE_CHILDREN_CLI in cmd:
            raise OSError("simulated subprocess spawn failure")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _raising_run)
    calls = []
    monkeypatch.setattr(mod, "_run_archive_stamp_cli", lambda args: (calls.append(args), True)[1])

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, err = capsys.readouterr()

    assert rc == 0
    assert calls == [], "a guard subprocess spawn failure (OSError) must fail closed to skip, not release"
    assert open(orphan, encoding="utf-8").read() == before
    assert "1 orphaned in_flight handoffs skipped (live-children guard)" in out


def test_warn_degraded_ship_path_never_consults_guard(tmp_path, mod, monkeypatch, capsys):
    """WARN-degraded ship path (Lens 4 gap, code-reviewer finding): the
    ship-check (P1-P4) succeeds (`_shipped_orphan_sha` returns a truthy sha)
    and `stamp-shipped-in` lands, but the subsequent `ship-handoff` verb call
    fails. Per the module's own comment at lines 504-510, this degraded path
    must keep falling through toward release WITHOUT the live-children guard
    ever being consulted — the guard is reachable only from the clean
    fall-through where sha stays "". This pins by test the invariant the
    review verified only by manual trace.
    """
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-warn-degraded-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-warn-degraded.md")
    fake_sha = "deadbeef00112233445566778899aabbccddeeff"

    monkeypatch.setattr(mod, "_shipped_orphan_sha", lambda *a, **kw: fake_sha)

    stamp_calls = []

    def _fake_archive_stamp_cli(args):
        stamp_calls.append(args)
        if args[0] == "stamp-shipped-in":
            # Simulate the stamp landing: write shipped_in into frontmatter
            # so the module's post-stamp re-read sees it as populated.
            text = open(orphan, encoding="utf-8").read()
            text = text.replace("---\n", f"---\nshipped_in: {fake_sha}\n", 1)
            with open(orphan, "w", encoding="utf-8") as fh:
                fh.write(text)
            return True
        if args[0] == "ship-handoff":
            return False  # the ship-verb failure this WARN-degraded path guards
        if args[0] == "unconsume-handoff":
            return True
        raise AssertionError(f"unexpected archive-stamp-cli verb: {args[0]}")

    monkeypatch.setattr(mod, "_run_archive_stamp_cli", _fake_archive_stamp_cli)

    def _guard_must_not_be_called(path):
        raise AssertionError("live-children guard must not be consulted on the WARN-degraded ship path")

    monkeypatch.setattr(mod, "_has_live_children_exit_code", _guard_must_not_be_called)

    monkeypatch.chdir(sandbox)
    rc = mod.main([])
    out, err = capsys.readouterr()

    assert rc == 0
    assert [c[0] for c in stamp_calls] == ["stamp-shipped-in", "ship-handoff", "unconsume-handoff"], (
        "degraded path must fall through to the ordinary unconsume-handoff claim-release dispatch"
    )
    assert "error shipping" in err
    assert "falling through to claim-release" in err
    assert "released" in out


def test_guard_exit_0_dry_run_says_would_skip_not_would_release(tmp_path, mod, monkeypatch, capsys):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", ".git/coordinator-sessions"))
    dead_sid = "sess-guard-dryrun-0001"
    orphan = _dead_orphan_no_completion(sandbox, dead_sid, "orphan-dryrun-guard.md")
    before = open(orphan, encoding="utf-8").read()

    monkeypatch.setattr(mod, "_has_live_children_exit_code", lambda path: 0)
    calls = []
    monkeypatch.setattr(mod, "_run_archive_stamp_cli", lambda args: (calls.append(args), True)[1])

    monkeypatch.chdir(sandbox)
    rc = mod.main(["--dry-run"])
    out, _err = capsys.readouterr()

    assert rc == 0
    assert calls == [], "dry-run must never mutate, even on a guard skip"
    assert open(orphan, encoding="utf-8").read() == before
    assert "would skip" in out
    assert "would release" not in out


# ---------------------------------------------------------------------------
# Archived-twin guard — DR-084 C8 incident regression (example-doctrine-repo commits
# 339b269a / 073b6b1f / a33f3598). A live-path in_flight candidate whose
# handoff_id also exists under archive/handoffs/ is residue, never a real
# orphan — refuse to claim-release/resurrect it. See
# reap-orphaned-in-flight-handoffs.py's _handoff_id_archived_twin() docstring.
# ---------------------------------------------------------------------------

def _write_valid_handoff_with_handoff_id(path, title, consumed_by, handoff_id):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
schema: handoff
title: "{title}"
handoff_id: "{handoff_id}"
status: consumed
deployment_state: in_flight
consumed_at: 2026-07-20T10:00:00Z
consumed_by: {consumed_by}
predecessor: none
kind: session-handoff
category: infra
summary: "fixture handoff for archived-twin guard regression test"
pickup_ready: true
created: 2026-07-20
created_at: 2026-07-20
branch: work/machine-a/2026-07-20
---
# Fixture Handoff — {title}
body
""")


def _write_archived_twin(path, handoff_id):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"""---
schema: handoff
title: "archived twin"
handoff_id: "{handoff_id}"
status: claimed
deployment_state: closed
closed_reason: stale
predecessor: none
kind: session-handoff
category: infra
summary: "archived twin fixture for archived-twin guard regression test"
pickup_ready: false
created: 2026-07-18
created_at: 2026-07-18
branch: work/machine-a/2026-07-18
---
# Fixture Handoff — archived twin
body
""")


def test_archived_twin_guard_skips_live_duplicate_dead_holder(tmp_path):
    """A dead-holder in_flight candidate whose handoff_id also exists in
    archive/handoffs/ must be SKIPPED, not claim-released -- this is exactly
    the shape the crash-orphan reaper mis-resurrected in the DR-084 C8
    incident (a33f3598)."""
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", "archive/handoffs/2026-07", ".git/coordinator-sessions"))
    dead_sid = "sess-archived-twin-guard-0001"
    handoff_id = "hnd-archived-twin-guard-test"

    live_handoff = os.path.join(sandbox, "state/handoffs/orphan-archived-twin.md")
    archived_twin = os.path.join(sandbox, "archive/handoffs/2026-07/orphan-archived-twin.md")
    _write_valid_handoff_with_handoff_id(live_handoff, "live residue (must be skipped)", dead_sid, handoff_id)
    _write_archived_twin(archived_twin, handoff_id)

    live_before = open(live_handoff, encoding="utf-8").read()
    archived_before = open(archived_twin, encoding="utf-8").read()

    result = _run_target(sandbox)
    assert result.returncode == 0, result.stderr

    assert open(live_handoff, encoding="utf-8").read() == live_before, "live residue must be left byte-for-byte untouched"
    assert open(archived_twin, encoding="utf-8").read() == archived_before, "archived twin must not be touched by the reaper either"
    assert "archived-twin guard" in result.stdout
    assert "no orphaned in_flight handoffs released" in result.stdout


def test_archived_twin_guard_dry_run_says_would_skip(tmp_path):
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", "archive/handoffs/2026-07", ".git/coordinator-sessions"))
    dead_sid = "sess-archived-twin-guard-dryrun-0001"
    handoff_id = "hnd-archived-twin-guard-dryrun-test"

    live_handoff = os.path.join(sandbox, "state/handoffs/orphan-archived-twin-dryrun.md")
    archived_twin = os.path.join(sandbox, "archive/handoffs/2026-07/orphan-archived-twin-dryrun.md")
    _write_valid_handoff_with_handoff_id(live_handoff, "live residue (dry-run, should be untouched)", dead_sid, handoff_id)
    _write_archived_twin(archived_twin, handoff_id)

    before = open(live_handoff, encoding="utf-8").read()
    result = _run_target(sandbox, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert open(live_handoff, encoding="utf-8").read() == before
    assert "would be skipped (archived-twin guard, dry-run)" in result.stdout
    assert "would release" not in result.stdout or "0 orphaned" in result.stdout


def test_no_archived_twin_dead_holder_still_released(tmp_path):
    """Control case: a dead-holder candidate with NO archive twin (the
    ordinary, common shape) must still be released exactly as before -- the
    new guard must not over-fire on records that were never duplicated."""
    sandbox = _sandbox(tmp_path, extra_dirs=("state/handoffs", "archive/handoffs/2026-07", ".git/coordinator-sessions"))
    dead_sid = "sess-no-archived-twin-0001"
    handoff_id = "hnd-no-archived-twin-test-a1b2c3"

    live_handoff = os.path.join(sandbox, "state/handoffs/orphan-no-twin.md")
    _write_valid_handoff_with_handoff_id(live_handoff, "ordinary orphan (should be released)", dead_sid, handoff_id)

    result = _run_target(sandbox)
    assert result.returncode == 0, result.stderr

    after = open(live_handoff, encoding="utf-8").read()
    _assert_released_status(after)
    assert "archived-twin guard" not in result.stdout
