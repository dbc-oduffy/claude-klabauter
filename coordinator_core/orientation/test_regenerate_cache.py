"""
coordinator_core.orientation.test_regenerate_cache — tests for the
``--pinboard-only`` fast path (patch_pinboard_only) added on top of the
existing byte-for-byte bash-oracle port.

Spec backlink: pln-wsc-tail-slim-down-op-scoped-c-e9a265 (C18)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.orientation import regenerate_cache as mod

# Declared, not excused: `_init_git_repo` spawns real git to give
# `emit_recent_commits` genuine `git log` output to render (including a real
# merge history for the ## Recent commits section), and the CLI-trampoline
# tests at the bottom of this file spawn the real
# coordinator/bin/regenerate-orientation-cache script as a subprocess (it is
# not a `.py`-suffixed, cleanly importable module) to exercise its actual
# --invoker/--pinboard-only argv contract. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "tasks").mkdir(parents=True)
    (repo / "state").mkdir()
    # `.git` marker: `housekeeping_liveness.liveness_path` now validates that repo_root
    # resolves to a git repo -- every fixture that stamps/reads through that seam needs
    # a real (if minimal) marker, not a bare non-git tmp dir.
    (repo / ".git").mkdir()
    return repo


def _init_git_repo(repo: Path, commit_subjects: list) -> None:
    """Turn *repo* into a real git repo with one commit per entry in
    *commit_subjects* (oldest first), so ``emit_recent_commits`` has real
    ``git log`` output to render instead of degrading to ``[]``."""
    env = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    for i, subject in enumerate(commit_subjects):
        (repo / f"file-{i}.txt").write_text(subject, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", subject], cwd=str(repo), check=True, env=env
        , **no_console_passthrough_kwargs())


def _seed_full_cache(repo: Path, pinboard: str = "") -> Path:
    """Build+write a full cache once (the thing patch_pinboard_only must NOT redo)."""
    result = mod.build_cache(
        invoker="handoff",
        repo_root=repo,
        pinboard=pinboard,
        pinboard_set=bool(pinboard),
    )
    assert not result["skipped"]
    mod.write_cache(result["cache_file"], result["output"])
    return result["cache_file"]


def test_pinboard_only_performs_zero_section_rederive(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="2026-07-23 handoff: old note")

    calls = {"git": 0, "find": 0, "uproject": 0, "workstreams": 0, "rechecks": 0, "branch": 0, "push_health": 0}

    def _boom_git(*a, **k):
        calls["git"] += 1
        raise AssertionError("patch_pinboard_only must not spawn git")

    def _boom_find(*a, **k):
        calls["find"] += 1
        raise AssertionError("patch_pinboard_only must not run a find(1) uproject walk")

    def _boom_uproject(*a, **k):
        calls["uproject"] += 1
        raise AssertionError("patch_pinboard_only must not re-derive *.uproject detection")

    def _boom_workstreams(*a, **k):
        calls["workstreams"] += 1
        raise AssertionError("patch_pinboard_only must not glob tasks/ for workstreams")

    def _boom_rechecks(*a, **k):
        calls["rechecks"] += 1
        raise AssertionError("patch_pinboard_only must not glob tasks/ for rechecks")

    monkeypatch.setattr(mod, "_git", _boom_git)
    monkeypatch.setattr(mod, "_find_uproject", _boom_find)
    monkeypatch.setattr(mod, "detect_uproject", _boom_uproject)
    monkeypatch.setattr(mod, "emit_workstreams", _boom_workstreams)
    monkeypatch.setattr(mod, "emit_rechecks", _boom_rechecks)
    monkeypatch.setattr(mod, "emit_branch_line", lambda *a, **k: calls.__setitem__("branch", calls["branch"] + 1))
    monkeypatch.setattr(
        mod, "emit_auto_push_health", lambda *a, **k: calls.__setitem__("push_health", calls["push_health"] + 1)
    )

    before = cache_file.read_text(encoding="utf-8")

    mod.patch_pinboard_only(cache_file, "2026-07-23 handoff: new note")

    assert calls == {"git": 0, "find": 0, "uproject": 0, "workstreams": 0, "rechecks": 0, "branch": 0, "push_health": 0}

    after = cache_file.read_text(encoding="utf-8")
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert before_lines[:-2] == after_lines[:-2], "every non-pinboard section must stay byte-identical"
    assert after_lines[-2:] == ["## Pinboard", "- 2026-07-23 handoff: new note"]
    assert before_lines[-2:] == ["## Pinboard", "- 2026-07-23 handoff: old note"]


_GENERATED_AT_RE = re.compile(r"^generated_at: .*$", re.MULTILINE)


def _normalize_generated_at(text: str) -> str:
    """Blank out the ``generated_at:`` frontmatter value before a byte-for-byte compare.

    `_render_cache` embeds `datetime.now(timezone.utc)` at one-second granularity
    (regenerate_cache.py's ``iso_now`` in `build_cache`, consumed at `_render_cache`'s
    frontmatter template). `test_pinboard_only_byte_matches_native_full_render` seeds one
    render via `_seed_full_cache` -> `build_cache` and then compares TWO further independent
    `build_cache`/`patch_pinboard_only` calls against each other -- if the wall clock ticks
    over a second boundary between either pair, the two `generated_at` values differ even
    though every byte this test actually means to guard (the section-separator contract) is
    identical, reddening the test on a real clock tick with no code change (reproduced
    2026-07-23: red once, green on immediate re-run). Normalizing this ONE field's value
    keeps the guard at full strength for what it actually asserts -- the section-separator
    byte-contract on insert/replace/clear -- while making the comparison deterministic with
    respect to wall-clock timing, which this test was never meant to exercise.
    """
    return _GENERATED_AT_RE.sub("generated_at: <normalized>", text)


def test_pinboard_only_byte_matches_native_full_render(tmp_path):
    """The patched file must be BYTE-IDENTICAL (modulo `generated_at`, see
    `_normalize_generated_at`) to what a full regen would have produced with the same
    pinboard value — insert, replace, and clear cases. Regression guard: an earlier draft
    appended only a single "\\n" before a freshly-inserted "## Pinboard" section, whereas
    _render_cache always separates sections with a blank line ("\\n\\n## X") — this silently
    diverged from the byte-for-byte contract on the from-empty insert path."""
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="")

    native_with_note = mod.build_cache(
        invoker="handoff", repo_root=repo, pinboard="orig note", pinboard_set=True
    )["output"]
    patched_with_note = mod.patch_pinboard_only(cache_file, "orig note")
    assert _normalize_generated_at(patched_with_note) == _normalize_generated_at(native_with_note)

    native_replaced = mod.build_cache(
        invoker="handoff", repo_root=repo, pinboard="second note", pinboard_set=True
    )["output"]
    patched_replaced = mod.patch_pinboard_only(cache_file, "second note")
    assert _normalize_generated_at(patched_replaced) == _normalize_generated_at(native_replaced)

    native_cleared = mod.build_cache(invoker="handoff", repo_root=repo, pinboard="", pinboard_set=True)[
        "output"
    ]
    patched_cleared = mod.patch_pinboard_only(cache_file, "")
    assert _normalize_generated_at(patched_cleared) == _normalize_generated_at(native_cleared)


def test_pinboard_only_can_insert_when_no_prior_pinboard(tmp_path):
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="")
    before = cache_file.read_text(encoding="utf-8")
    assert "## Pinboard" not in before

    mod.patch_pinboard_only(cache_file, "2026-07-23 handoff: first note")

    after = cache_file.read_text(encoding="utf-8")
    assert after.startswith(before.rstrip("\n"))
    assert after.rstrip("\n").endswith("## Pinboard\n- 2026-07-23 handoff: first note")


def test_pinboard_only_can_clear(tmp_path):
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="2026-07-23 handoff: note")
    assert "## Pinboard" in cache_file.read_text(encoding="utf-8")

    mod.patch_pinboard_only(cache_file, "")

    after = cache_file.read_text(encoding="utf-8")
    assert "## Pinboard" not in after


def test_pinboard_only_check_mode_does_not_write(tmp_path):
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="2026-07-23 handoff: note")
    before = cache_file.read_text(encoding="utf-8")

    output = mod.patch_pinboard_only(cache_file, "2026-07-23 handoff: would-be note", check=True)

    assert cache_file.read_text(encoding="utf-8") == before
    assert "would-be note" in output


def test_pinboard_only_missing_cache_file_raises(tmp_path):
    repo = _make_repo(tmp_path)
    missing = repo / "state" / "orientation_cache.md"
    with pytest.raises(FileNotFoundError):
        mod.patch_pinboard_only(missing, "note")


def test_write_cache_and_patch_pinboard_only_serialize_via_same_lock(tmp_path, monkeypatch):
    """Both writers must contend on the identical lock directory so a
    concurrent full regen and a pinboard-only patch never interleave their
    writes (see patch_pinboard_only's docstring for the residual race this
    does NOT close — the build_cache-side pre-lock read)."""
    import os

    monkeypatch.setattr(mod, "_LOCK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mod, "_LOCK_POLL_S", 0.01)

    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="")
    lock_dir = cache_file.parent / mod._LOCK_DIR_SUFFIX

    os.mkdir(lock_dir)
    try:
        with pytest.raises(TimeoutError):
            mod.write_cache(cache_file, "irrelevant")
    finally:
        os.rmdir(lock_dir)

    # patch_pinboard_only contends on the SAME lock dir name
    os.mkdir(lock_dir)
    try:
        with pytest.raises(TimeoutError):
            mod.patch_pinboard_only(cache_file, "note")
    finally:
        os.rmdir(lock_dir)


# ---------------------------------------------------------------------------
# C17b: ## Housekeeping surfacing (housekeeping-failures.log + liveness stamps)
# ---------------------------------------------------------------------------


def test_full_regen_surfaces_synthetic_failed_spawn(tmp_path):
    """2026-07-28: the raw ``CHILD FAILED``/``SPAWN FAILED`` verb is no longer rendered
    verbatim (Defect 2 fix -- _emit_housekeeping now dedups+caps rather than dumping raw
    log lines; the verb is still part of the internal dedup key, see
    _dedup_failure_lines, just not displayed -- matches the dispatcher's own rendering
    example, which likewise omits it)."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/some/housekeeping_cli.py", exit_code=1)

    result = mod.build_cache(invoker="handoff", repo_root=repo, pinboard="note", pinboard_set=True)
    assert not result["skipped"]
    output = result["output"]

    assert "## Housekeeping" in output
    assert "non-zero exit 1" in output
    assert "housekeeping_cli.py" in output
    # Housekeeping precedes Pinboard -- patch_pinboard_only's "Pinboard is last" invariant.
    assert output.index("## Housekeeping") < output.index("## Pinboard")


def test_full_regen_omits_housekeeping_section_when_clean(tmp_path):
    repo = _make_repo(tmp_path)

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)

    assert "## Housekeeping" not in result["output"]


def test_full_regen_surfaces_synthetic_stale_liveness_key(tmp_path):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl
    from datetime import datetime, timedelta, timezone

    repo = _make_repo(tmp_path)
    liveness_path = hl.liveness_path(str(repo))
    liveness_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    liveness_path.write_text('{"' + hl.ARCHIVE_SWEEPS + '": "' + stale_ts + '"}', encoding="utf-8")

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)

    assert "## Housekeeping" in result["output"]
    assert "stale" in result["output"]
    assert hl.ARCHIVE_SWEEPS in result["output"]


def test_check_mode_peeks_without_clearing_failures_log(tmp_path):
    """--check is a true dry run: build_cache's own peek must not clear the log --
    only the write call site (CLI/RPC), after a real non-check write, may clear it."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    assert "## Housekeeping" in result["output"]

    # The log must still be there -- build_cache alone (no write_cache call) never clears it.
    assert detached_spawn.read_failures_log(str(repo)) != ""


def test_write_cache_then_clear_delivers_failure_exactly_once(tmp_path):
    """Simulates the CLI trampoline / RPC handler's own sequence: build -> write ->
    clear. After the clear, a fresh regen with no new failures omits the section."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    assert "## Housekeeping" in result["output"]
    mod.write_cache(result["cache_file"], result["output"])
    detached_spawn.clear_failures_log(str(repo))

    assert detached_spawn.read_failures_log(str(repo)) == ""

    result2 = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    assert "## Housekeeping" not in result2["output"]


# ---------------------------------------------------------------------------
# C21 leg 2: remedy commands surfaced alongside failures / stale liveness keys
# ---------------------------------------------------------------------------


def test_archive_sweeps_failure_reports_the_failure_and_invents_no_remedy(tmp_path):
    """A failure record still surfaces as a failure bullet — and carries NO remedy
    sub-bullet, because `REMEDY_COMMANDS[ARCHIVE_SWEEPS]` is now empty.

    This test used to assert `python3 coordinator/bin/sweep-shipped-handoffs.py`
    appeared as a sub-bullet. That script was DELETED at `648f2e4eb` under the
    brightline kill bar, and the same commit correctly emptied the remedy tuple —
    but missed this file, so the assertion outlived the script it named by
    pointing the reader at a remedy that cannot be run. Repointed rather than
    repopulated: `housekeeping_liveness`'s own negative-spec says a class with no
    on-demand CLI renders nothing and "do not invent commands to fill a gap", so
    putting a command back to make this green would be the forbidden move, not
    the fix. The rendering machinery itself is covered by
    `test_remedy_sub_bullets_render_when_a_class_has_commands` below, which
    supplies its own commands instead of borrowing a dead one.
    """
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(
        str(repo), "coordinator/bin/sweep-shipped-handoffs.py", exit_code=1
    )

    result = mod.build_cache(invoker="handoff", repo_root=repo, pinboard="note", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert "sweep-shipped-handoffs.py" in output
    assert "python3 coordinator/bin/sweep-shipped-handoffs.py" not in output


def test_remedy_sub_bullets_render_when_a_class_has_commands(monkeypatch):
    """The sub-bullet renderer, exercised without naming a real command.

    Every entry in `REMEDY_COMMANDS` is an empty tuple today, so nothing in the
    live map can prove this formatting still works — and the two tests that used
    to prove it were doing so against a script that has since been deleted. A
    supplied command keeps the mechanism covered without re-asserting a dead one.
    """
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl

    monkeypatch.setitem(mod.REMEDY_COMMANDS, hl.ARCHIVE_SWEEPS, ("python3 some/cli.py",))

    assert mod._remedy_sub_bullets(hl.ARCHIVE_SWEEPS) == ["  - `python3 some/cli.py`"]
    assert mod._remedy_sub_bullets(hl.ROADMAP_CALLOUT) == []


def test_unrelated_failure_does_not_surface_archive_sweeps_remedy(tmp_path):
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/some/other_cli.py", exit_code=1)

    result = mod.build_cache(invoker="handoff", repo_root=repo, pinboard="note", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert "sweep-shipped-handoffs.py" not in output


def test_stale_archive_sweeps_liveness_surfaces_remedy_sub_bullets(tmp_path):
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl
    from datetime import datetime, timedelta, timezone

    repo = _make_repo(tmp_path)
    liveness_path = hl.liveness_path(str(repo))
    liveness_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    liveness_path.write_text('{"' + hl.ARCHIVE_SWEEPS + '": "' + stale_ts + '"}', encoding="utf-8")

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "stale" in output
    # No remedy sub-bullet: the archive_sweeps CLI was deleted at 648f2e4eb and its
    # remedy tuple emptied with it. See
    # test_archive_sweeps_failure_reports_the_failure_and_invents_no_remedy above
    # for why this asserts absence rather than naming a replacement command.
    assert "python3 coordinator/bin/sweep-shipped-handoffs.py" not in output


def test_stale_class_with_no_remedy_renders_no_extra_commands(tmp_path):
    """ROADMAP_CALLOUT has no on-demand CLI (REMEDY_COMMANDS maps it to ()) -- its stale
    bullet must render with NO sub-bullet commands invented."""
    from coordinator_core.ops.ceremony import housekeeping_liveness as hl
    from datetime import datetime, timedelta, timezone

    repo = _make_repo(tmp_path)
    liveness_path = hl.liveness_path(str(repo))
    liveness_path.parent.mkdir(parents=True, exist_ok=True)
    stale_ts = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    liveness_path.write_text('{"' + hl.ROADMAP_CALLOUT + '": "' + stale_ts + '"}', encoding="utf-8")

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert hl.ROADMAP_CALLOUT in output
    assert "sweep-" not in output


def test_pinboard_only_rederives_housekeeping_section(tmp_path):
    """AC5 fix (2026-07-23): --pinboard-only now RE-DERIVES the ## Housekeeping section
    (only Housekeeping + Pinboard are re-derived; every other section stays byte-identical
    per test_pinboard_only_performs_zero_section_rederive above).

    This test used to be `test_pinboard_only_never_touches_housekeeping_section` and pinned
    the OLD, now-wrong behaviour (housekeeping frozen at seed time, surviving byte-identical
    through a pinboard-only patch). That was exactly the AC5 hole reported in
    `cross-repo/inbox/2026-07-23-claude-central-em-wsc-tail-preflight-doe-reply.md`
    § "New -- an AC5 hole your own ask 4 opens": DoE adopted --pinboard-only at BOTH
    mid-session call sites (/workstream-complete, /handoff), so a failure recorded between
    two full regens would not reach the EM within one session boundary. DO NOT "restore" the
    old assertions below -- they encode the bug, not the contract.

    2026-07-28 update: this test ALSO used to assert the fast path never clears the
    failures log after embedding it (that was Defect 1 -- see
    cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md
    -- a third write path that delivered records into the cache without ever completing
    the deliver-exactly-once contract, so every record surfaced here would repeat at
    every future regen forever). Now that `patch_pinboard_only` clears after a real,
    non-check write that actually embedded content, the log IS expected to be empty
    afterward -- see `test_write_cache_then_clear_delivers_failure_exactly_once` for the
    analogous full-regen idiom this now matches.
    """
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)
    cache_file = _seed_full_cache(repo, pinboard="old note")
    before = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" in before
    assert "x.py" in before  # basename rendering, see Defect 2 fix note above

    # A SECOND failure recorded after the seed -- the fix must pick this up too, proving
    # a real re-derive (not merely "still shows the seed-time content").
    detached_spawn.record_child_failure(str(repo), "/y.py", exit_code=1)

    mod.patch_pinboard_only(cache_file, "new note")

    after = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" in after
    assert "x.py" in after
    assert "y.py" in after
    # Housekeeping still precedes Pinboard (patch_pinboard_only's own invariant).
    assert after.index("## Housekeeping") < after.index("## Pinboard")
    # Deliver-exactly-once, per READER (2026-08-27): a real, non-check write that
    # embedded non-empty housekeeping content advances its OWN cursor and leaves
    # the shared log intact for the other ~49 sessions reading it.
    assert "x.py" in detached_spawn.read_failures_log(str(repo))
    assert detached_spawn.read_failures_log(
        str(repo), cursor_key=mod._failures_cursor_key()
    ) == ""


def test_pinboard_only_housekeeping_absent_to_present(tmp_path):
    """Transition 1/3: no failures at seed time (no ## Housekeeping section at all) ->
    a failure recorded before the pinboard-only patch must surface it fresh."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="old note")
    before = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" not in before

    detached_spawn.record_child_failure(str(repo), "/fresh.py", exit_code=1)
    mod.patch_pinboard_only(cache_file, "new note")

    after = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" in after
    assert "fresh.py" in after
    assert after.index("## Housekeeping") < after.index("## Pinboard")


def test_pinboard_only_housekeeping_present_to_absent(tmp_path):
    """Transition 3/3: a failure present at seed time is cleared (via clear_failures_log,
    simulating the write call site's post-write clear on a prior full regen) before the
    pinboard-only patch runs -- the section must be OMITTED, not left stale or empty."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)
    cache_file = _seed_full_cache(repo, pinboard="old note")
    before = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" in before

    detached_spawn.clear_failures_log(str(repo))
    mod.patch_pinboard_only(cache_file, "new note")

    after = cache_file.read_text(encoding="utf-8")
    assert "## Housekeeping" not in after
    assert "## Pinboard" in after
    assert "- new note" in after
    # No empty heading left behind anywhere (omit-when-empty contract).
    assert "## Housekeeping\n\n" not in after
    assert not after.rstrip("\n").endswith("## Housekeeping")


# ---------------------------------------------------------------------------
# Defect 1 fix (2026-07-28) -- patch_pinboard_only's own deliver-exactly-once clear.
# cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md
# ---------------------------------------------------------------------------


def test_pinboard_only_advances_its_own_cursor_without_draining_the_log(tmp_path):
    """A real (non-check) write delivers to THIS reader and leaves the shared log
    intact for the other ~49 sessions, same as the other two write paths.

    Supersedes `test_pinboard_only_clears_failures_log_after_real_write`
    (2026-08-27). The clear it pinned was written for one reader and deployed
    against fifty: whichever session regenerated first consumed every record and
    the rest never saw them. Deliver-once is now per-reader — see
    `detached_spawn.read_failures_log`'s cursor_key note.
    """
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="old note")
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)
    assert detached_spawn.read_failures_log(str(repo)) != ""

    mod.patch_pinboard_only(cache_file, "new note")

    # The shared log still holds the record: a peer session has not lost it.
    assert "x.py" in detached_spawn.read_failures_log(str(repo))
    # This reader has been delivered to, so it does not re-report the same record.
    assert detached_spawn.read_failures_log(
        str(repo), cursor_key=mod._failures_cursor_key()
    ) == ""


def test_pinboard_only_check_mode_does_not_clear_failures_log(tmp_path):
    """--check must stay a true dry run: even though the fast path RE-DERIVES and
    would embed housekeeping content into its returned (not written) output, a
    check=True call must not clear the underlying log."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="old note")
    detached_spawn.record_child_failure(str(repo), "/x.py", exit_code=1)

    output = mod.patch_pinboard_only(cache_file, "new note", check=True)

    assert "x.py" in output
    assert detached_spawn.read_failures_log(str(repo)) != ""


def test_pinboard_only_real_write_with_no_housekeeping_content_does_not_touch_log(tmp_path):
    """A real write that embeds NO housekeeping content (nothing to report) must not
    call clear_failures_log at all -- distinguishable from "cleared an empty log" only
    by absence of a write, but pinned here so a future refactor can't silently start
    unconditionally clearing regardless of what (if anything) was embedded."""
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="old note")

    log_path = repo / "state" / "housekeeping-failures.log"
    assert not log_path.exists()

    mod.patch_pinboard_only(cache_file, "new note")

    assert not log_path.exists()


# ---------------------------------------------------------------------------
# Defect 2 fix (2026-07-28) -- _emit_housekeeping dedup + per-record cap.
# cross-repo/inbox/2026-07-28-example-retrieval-repo-em-orientation-cache-housekeeping-flood.md
# ---------------------------------------------------------------------------


def test_housekeeping_dedups_flapping_records_with_count(tmp_path):
    """N byte-identical (modulo timestamp) records for the same (script, error class)
    collapse to ONE rendered line carrying a count and a first/latest timestamp range,
    instead of N raw lines."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    for _ in range(24):
        detached_spawn.record_child_failure(
            str(repo), "sweep-boot.py", exc=RuntimeError("RouteMutationError: op refused")
        )

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert "24x sweep-boot.py" in output
    # Collapsed to one rendered failure line, not 24 raw lines.
    assert output.count("sweep-boot.py") == 1
    assert "24 housekeeping failure(s) recorded (1 distinct issue(s))" in output


def test_housekeeping_detail_is_length_capped(tmp_path):
    """A single record with a pathologically long detail renders truncated, bounding
    the section's total size even under one giant record."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    huge_detail = "X" * 5000
    detached_spawn.record_child_failure(str(repo), "/huge.py", exc=RuntimeError(huge_detail))

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert len(output) < 2000
    assert "X" * 5000 not in output


def test_housekeeping_bounds_total_section_under_many_distinct_failures(tmp_path):
    """A pathological log with many DISTINCT (not flapping) failure types still renders
    a bounded number of groups, with the overflow summarized rather than enumerated."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    for i in range(50):
        detached_spawn.record_child_failure(
            str(repo), f"/script_{i}.py", exc=RuntimeError(f"Error{i}: boom")
        )

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    rendered_group_lines = [ln for ln in output.splitlines() if ln.strip().startswith("- `")]
    assert len(rendered_group_lines) <= mod._HOUSEKEEPING_MAX_RENDERED_GROUPS
    assert "more distinct issue(s) not shown" in output


def test_housekeeping_malformed_line_renders_degraded_not_dropped(tmp_path):
    """A line that does not match the expected record shape must still render
    (truncated, degraded) -- never crash the regen and never silently vanish."""
    repo = _make_repo(tmp_path)
    log_path = repo / "state" / "housekeeping-failures.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("this is not a well-formed housekeeping record at all\n", encoding="utf-8")

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert "this is not a well-formed housekeeping record at all" in output


def test_housekeeping_does_not_crash_on_embedded_escaped_newline(tmp_path):
    """detail may contain a literal backslash-n (detached_spawn's own \\n-escaping of a
    real newline before writing) -- must render as ordinary text, not break parsing."""
    from coordinator_core.ops.ceremony import detached_spawn

    repo = _make_repo(tmp_path)
    detached_spawn.record_child_failure(
        str(repo), "/multi.py", exc=RuntimeError("first line\nsecond line")
    )

    result = mod.build_cache(invoker="workday-start", repo_root=repo, pinboard="", pinboard_set=True)
    output = result["output"]

    assert "## Housekeeping" in output
    assert "\\n" in output


# ---------------------------------------------------------------------------
# WORKSTREAM_MAX (2026-07-28 reconciliation) -- shared constant, imported by
# verify_orientation_cache_sync instead of redeclared. emit_workstreams's own
# cap must reference this constant, not a bare literal, or the two could
# silently diverge again the way LINE_CEILING once did.
# ---------------------------------------------------------------------------


def test_emit_workstreams_caps_at_workstream_max(tmp_path):
    repo = _make_repo(tmp_path)
    lines = ["## Active workstreams"]
    lines += [f"### {i}. Item {i}" for i in range(1, mod.WORKSTREAM_MAX + 5)]
    (repo / "tasks" / "project-tracker.md").write_text("\n".join(lines) + "\n")

    out = mod.emit_workstreams(repo)

    assert len(out) == mod.WORKSTREAM_MAX


# ---------------------------------------------------------------------------
# Cache size budget (2026-07-28) -- structural cap enforced in _atomic_replace,
# generalizing the Housekeeping dedup fix (single-section, semantic) into a
# whole-artifact structural bound. See regenerate_cache.CACHE_BUDGET_BYTES.
# ---------------------------------------------------------------------------

_CACHE_PREAMBLE = "---\ngenerated_by: x\ngenerated_at: t\ngit_head_at_generation: abc\n---\n\n# Orientation Cache\n"


def _huge_body(n: int = 300) -> str:
    return "\n".join(f"- failure line {i} " + "x" * 80 for i in range(n))


def test_enforce_cache_budget_leaves_healthy_cache_untouched():
    """No gratuitous rewriting -- an already-under-budget cache is returned
    byte-identical (not merely equivalent)."""
    text = f"{_CACHE_PREAMBLE}\n## Branch\nhello\n\n## Pinboard\n- note\n"
    assert mod._cache_size(text) <= mod.CACHE_BUDGET_BYTES
    assert mod._enforce_cache_budget(text) == text


def test_enforce_cache_budget_trims_oversized_elastic_section_under_budget():
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\nsome project\n"
        f"\n## Housekeeping\n{_huge_body()}\n"
        "\n## Pinboard\n- note\n"
    )
    assert mod._cache_size(text) > mod.CACHE_BUDGET_BYTES

    out = mod._enforce_cache_budget(text)

    assert mod._cache_size(out) <= mod.CACHE_BUDGET_BYTES
    assert "trimmed to fit cache budget" in out


def test_enforce_cache_budget_keeps_protected_sections_intact_under_pressure():
    """Project and Pinboard (protected) survive byte-for-byte even though the
    Housekeeping section between them is pathological and must be trimmed."""
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\nsome project\n"
        f"\n## Housekeeping\n{_huge_body()}\n"
        "\n## Pinboard\n- note\n"
    )

    out = mod._enforce_cache_budget(text)

    assert "## Branch\nsome project\n" in out
    assert "## Pinboard\n- note\n" in out
    assert "## Housekeeping\n- [" in out  # trimmed to a marker, not dropped


# ---------------------------------------------------------------------------
# Hook cancellation miss-rate signal (2026-08-15) -- see
# coordinator_core.orientation.hook_cancellation_signal and
# docs/plans/2026-08-15-hook-cancellation-miss-rate-signal.md § C3.
# ---------------------------------------------------------------------------

import json as _json

from coordinator_core.orientation import hook_cancellation_signal as _hcs


def _cancel_line(cwd: str, hook_name: str = "PreToolUse:Bash") -> str:
    return _json.dumps({
        "type": "attachment",
        "attachment": {
            "type": "hook_cancelled",
            "hookName": hook_name,
            "durationMs": 16336,
            "timedOut": True,
            "timeoutMs": 15000,
        },
        "cwd": cwd,
    })


def _bash_tool_use_line(cwd: str) -> str:
    return _json.dumps({
        "type": "assistant",
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "id": "toolu_1", "input": {"command": "ls"}}],
        },
    })


def _write_fixture_transcript(project_dir: Path, name: str, repo_cwd: str, other_cwd: str) -> None:
    # 3 PreToolUse:Bash cancellations, 5 Bash tool_use entries (denominator),
    # plus one cancellation for a DIFFERENT hookName (must not count) and one
    # Bash tool_use for a DIFFERENT cwd (must not count) -- pins both the
    # numerator filter and the cwd scope in one fixture.
    lines = [
        _bash_tool_use_line(repo_cwd),
        _cancel_line(repo_cwd),
        _bash_tool_use_line(repo_cwd),
        _cancel_line(repo_cwd),
        _bash_tool_use_line(repo_cwd),
        _cancel_line(repo_cwd, hook_name="Stop"),  # wrong hook -- must not count
        _bash_tool_use_line(other_cwd),  # wrong cwd -- must not count
        _cancel_line(repo_cwd),
        _bash_tool_use_line(repo_cwd),
    ]
    (project_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed_fixture_projects_root(tmp_path: Path, repo: Path, monkeypatch) -> Path:
    """Build a fake `<claude-home>/projects/<encoded-repo>/` directory holding one
    fixture transcript, and point `hook_cancellation_signal.home_dir` at it."""
    claude_home = tmp_path / "fake-claude-home"
    encoded = _hcs._encode_project_dir_name(repo)
    project_dir = claude_home / ".claude" / "projects" / encoded
    project_dir.mkdir(parents=True)
    other_cwd = str(repo) + "-sibling"
    _write_fixture_transcript(project_dir, "session-1.jsonl", str(repo), other_cwd)
    monkeypatch.setattr(_hcs, "home_dir", lambda: claude_home)
    return project_dir


def test_scan_hook_cancellation_rate_matches_known_fixture_count(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _seed_fixture_projects_root(tmp_path, repo, monkeypatch)

    result = _hcs.scan_hook_cancellation_rate(repo)

    assert result.cancelled == 3
    assert result.denominator == 4
    assert result.rate == pytest.approx(3 / 4)


def test_scan_hook_cancellation_rate_missing_project_dir_yields_no_signal(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    claude_home = tmp_path / "fake-claude-home-empty"
    monkeypatch.setattr(_hcs, "home_dir", lambda: claude_home)

    result = _hcs.scan_hook_cancellation_rate(repo)

    assert result.cancelled == 0
    assert result.denominator == 0
    assert result.rate is None


def test_emit_hook_cancellation_rate_omitted_when_no_denominator(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    claude_home = tmp_path / "fake-claude-home-empty"
    monkeypatch.setattr(_hcs, "home_dir", lambda: claude_home)

    assert mod.emit_hook_cancellation_rate(repo) == ""


def test_full_regen_renders_hook_cancellation_on_its_own_line_not_housekeeping(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _seed_fixture_projects_root(tmp_path, repo, monkeypatch)

    result = mod.build_cache(invoker="workday-start", repo_root=repo)

    assert not result["skipped"]
    output = result["output"]
    assert "## Hook cancellation miss rate" in output
    assert "75.0% cancelled (3/4" in output
    # own section, strictly before the (absent-here) Housekeeping/Pinboard tail --
    # and never folded into Housekeeping content (plan Anti-scope).
    hook_idx = output.index("## Hook cancellation miss rate")
    housekeeping_idx = output.find("## Housekeeping")
    if housekeeping_idx != -1:
        assert hook_idx < housekeeping_idx
    housekeeping_section = output[housekeeping_idx:] if housekeeping_idx != -1 else ""
    assert "cancelled" not in housekeeping_section


def test_enforce_cache_budget_never_zeroes_an_unrecognized_section():
    """A ``## Heading`` this module has no name for (e.g. a ceremony-supplied
    section _render_cache never emits, like ``Week priorities``) is trimmable
    like any elastic section but its heading and a marker must always survive
    -- the cap must never be the thing that makes such a section vanish."""
    text = f"{_CACHE_PREAMBLE}\n## Branch\np\n\n## Week priorities\n{_huge_body()}\n"

    out = mod._enforce_cache_budget(text)

    assert "## Week priorities" in out
    assert "trimmed to fit cache budget" in out


def test_enforce_cache_budget_trims_multiple_oversized_sections_each_with_own_marker():
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\np\n"
        f"\n## Housekeeping\n{_huge_body()}\n"
        f"\n## Active workstreams\n{_huge_body()}\n"
        "\n## Pinboard\n- note\n"
    )

    out = mod._enforce_cache_budget(text)

    assert mod._cache_size(out) <= mod.CACHE_BUDGET_BYTES
    assert out.count("trimmed to fit cache budget") == 2


def test_enforce_cache_budget_is_idempotent():
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\np\n"
        f"\n## Housekeeping\n{_huge_body()}\n"
        "\n## Pinboard\n- note\n"
    )

    once = mod._enforce_cache_budget(text)
    twice = mod._enforce_cache_budget(once)

    assert once == twice
    assert once.count("trimmed to fit cache budget") == 1


def test_enforce_cache_budget_passes_through_unparseable_text_unharmed():
    """No ``## `` heading anywhere -- this parser has nothing to act on, so the
    oversized text must pass through unchanged rather than risk corruption."""
    garbage = "x" * (mod.CACHE_BUDGET_BYTES + 100)

    assert mod._enforce_cache_budget(garbage) == garbage


def test_enforce_cache_budget_surfaces_loud_marker_when_protected_alone_exceeds_budget():
    """A single protected section that alone blows the budget is a bug to
    surface, not hide -- it is kept whole and a top-level marker is added
    instead of truncating it."""
    huge_project = "p" * (mod.CACHE_BUDGET_BYTES + 500)
    text = f"{_CACHE_PREAMBLE}\n## Branch\n{huge_project}\n"

    out = mod._enforce_cache_budget(text)

    assert huge_project in out
    assert "exceeded its" in out

    out2 = mod._enforce_cache_budget(out)
    assert out2 == out
    assert out2.count("exceeded its") == 1


# ---------------------------------------------------------------------------
# Line ceiling (2026-07-28 reconciliation) -- LINE_CEILING, enforced in the
# SAME _enforce_cache_budget pass as CACHE_BUDGET_BYTES. See
# regenerate_cache.LINE_CEILING's docstring for the measured evidence behind
# its value and for the retired verifier-only, post-hoc `_LINE_CEILING = 35`
# this replaces.
# ---------------------------------------------------------------------------


def test_enforce_cache_budget_leaves_a_cache_under_both_axes_untouched():
    text = f"{_CACHE_PREAMBLE}\n## Branch\nhello\n\n## Pinboard\n- note\n"
    assert mod._cache_size(text) <= mod.CACHE_BUDGET_BYTES
    assert mod._cache_line_count(text) <= mod.LINE_CEILING
    assert mod._enforce_cache_budget(text) == text


def test_enforce_cache_budget_trims_when_only_line_ceiling_is_exceeded(monkeypatch):
    """Many short lines can blow the line ceiling while staying well under the
    byte budget -- the byte check alone would have missed this entirely."""
    monkeypatch.setattr(mod, "LINE_CEILING", 20)
    body = "\n".join(f"- x{i}" for i in range(50))
    text = f"{_CACHE_PREAMBLE}\n## Branch\np\n\n## Housekeeping\n{body}\n\n## Pinboard\n- note\n"
    assert mod._cache_size(text) <= mod.CACHE_BUDGET_BYTES
    assert mod._cache_line_count(text) > 20

    out = mod._enforce_cache_budget(text)

    assert mod._cache_line_count(out) <= 20
    assert "trimmed to fit cache budget" in out
    assert "## Branch\np\n" in out  # protected section untouched
    assert "## Pinboard\n- note\n" in out


def test_enforce_cache_budget_line_trim_is_idempotent(monkeypatch):
    monkeypatch.setattr(mod, "LINE_CEILING", 20)
    body = "\n".join(f"- x{i}" for i in range(50))
    text = f"{_CACHE_PREAMBLE}\n## Branch\np\n\n## Housekeeping\n{body}\n\n## Pinboard\n- note\n"

    once = mod._enforce_cache_budget(text)
    twice = mod._enforce_cache_budget(once)

    assert once == twice
    assert once.count("trimmed to fit cache budget") == 1


def test_enforce_cache_budget_marker_names_only_the_axis_actually_exceeded(monkeypatch):
    """A protected section alone can blow one axis without the other -- the
    surfaced marker must say which, not blanket-claim both."""
    monkeypatch.setattr(mod, "LINE_CEILING", 1)
    text = f"{_CACHE_PREAMBLE}\n## Branch\nline1\nline2\nline3\n"
    assert mod._cache_size(text) <= mod.CACHE_BUDGET_BYTES

    out = mod._enforce_cache_budget(text)

    assert "line ceiling" in out
    assert "byte budget" not in out


def test_enforce_cache_budget_marker_names_both_axes_when_both_exceeded(monkeypatch):
    monkeypatch.setattr(mod, "CACHE_BUDGET_BYTES", 50)
    monkeypatch.setattr(mod, "LINE_CEILING", 1)
    huge_project = "p" * 200
    text = f"{_CACHE_PREAMBLE}\n## Branch\n{huge_project}\n"

    out = mod._enforce_cache_budget(text)

    assert "byte budget" in out
    assert "line ceiling" in out
    out2 = mod._enforce_cache_budget(out)
    assert out2 == out


def test_patch_pinboard_only_enforces_line_ceiling(tmp_path, monkeypatch):
    """Regression for the same defect class CACHE_BUDGET_BYTES's own
    patch_pinboard_only test guards: the fast path splices bytes directly and
    must still be bound by LINE_CEILING via the shared _atomic_replace
    chokepoint, not merely by _render_cache (which patch_pinboard_only never
    calls for most sections)."""
    from coordinator_core.ops.ceremony import detached_spawn

    monkeypatch.setattr(mod, "LINE_CEILING", 20)
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="orig")

    for i in range(15):
        detached_spawn.record_child_failure(str(repo), f"/script_{i}.py", exit_code=1)

    mod.patch_pinboard_only(cache_file, "new note", repo_root=repo)

    written = cache_file.read_text(encoding="utf-8")
    assert mod._cache_line_count(written) <= 20


def test_patch_pinboard_only_enforces_cache_budget(tmp_path, monkeypatch):
    """Regression for the exact defect this cap generalizes: patch_pinboard_only
    is a THIRD write path that bypasses _render_cache entirely (splices bytes
    directly) -- the budget must still bind here because enforcement lives in
    _atomic_replace, the single shared byte-write chokepoint, not in
    _render_cache (a bypassable contract is exactly the bug a629e188 fixed for
    Housekeeping's own deliver-exactly-once clearing)."""
    from coordinator_core.ops.ceremony import detached_spawn

    monkeypatch.setattr(mod, "CACHE_BUDGET_BYTES", 2048)
    repo = _make_repo(tmp_path)
    cache_file = _seed_full_cache(repo, pinboard="orig")

    for i in range(80):
        detached_spawn.record_child_failure(
            str(repo), f"/script_{i}.py", exc=RuntimeError(f"Error{i}: " + "z" * 150)
        )

    mod.patch_pinboard_only(cache_file, "new note", repo_root=repo)

    written = cache_file.read_text(encoding="utf-8")
    assert mod._cache_size(written) <= 2048
    assert "trimmed to fit cache budget" in written


# ---------------------------------------------------------------------------
# sweep-boot invoker (leg 2/3, 2026-07-29) -- new mid-session-tier MACHINE
# invoker fired by the async sweep-boot SessionStart hook on a detected-stale
# cache. See _tier_for_invoker's own docstring for why it shares the
# mid-session tier rather than getting a fourth tier of its own.
# ---------------------------------------------------------------------------


def test_tier_for_invoker_sweep_boot_is_mid_session():
    assert mod._tier_for_invoker("sweep-boot") == "mid-session"


def test_tier_for_invoker_unknown_still_raises():
    with pytest.raises(ValueError):
        mod._tier_for_invoker("not-a-real-invoker")


# ---------------------------------------------------------------------------
# quick-wrap invoker (2026-07-31). The skill's step 4 instructed the operator
# to run `regenerate-orientation-cache` while the CLI's `--invoker` allowlist
# had no `quick-wrap` value -- argparse rejected it outright, so a prescribed
# ceremony step could not be performed at all. Mid-session tier: quick-wrap
# closes a SESSION, and the pinboard is day-scoped (the skill itself defers to
# /workday-complete as the day-level close).
#
# The CLI keeps its own `_VALID_INVOKERS` tuple, separate from the engine's
# tier map -- exactly the two-list shape that drifted here. The parity test
# below is what makes the next added invoker fail loudly in ONE place instead
# of half-landing.
# ---------------------------------------------------------------------------


def test_tier_for_invoker_quick_wrap_is_mid_session():
    assert mod._tier_for_invoker("quick-wrap") == "mid-session"


def test_cli_valid_invokers_match_the_engine_tier_map():
    """Every invoker the CLI accepts must resolve to a tier, and every invoker
    the engine gives a tier must be reachable from the CLI. A value in one
    list and not the other is either an argparse rejection of a supported
    invoker (the 2026-07-31 quick-wrap defect) or a CLI that accepts a value
    the engine then raises ValueError on."""
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    cli_path = (
        Path(__file__).resolve().parents[2] / "coordinator" / "bin"
        / "regenerate-orientation-cache.py"
    )
    spec = importlib.util.spec_from_loader(
        "_regen_cli_under_test",
        importlib.machinery.SourceFileLoader("_regen_cli_under_test", str(cli_path)),
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    for invoker in cli._VALID_INVOKERS:
        assert mod._tier_for_invoker(invoker) in ("ceremony", "mid-session"), (
            f"CLI accepts {invoker!r} but the engine gives it no tier"
        )

    engine_invokers = {"workday-start", "update-docs", "workstream-complete",
                       "handoff", "quick-wrap", "sweep-boot"}
    assert set(cli._VALID_INVOKERS) == engine_invokers, (
        "CLI allowlist and engine tier map have drifted: "
        f"CLI-only={set(cli._VALID_INVOKERS) - engine_invokers}, "
        f"engine-only={engine_invokers - set(cli._VALID_INVOKERS)}"
    )


def test_sweep_boot_full_regen_preserves_existing_pinboard(tmp_path):
    """mid-session semantics: a sweep-boot regen with no explicit --pinboard must
    preserve whatever pinboard note is already on disk (same as workstream-complete
    /handoff) -- the self-heal is not a ceremony and must not silently clear it."""
    repo = _make_repo(tmp_path)
    _seed_full_cache(repo, pinboard="pre-existing note")

    result = mod.build_cache(invoker="sweep-boot", repo_root=repo)

    assert not result["skipped"]
    assert result["tier"] == "mid-session"
    assert "- pre-existing note" in result["output"]


def test_workday_start_full_regen_clears_pinboard(tmp_path):
    """Contrast case for the above: a ceremony invoker (workday-start) clears the
    pinboard slot rather than preserving it -- pins the tier boundary sweep-boot
    must land on the mid-session side of."""
    repo = _make_repo(tmp_path)
    _seed_full_cache(repo, pinboard="pre-existing note")

    result = mod.build_cache(invoker="workday-start", repo_root=repo)

    assert not result["skipped"]
    assert "## Pinboard" not in result["output"]


# ---------------------------------------------------------------------------
# ## Recent commits (leg 3, 2026-07-29) -- git log recency, sourced via
# emit_recent_commits and wired into _render_cache between Branch and
# Auto-push health. See regenerate_cache.py's own section comment for the
# negative-spec (never caches working-tree/dirty state).
# ---------------------------------------------------------------------------


def test_recent_commits_renders_between_branch_and_auto_push_health(tmp_path):
    repo = _make_repo(tmp_path)
    # oldest first -- "newest commit" lands on HEAD, so it is git log's top line
    _init_git_repo(repo, ["oldest commit", "middle commit", "newest commit"])
    # make it look like an unpushed work/ branch so Auto-push health also renders
    subprocess.run(["git", "checkout", "-q", "-b", "work/x"], cwd=str(repo), check=True, **no_console_passthrough_kwargs())

    result = mod.build_cache(invoker="workday-start", repo_root=repo)
    output = result["output"]

    assert "## Recent commits" in output
    assert output.index("## Branch") < output.index("## Recent commits")
    lines = [ln for ln in output.splitlines() if ln.startswith("- ")]
    assert any("newest commit" in ln for ln in lines)
    # Shape: "- <short sha> <subject>"
    recent_section = output.split("## Recent commits\n", 1)[1]
    first_line = recent_section.splitlines()[0]
    assert re.match(r"^- [0-9a-f]{4,40} newest commit$", first_line)


def test_recent_commits_omitted_when_no_commits(tmp_path):
    repo = _make_repo(tmp_path)
    _init_git_repo(repo, [])  # git repo with zero commits

    result = mod.build_cache(invoker="workday-start", repo_root=repo)

    assert "## Recent commits" not in result["output"]


def test_recent_commits_omitted_when_git_fails(tmp_path):
    repo = _make_repo(tmp_path)  # never git-initialized -- git log fails

    result = mod.build_cache(invoker="workday-start", repo_root=repo)

    assert "## Recent commits" not in result["output"]
    assert mod.emit_recent_commits(repo) == []


def test_recent_commits_truncates_long_subject(tmp_path):
    repo = _make_repo(tmp_path)
    long_subject = "x" * 200
    _init_git_repo(repo, [long_subject])

    lines = mod.emit_recent_commits(repo)

    assert len(lines) == 1
    rendered_subject = lines[0].split(" ", 2)[2]
    assert rendered_subject == "x" * mod._COMMIT_SUBJECT_TRUNCATE_CHARS + "…"
    assert long_subject not in lines[0]


def test_recent_commits_truncation_preserves_non_ascii_characters(tmp_path):
    """Finding 3: truncation is a code-point slice, not a byte-count bound --
    a subject dense in multi-byte UTF-8 (CJK here) must truncate to exactly
    ``_COMMIT_SUBJECT_TRUNCATE_CHARS`` CHARACTERS with no character split
    mid-sequence, and the whole rendered line must still fit comfortably
    under the cache's byte budget (the real backstop, per the corrected
    docstring -- see ``_enforce_cache_budget``)."""
    repo = _make_repo(tmp_path)
    non_ascii_subject = "漢字" * 100  # 200 CJK code points, ~600 bytes in UTF-8
    _init_git_repo(repo, [non_ascii_subject])

    lines = mod.emit_recent_commits(repo)

    assert len(lines) == 1
    rendered_subject = lines[0].split(" ", 2)[2]
    assert rendered_subject == non_ascii_subject[: mod._COMMIT_SUBJECT_TRUNCATE_CHARS] + "…"
    # No mid-sequence corruption -- every character in the rendered subject
    # round-trips through UTF-8 encode/decode unchanged.
    assert rendered_subject.encode("utf-8").decode("utf-8") == rendered_subject
    assert len(lines[0].encode("utf-8")) < mod.CACHE_BUDGET_BYTES


def test_recent_commits_caps_at_recent_commits_max(tmp_path):
    repo = _make_repo(tmp_path)
    _init_git_repo(repo, [f"commit {i}" for i in range(mod.RECENT_COMMITS_MAX + 5)])

    lines = mod.emit_recent_commits(repo)

    assert len(lines) == mod.RECENT_COMMITS_MAX


# ---------------------------------------------------------------------------
# Capabilities (2026-08-14) -- repo-declared discoverability pointers, fed
# from coordinator.local.md's `capability_pointers:` frontmatter list via the
# same declare-once-in-config seam `fast_test_cmd:` established.
# Spec backlink: cross-repo/inbox/2026-08-14-doe-claude-em-orientation-cache-capability-pointers.md
# ---------------------------------------------------------------------------


def test_resolve_capability_pointers_reads_local_md_list(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "coordinator.local.md").write_text(
        "---\n"
        "capability_pointers:\n"
        '  - "example-retrieval-repo: query engine source via mcp__example_retrieval_repo__* tools"\n'
        "  - architecture atlas: docs/architecture/systems/\n"
        "---\n",
        encoding="utf-8",
    )

    pointers = mod.resolve_capability_pointers(str(repo))

    assert pointers == [
        "example-retrieval-repo: query engine source via mcp__example_retrieval_repo__* tools",
        "architecture atlas: docs/architecture/systems/",
    ]


def test_resolve_capability_pointers_absent_key_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "coordinator.local.md").write_text(
        "---\nfast_test_cmd: pytest -q\n---\n", encoding="utf-8"
    )

    assert mod.resolve_capability_pointers(str(repo)) == []


def test_resolve_capability_pointers_no_local_md_returns_empty(tmp_path):
    repo = _make_repo(tmp_path)
    assert mod.resolve_capability_pointers(str(repo)) == []


def test_emit_capability_pointers_omitted_when_absent(tmp_path):
    repo = _make_repo(tmp_path)
    assert mod.emit_capability_pointers(repo) == []


def test_emit_capability_pointers_renders_configured_entries(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "coordinator.local.md").write_text(
        "---\ncapability_pointers:\n  - one\n  - two\n---\n", encoding="utf-8"
    )

    assert mod.emit_capability_pointers(repo) == ["- one", "- two"]


def test_emit_capability_pointers_caps_with_ellipsis_not_count(tmp_path):
    repo = _make_repo(tmp_path)
    entries = "\n".join(f"  - entry {i}" for i in range(mod.CAPABILITY_POINTERS_MAX + 3))
    (repo / "coordinator.local.md").write_text(
        f"---\ncapability_pointers:\n{entries}\n---\n", encoding="utf-8"
    )

    out = mod.emit_capability_pointers(repo)

    assert len(out) == mod.CAPABILITY_POINTERS_MAX + 1  # capped entries + ellipsis marker
    assert out[-1] == "- …"
    assert all("entry " in line for line in out[:-1])


def test_capabilities_section_renders_between_atlas_and_fast_test(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "docs" / "architecture" / "systems").mkdir(parents=True)
    (repo / "docs" / "architecture" / "systems" / "foo.md").write_text("x", encoding="utf-8")
    (repo / "coordinator.local.md").write_text(
        "---\n"
        "fast_test_cmd: pytest -q\n"
        "capability_pointers:\n"
        "  - example-retrieval-repo: query engine source\n"
        "---\n",
        encoding="utf-8",
    )

    output = mod.build_cache(invoker="workday-start", repo_root=repo)["output"]

    atlas_idx = output.index("## Architecture atlas")
    caps_idx = output.index("## Capabilities")
    fast_idx = output.index("## Fast test")
    assert atlas_idx < caps_idx < fast_idx
    assert "- example-retrieval-repo: query engine source" in output


def test_capabilities_section_is_elastic_not_protected():
    """Same discipline as `test_recent_commits_section_is_trimmed_not_protected_under_budget`
    above -- exercises the real `_enforce_cache_budget` path, not membership by inspection."""
    assert "Capabilities" in mod._CACHE_ELASTIC_SECTIONS
    assert "Capabilities" not in mod._CACHE_PROTECTED_SECTIONS

    huge = "\n".join(f"- capability entry {i} " + "x" * 80 for i in range(200))
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\n`main` (no origin/main reference)\n"
        f"\n## Capabilities\n{huge}\n"
        "\n## Pinboard\n- note\n"
    )
    assert mod._cache_size(text) > mod.CACHE_BUDGET_BYTES

    out = mod._enforce_cache_budget(text)

    assert mod._cache_size(out) <= mod.CACHE_BUDGET_BYTES
    assert "## Capabilities\n- [" in out  # trimmed to a marker, not dropped
    assert "## Branch\n`main` (no origin/main reference)\n" in out
    assert "## Pinboard\n- note\n" in out


def test_pinboard_only_does_not_rederive_capabilities(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    (repo / "coordinator.local.md").write_text(
        "---\ncapability_pointers:\n  - example-retrieval-repo: query engine source\n---\n",
        encoding="utf-8",
    )
    cache_file = _seed_full_cache(repo, pinboard="2026-08-14 handoff: old note")
    before = cache_file.read_text(encoding="utf-8")
    assert "## Capabilities" in before

    def _boom(*a, **k):
        raise AssertionError("patch_pinboard_only must not re-derive Capabilities")

    monkeypatch.setattr(mod, "emit_capability_pointers", _boom)
    monkeypatch.setattr(mod, "resolve_capability_pointers", _boom)

    mod.patch_pinboard_only(cache_file, "2026-08-14 handoff: new note")

    after = cache_file.read_text(encoding="utf-8")
    caps_before = before.split("## Capabilities", 1)[1].split("\n## ", 1)[0]
    caps_after = after.split("## Capabilities", 1)[1].split("\n## ", 1)[0]
    assert caps_before == caps_after


def test_recent_commits_section_is_trimmed_not_protected_under_budget():
    """Recent commits is an ELASTIC section -- must be trimmable via the real
    _enforce_cache_budget path (not merely present in _CACHE_ELASTIC_SECTIONS by
    inspection), same discipline as every other elastic-section budget test above."""
    assert "Recent commits" in mod._CACHE_ELASTIC_SECTIONS
    assert "Recent commits" not in mod._CACHE_PROTECTED_SECTIONS

    huge_commits = "\n".join(f"- abc{i:04x} commit subject {i} " + "x" * 80 for i in range(200))
    text = (
        f"{_CACHE_PREAMBLE}\n## Branch\n`main` (no origin/main reference)\n"
        f"\n## Recent commits\n{huge_commits}\n"
        "\n## Pinboard\n- note\n"
    )
    assert mod._cache_size(text) > mod.CACHE_BUDGET_BYTES

    out = mod._enforce_cache_budget(text)

    assert mod._cache_size(out) <= mod.CACHE_BUDGET_BYTES
    assert "## Recent commits\n- [" in out  # trimmed to a marker, not dropped
    assert "## Branch\n`main` (no origin/main reference)\n" in out  # protected sections survive whole
    assert "## Pinboard\n- note\n" in out


# ---------------------------------------------------------------------------
# CLI trampoline -- --invoker sweep-boot acceptance / --pinboard-only rejection.
# Invokes coordinator/bin/regenerate-orientation-cache.py as a real subprocess
# with COORDINATOR_ENGINE_ROOT pinned to this checkout so it resolves
# coordinator_core without any machine-local registry dependency. The pin used
# to name CLAUDE_KLABAUTER_ROOT, which C14 (`fb1421af2`) stopped the engine-root gate
# reading -- so it silently bought nothing and the child resolved ambiently,
# which is the same pin-is-dead class as
# state/bug-backlog/2026-08-25-fixture-env-dicts-still-pin-the-retired-claude-klabauter-live-root.
# ---------------------------------------------------------------------------

_CLI_SCRIPT = Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "regenerate-orientation-cache.py"
_CLAUDE_KLABAUTER_ROOT = Path(__file__).resolve().parents[2]


def _run_cli(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, COORDINATOR_ENGINE_ROOT=str(_CLAUDE_KLABAUTER_ROOT))
    return subprocess.run(
        [sys.executable, str(_CLI_SCRIPT), *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_cli_accepts_sweep_boot_invoker(tmp_path):
    repo = _make_repo(tmp_path)

    result = _run_cli(repo, "--invoker", "sweep-boot", "--check")

    assert result.returncode == 0, result.stderr
    assert "# Orientation Cache" in result.stdout


def test_cli_rejects_sweep_boot_with_pinboard_only(tmp_path):
    repo = _make_repo(tmp_path)
    # seed a real cache first so a wrong CLI would fail on FileNotFoundError
    # instead of the invoker gate this test actually means to exercise.
    _run_cli(repo, "--invoker", "sweep-boot")

    result = _run_cli(repo, "--invoker", "sweep-boot", "--pinboard-only", "note")

    assert result.returncode == 2


# ---------------------------------------------------------------------------
# _atomic_replace permission-bit behavior (Review: coordinator:code-reviewer
# 9b8765ad finding 3 -- the fchmod-on-common-path and first-write-default-
# mode behavior shipped in the C10 port with zero coverage; added here.)
# ---------------------------------------------------------------------------


def test_atomic_replace_first_write_uses_fixed_default_not_umask(tmp_path, monkeypatch):
    """No existing cache_file -> target mode is the fixed
    `_FIRST_WRITE_DEFAULT_MODE`, and the process umask is never read/restored
    (Review: 9b8765ad finding 2 -- the umask read/restore window this
    replaces was itself the residual security exposure)."""
    cache_file = tmp_path / "orientation_cache.md"
    assert not cache_file.exists()

    def _forbidden_umask(_mask):
        raise AssertionError("os.umask must not be called on the first-write path")

    monkeypatch.setattr(mod.os, "umask", _forbidden_umask)

    captured = {}

    def _spy_fchmod(fd, mode):
        captured["mode"] = mode

    monkeypatch.setattr(mod.os, "fchmod", _spy_fchmod, raising=False)
    monkeypatch.setattr(mod.os, "chmod", lambda path, mode: captured.setdefault("mode", mode))

    mod._atomic_replace(cache_file, "hello\n")

    assert cache_file.read_text(encoding="utf-8") == "hello\n"
    assert captured["mode"] == mod._FIRST_WRITE_DEFAULT_MODE


def test_atomic_replace_common_path_inherits_existing_mode(tmp_path, monkeypatch):
    """An existing cache_file -> target mode is that file's own current
    `st_mode & 0o777`, not the fixed first-write default and not a
    umask-derived value."""
    cache_file = tmp_path / "orientation_cache.md"
    cache_file.write_text("old\n", encoding="utf-8")
    existing_mode = cache_file.stat().st_mode & 0o777

    captured = {}
    monkeypatch.setattr(
        mod.os, "fchmod", lambda fd, mode: captured.setdefault("mode", mode), raising=False
    )
    monkeypatch.setattr(mod.os, "chmod", lambda path, mode: captured.setdefault("mode", mode))

    mod._atomic_replace(cache_file, "new\n")

    assert cache_file.read_text(encoding="utf-8") == "new\n"
    assert captured["mode"] == existing_mode


def test_atomic_replace_stat_permission_error_propagates(tmp_path, monkeypatch):
    """A real stat failure on an EXISTING cache_file (Review: 9b8765ad
    finding 4 -- narrowed from `except OSError` to `except FileNotFoundError`)
    must propagate rather than being silently treated as first-write."""
    cache_file = tmp_path / "orientation_cache.md"
    cache_file.write_text("old\n", encoding="utf-8")

    real_stat = Path.stat

    def _denied_stat(self, *a, **kw):
        if self == cache_file:
            raise PermissionError("simulated stat failure")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _denied_stat)

    with pytest.raises(PermissionError):
        mod._atomic_replace(cache_file, "new\n")


# ---------------------------------------------------------------------------
# ## Auto-push health -- hold-window awareness, failure-recency bounding, and
# the "timeout" classification (2026-08-20 dispatch, three-defect fix). See
# regenerate_cache.py's `emit_auto_push_health` docstring-adjacent comments
# for the full rationale; these tests exercise the reporting surface only,
# never auto_push.py's own timing (_HOLD_WINDOW_SECONDS is untouched).
# ---------------------------------------------------------------------------

import time as _time
from datetime import datetime as _datetime, timezone as _timezone
from coordinator_core.win_portability import no_console_passthrough_kwargs


def _make_repo_with_upstream(tmp_path: Path, branch: str = "work/x", unpushed: int = 2) -> Path:
    """A real repo + bare "origin" remote, `branch` pushed once then given
    `unpushed` further local-only commits -- the shape emit_auto_push_health
    needs to get past its "0 unpushed -> no section" early return."""
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com",
    )
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True, env=env, **no_console_passthrough_kwargs())
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    (repo / "base.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    for i in range(unpushed):
        (repo / f"file-{i}.txt").write_text(str(i), encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
        subprocess.run(["git", "commit", "-q", "-m", f"unpushed {i}"], cwd=str(repo), check=True, env=env, **no_console_passthrough_kwargs())
    return repo


def _write_pending_record(repo: Path, branch: str, hold_until: float, holder_pid: int | None = None) -> None:
    record = {
        "branch": branch,
        "sha": "deadbeef",
        "hold_until": hold_until,
        "holder_pid": holder_pid if holder_pid is not None else os.getpid(),
    }
    (repo / ".git" / "coordinator-auto-push-pending.json").write_text(
        _json.dumps(record), encoding="utf-8"
    )


def _write_failure_log_line(repo: Path, branch: str, err_class: str, when: _datetime) -> None:
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{stamp}] PUSH FAILED on {branch} (direct push/{err_class} after 2) :: some detail :: stderr=<empty>\n"
    logf = repo / ".git" / "push-failures.log"
    with open(logf, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line)


def test_auto_push_health_live_hold_renders_as_holding_not_warning(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/hold")
    _write_pending_record(repo, "work/hold", hold_until=_time.time() + 120)

    result = mod.emit_auto_push_health(repo)

    assert "⚠" not in result
    assert "holding" in result
    assert "2 unpushed commit(s)" in result


def test_auto_push_health_stale_record_still_warns(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/stale")
    # hold_until far enough in the past to clear _STALE_GRACE_SECONDS (60s)
    # even though the recorded holder_pid (self) is genuinely alive.
    _write_pending_record(repo, "work/stale", hold_until=_time.time() - 1000)

    result = mod.emit_auto_push_health(repo)

    assert "⚠" in result
    assert "lagging" in result


def test_auto_push_health_absent_record_still_warns(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/nohold")

    result = mod.emit_auto_push_health(repo)

    assert "⚠" in result
    assert "lagging" in result


def test_auto_push_health_old_failure_does_not_attach_to_fresh_count(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/oldfail")
    old = _datetime.now(_timezone.utc) - _time_delta_seconds(mod._FAILURE_RECENCY_SECONDS + 60)
    _write_failure_log_line(repo, "work/oldfail", "network", old)

    result = mod.emit_auto_push_health(repo)

    assert "⚠" in result
    assert "last failure" not in result


def test_auto_push_health_recent_failure_still_attaches(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/recentfail")
    recent = _datetime.now(_timezone.utc) - _time_delta_seconds(30)
    _write_failure_log_line(repo, "work/recentfail", "network", recent)

    result = mod.emit_auto_push_health(repo)

    assert "⚠" in result
    assert "last failure: network;" in result
    assert "transient — retry: git push" in result


def test_auto_push_health_timeout_class_maps_to_its_own_action(tmp_path):
    repo = _make_repo_with_upstream(tmp_path, branch="work/timedout")
    recent = _datetime.now(_timezone.utc) - _time_delta_seconds(30)
    _write_failure_log_line(repo, "work/timedout", "timeout", recent)

    result = mod.emit_auto_push_health(repo)

    assert "last failure: timeout;" in result
    assert result.rstrip().endswith(mod._ACTION_MAP["timeout"])


def _time_delta_seconds(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)
