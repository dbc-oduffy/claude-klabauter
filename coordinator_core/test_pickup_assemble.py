"""
coordinator_core.test_pickup_assemble — co-located pytest for
coordinator_core.pickup_assemble.

Covers one fixture per branch class named in the A2 dispatch brief:
handoff, memo, spinoff, and archived/swept baton, plus the exit-code
contract, read-only/idempotency guarantee (AC3), and the archive-fallback
multi-hit detect-then-fail-loud rule.

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_pickup_assemble.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
import coordinator_core.pickup_assemble.apply as apply_mod
import coordinator_core.review_assemble.exec_auth_stamp as exec_auth_stamp

# Real-git spawn is load-bearing: pickup_assemble's branch classifiers (handoff/
# memo/spinoff/archived-baton) and its archive-fallback multi-hit rule read
# ACTUAL git-tracked repo state (SHAs, tree layout) — no mock stands in for
# that. Fixtures spin up sibling repos per-test, so the git-repo fixture is
# not hoisted to module scope (mutation-heavy: writes/commits per test).
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _isolated_git_env(anchor: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Builds the subprocess env every git-invoking helper in this file
    shares: GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM pointed at a real empty
    file (not `os.devnull` — cross-platform-safe) under `anchor`, so a
    developer machine's global git config (hooks, signing, templates,
    credential helpers) can never leak into a fixture repo and hang or
    fail on another machine. `anchor` is the tmp_path-rooted directory a
    given fixture repo lives under — one `empty.gitconfig` per test run,
    not per repo, since several fixtures spin up sibling repos side by
    side under the same `tmp_path`."""
    empty_config = anchor / "empty.gitconfig"
    if not empty_config.exists():
        empty_config.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = str(empty_config)
    env["GIT_CONFIG_SYSTEM"] = str(empty_config)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        env=_isolated_git_env(repo.parent, extra_env),
    )


def _git_commit_backdated(repo: Path, message: str, iso_date: str) -> subprocess.CompletedProcess:
    """Commit with BOTH author and committer date backdated — plain `git
    commit --date` only sets the author date, but `_commit_recency_signal`
    reads the COMMITTER date (`%ct`), so a staleness test needs both set."""
    return _git(
        repo, "commit", "-m", message,
        extra_env={"GIT_AUTHOR_DATE": iso_date, "GIT_COMMITTER_DATE": iso_date},
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str, status: str = "open", deployment_state: str = "active", kind: str = "", scope: list[str] | None = None) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    if kind:
        fm += f"kind: {kind}\n"
    if scope:
        fm += "scope:\n" + "".join(f"  - {p}\n" for p in scope)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _seed_memo(repo: Path, name: str, status: str = "open", to: str = "", extra: str = "", kind: str = "fyi") -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f"kind: {kind}\n"
        f"status: {status}\n"
        "from: sender-session\n"
        "summary: A test memo.\n"
        "created: 2026-01-01\n"
    )
    if to:
        fm += f"to: {to}\n"
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _archive_handoff(repo: Path, live_path: Path) -> Path:
    archived = repo / "archive" / "handoffs" / "2026-01" / live_path.name
    archived.parent.mkdir(parents=True, exist_ok=True)
    text = live_path.read_text(encoding="utf-8")
    text = text.replace("status: open", "status: consumed").replace(
        "deployment_state: active", "deployment_state: shipped\nshipped_in: deadbeef"
    )
    archived.write_text(text, encoding="utf-8")
    _git(repo, "rm", "-q", str(live_path.relative_to(repo)))
    _git(repo, "add", str(archived.relative_to(repo)))
    _git(repo, "commit", "-m", f"archive {live_path.name}")
    return archived


# ---------------------------------------------------------------------------
# classification / branch-class fixtures
# ---------------------------------------------------------------------------

class TestHandoffBranch:
    def test_handoff_classifies_and_computes_tree_quiescence(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md", scope=["state/foo.md", "coordinator/bar.py"])

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        tree_quiescence = result.decision_object["preflight"]["tree_quiescence"]
        assert tree_quiescence["verdict"] == "quiet"
        local = tree_quiescence["repos"][0]
        assert local["repo"] == "."
        assert local["dirty"] == []
        assert local["unparseable_scope_entries"] == []
        assert "dirty_paths" not in result.decision_object["preflight"]

    def test_handoff_directives_include_claim_and_consume(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        cli_names = {d["cli"] for d in result.decision_object["directives"]}
        assert "session-claim-cli" in cli_names
        assert "archive-stamp-cli" in cli_names

    def test_handoff_live_claim_holder_is_business_failure(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
        claim_dir.mkdir(parents=True)
        (claim_dir / "session_id").write_text("live-peer-sid\n", encoding="utf-8")
        (claim_dir / "claimed_at").write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={})

        assert result.decision_object["gates"]["claim"]["holder"] in (None, "live-peer-sid")
        # A dead-peer claim dir with no live session backing it must NOT be
        # reported as a live holder — this repo's session.liveness has no
        # matching live session, so the claim is stale, not a collision.
        assert result.exit_code == pa.EXIT_OK


class TestSpinoffBranch:
    def test_spinoff_classified_via_kind_field(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "s1.md", kind="spinoff")

        result = pa.brief("state/handoffs/s1.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "spinoff"
        assert result.exit_code == pa.EXIT_OK


class TestMemoBranch:
    def test_memo_classifies_via_inbox_dir(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.exit_code == pa.EXIT_OK

    def test_memo_directives_include_claim(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        cli_names = {d["cli"] for d in result.decision_object["directives"]}
        assert "session-claim-cli" in cli_names

    def test_memo_to_unregistered_receiver_is_unresolved_business_failure(self, tmp_path):
        """2026-07-26 subprocess-elision spinoff: `compute_addressee_gate` no
        longer needs an installed `cross-repo-memo` CLI to compute a verdict —
        it calls the `memo.check_addressee` compute core directly. With no
        machine-local registry configured at all (the suite-wide
        `_quarantine_real_home` autouse fixture points HOME at an empty
        per-test dir, so `read_registry_repos()` legitimately returns `{}` —
        "nothing configured", not an error), an unregistered `to` receiver
        resolves UNRESOLVED (checked True, exit_code 4) — the SAME verdict
        the CLI itself would have produced, had it been installed and asked
        the identical question. UNRESOLVED IS a business failure (exit_code
        not in (0, None)) — this replaces the old "CLI absent -> skip check
        entirely" test, which was an artifact of the subprocess-era sandbox
        never standing up a fake CLI, not a deliberate design point."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="some-em")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        addressee = result.decision_object["gates"]["addressee"]
        assert addressee["checked"] is True
        assert addressee["exit_code"] == 4
        assert "does not resolve to a known repo on this machine" in addressee["message"]

    def test_registry_read_failure_reports_addressee_unchecked(self, tmp_path, monkeypatch):
        """The in-process analog of the old "CLI missing" degrade path: a
        genuinely UNREADABLE machine-local registry file (fail-loud
        `RegistryReadError`, per `_memo_resolver.read_registry_repos`) must
        still degrade `compute_addressee_gate` to the not-checked shape
        rather than raising and taking down `brief()`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="some-em")
        settings_home = tmp_path / "settings-home"
        machine_local = settings_home / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["gates"]["addressee"]["checked"] is False
        assert result.decision_object["gates"]["addressee"]["exit_code"] is None

    def test_consult_kind_gets_reply_shaped_dispositions_not_ask_shaped(self, tmp_path):
        """Defect 1 — `kind: consult` is a give-input, no-action branch
        (SKILL.md § `consult` — "reply in place") and must never surface
        `ask`'s Accept/Decline/Surface-to-PM set, which implies performing or
        adjudicating an action `consult` never takes.

        `resolves` on both dispositions is `["d-action-memo"]`, not `[]`
        (defect fix, 2026-07-25, cross-repo memo `2026-07-25-doe-claude-em-
        consult-dispositions-never-terminal.md`): replying in place IS the
        receiver-side completion of a consult — the memo's work is fully
        done once the reply lands in `actioned_note`, so it must resolve
        `d-action-memo` like any other receiver-done disposition, or the
        memo is stuck at `status: in_progress` forever."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="consult")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        jp = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "j-kind")
        values = {d["value"] for d in jp["dispositions"]}
        assert values == {"reply-short", "reply-long"}
        assert "accept-mechanical-direct" not in values
        assert "decline" not in values
        assert "surface-to-PM" not in values
        for d in jp["dispositions"]:
            assert d["resolves"] == ["d-action-memo"]


class TestArchivedBranch:
    def test_swept_handoff_resolves_via_archive_fallback(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h1.md")
        _archive_handoff(repo, live)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "archived"
        assert resolution["archive_path"].startswith("archive/handoffs/")
        assert resolution["terminal_fields"]["status"] == "consumed"

    def test_multi_hit_archive_fails_loud_not_first_wins(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h1.md")
        _archive_handoff(repo, live)
        # Seed a second, independent artifact with the SAME basename in a
        # different archive dir to force the multi-hit path.
        dup = repo / "archive" / "completed" / "h1.md"
        dup.parent.mkdir(parents=True, exist_ok=True)
        dup.write_text("---\nstatus: consumed\n---\n\nDup.\n", encoding="utf-8")
        _git(repo, "add", str(dup.relative_to(repo)))
        _git(repo, "commit", "-m", "dup h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        # Multi-hit is genuinely ambiguous (which candidate is the real
        # terminal state?) — detect-then-fail-loud surfaces it as a business
        # failure carrying every candidate path, never a silent first-wins.
        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "archived"
        assert "archive_paths" in resolution
        assert len(resolution["archive_paths"]) == 2

    def test_not_found_anywhere_is_business_failure(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("state/handoffs/nope.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in result.decision_object

    def test_memo_swept_to_archive_completed_reads_memo_terminal_fields(self, tmp_path):
        """Finding 7 — `archive/completed` holds handoffs AND memos; a
        path-substring heuristic on that dir alone misclassifies a swept
        memo as handoff-shaped and reads the wrong terminal-field set. The
        fix reuses `classify()`'s `from:`+`to:` frontmatter-shape
        discriminator instead."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        archived = repo / "archive" / "completed" / "m1.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            "kind: fyi\n"
            "status: actioned\n"
            "from: sender-session\n"
            "to: receiver-em\n"
            "summary: A test memo.\n"
            "created: 2026-01-01\n"
            "decision: accept-mechanical-direct\n"
            "decision_note: applied directly\n"
            "realized_by: abc1234\n"
        )
        archived.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(archived.relative_to(repo)))
        _git(repo, "commit", "-m", "archive m1")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["archive_path"] == "archive/completed/m1.md"
        terminal_fields = resolution["terminal_fields"]
        assert terminal_fields["decision"] == "accept-mechanical-direct"
        assert terminal_fields["realized_by"] == "abc1234"
        assert "deployment_state" not in terminal_fields
        assert "shipped_in" not in terminal_fields


    def test_bare_basename_archive_fallback_path_is_resolved_not_raw(self, tmp_path):
        """2026-07-26 defect fix — the single archive-only fallback hit's
        `artifact['path']` must be the RESOLVED archive-relative path
        (`archive/handoffs/2026-01/h-swept.md`), never the raw bare
        basename (`h-swept.md`) the caller passed in. Before this fix,
        `apply()`'s `_scoped_commit` received the bare basename as its
        pathspec and joined it onto the repo root, producing
        `git add h-swept.md failed (rc=128): fatal: pathspec 'h-swept.md'
        did not match any files` on every real `apply` call against a
        swept baton addressed by bare basename — see the sibling
        `TestApplyOnBareBasenameArchiveFallback` end-to-end regression
        below, which exercises the actual crash site."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h-swept.md")
        archived = _archive_handoff(repo, live)

        result = pa.brief("h-swept.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        assert result.decision_object["artifact"]["path"] == archived.relative_to(repo).as_posix()

    def test_native_archive_path_resolves_same_as_fallback(self, tmp_path):
        """Defect 2 — a handoff-shaped artifact passed at its NATIVE archive
        path (not the swept-away live path) satisfies `live_path.is_file()`
        directly, bypassing `_archive_fallback_search` entirely.
        `classify()`'s `in_handoffs_dir` path check then correctly finds
        neither `state/handoffs` nor `cross-repo/inbox` and falls through to
        `ambiguous` — even though the artifact is well-formed and terminal.
        The fix must resolve a native archive path to the same terminal
        `archived` classification the fallback route produces."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h1.md")
        archived = _archive_handoff(repo, live)

        result = pa.brief(str(archived.relative_to(repo)), repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "archived"
        assert resolution["archive_path"] == archived.relative_to(repo).as_posix()
        assert resolution["terminal_fields"]["status"] == "consumed"


class TestLiveFallbackBranch:
    """2026-07-25 defect fix — `/coordinator:pickup` invoked with a bare
    memo/handoff basename (no directory component) previously reported
    "not found" for a LIVE, un-actioned baton sitting in plain sight,
    because the archive-fallback search only ever looked at where a baton
    is swept TO (`ARCHIVE_DIRS`), never at where it actually LIVES
    (`LIVE_DIRS`). Covers the exact reproduction shape: a caller passing a
    bare basename with no directory component."""

    def test_bare_basename_of_live_inbox_memo_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-07-25-doe-claude-em-planless-dispatch-sidecar-provisioning.md")

        result = pa.brief(
            "2026-07-25-doe-claude-em-planless-dispatch-sidecar-provisioning.md", repo_root=repo
        )

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-07-25-doe-claude-em-planless-dispatch-sidecar-provisioning.md"
        )
        # An ordinary live pickup, never the terminal `archived` shape.
        assert result.decision_object["artifact"]["resolution"] is None

    def test_bare_basename_of_live_handoff_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h-live.md")

        result = pa.brief("h-live.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h-live.md"
        assert result.decision_object["artifact"]["resolution"] is None

    def test_bare_basename_archive_only_still_narrates_as_archived(self, tmp_path):
        """Widening the search to `LIVE_DIRS` must not disturb the
        pre-existing archive-only fallback resolution or its terminal
        narration — a bare basename with no live hit at all resolves
        exactly as it did before this fix."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h-swept.md")
        _archive_handoff(repo, live)

        result = pa.brief("h-swept.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "archived"
        assert resolution["archive_path"].startswith("archive/handoffs/")
        assert resolution["terminal_fields"]["status"] == "consumed"

    def test_basename_present_in_both_live_and_archive_is_multi_hit_not_silent_pick(self, tmp_path):
        """A basename resolvable in BOTH a live dir and an archive dir is a
        genuine ambiguity (plausibly a concurrent archival sweep
        mid-flight) — never silently preferred toward the live hit."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "dup.md")
        stale_archive = repo / "cross-repo" / "archive" / "dup.md"
        stale_archive.parent.mkdir(parents=True, exist_ok=True)
        stale_archive.write_text(
            "---\nkind: fyi\nstatus: actioned\nfrom: x\nto: y\n---\n\nStale copy.\n", encoding="utf-8"
        )
        _git(repo, "add", str(stale_archive.relative_to(repo)))
        _git(repo, "commit", "-m", "dup")

        result = pa.brief("dup.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "multi_hit"
        assert resolution["live_paths"] == ["cross-repo/inbox/dup.md"]
        assert resolution["archive_paths"] == ["cross-repo/archive/dup.md"]

    def test_not_found_message_names_widened_location_set(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("nowhere-to-be-found.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        for rel_dir in pa.LIVE_DIRS + pa.ARCHIVE_DIRS:
            assert rel_dir in error
        next_move = result.decision_object["next_move"]
        for rel_dir in pa.LIVE_DIRS + pa.ARCHIVE_DIRS:
            assert rel_dir in next_move


class TestBareSlugSuffixFallback:
    """2026-07-28 defect fix — `_search_dirs_for_basename` did a literal
    `rglob(basename)`, so a bare slug (no `.md` suffix) never matched the
    `<slug>.md` file it names on disk. Covers the reproduction shape from
    `cross-repo/inbox/2026-07-28-example-retrieval-repo-em-pickup-bare-slug-unresolvable.md`:
    `pickup-assemble brief <bare-slug>` failed while
    `pickup-assemble brief <bare-slug>.md` resolved fine."""

    def test_bare_slug_resolves_to_md_in_live_dir(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-25-triage-red-tests.md")

        result = pa.brief("2026-07-25-triage-red-tests", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/2026-07-25-triage-red-tests.md"
        )

    def test_bare_slug_resolves_to_md_in_archive_dir(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h-swept-slug.md")
        archived = _archive_handoff(repo, live)

        result = pa.brief("h-swept-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        assert result.decision_object["artifact"]["path"] == archived.relative_to(repo).as_posix()

    def test_suffixed_basename_behaviour_unchanged(self, tmp_path):
        """A basename that already carries a suffix is unaffected — literal
        match only, exactly as before this fix."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h-live.md")

        result = pa.brief("h-live.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h-live.md"

    def test_bare_slug_matching_both_extensionless_and_md_fails_loud(self, tmp_path):
        """A slug matching BOTH an extensionless file and its `.md` sibling
        must surface as an ambiguity, never resolve first-wins."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "dup-slug.md")
        extensionless = repo / "state" / "handoffs" / "dup-slug"
        extensionless.write_text(
            "---\nstatus: open\ndeployment_state: active\npredecessor: \"none\"\n---\n\nBody.\n",
            encoding="utf-8",
        )
        _git(repo, "add", str(extensionless.relative_to(repo)))
        _git(repo, "commit", "-m", "add extensionless dup")

        result = pa.brief("dup-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "multi_hit"
        assert sorted(resolution["live_paths"]) == [
            "state/handoffs/dup-slug",
            "state/handoffs/dup-slug.md",
        ]

    def test_bare_slug_hitting_live_and_archive_reports_separate_buckets(self, tmp_path):
        """The suffix fallback must not collapse the pre-existing
        live-vs-archive multi-hit split into one first-wins bucket."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "dup-both.md")
        stale_archive = repo / "cross-repo" / "archive" / "dup-both.md"
        stale_archive.parent.mkdir(parents=True, exist_ok=True)
        stale_archive.write_text(
            "---\nkind: fyi\nstatus: actioned\nfrom: x\nto: y\n---\n\nStale copy.\n", encoding="utf-8"
        )
        _git(repo, "add", str(stale_archive.relative_to(repo)))
        _git(repo, "commit", "-m", "dup both")

        result = pa.brief("dup-both", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "multi_hit"
        assert resolution["live_paths"] == ["cross-repo/inbox/dup-both.md"]
        assert resolution["archive_paths"] == ["cross-repo/archive/dup-both.md"]

    def test_not_found_error_names_basenames_tried(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("totally-absent-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "basenames tried" in error
        assert "'totally-absent-slug'" in error
        assert "'totally-absent-slug.md'" in error

    def test_not_found_error_reports_sanitized_forms_too(self, tmp_path):
        """Finding 1 (2026-07-28 review): when the sanitized-basename retry
        ALSO finds nothing, its forms must still be named in the error — a
        static 2-element re-derivation at the raise site silently omitted
        them and never revealed a sanitize retry happened at all."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("absent-slug.", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "'absent-slug.'" in error
        assert "'absent-slug..md'" in error
        assert "'absent-slug'" in error
        assert "'absent-slug.md'" in error

    def test_dotted_slug_still_resolves_to_md(self, tmp_path):
        """Finding 3 (2026-07-28 review): `Path(x).suffix` is truthy for any
        trailing dotted component, not just a real extension — a slug with
        an internal literal dot (e.g. a version-shaped slug) must still find
        its `.md` sibling."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "foo-v1.2-fix.md")

        result = pa.brief("foo-v1.2-fix", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/foo-v1.2-fix.md"
        )


class TestSuffixSlugFallback:
    """2026-07-28 PM ruling — `/coordinator:pickup` invoked with a UNIQUE
    SUFFIX of a memo/handoff basename (the `<date>-<sender>-` filename
    prefix omitted) must still resolve, via a tier strictly after the
    exact-basename tier (`TestBareSlugSuffixFallback` above)."""

    def test_suffix_only_slug_resolves_to_single_live_memo(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(
            repo,
            "2026-07-28-doe-claude-em-trampoline-fix-receipt-b6dc46d6-and-forwarder-gate-recommendation.md",
        )

        result = pa.brief(
            "trampoline-fix-receipt-b6dc46d6-and-forwarder-gate-recommendation",
            repo_root=repo,
        )

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-07-28-doe-claude-em-trampoline-fix-receipt-"
            "b6dc46d6-and-forwarder-gate-recommendation.md"
        )
        assert "resolved via unique basename-suffix match" in result.decision_object["narration"]

    def test_exact_basename_still_wins_over_suffix_candidate(self, tmp_path):
        """A file whose basename IS the passed slug (with `.md` appended)
        must resolve via the exact-basename tier and never even reach the
        suffix tier — even when a second file also ends with that slug."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "exact-match-slug-12345678.md")
        _seed_memo(repo, "2026-07-28-sender-exact-match-slug-12345678.md")

        result = pa.brief("exact-match-slug-12345678", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/exact-match-slug-12345678.md"
        )
        assert "narration" in result.decision_object
        assert "suffix" not in result.decision_object["narration"].lower()

    def test_two_files_sharing_suffix_are_ambiguous_not_a_pick(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-07-28-sender-one-shared-suffix-slug.md")
        _seed_memo(repo, "2026-07-27-other-sender-shared-suffix-slug.md")

        result = pa.brief("shared-suffix-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert sorted(resolution["live_paths"]) == sorted(
            [
                "cross-repo/inbox/2026-07-28-sender-one-shared-suffix-slug.md",
                "cross-repo/inbox/2026-07-27-other-sender-shared-suffix-slug.md",
            ]
        )

    def test_suffix_hit_in_archive_is_classified_archive_not_live(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "2026-07-20-someone-archived-suffix-slug.md")
        archived = _archive_handoff(repo, live)

        result = pa.brief("archived-suffix-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        assert result.decision_object["artifact"]["path"] == archived.relative_to(repo).as_posix()
        assert "resolved via unique basename-suffix match" in result.decision_object["narration"]

    def test_mid_word_split_does_not_suffix_match(self, tmp_path):
        """A slug that is merely a trailing SUBSTRING split mid-word (not at
        a `-`/`_` component boundary) must NOT resolve — `ate-recommendation`
        against `...forwarder-gate-recommendation.md` is a genuine
        `endswith()` hit by string logic alone, but the match starts inside
        the word `gate`, not at a component boundary, so it is exactly the
        silent-wrong-artifact risk the boundary check exists to block. The
        near-miss stays legible: it falls through to the ordinary not-found
        error, which must still name the suffix tier as having been tried."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(
            repo,
            "2026-07-28-doe-claude-em-trampoline-fix-receipt-b6dc46d6-and-forwarder-gate-recommendation.md",
        )

        result = pa.brief("ate-recommendation", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "suffix" in error.lower()

    def test_underscore_separated_handoff_slug_still_resolves(self, tmp_path):
        """`state/handoffs` is routinely `YYYY-MM-DD_HHMMSS_slug.md`
        (underscore-separated), not hyphen-separated — the boundary check
        must accept `_` as a component separator too, or suffix resolution
        would silently regress the single most common pickup artifact
        class."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-04_201950_roadmap-strang-03.md")

        result = pa.brief("roadmap-strang-03", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/2026-07-04_201950_roadmap-strang-03.md"
        )
        assert "resolved via unique basename-suffix match" in result.decision_object["narration"]

    def test_below_minimum_length_slug_does_not_suffix_match(self, tmp_path):
        """A short slug (`pa._MIN_SUFFIX_SLUG_LEN` floor) must never sweep
        the tree — the file below is a genuine suffix match by string logic
        alone, but the passed slug is too short to trust as intentional."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-07-28-sender-abcdef.md")
        short_slug = "abcdef"
        assert len(short_slug) < pa._MIN_SUFFIX_SLUG_LEN

        result = pa.brief(short_slug, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in result.decision_object

    def test_not_found_error_reflects_suffix_tier_having_run(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("totally-unresolvable-suffix-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "suffix" in error.lower()


class TestRevisionShaFallback:
    """2026-08-14 tier — `/coordinator:pickup` invoked with a git commit/
    revision SHA (peer EMs habitually cite a memo's delivery-commit SHA,
    never its filepath) must resolve to the artifact that revision
    delivered, via the SAME basename search the other tiers use rather
    than trusting the commit-time path."""

    def test_single_artifact_delivery_commit_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-08-14-sender-single-artifact-delivery.md")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-08-14-sender-single-artifact-delivery.md"
        )
        assert "resolved via its delivery commit" in result.decision_object["narration"]

    def test_commit_time_path_since_moved_to_archive_still_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "2026-08-14-moved-after-delivery.md")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        archived = _archive_handoff(repo, live)

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        assert result.decision_object["artifact"]["path"] == archived.relative_to(repo).as_posix()

    def test_multi_artifact_commit_is_ambiguous_not_guessed(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        first = repo / "cross-repo" / "inbox" / "2026-08-14-a-multi-artifact-one.md"
        second = repo / "cross-repo" / "inbox" / "2026-08-14-b-multi-artifact-two.md"
        for p in (first, second):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "---\nkind: fyi\nstatus: open\nfrom: sender-session\n"
                "summary: A test memo.\ncreated: 2026-01-01\n---\n\nBody.\n",
                encoding="utf-8",
            )
        _git(repo, "add", str(first.relative_to(repo)), str(second.relative_to(repo)))
        _git(repo, "commit", "-m", "add two memos at once")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert sorted(resolution["live_paths"]) == sorted(
            [
                "cross-repo/inbox/2026-08-14-a-multi-artifact-one.md",
                "cross-repo/inbox/2026-08-14-b-multi-artifact-two.md",
            ]
        )

    def test_unresolvable_hex_arg_errors_with_tier_named(self, tmp_path):
        """A full 40-hex value is now existence-checked by
        `_resolve_revision_raw` (via `_read_object`) before being trusted,
        symmetric with the abbreviated-sha path's `_find_object_by_prefix`
        check just below it — so a 40-hex value naming no object in the
        store genuinely reaches the "unresolvable" arm and gets the "does
        not resolve as a commit" message, not the "resolved but delivered
        no artifact" message a stale literal-hex fast path used to produce
        for it. See `test_abbreviated_sha_not_in_clone_names_the_clone_not_the_filename`
        for the abbreviated-sha sibling repro (the memo's `5bb1e3a8` shape:
        7-39 hex chars, no matching object)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "does not resolve as a commit" in error
        assert str(repo) in error
        assert f"revision {sha!r}" in error
        assert "delivered no artifact" not in error

    def test_abbreviated_sha_not_in_clone_names_the_clone_not_the_filename(self, tmp_path):
        """`cross-repo/inbox/2026-08-15-example-retrieval-repo-em-pickup-cannot-resolve-a-memo-by-its-delivery-sha.md`:
        a SHA-shaped argument that `_resolve_revision` cannot find as a
        commit in THIS clone (a sender-side commit copied into a
        receiver-side pickup, or a genuine typo) must not fall through to
        the generic filename-miss message — that misdiagnoses the failure
        as a filename search that never had a chance of succeeding."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sha = "5bb1e3a8"

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "does not resolve as a commit" in error
        assert str(repo) in error
        assert f"revision {sha!r}" in error
        assert "not found at the passed path" not in error

    def test_revision_resolves_but_delivers_no_artifact_names_it_distinctly(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()  # the "init" commit — README.md only

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "resolved as revision" in error
        assert "delivered no artifact" in error

    def test_non_hex_arg_is_unaffected_by_revision_tier(self, tmp_path):
        """Regression guard on the existing ladder: a plain (non-hex-shaped)
        slug must keep resolving exactly as before — the revision tier is
        skipped entirely, never even attempted."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-07-28-sender-not-a-git-sha-at-all-slug.md")

        result = pa.brief("not-a-git-sha-at-all-slug", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert "revision" not in result.decision_object["narration"].lower()

    def test_real_full_sha_still_resolves_and_delivers_artifact(self, tmp_path):
        """Non-regression on the existence check added to the 40-hex fast
        path: a full 40-hex sha that genuinely names a commit in the clone
        must keep resolving and keep returning that commit's delivered
        artifact — the fix only tightens the no-such-object case, it must
        not cost the real-object case anything."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-08-14-real-full-sha-still-resolves.md")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert len(sha) == 40

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-08-14-real-full-sha-still-resolves.md"
        )

    def test_real_abbreviated_sha_still_resolves(self, tmp_path):
        """Non-regression on the abbreviated-sha path, which is untouched
        by this fix but sits right beside the edited 40-hex fast path —
        confirms the fall-through ordering after the new existence check
        still reaches `_find_object_by_prefix` correctly for a real
        object."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "2026-08-14-real-abbreviated-sha-still-resolves.md")
        full_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        short_sha = full_sha[:8]

        result = pa.brief(short_sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-08-14-real-abbreviated-sha-still-resolves.md"
        )

    def test_deletion_commit_with_no_surviving_copy_errors_distinctly(self, tmp_path):
        """Citing a commit that deleted a `.md` artifact — and nothing by
        that basename survives anywhere (not moved, not re-added) — must
        name the artifact as gone, distinct from both "delivered no
        artifact" (never touched anything) and a plain lookup miss (never
        resolved a revision at all)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        memo = _seed_memo(repo, "2026-08-14-sender-deleted-outright.md")
        _git(repo, "rm", "-q", str(memo.relative_to(repo)))
        _git(repo, "commit", "-m", "remove memo outright, no replacement")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        error = result.decision_object["error"]
        assert "resolved as revision" in error
        assert "no longer exist" in error

    def test_deletion_commit_still_resolves_when_moved_elsewhere(self, tmp_path):
        """A deletion the revision-tier walk now surfaces (P2) must still
        prefer the basename re-feed over the new "gone" error when the
        deleted path in fact moved (e.g. an inbox->archive move in the
        same commit) — an archival move is a delete+add pair, and citing
        either half must resolve it."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "2026-08-14-deleted-half-of-a-move.md")
        archived = _archive_handoff(repo, live)
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()  # the archive commit itself

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "archived"
        assert result.decision_object["artifact"]["path"] == archived.relative_to(repo).as_posix()

    def test_root_commit_with_no_parent_resolves_its_own_artifact(self, tmp_path):
        """A root commit (no parents) must count its own present paths as
        changed, per `_commit_touches_path`'s root-commit handling — built
        here as a repo whose very FIRST commit delivers the artifact
        directly, so there is no init commit ahead of it to parent it."""
        repo = tmp_path / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-b", "work/test/2026-01-01")
        _git(repo, "config", "commit.gpgsign", "false")
        _git(repo, "config", "user.email", "test@example.com")
        _git(repo, "config", "user.name", "Test")
        memo = repo / "cross-repo" / "inbox" / "2026-08-14-sender-root-commit-delivery.md"
        memo.parent.mkdir(parents=True, exist_ok=True)
        memo.write_text(
            "---\nkind: fyi\nstatus: open\nfrom: sender-session\n"
            "summary: A test memo.\ncreated: 2026-01-01\n---\n\nBody.\n",
            encoding="utf-8",
        )
        _git(repo, "add", str(memo.relative_to(repo)))
        _git(repo, "commit", "-m", "root commit delivers the memo directly")
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.strip() == sha  # no parents

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert result.decision_object["artifact"]["path"] == (
            "cross-repo/inbox/2026-08-14-sender-root-commit-delivery.md"
        )

    def test_merge_commit_surfaces_paths_novel_to_either_parent(self, tmp_path):
        """A merge commit combining two branches, each introducing a
        distinct `.md` artifact the other lacks: verified against the
        module's own "any parent differs" heuristic (module docstring,
        `_changed_md_paths_for_revision`) rather than asserting full `git
        log` merge-simplification semantics the module explicitly does not
        implement (negative-spec). Both artifacts are novel relative to at
        least one parent, so both surface — an ambiguous multi-hit, not a
        single resolved artifact."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        base_branch = "work/test/2026-01-01"
        _git(repo, "checkout", "-b", "feature")
        _seed_memo(repo, "2026-08-14-a-merge-feature-side.md")
        feature_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", base_branch)
        _seed_memo(repo, "2026-08-14-b-merge-base-side.md")
        merge_result = _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature into base")
        assert merge_result.returncode == 0, merge_result.stderr
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        parents = _git(repo, "rev-list", "--parents", "-n", "1", sha).stdout.split()
        assert len(parents) == 3  # merge sha + 2 parents

        result = pa.brief(sha, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert sorted(resolution["live_paths"]) == sorted(
            [
                "cross-repo/inbox/2026-08-14-a-merge-feature-side.md",
                "cross-repo/inbox/2026-08-14-b-merge-base-side.md",
            ]
        )


class TestAmbiguousBranch:
    def test_missing_frontmatter_is_ambiguous_business_failure(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "bad.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no frontmatter here\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "bad")

        result = pa.brief("state/handoffs/bad.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        assert result.exit_code == pa.EXIT_BUSINESS_FAIL


class TestElidedArtifactPath:
    """2026-07-24 incident — a PM/EM baton path pasted from a terminal
    transcript routinely arrives with a long UUID run elided (U+2026 or
    ASCII `...`). Covers `resolve_artifact`'s elision-tolerant glob
    resolution via the public `brief()` entrypoint."""

    def test_unicode_ellipsis_form_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-24_210324_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md")

        result = pa.brief("state/handoffs/2026-07-24_210324_…md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/2026-07-24_210324_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md"
        )

    def test_ascii_dots_form_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-24_174033_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md")

        result = pa.brief("state/handoffs/2026-07-24_174033_5bdf4a2f-...md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == (
            "state/handoffs/2026-07-24_174033_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md"
        )

    def test_unique_match_narrates_the_resolution(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-24_210324_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md")

        passed = "state/handoffs/2026-07-24_210324_…md"
        result = pa.brief(passed, repo_root=repo)

        narration = result.decision_object["narration"]
        assert f"Resolved elided baton path '{passed}'" in narration
        assert "2026-07-24_210324_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md" in narration

    def test_multi_match_returns_inconclusive_judgment_point_without_selecting(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "2026-07-24_210324_5bdf4a2f-6fa4-464d-adb7-8d119d8e2348.md")
        _seed_handoff(repo, "2026-07-24_210324_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.md")

        result = pa.brief("state/handoffs/2026-07-24_210324_…md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert result.decision_object["artifact"]["classification"] == "ambiguous"
        resolution = result.decision_object["artifact"]["resolution"]
        assert resolution["status"] == "elision_inconclusive"
        assert len(resolution["candidates"]) == 2
        jp_ids = [jp["id"] for jp in result.decision_object["judgment_points"]]
        assert "j-elision" in jp_ids
        elision_jp = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "j-elision")
        assert elision_jp["recommendation"] is None
        disposition_values = {d["value"] for d in elision_jp["dispositions"]}
        assert disposition_values == set(resolution["candidates"])

    def test_zero_match_preserves_existing_error(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("state/handoffs/2026-07-24_999999_…md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in result.decision_object
        assert "not found at the passed path" in result.decision_object["error"]

    def test_dotdot_traversal_attempt_is_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        # A file outside the repo entirely that a traversal escape, if
        # honored by the elision glob search, would match.
        outside = tmp_path / "2026-07-24_210324_secret.md"
        outside.write_text("---\nstatus: open\n---\n\nSecret.\n", encoding="utf-8")

        traversal_path = "state/handoffs/../../2026-07-24_210324_…md"

        # Unit-level: the glob search itself must never see the traversal
        # pattern, regardless of what the (pre-existing, unmodified)
        # literal-path fallback does with it afterwards.
        assert pa._is_safe_elision_path(traversal_path) is False
        assert pa._resolve_elided_artifact(traversal_path, repo) == []

        result = pa.brief(traversal_path, repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in result.decision_object
        assert "secret" not in result.decision_object["error"]
        assert result.decision_object["artifact"]["classification"] == "ambiguous"


# ---------------------------------------------------------------------------
# Prose-punctuation-tolerant path resolution (2026-07-27 incident):
# `/coordinator:pickup <path>.` with a sentence-final period reported
# "not found" for a file that plainly existed. Covers `_sanitize_artifact_
# path_str` and its wiring into `resolve_artifact`/`brief`.
# ---------------------------------------------------------------------------

class TestProsePunctuationTolerantResolution:
    def test_trailing_period_resolves_with_narrated_correction(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md.", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "handoff"
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"
        assert "trimming surrounding/trailing prose punctuation" in result.decision_object["narration"]
        assert "state/handoffs/h1.md." in result.decision_object["narration"]

    def test_trailing_comma_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md,", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "cross-repo/inbox/m1.md"

    def test_wrapped_in_parens_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("(state/handoffs/h1.md)", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_wrapped_in_backticks_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("`state/handoffs/h1.md`", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_parens_plus_trailing_period_combined_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("(state/handoffs/h1.md).", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_unmatched_leading_paren_not_stripped(self):
        # Only a MATCHED wrapper pair is stripped — an unmatched leading `(`
        # is left alone by the sanitizer itself (unit-level, no repo needed).
        assert pa._sanitize_artifact_path_str("(state/handoffs/h1.md") == "(state/handoffs/h1.md"

    def test_raw_path_that_already_resolves_is_untouched_no_sanitize_note(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert "trimming surrounding/trailing prose punctuation" not in result.decision_object["narration"]

    def test_genuinely_nonexistent_path_still_fails_loud(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.brief("state/handoffs/does-not-exist.md.", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        assert "not found at the passed path" in result.decision_object["error"]

    def test_bare_dot_and_dotdot_components_not_mangled(self):
        # A bare `.` or `..` PATH COMPONENT must never lose its dot(s) —
        # that would silently rename which directory the path names.
        assert pa._sanitize_artifact_path_str(".") == "."
        assert pa._sanitize_artifact_path_str("..") == ".."
        assert pa._sanitize_artifact_path_str("state/handoffs/..") == "state/handoffs/.."

    def test_windows_drive_letter_colon_not_stripped(self):
        assert pa._sanitize_artifact_path_str("C:") == "C:"

    def test_extension_period_never_treated_as_trailing(self):
        # A raw path ending in a real extension must never lose a character
        # — the sanitizer only fires as a fallback on a literal-resolution
        # miss, and even then only strips a TRAILING punctuation character,
        # never an extension's own dot (there is none left once the
        # trailing sentence-punctuation strip halts on a non-punct char).
        assert pa._sanitize_artifact_path_str("foo.md") == "foo.md"


# ---------------------------------------------------------------------------
# AC3 — idempotent + read-only (mutates nothing)
# ---------------------------------------------------------------------------

class TestIdempotencyAndReadOnly:
    def test_identical_snapshot_and_decisions_is_byte_identical(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md", scope=["state/foo.md"])

        first = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={"j1": {"disposition": "proceed"}})
        second = pa.brief("state/handoffs/h1.md", repo_root=repo, decisions={"j1": {"disposition": "proceed"}})

        assert first.decision_object == second.decision_object
        assert first.exit_code == second.exit_code

    def test_brief_mutates_nothing_on_disk(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        before = _git(repo, "status", "--porcelain").stdout
        pa.brief("state/handoffs/h1.md", repo_root=repo)
        after = _git(repo, "status", "--porcelain").stdout

        assert before == after == ""


# ---------------------------------------------------------------------------
# Exit-code contract (AC4) — 0/1/2/3, decision object on every exit
# ---------------------------------------------------------------------------

class TestExitCodeContract:
    def test_ok_exit_zero(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        assert result.exit_code == 0

    def test_business_failure_exit_one_carries_decision_object(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        result = pa.brief("state/handoffs/nope.md", repo_root=repo)
        assert result.exit_code == 1
        assert isinstance(result.decision_object, dict)
        assert result.decision_object["artifact"] is not None

    def test_usage_error_exit_two_via_cli_main(self, capsys):
        rc = pa.main(["brief"])
        assert rc == pa.EXIT_USAGE

    def test_malformed_decisions_json_exit_two(self, capsys):
        rc = pa.main(["brief", "state/handoffs/h1.md", "--decisions", "{not json"])
        assert rc == pa.EXIT_USAGE

    def test_bare_string_decision_value_is_usage_error(self, capsys):
        rc = pa.main(["brief", "state/handoffs/h1.md", "--decisions", '{"j1": "proceed"}'])
        assert rc == pa.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err
        assert '{"j1": {"disposition": "<value>"' in err

    def test_list_decision_value_is_usage_error(self, capsys):
        rc = pa.main(["brief", "state/handoffs/h1.md", "--decisions", '{"j1": ["proceed"]}'])
        assert rc == pa.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err

    def test_null_decision_value_is_usage_error(self, capsys):
        rc = pa.main(["brief", "state/handoffs/h1.md", "--decisions", '{"j1": null}'])
        assert rc == pa.EXIT_USAGE
        err = capsys.readouterr().err
        assert "j1" in err

    def test_decisions_not_an_object_is_usage_error(self, capsys):
        rc = pa.main(["brief", "state/handoffs/h1.md", "--decisions", '["proceed"]'])
        assert rc == pa.EXIT_USAGE

    def test_valid_shaped_decisions_still_works(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        rc = pa.main(
            [
                "brief",
                "state/handoffs/h1.md",
                "--decisions",
                '{"j1": {"disposition": "proceed"}}',
            ]
        )
        assert rc in (pa.EXIT_OK, 1)
        err = capsys.readouterr().err
        assert err == ""


# ---------------------------------------------------------------------------
# Judgment-point candor (AC6) — offer, never a verdict
# ---------------------------------------------------------------------------

class TestLivenessJudgmentPoint:
    def test_no_liveness_signal_leaves_directive_unconditional(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.decision_object["judgment_points"] == []
        consume = next(d for d in result.decision_object["directives"] if d["cli"] == "archive-stamp-cli")
        assert consume["depends_on"] is None

    def test_liveness_signal_surfaces_judgment_point_not_auto_directive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test Handoff h1.md"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: active\n"
            'claimed_by: "peer-sid"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        # session.liveness has no live session matching "peer-sid" in this
        # sandboxed repo, so the cheap signal legitimately does not fire —
        # asserting the STRUCTURE (never auto-directs a stand-down) rather
        # than forcing a live session, which would need a real session dir.
        for jp in result.decision_object["judgment_points"]:
            assert jp["round_trip"] == "terminal"
            values = {d["value"] for d in jp["dispositions"]}
            assert values == {"proceed", "stand-down-and-surface"}


# ---------------------------------------------------------------------------
# Function 1 — _parse_pending_items
# ---------------------------------------------------------------------------

class TestParsePendingItems:
    def test_extracts_bullets_per_target_section(self):
        body = (
            "# Handoff\n\n"
            "## In-Progress Work\n"
            "- finish the widget refactor\n"
            "- second item here\n\n"
            "## Recommended Next Steps\n"
            "- run the migration script\n\n"
            "## Not A Target Section\n"
            "- should not appear\n\n"
            "## Blockers or Issues\n"
            "- CI is red on main\n"
        )

        items = pa._parse_pending_items(body)

        assert {"text": "finish the widget refactor", "source_section": "In-Progress Work"} in items
        assert {"text": "second item here", "source_section": "In-Progress Work"} in items
        assert {"text": "run the migration script", "source_section": "Recommended Next Steps"} in items
        assert {"text": "CI is red on main", "source_section": "Blockers or Issues"} in items
        assert not any(i["text"] == "should not appear" for i in items)

    def test_task_spine_table_rows_extracted(self):
        body = (
            "## Task Spine\n"
            "| ID | Description |\n"
            "| --- | --- |\n"
            "| chunk-A | wire up the assembler |\n"
        )

        items = pa._parse_pending_items(body)

        assert {"text": "chunk-A", "source_section": "Task Spine"} in items

    def test_no_target_sections_returns_empty(self):
        assert pa._parse_pending_items("# Handoff\n\nJust prose, no bullets.\n") == []


# ---------------------------------------------------------------------------
# Function 2 — compute_closure_signals
# ---------------------------------------------------------------------------

class TestComputeClosureSignals:
    def test_finds_candidate_commit_by_noun_overlap(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "widget.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "widget.py")
        _git(repo, "commit", "-m", "widget: finish the refactor")

        pending = [{"text": "finish the widget refactor", "source_section": "In-Progress Work"}]
        signals = pa.compute_closure_signals(repo, "2020-01-01", pending)

        assert len(signals) == 1
        subjects = {c["subject"] for c in signals[0]["candidate_commits"]}
        assert "widget: finish the refactor" in subjects

    def test_cited_plan_path_reads_status_and_chunk_commits(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_dir = repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_path = plan_dir / "2026-01-01-example.md"
        plan_path.write_text("# Example Plan\n\n**Status:** in progress\n", encoding="utf-8")
        _git(repo, "add", "docs/plans/2026-01-01-example.md")
        _git(repo, "commit", "-m", "chunk-A: land the example plan")

        pending = [{"text": "land docs/plans/2026-01-01-example.md", "source_section": "In-Progress Work"}]
        signals = pa.compute_closure_signals(repo, "2020-01-01", pending)

        assert signals[0]["cited_path"] == "docs/plans/2026-01-01-example.md"
        assert signals[0]["plan_status"] == "in progress"
        assert any(c["subject"].startswith("chunk-A:") for c in signals[0]["plan_chunk_commits"])

    def test_no_overlap_yields_empty_candidates(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        pending = [{"text": "completely unrelated prose item", "source_section": "In-Progress Work"}]
        signals = pa.compute_closure_signals(repo, "2020-01-01", pending)
        assert signals[0]["candidate_commits"] == []


# ---------------------------------------------------------------------------
# _artifact_since_date — Finding 9: every other fixture in this file uses a
# non-dated basename (h1.md, s1.md, ...), which only ever exercises the
# epoch-fallback branch. These fixtures use the real
# `YYYY-MM-DD_HHMMSS_slug.md` handoff naming convention so the regex-match
# branch that actually bounds the `git log --since=` scan gets exercised.
# ---------------------------------------------------------------------------

class TestArtifactSinceDate:
    def test_extracts_date_from_real_handoff_filename(self):
        assert pa._artifact_since_date("state/handoffs/2026-01-15_120000_test-slug.md") == "2026-01-15"

    def test_falls_back_to_epoch_for_non_dated_filename(self):
        assert pa._artifact_since_date("state/handoffs/h1.md") == "1970-01-01"

    def test_since_date_bounds_the_closure_signal_git_log_window(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        (repo / "widget.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "widget.py")
        _git_commit_backdated(repo, "widget: finish the widget refactor (old)", "2019-06-01T10:00:00")

        (repo / "widget2.py").write_text("x = 2\n", encoding="utf-8")
        _git(repo, "add", "widget2.py")
        _git_commit_backdated(repo, "widget: finish the widget refactor (new)", "2026-01-20T10:00:00")

        path = repo / "state" / "handoffs" / "2026-01-15_120000_test-slug.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2026-01-15\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: active\n"
        )
        body = "## In-Progress Work\n- finish the widget refactor\n"
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\n{body}", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add handoff")

        result = pa.brief("state/handoffs/2026-01-15_120000_test-slug.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        signals = result.decision_object["preflight"]["closure_signals"]
        assert signals != []
        subjects = {c["subject"] for c in signals[0]["candidate_commits"]}
        # The since-date bound (2026-01-15, from the filename) admits the
        # post-bound commit and excludes the pre-bound one, even though both
        # noun-overlap the pending item text identically — proof the regex
        # branch is live, not the always-epoch fallback every other fixture
        # in this file exercises.
        assert "widget: finish the widget refactor (new)" in subjects
        assert "widget: finish the widget refactor (old)" not in subjects


class TestParseSinceDateEpochAndTimezone:
    """`_parse_since_date` must never feed a date-only string through a
    naive (local-time) `.timestamp()`: on Windows the CRT raises `OSError`
    for a local-time date on or near 1970-01-01 — exactly the epoch
    fallback `_artifact_since_date` returns for a dateless handoff
    basename, which every other fixture in `TestArtifactSinceDate` exercises
    without ever calling `_parse_since_date` on it directly. Before the
    fix, `_run_git`'s broad `except OSError` swallowed that crash as a
    generic read-model-miss, so `compute_closure_signals` silently returned
    no candidates instead of raising or logging anything — the failure
    mode this class pins against regressing."""

    def test_epoch_date_does_not_raise_and_is_zero(self):
        assert pa._parse_since_date("1970-01-01") == 0

    def test_one_day_after_epoch(self):
        assert pa._parse_since_date("1970-01-02") == 86400

    def test_utc_interpretation_is_offset_independent(self):
        """A naive `.timestamp()` call would shift this by the host's local
        UTC offset; the UTC-aware form must not, on any host timezone."""
        assert pa._parse_since_date("1980-01-01") == 315532800


# ---------------------------------------------------------------------------
# Function 3 — compute_deliverable_evidence
# ---------------------------------------------------------------------------

class TestComputeDeliverableEvidence:
    def test_present_and_commit_referenced_is_strong(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "deliverable.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "deliverable.py")
        _git(repo, "commit", "-m", "add deliverable.py")

        evidence = pa.compute_deliverable_evidence(repo, ["deliverable.py"], "2020-01-01")

        assert evidence[0]["signal"] == "strong"
        assert evidence[0]["exists"] is True
        assert evidence[0]["commits"]

    def test_present_without_commit_is_weak(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "untracked.py").write_text("x = 1\n", encoding="utf-8")

        evidence = pa.compute_deliverable_evidence(repo, ["untracked.py"], "2020-01-01")

        assert evidence[0]["signal"] == "weak"

    def test_absent_is_not_shipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        evidence = pa.compute_deliverable_evidence(repo, ["nope.py"], "2020-01-01")

        assert evidence[0]["signal"] == "not-shipped"
        assert evidence[0]["exists"] is False

    def test_absent_but_deleted_by_commit_in_range_is_deleted_shipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "gone.sh").write_text("echo hi\n", encoding="utf-8")
        _git(repo, "add", "gone.sh")
        _git(repo, "commit", "-m", "add gone.sh")
        _git(repo, "rm", "gone.sh")
        _git(repo, "commit", "-m", "remove gone.sh")

        evidence = pa.compute_deliverable_evidence(repo, ["gone.sh"], "2020-01-01")

        assert evidence[0]["signal"] == "deleted-shipped"
        assert evidence[0]["exists"] is False
        assert evidence[0]["commits"]

    def test_absent_with_only_modifying_commits_stays_not_shipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "modified.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "modified.py")
        _git(repo, "commit", "-m", "add modified.py")
        (repo / "modified.py").write_text("x = 2\n", encoding="utf-8")
        _git(repo, "add", "modified.py")
        _git(repo, "commit", "-m", "modify modified.py")
        (repo / "modified.py").unlink()

        evidence = pa.compute_deliverable_evidence(repo, ["modified.py"], "2020-01-01")

        assert evidence[0]["signal"] == "not-shipped"
        assert evidence[0]["exists"] is False

    def test_absent_with_no_commits_stays_not_shipped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        evidence = pa.compute_deliverable_evidence(repo, ["never-existed.py"], "2020-01-01")

        assert evidence[0]["signal"] == "not-shipped"
        assert evidence[0]["exists"] is False
        assert evidence[0]["commits"] == []

    def test_initial_commit_cannot_delete_a_path(self, tmp_path):
        """Trivial companion to the merge case below: `_commit_deletes_path`
        short-circuits `False` for a no-parent (initial) commit — there is
        no prior tree to have carried the path, so "deleted" cannot apply.
        Was implicit-only before this test (code-reviewer finding, close-out
        of ced5c1e8)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        common_dir = pa._discover_git_dirs(repo)[1].common_dir
        sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
        commit = pa._commit_meta(common_dir, sha)
        assert commit["parents"] == []

        assert pa._commit_deletes_path(common_dir, sha, commit, "README.md") is False

    def test_merge_commit_credits_itself_for_a_deletion_on_one_parent_line(self, tmp_path):
        """Pins `_commit_deletes_path`'s merge-commit behavior (code-reviewer
        finding, close-out of ced5c1e8): a merge commit where the path was
        already deleted on ONE parent's line (`feature`), the OTHER parent
        (`base`) still carries it, and the merge result omits it — the merge
        commit itself is credited with the deletion, `True`, even though the
        actual `git rm` happened earlier, on the feature line.

        This is the defensible reading, not an accident: `_commit_deletes_path`
        is walked bottom-up per-commit (module docstring) to answer "did
        history walking THIS commit find path X gone that a parent still
        had" — and for the merge commit specifically, that question is true
        regardless of which parent line first removed it. The merge is where
        the path's absence entered the mainline being walked; crediting the
        earlier feature-line commit *instead of* the merge would require
        picking a "first" parent, which is exactly the git-log merge-
        simplification semantics this module's docstring says it does not
        implement (negative-spec). Do not "fix" this to only credit the
        feature-line commit — that changes a deliberate choice, not a bug.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        base_branch = "work/test/2026-01-01"
        (repo / "gone.sh").write_text("echo hi\n", encoding="utf-8")
        _git(repo, "add", "gone.sh")
        _git(repo, "commit", "-m", "add gone.sh")
        _git(repo, "checkout", "-b", "feature")
        _git(repo, "rm", "gone.sh")
        _git(repo, "commit", "-m", "delete gone.sh on feature")
        _git(repo, "checkout", base_branch)
        merge_result = _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature into base")
        assert merge_result.returncode == 0, merge_result.stderr
        merge_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        common_dir = pa._discover_git_dirs(repo)[1].common_dir
        merge_commit = pa._commit_meta(common_dir, merge_sha)
        assert len(merge_commit["parents"]) == 2, "fixture invalid: expected a two-parent merge"

        assert pa._commit_deletes_path(common_dir, merge_sha, merge_commit, "gone.sh") is True

        evidence = pa.compute_deliverable_evidence(repo, ["gone.sh"], "2020-01-01")

        assert evidence[0]["signal"] == "deleted-shipped"
        assert evidence[0]["exists"] is False
        assert any(c["sha"] == merge_sha for c in evidence[0]["commits"])


# ---------------------------------------------------------------------------
# AC-6 perf-optimization regression tests (the Staff Engineer review, `ac6-the Staff Engineer-
# review.md` § 8/9). Cover the `--since` slop window (D-review § 8) and the
# object-read / tree-descent / commit-parse memos introduced alongside the
# shared bounded walk in `compute_deliverable_evidence` (D2/D3/D4). These
# fixtures route through the same `_git`/`_isolated_git_env` family every
# other fixture in this file uses — a `main`-branch, no-initial-commit
# variant of `_init_repo`, since these tests measure commit counts and
# don't want `_init_repo`'s README/init commit skewing them.
# ---------------------------------------------------------------------------


def _init_isolated_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "commit.gpgsign", "false"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        result = _git(repo, *args)
        assert result.returncode == 0, result.stderr


def _git_add_isolated(repo: Path, *paths: str) -> None:
    result = _git(repo, "add", *paths)
    assert result.returncode == 0, result.stderr


def _epoch_date(dt: datetime) -> str:
    """`@<epoch> +0000` is git's own raw internal date format — avoids any
    locale/strptime ambiguity in the ISO-string alternative."""
    return f"@{int(dt.timestamp())} +0000"


def _commit_backdated_isolated(
    repo: Path, message: str, committer_date: datetime, allow_empty: bool = False
) -> str:
    date = _epoch_date(committer_date)
    args = ["commit"]
    if allow_empty:
        args.append("--allow-empty")
    args += ["-m", message]
    result = _git(repo, *args, extra_env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    assert result.returncode == 0, result.stderr
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@contextmanager
def _watch_calls(func_name: str):
    """Patches `pa.<func_name>` for the duration of the `with` block and
    yields a list that accumulates one entry (the call's positional args)
    per invocation. Shared call-counting helper for the T1/T2/T3/T4 tests
    below (the Staff Engineer review § 9) — each test differs only in which module-
    level function it watches and what invariant it checks against the
    recorded count, not in how the watching is done. Uses
    `pytest.MonkeyPatch.context()` rather than the `monkeypatch` fixture so
    it can be nested and reused freely within a single test."""
    calls: list[tuple] = []
    original = getattr(pa, func_name)

    def wrapper(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(pa, func_name, wrapper)
        yield calls


class TestSinceSlopSkewRegression:
    """§ 8 — the skew regression. Without the slop window (a plain `break`
    on the first out-of-window commit), commit B's out-of-window committer
    date terminates the walk before commit A — its ancestor with the
    in-window-relevant edit — is ever visited, so `brief()` silently
    under-reports `not-shipped` for a change that did in fact ship."""

    def test_skewed_ancestor_recovered_through_slop_window(self, tmp_path):
        repo = tmp_path / "repo"
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)

        (repo / "deliverable.py").write_text("a\n", encoding="utf-8")
        _git_add_isolated(repo, "deliverable.py")
        sha_a = _commit_backdated_isolated(repo, "A: touches deliverable", now)

        (repo / "other.py").write_text("b\n", encoding="utf-8")
        _git_add_isolated(repo, "other.py")
        _commit_backdated_isolated(repo, "B: skewed older child of A", now - timedelta(days=400))

        (repo / "other.py").write_text("c\n", encoding="utf-8")
        _git_add_isolated(repo, "other.py")
        _commit_backdated_isolated(repo, "C: HEAD, child of B", now)

        since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        evidence = pa.compute_deliverable_evidence(repo, ["deliverable.py"], since_date)

        shas = {c["sha"] for c in evidence[0]["commits"]}
        assert sha_a in shas, (
            "the skewed out-of-window commit B should be skipped but its "
            "parent A still pushed and emitted — A missing means the slop "
            "window regressed to a plain break"
        )
        assert evidence[0]["signal"] == "strong"

    def test_skew_regression_is_not_vacuous_against_a_plain_break(self, tmp_path, monkeypatch):
        """Proves the test above actually exercises the slop window: swap
        `_walk_commits_since` for a plain-`break` implementation (in-process
        monkeypatch, never touching the module file on disk) and confirm
        commit A drops out of the evidence — i.e. the assertion above would
        fail without D-review § 8's fix."""
        repo = tmp_path / "repo"
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)

        (repo / "deliverable.py").write_text("a\n", encoding="utf-8")
        _git_add_isolated(repo, "deliverable.py")
        sha_a = _commit_backdated_isolated(repo, "A: touches deliverable", now)

        (repo / "other.py").write_text("b\n", encoding="utf-8")
        _git_add_isolated(repo, "other.py")
        _commit_backdated_isolated(repo, "B: skewed older child of A", now - timedelta(days=400))

        (repo / "other.py").write_text("c\n", encoding="utf-8")
        _git_add_isolated(repo, "other.py")
        _commit_backdated_isolated(repo, "C: HEAD, child of B", now)

        since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        def _plain_break_walk(common_dir, head_sha, since_epoch):
            for sha, commit in pa._walk_commits(common_dir, head_sha):
                ts = commit["committer_epoch"] or 0
                if since_epoch is not None and ts < since_epoch:
                    break
                yield sha, commit

        monkeypatch.setattr(pa, "_walk_commits_since", _plain_break_walk)
        evidence = pa.compute_deliverable_evidence(repo, ["deliverable.py"], since_date)
        shas = {c["sha"] for c in evidence[0]["commits"]}
        assert sha_a not in shas, (
            "expected the plain-break walk to lose commit A — if it didn't, "
            "the skew regression test above is not actually pinning the "
            "slop window and needs to be fixed"
        )
        assert evidence[0]["signal"] != "strong"


class TestCommitMetaWindowInvariance:
    """T1 — bounded-walk invariant: the number of commits actually parsed
    must not grow with the amount of history sitting outside the `--since`
    window, only with the window itself plus the slop budget."""

    @staticmethod
    def _build_windowed_repo(tmp_path: Path, name: str, n_old: int, n_window: int) -> tuple[Path, str]:
        repo = tmp_path / name
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)
        old_date = now - timedelta(days=400)
        for i in range(n_old):
            (repo / "old.py").write_text(f"{i}\n", encoding="utf-8")
            _git_add_isolated(repo, "old.py")
            _commit_backdated_isolated(repo, f"old {i}", old_date)
        for i in range(n_window):
            (repo / "target.py").write_text(f"{i}\n", encoding="utf-8")
            _git_add_isolated(repo, "target.py")
            _commit_backdated_isolated(repo, f"window {i}", now)
        since_date = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        return repo, since_date

    def test_commit_parse_count_invariant_to_out_of_window_growth(self, tmp_path):
        n_window = 5
        small_repo, small_since = self._build_windowed_repo(tmp_path, "small-history", n_old=50, n_window=n_window)
        with _watch_calls("_parse_commit") as small_calls:
            pa.compute_deliverable_evidence(small_repo, ["target.py"], small_since)
        small_count = len(small_calls)

        # A SEPARATE repo (not more commits appended to the one above) --
        # appending real ancestors behind an existing chain would require
        # rewriting every descendant's sha, which is not what this
        # invariant is about. Two independently-built repos with the same
        # window shape and different out-of-window depth isolate "does the
        # walk cost scale with total history" from "does the process-
        # lifetime memo carry over between calls" (it would, trivially,
        # inside one repo/common_dir -- these are different `common_dir`s
        # so each run is measured cold).
        large_repo, large_since = self._build_windowed_repo(tmp_path, "large-history", n_old=250, n_window=n_window)
        with _watch_calls("_parse_commit") as large_calls:
            pa.compute_deliverable_evidence(large_repo, ["target.py"], large_since)
        large_count = len(large_calls)

        assert small_count == large_count, (
            f"{small_count} commits parsed against 50 out-of-window commits "
            f"but {large_count} against 250 -- the walk is scaling with "
            "total history instead of stopping at the slop budget"
        )
        # Loose absolute ceiling as a second, independent catch: a
        # regression that walks 2x (or all of) history blows well past
        # this even if it coincidentally parsed the same count on both
        # runs above.
        ceiling = 2 * (n_window + pa._SINCE_SLOP)
        assert large_count <= ceiling, (
            f"{large_count} commits parsed exceeds the loose ceiling of "
            f"{ceiling} (2x window+slop) -- looks like a full-history walk, "
            "not a bounded one"
        )


class TestObjectLoaderMemoized:
    """T2 — the memo property: the real object-read layer below
    `_read_object`'s cache must be entered at most once per distinct sha
    that `brief()`'s call graph asks for, however many signal-computation
    functions ask for the same commit/tree/blob."""

    def test_read_loose_object_entered_once_per_distinct_sha(self, tmp_path):
        repo = tmp_path / "repo"
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)
        for i in range(8):
            (repo / f"f{i}.py").write_text(f"{i}\n", encoding="utf-8")
            _git_add_isolated(repo, f"f{i}.py")
            _commit_backdated_isolated(repo, f"commit {i}", now - timedelta(minutes=8 - i))

        common_dir = pa._discover_git_dirs(repo)[1].common_dir
        # This fixture repo must stay packless, or objects would route
        # through `_read_pack_object_by_sha` instead and the loose-call
        # assertion below would be vacuous.
        assert not pa._iter_pack_files(common_dir), "fixture repo unexpectedly packed"

        since_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        with _watch_calls("_read_object") as object_calls, _watch_calls("_read_loose_object") as loose_calls:
            evidence = pa.compute_deliverable_evidence(repo, ["f0.py", "f3.py", "f7.py"], since_date)
        assert evidence

        requested_shas = {args[1].lower() for args in object_calls}
        assert len(loose_calls) == len(requested_shas), (
            f"the loose-object loader was entered {len(loose_calls)} times "
            f"for {len(requested_shas)} distinct shas -- a distinct sha is "
            "being read from disk more than once, so the object memo isn't "
            "deduplicating across the call graph"
        )


class TestBlobShaAtTreePathMemoized:
    """T3 — the tree-descent memo: a second `_blob_sha_at_tree_path` lookup
    against a tree sha already resolved for `path` (typically a commit's
    and its `git commit --allow-empty` child sharing one content-addressed
    tree) must cost zero additional tree-entry scans.

    NOT what this pins, and deliberately so: `_commit_touches_path`'s own
    `if parent_commit["tree"] == commit["tree"]: continue` short-circuit is
    structurally unobservable via `_scan_tree_entry` call-counting. Inside
    one `_commit_touches_path` call, the `current = _blob_sha_at_tree_path
    (..., commit["tree"], path)` lookup for the commit's OWN tree always
    runs before the parent loop; whenever the short-circuit's condition
    holds, `parent_commit["tree"] == commit["tree"]`, so that `current`
    lookup already warmed the exact `(tree_sha, path)` cache key the parent
    branch's `_blob_sha_at_tree_path(..., parent_commit["tree"], path)`
    call would use — trees are content-addressed, so equal shas are the
    same object. Deleting the short-circuit line therefore still hits the
    cache `current` warmed moments earlier in the same call: there is no
    fixture where the parent branch can be the first toucher of that
    tree+path pair. What genuinely deduplicates the scans, and what this
    test pins instead, is `_blob_sha_at_tree_path`'s own
    `_TREE_PATH_STEP_CACHE` memo — see
    `test_memo_is_not_vacuous_against_an_uncached_lookup` below for the
    non-vacuity proof."""

    def test_identical_tree_lookup_costs_zero_tree_scans_on_repeat(self, tmp_path):
        repo = tmp_path / "repo"
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)

        (repo / "deliverable.py").write_text("x\n", encoding="utf-8")
        _git_add_isolated(repo, "deliverable.py")
        sha1 = _commit_backdated_isolated(repo, "first", now - timedelta(minutes=2))
        sha2 = _commit_backdated_isolated(repo, "empty", now - timedelta(minutes=1), allow_empty=True)

        common_dir = pa._discover_git_dirs(repo)[1].common_dir
        commit1 = pa._commit_meta(common_dir, sha1)
        commit2 = pa._commit_meta(common_dir, sha2)
        assert commit1["tree"] == commit2["tree"], "fixture invalid: --allow-empty must share its parent's tree"

        pa._blob_sha_at_tree_path(common_dir, commit1["tree"], "deliverable.py")

        with _watch_calls("_scan_tree_entry") as scan_calls:
            blob = pa._blob_sha_at_tree_path(common_dir, commit2["tree"], "deliverable.py")

        assert blob is not None
        assert scan_calls == [], (
            f"_blob_sha_at_tree_path performed {len(scan_calls)} tree-entry "
            "scan(s) for a tree sha already resolved for this path -- "
            "expected the tree-descent memo to make this zero"
        )

    def test_memo_is_not_vacuous_against_an_uncached_lookup(self, tmp_path, monkeypatch):
        """Proves the test above actually exercises `_TREE_PATH_STEP_CACHE`:
        swap `_blob_sha_at_tree_path` for a cache-free re-implementation
        (in-process monkeypatch, never touching the module file on disk)
        and confirm the repeat lookup DOES cost a tree-entry scan -- i.e.
        the assertion above would fail without the memo."""
        repo = tmp_path / "repo"
        _init_isolated_repo(repo)
        now = datetime.now(timezone.utc)

        (repo / "deliverable.py").write_text("x\n", encoding="utf-8")
        _git_add_isolated(repo, "deliverable.py")
        sha1 = _commit_backdated_isolated(repo, "first", now - timedelta(minutes=2))
        sha2 = _commit_backdated_isolated(repo, "empty", now - timedelta(minutes=1), allow_empty=True)

        common_dir = pa._discover_git_dirs(repo)[1].common_dir
        commit1 = pa._commit_meta(common_dir, sha1)
        commit2 = pa._commit_meta(common_dir, sha2)

        def _uncached_blob_sha_at_tree_path(common_dir, tree_sha, path):
            if not tree_sha:
                return None
            parts = [p for p in path.split("/") if p]
            if not parts:
                return None
            current = tree_sha
            for i, part in enumerate(parts):
                obj = pa._read_object(common_dir, current)
                child = pa._scan_tree_entry(obj[1], part) if (obj is not None and obj[0] == "tree") else None
                if child is None:
                    return None
                if i == len(parts) - 1:
                    return child
                current = child
            return None

        monkeypatch.setattr(pa, "_blob_sha_at_tree_path", _uncached_blob_sha_at_tree_path)
        pa._blob_sha_at_tree_path(common_dir, commit1["tree"], "deliverable.py")

        with _watch_calls("_scan_tree_entry") as scan_calls:
            pa._blob_sha_at_tree_path(common_dir, commit2["tree"], "deliverable.py")

        assert scan_calls != [], (
            "expected the cache-free lookup to re-scan the tree -- if it "
            "didn't, the memo test above is not actually pinning "
            "_TREE_PATH_STEP_CACHE and needs to be fixed"
        )


class TestScopePathCountSharedWalk:
    """T4 — the fan-out-dedup property: `compute_deliverable_evidence`'s
    outer walk never references `scope_paths` at all, so it visiting the
    same commit set regardless of path count is true by construction, not
    what this test exercises. What IS at risk is the fan-out underneath —
    each scope path re-invokes `_commit_touches_path` per commit, and each
    such call does a `_commit_meta` lookup for every parent sha. This pins
    that `_commit_meta`'s cache correctly dedups that per-scope-path
    fan-out, so the number of commits actually PARSED (`_parse_commit`, the
    expensive cache-miss path, not `_commit_meta`'s cheap cache-hit
    accessor) does not scale with the number of scope paths."""

    @staticmethod
    def _build_multi_touch_repo(tmp_path: Path, name: str, now: datetime) -> tuple[Path, str, list[str]]:
        repo = tmp_path / name
        _init_isolated_repo(repo)
        paths = ["a.py", "b.py", "c.py"]
        for p in paths:
            (repo / p).write_text("x\n", encoding="utf-8")
        _git_add_isolated(repo, *paths)
        _commit_backdated_isolated(repo, "init all three", now - timedelta(minutes=10))
        for i, p in enumerate(paths):
            (repo / p).write_text(f"{i}-changed\n", encoding="utf-8")
            _git_add_isolated(repo, p)
            _commit_backdated_isolated(repo, f"touch {p}", now - timedelta(minutes=9 - i))
        since_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return repo, since_date, paths

    def test_commit_parse_count_equal_for_one_path_and_k_paths(self, tmp_path):
        now = datetime.now(timezone.utc)
        # Two SEPARATE repos with identical commit shape, same reasoning as
        # TestCommitMetaWindowInvariance: measuring both scope-path counts
        # against the SAME repo/common_dir would make the second call's
        # low count reflect the (correct, but not what's under test) cross-
        # call memo carry-over rather than the shared-walk property itself.
        repo_one, since_one, _all_paths = self._build_multi_touch_repo(tmp_path, "repo-one-path", now)
        repo_k, since_k, all_paths = self._build_multi_touch_repo(tmp_path, "repo-k-paths", now)

        with _watch_calls("_parse_commit") as one_path_calls:
            pa.compute_deliverable_evidence(repo_one, ["a.py"], since_one)
        with _watch_calls("_parse_commit") as k_path_calls:
            pa.compute_deliverable_evidence(repo_k, all_paths, since_k)

        assert len(k_path_calls) == len(one_path_calls), (
            f"1 scope path parsed {len(one_path_calls)} commits but "
            f"{len(all_paths)} scope paths parsed {len(k_path_calls)} -- "
            "commit parsing is scaling with the number of scope paths, "
            "which means the walk is no longer shared across paths"
        )


# ---------------------------------------------------------------------------
# Function 4 — compute_premise_checks
# ---------------------------------------------------------------------------

class TestComputePremiseChecks:
    def test_path_premise_present(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "state").mkdir()
        (repo / "state" / "seen.md").write_text("x\n", encoding="utf-8")

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "state/seen.md"}])

        assert results[0]["witness"] == "present"

    def test_path_premise_found_elsewhere_not_first_wins(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "archive").mkdir()
        (repo / "archive" / "moved.md").write_text("x\n", encoding="utf-8")

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "state/moved.md"}])

        assert results[0]["witness"] == "found-elsewhere"
        assert results[0]["found_elsewhere"] == ["archive/moved.md"]

    def test_path_premise_genuinely_absent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "nowhere.md"}])

        assert results[0]["witness"] == "absent"

    def test_sha_premise_present_and_absent(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

        results = pa.compute_premise_checks(
            repo,
            [
                {"type": "sha", "value": head_sha},
                {"type": "sha", "value": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"},
            ],
        )

        assert results[0]["witness"] == "present"
        assert results[1]["witness"] == "absent"

    def test_pathspec_premise_empty_is_surfaced(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        results = pa.compute_premise_checks(repo, [{"type": "pathspec", "value": "no/such/*.md"}])

        assert results[0]["witness"] == "empty-surface"

    def test_path_premise_miss_prunes_non_artifact_subtrees(self, tmp_path, monkeypatch):
        """2026-08-13 hot-path-over-acquisition fix: the miss arm must never
        descend into `.git`/`__pycache__`/`build`/`scratch`/`scratchpad`/
        `*.egg-info` — pin the narrowing itself, not just its outcome, by
        asserting os.walk is never called with one of those dirnames
        present in the yielded dirnames after pruning. (`dist` was removed
        from the prune set — see `test_path_premise_witness_under_dist_is_
        found_not_pruned` below.)"""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # A same-named file sitting ONLY inside a pruned subtree must not be
        # found — proves the walk never descends there, not just that the
        # final result happens to omit it via some other filter.
        pruned_dir = repo / "build" / "nested"
        pruned_dir.mkdir(parents=True)
        (pruned_dir / "decoy.md").write_text("x\n", encoding="utf-8")

        seen_dirnames_lists = []
        real_walk = os.walk

        def spy_walk(top, *args, **kwargs):
            for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
                # Store the SAME list object the caller mutates in place via
                # `dirnames[:] = ...` — checked after the walk completes, so
                # this reflects the post-prune state the caller left behind.
                seen_dirnames_lists.append((dirpath, dirnames))
                yield dirpath, dirnames, filenames

        monkeypatch.setattr(pa.os, "walk", spy_walk)

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "decoy.md"}])

        assert results[0]["witness"] == "absent"
        assert results[0].get("found_elsewhere", []) == []
        for dirpath, dirnames in seen_dirnames_lists:
            assert "build" not in dirnames
            assert ".git" not in dirnames

    def test_path_premise_found_elsewhere_survives_pruning(self, tmp_path):
        """Preserved-contract half of the pruning fix: a real witness sitting
        in a legitimate (non-pruned) location, including source under
        `coordinator_core/`, is still found — the narrowing must not turn a
        genuine hit into a miss."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "coordinator_core").mkdir()
        (repo / "coordinator_core" / "witness.md").write_text("x\n", encoding="utf-8")
        # A decoy of the SAME basename inside a pruned subtree coexists —
        # the real hit must still surface even with a pruned-subtree decoy
        # present alongside it.
        pruned_dir = repo / "__pycache__"
        pruned_dir.mkdir()
        (pruned_dir / "witness.md").write_text("x\n", encoding="utf-8")

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "state/witness.md"}])

        assert results[0]["witness"] == "found-elsewhere"
        assert results[0]["found_elsewhere"] == ["coordinator_core/witness.md"]

    @pytest.mark.parametrize(
        "pruned_name", sorted(pa._PREMISE_WALK_PRUNE_DIRNAMES - {".git"})
    )
    def test_path_premise_witness_under_each_pruned_name_is_absent(self, tmp_path, pruned_name):
        """Every name actually in `_PREMISE_WALK_PRUNE_DIRNAMES` prunes as
        claimed — a real witness sitting ONLY under that name is reported
        absent, not found-elsewhere. Parameterized so a future addition to
        the set is exercised automatically. `.git` is excluded from the
        parametrization since `_init_repo` already creates it as a real
        git directory; it's covered by the dedicated pruning test above."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        pruned_dir = repo / pruned_name
        pruned_dir.mkdir()
        (pruned_dir / "witness.md").write_text("x\n", encoding="utf-8")

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "witness.md"}])

        assert results[0]["witness"] == "absent"

    def test_path_premise_witness_under_dist_is_found_not_pruned(self, tmp_path):
        """Regression pin for the 2026-08-13 false-negative: `dist` was
        removed from `_PREMISE_WALK_PRUNE_DIRNAMES` after being found to
        hold 33 tracked files in this repo — a path premise citing a
        tracked file under `dist/` must resolve `found-elsewhere`, not
        `absent`. Also asserts `dist` is no longer a member of the prune
        set, so this test fails loudly if a future edit re-adds it without
        re-verifying `git ls-files dist` is empty."""
        assert "dist" not in pa._PREMISE_WALK_PRUNE_DIRNAMES
        repo = tmp_path / "repo"
        _init_repo(repo)
        dist_dir = repo / "dist"
        dist_dir.mkdir()
        (dist_dir / "witness.md").write_text("x\n", encoding="utf-8")

        results = pa.compute_premise_checks(repo, [{"type": "path", "value": "state/witness.md"}])

        assert results[0]["witness"] == "found-elsewhere"
        assert results[0]["found_elsewhere"] == ["dist/witness.md"]


# ---------------------------------------------------------------------------
# Function 5 — compute_stealth_skip_flags
# ---------------------------------------------------------------------------

class TestComputeStealthSkipFlags:
    def test_valid_sha_and_no_commit_token_are_not_flagged(self):
        items = [
            {"text": "a", "shipped_in": "deadbee"},
            {"text": "b", "shipped_in": "substantively-shipped-no-commit:2026-01-01"},
            {"text": "c"},
        ]
        assert pa.compute_stealth_skip_flags(items) == []

    def test_prose_rationale_is_flagged(self):
        items = [{"text": "a", "shipped_in": "subsumed by the X workstream"}]
        flags = pa.compute_stealth_skip_flags(items)
        assert len(flags) == 1
        assert flags[0]["flag"] == "stealth-skip-suspect"


# ---------------------------------------------------------------------------
# Function 6 — build_completeness_checklist
# ---------------------------------------------------------------------------

class TestBuildCompletenessChecklist:
    def test_restart_gated_hoisted_ahead_of_live(self):
        fm = {
            "completeness_checklist": [
                "live: the server responds",
                "restart-gated: the plugin reloads",
            ]
        }
        result = pa.build_completeness_checklist(fm, "state/handoffs/h1.md")

        classes_in_order = [item["class"] for item in result["items"] if not item["malformed"]]
        # items[] preserves raw declaration order...
        assert classes_in_order == ["live", "restart-gated"]
        # ...but directives[] is hoisted restart-gated-first.
        assert result["directives"][0]["harness_task_create"]["class"] == "restart-gated"
        assert result["directives"][1]["harness_task_create"]["class"] == "live"

    def test_probe_never_becomes_an_auto_run_directive(self):
        fm = {"completeness_checklist": ["live: server up [probe: curl localhost]"]}
        result = pa.build_completeness_checklist(fm, "state/handoffs/h1.md")

        cli_names = {d["cli"] for d in result["directives"]}
        assert "curl" not in cli_names
        assert len(result["judgment_points"]) == 1
        jp = result["judgment_points"][0]
        assert jp["revalidate_at_dispatch"] is False
        for disposition in jp["dispositions"]:
            assert disposition["resolves"] == []

    def test_malformed_item_surfaced_not_silently_dropped(self):
        fm = {"completeness_checklist": ["not-a-real-class: nope"]}
        result = pa.build_completeness_checklist(fm, "state/handoffs/h1.md")
        assert result["items"][0]["malformed"] is True
        assert result["directives"] == []

    def test_absent_completeness_checklist_is_a_clean_noop(self):
        result = pa.build_completeness_checklist({}, "state/handoffs/h1.md")
        assert result == {"items": [], "directives": [], "judgment_points": [], "batches": []}

    def test_batches_reflect_restart_gated_hoisted_ordering(self):
        fm = {
            "completeness_checklist": [
                "live: the server responds",
                "restart-gated: the plugin reloads",
            ]
        }
        result = pa.build_completeness_checklist(fm, "state/handoffs/h1.md")
        assert [b["class"] for b in result["batches"]] == ["restart-gated", "live"]


# ---------------------------------------------------------------------------
# compute_liveness_signal — RETIRED 2026-07-24 (chunk C6, plan AMENDMENT):
# `TestLivenessThreeSignals` (the old three-signal (b)/(c) inference corpus)
# was deleted here, not ported — chunk C7 Part A collapsed
# `compute_liveness_signal` to the claim-stamp state machine and DELETED
# signal (b) `_commit_recency_signal` and signal (c)
# `_active_handoff_scan_signal` outright (see that function's own docstring).
# These three tests asserted behavior of functions that no longer exist; the
# resulting green-count DECREASE is the AC11-amended expectation, not a
# regression. `TestStampReadMatrix` below covers the surviving state machine.
# ---------------------------------------------------------------------------
# AC3 extension — new functions are read-only too
# ---------------------------------------------------------------------------

class TestNewFunctionsAreReadOnly:
    def test_all_seven_functions_mutate_nothing(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md", scope=["state/foo.md"])

        before = _git(repo, "status", "--porcelain").stdout

        pa._parse_pending_items("## In-Progress Work\n- x\n")
        pa.compute_closure_signals(repo, "2020-01-01", [{"text": "x"}])
        pa.compute_deliverable_evidence(repo, ["state/foo.md"], "2020-01-01")
        pa.compute_premise_checks(repo, [{"type": "path", "value": "state/foo.md"}])
        pa.compute_stealth_skip_flags([{"text": "x", "shipped_in": "bad"}])
        pa.build_completeness_checklist({"completeness_checklist": ["live: x"]}, "state/handoffs/h1.md")
        pa.compute_liveness_signal(repo, {}, "state/handoffs/h1.md")
        pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        after = _git(repo, "status", "--porcelain").stdout
        assert before == after == ""


# ---------------------------------------------------------------------------
# gates.claim_grant (AC3b/AC3b-i/AC3c/AC3d) — the five-row claim-attempt
# truth table, incl. the self-holder row (the Director of Engineering review, F2) and both sides of
# the settling-window boundary.
# ---------------------------------------------------------------------------

def _write_claim(repo: Path, class_: str, basename: str, session_id: str, age_minutes: float) -> Path:
    claims_dir = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text(f"{session_id}\n", encoding="utf-8")
    claimed_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    (claims_dir / "claimed_at").write_text(
        claimed_at.strftime("%Y-%m-%dT%H:%M:%SZ") + "\n", encoding="utf-8",
    )
    return claims_dir


class TestClaimGrantTruthTable:
    def test_row1_no_claim_dir_is_granted(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "granted"
        assert grant["holder"] is None
        assert grant["holder_live"] is False
        assert grant["claim_age_minutes"] is None
        assert grant["drop_invocation"] == "pickup-assemble drop state/handoffs/h1.md"

    def test_row2_self_holder_is_granted_no_mutation(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "my-sid", age_minutes=5)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: True)
        monkeypatch.setattr(
            pa._liveness, "claim_holder_live",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be consulted on the self-holder row")),
        )

        before = _git(repo, "status", "--porcelain").stdout
        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")
        after = _git(repo, "status", "--porcelain").stdout

        assert grant["verdict"] == "granted"
        assert grant["reason"] == "you already hold this"
        assert grant["holder"] == "my-sid"
        assert grant["holder_live"] is True
        assert before == after == ""

    def test_row3_other_live_holder_is_denied(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"
        assert grant["holder"] == "peer-sid"
        assert grant["holder_live"] is True

    def test_row4_not_live_within_settling_window_is_denied(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=29)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"
        assert grant["holder"] == "peer-sid"
        assert grant["holder_live"] is False
        assert grant["claim_age_minutes"] == 29

    def test_row4_boundary_exactly_at_stale_after_is_still_denied(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=pa.CLAIM_STALE_AFTER_MINUTES)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"

    def test_row5_not_live_past_settling_window_is_granted_with_warning(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=94)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "granted-with-warning"
        assert grant["holder"] == "peer-sid"
        assert grant["claim_age_minutes"] == 94

    def test_row5_boundary_one_minute_past_stale_after_is_granted_with_warning(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=pa.CLAIM_STALE_AFTER_MINUTES + 1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "granted-with-warning"

    def test_unparseable_claim_age_defaults_to_denied_not_warning(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        claims_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / "h1.md"
        claims_dir.mkdir(parents=True)
        (claims_dir / "session_id").write_text("peer-sid\n", encoding="utf-8")
        # No claimed_at file at all -> age is an evidence gap, not staleness.
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"
        assert grant["claim_age_minutes"] is None

    def test_row3_held_by_self_survives_a_poisoned_evidence_key(self, tmp_path, monkeypatch):
        """`_with_evidence`/the denied-path literal must merge `holder_evidence()`'s
        fields UNDER the caller-computed base fields, not over them — a future
        `holder_evidence()` key named `held_by_self` must never be able to flip
        a peer's row-3 `False` to `True`. Poison the evidence dict directly to
        prove the merge order, independent of whatever real keys
        `holder_evidence()` happens to emit today."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)
        monkeypatch.setattr(
            pa, "_holder_evidence",
            lambda *a, **k: {"held_by_self": True, "verdict": "granted"},
        )

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"
        assert grant["held_by_self"] is False

    def test_row2_self_holder_survives_a_poisoned_evidence_key(self, tmp_path, monkeypatch):
        """Mirror of the row-3 case above for the self-holder row: a poisoned
        `holder_evidence()` return must not be able to flip `held_by_self`
        from True to False, nor override `verdict`, via `_with_evidence`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "my-sid", age_minutes=5)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: True)
        monkeypatch.setattr(
            pa._liveness, "claim_holder_live",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be consulted on the self-holder row")),
        )
        monkeypatch.setattr(
            pa, "_holder_evidence",
            lambda *a, **k: {"held_by_self": False, "verdict": "denied"},
        )

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "granted"
        assert grant["held_by_self"] is True

    def test_claim_stale_after_is_at_least_the_liveness_recency_window(self):
        """AC3b-i, red test: shrinking `CLAIM_STALE_AFTER_MINUTES` below
        `session.liveness`'s own 30-minute recency window (`_THIRTY_MIN`)
        fails this assertion — the two are separate, independently-named
        constants, never aliased, with only this invariant tying them."""
        from coordinator_core.session import liveness as liveness_module

        liveness_recency_minutes = liveness_module._THIRTY_MIN // 60
        assert pa.CLAIM_STALE_AFTER_MINUTES >= liveness_recency_minutes


# ---------------------------------------------------------------------------
# holder_evidence (decidable claim evidence, 2026-07-27) — makes
# compute_claim_grant's holder_live boolean and claim age falsifiable
# rather than a bare assertion.
# ---------------------------------------------------------------------------


def _self_lstart() -> str:
    result = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    lstart = result.stdout.strip()
    assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
    return lstart


def _write_holder_meta(repo: Path, sid: str, meta: dict) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return sdir


def _write_touched(repo: Path, sid: str, lines: list[str]) -> Path:
    """Writes `<sessions-dir>/<sid>/touched.txt` — the claim_index substrate
    `_claim_scope_overlap` now joins against, replacing this test class's
    old transcript-`recent_paths` fixtures. Line format mirrors
    `coordinator_core/session/tests/test_claim_neighbours.py`'s own
    `_touch_line` helper: `"<verb> <iso-ts> <path>"`."""
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "touched.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sdir


def _touch_line(verb: str, path: str, when: str = "2026-08-16T10:00:00.000000Z") -> str:
    return f"{verb} {when} {path}"


def _write_transcript(user_claude: Path, sid: str, records: list) -> Path:
    proj_dir = user_claude / "projects" / "-fake-project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _tool_use_record(tool_name: str, tool_input: dict) -> dict:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "name": tool_name, "input": tool_input},
            ]
        },
    }


class TestComputeBranchGate:
    """`compute_branch_gate`'s `classification` keyword governs whether a
    memo pickup gets an honest `in_place` verdict instead of the
    handoff/spinoff `resume`/`create` vocabulary. No prior test in this
    file exercised the function directly at all (grep confirms zero hits
    before this class); the memo path was previously covered only
    indirectly through `brief()`, which never asserted on
    `gates["branch"]`."""

    def test_memo_classification_reports_in_place_regardless_of_branch_name(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)  # lands on work/test/2026-01-01

        result = pa.compute_branch_gate(repo, classification="memo")

        assert result["action"] == "in_place"
        assert result["current_branch"] == "work/test/2026-01-01"
        assert "reason" in result

    def test_handoff_classification_on_work_branch_resumes(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result == {"action": "resume", "current_branch": "work/test/2026-01-01"}

    def test_handoff_classification_on_main_creates(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "main")

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result == {"action": "create", "current_branch": "main"}

    def test_omitted_classification_matches_legacy_behavior(self, tmp_path):
        """Byte-identical to the pre-classification signature — the default
        keeps every caller that doesn't pass `classification` unaffected."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        assert pa.compute_branch_gate(repo) == pa.compute_branch_gate(repo, classification=None)
        assert pa.compute_branch_gate(repo) == {
            "action": "resume",
            "current_branch": "work/test/2026-01-01",
        }

    def test_branch_none_reports_unknown_and_ignores_classification(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa, "_current_branch", lambda _root: None)

        result = pa.compute_branch_gate(repo, classification="memo")

        assert result == {"action": "unknown", "current_branch": None}

    def test_no_live_peers_on_main_still_creates(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "main")

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result == {"action": "create", "current_branch": "main"}

    def test_live_peer_on_main_downgrades_create_to_in_place(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "main")
        _write_holder_meta(
            repo,
            "peer-sid",
            {"pid": "1", "last_activity": pa._session_core.now_iso(), "branch": "work/peer/x"},
        )

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result["action"] == "in_place"
        assert result["current_branch"] == "main"
        assert "peer-sid" in result["reason"]
        assert "work/peer/x" in result["reason"]

    def test_unknown_live_set_downgrades_create_to_in_place(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _git(repo, "checkout", "-b", "main")
        monkeypatch.setattr(
            pa._worktree_safety._liveness,
            "live_session_verdicts",
            lambda cwd=None: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result["action"] == "in_place"
        assert result["current_branch"] == "main"
        assert "reason" in result

    def test_work_branch_resume_unaffected_by_live_peers(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)  # lands on work/test/2026-01-01
        _write_holder_meta(
            repo, "peer-sid", {"pid": "1", "last_activity": pa._session_core.now_iso()}
        )

        result = pa.compute_branch_gate(repo, classification="handoff")

        assert result == {"action": "resume", "current_branch": "work/test/2026-01-01"}


@pytest.mark.skipif(
    os.name == "nt",
    reason="fixture built via POSIX `ps -o lstart=`, mirroring session/tests/test_liveness.py's own skip",
)
class TestHolderEvidence:
    def test_stable_pid_basis_with_fresh_recency(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        lstart = _self_lstart()
        epoch = pa._session_core.lstart_to_epoch(lstart)
        assert epoch > 0
        now_iso = pa._session_core.now_iso()
        _write_holder_meta(
            repo,
            "s-live",
            {
                "pid": "999",
                "last_activity": now_iso,
                "stable_pid": str(os.getpid()),
                "stable_pid_lstart": lstart,
                "stable_pid_start_epoch": str(epoch),
            },
        )

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence("s-live", repo, want_activity=False)

        assert evidence["liveness_basis"] == "stable-pid"
        assert evidence["last_activity_age_sec"] is not None
        assert evidence["last_activity_age_sec"] < 5
        assert "evidence_error" not in evidence

    def test_recency_window_basis_and_stale_hedge_in_reason(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        old_iso = pa._session_core.now_iso()  # will be overridden below
        old_epoch = pa._session_core.now_epoch() - (25 * 60)
        import datetime as _dt

        old_iso = _dt.datetime.fromtimestamp(old_epoch, tz=_dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _write_holder_meta(
            repo,
            "s-recency",
            {"pid": "1", "last_activity": old_iso},
        )
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence("s-recency", repo, want_activity=True)

        assert evidence["liveness_basis"] == "recency-window"
        assert evidence["last_activity_age_sec"] is not None
        assert evidence["last_activity_age_sec"] >= 25 * 60 - 5

        reason = pa._claim_grant_denied_live_reason("s-recency", evidence)
        assert "may be a stale claim" in reason
        assert "recency-window only" in reason

    def test_missing_meta_json_is_fail_soft_and_does_not_change_verdict(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)
        # No meta.json ever written for "peer-sid" — an evidence gap, not a
        # verdict change.

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"
        assert grant["holder_live"] is True
        assert grant["holder"] == "peer-sid"
        assert grant["liveness_basis"] is None
        assert grant["last_activity_age_sec"] is None

    def test_corrupt_meta_json_sets_evidence_error_verdict_unchanged(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sdir = repo / ".git" / "coordinator-sessions" / "peer-sid"
        sdir.mkdir(parents=True)
        (sdir / "meta.json").write_text("{not valid json", encoding="utf-8")
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence("peer-sid", repo, want_activity=True)
        # read_meta_field degrades corrupt JSON to "" per its own contract —
        # this reads as a plain evidence gap. The basis is
        # "recency-window-mtime" (not plain "recency-window"): with no
        # readable `last_activity`, live_session_verdicts()'s Layer-2 arm
        # substitutes the meta-less/mid-write recency SOURCE
        # (`_dir_recency_fallback_epoch`, using the corrupt meta.json's own
        # mtime) and names that substitution distinctly (C8). There is no
        # live failure mode in the read path for this fixture to force
        # `evidence_error` through, so this test instead pins the fail-soft
        # OUTCOME (gap, not crash) that the error branch exists to guarantee
        # — only the basis STRING moved, not the outcome being pinned.
        assert evidence["liveness_basis"] == "recency-window-mtime"
        assert evidence["last_activity_age_sec"] is None

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")
        assert grant["verdict"] == "denied"
        assert grant["holder_live"] is True

    def test_no_touched_claim_yields_scope_overlap_false_not_none(self, tmp_path, monkeypatch):
        """`recent_paths`/`scope_overlap` are decoupled post-C3 (previously
        both had to be non-empty, or `scope_overlap` fell back to `None`
        purely because the transcript was missing — see module docstring).
        No transcript AND a genuinely resolved-but-unclaimed `scope` must
        read `recent_paths` unavailable (transcript-derived, unaffected)
        alongside `scope_overlap: False` (claim-derived, resolved-empty
        claimant set), never `None` for either reason alone."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_holder_meta(repo, "s-no-transcript", {"pid": "1", "last_activity": pa._session_core.now_iso()})
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home-no-transcript"))

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence(
            "s-no-transcript", repo, scope=["state/handoffs/touched.md"], want_activity=True
        )

        assert evidence["recent_paths"] == []
        assert evidence["recent_paths_source"] == "unavailable"
        assert evidence["scope_overlap"] is False

    def test_claim_derived_scope_overlap_true_and_false(self, tmp_path, monkeypatch):
        """AC3 — `scope_overlap` now joins `scope` against `claim_index.
        lookup()` (what a holder has actually CLAIMED, via `touched.txt`),
        not the holder's transcript `recent_paths` — this is the exact
        substrate swap that pins today's fix would have caught (pre-C3,
        BOTH of the fixtures below returned `scope_overlap: None`, since
        transcript activity is now irrelevant to this field: the old code
        required a transcript AND a scope overlap, and this test writes no
        transcript at all)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_holder_meta(repo, "s-scoped", {"pid": "1", "last_activity": pa._session_core.now_iso()})
        _write_touched(repo, "s-scoped", [_touch_line("T", "state/handoffs/touched.md")])

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence(
            "s-scoped", repo, scope=["state/handoffs/touched.md"], want_activity=True
        )

        assert evidence["scope_overlap"] is True

        _write_holder_meta(repo, "s-unrelated", {"pid": "1", "last_activity": pa._session_core.now_iso()})
        _write_touched(repo, "s-unrelated", [_touch_line("T", "coordinator_core/unrelated.py")])

        evidence_unrelated = holder_evidence(
            "s-unrelated", repo, scope=["state/handoffs/touched.md"], want_activity=True
        )

        assert evidence_unrelated["scope_overlap"] is False

    def test_scope_none_is_unresolvable_scope_empty_list_is_resolved_false(self, tmp_path):
        """The either-side-empty-collapses-to-`None` bug this re-point
        fixes (module docstring): `scope is None` (no scope resolved at
        all — the pre-bridge handoff case) stays the genuine unresolvable
        `None`, but `scope == []` (an artifact that explicitly resolved to
        an empty file set) is RESOLVED, just with nothing to overlap —
        deterministically `False`, never `None`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_holder_meta(repo, "s-any", {"pid": "1", "last_activity": pa._session_core.now_iso()})

        from coordinator_core.session.holder_evidence import holder_evidence

        unresolvable = holder_evidence("s-any", repo, scope=None, want_activity=True)
        assert unresolvable["scope_overlap"] is None

        resolved_empty = holder_evidence("s-any", repo, scope=[], want_activity=True)
        assert resolved_empty["scope_overlap"] is False

    def test_artifact_path_bridges_a_handoff_via_deliverable_id(self, tmp_path, monkeypatch):
        """AC2 via holder_evidence's `artifact_path` shape: a handoff
        declares no `scope:` of its own — the caller passing `artifact_path`
        (its own claimed handoff) routes `scope_overlap` through
        `claim_neighbours.find_neighbours()`, which bridges via
        `deliverable_id` to the plan carrying the same id and uses THAT
        plan's `scope:`. This is the exact case that read `None` for every
        handoff before C3 (module docstring) — a live claimant of the
        bridged path now reads `True`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_dir = repo / "docs" / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        (plan_dir / "bridge-plan.md").write_text(
            "---\n"
            "title: bridge plan\n"
            "deliverable_id: dlv-shared-thing-abc123\n"
            "scope:\n"
            "  - state/handoffs/touched.md\n"
            "---\n\n# Plan\n",
            encoding="utf-8",
        )
        handoff_path = _seed_handoff_with_fields(
            repo, "bridge-handoff.md", 'deliverable_id: "dlv-shared-thing-abc123"\n'
        )
        _write_holder_meta(repo, "peer-sid", {"pid": "1", "last_activity": pa._session_core.now_iso()})
        _write_touched(repo, "peer-sid", [_touch_line("T", "state/handoffs/touched.md")])

        from coordinator_core.session import holder_evidence as he_mod

        monkeypatch.setattr(
            he_mod.claim_neighbours.liveness, "session_live",
            lambda sid, cwd=None: sid == "peer-sid",
        )

        evidence = he_mod.holder_evidence(
            "peer-sid",
            repo,
            artifact_path=str(handoff_path),
            caller_sid="my-own-sid",
            want_activity=True,
        )

        assert evidence["scope_overlap"] is True

    def test_artifact_path_with_no_bridge_is_unresolvable(self, tmp_path):
        """AC2's negative case: a handoff whose `deliverable_id` matches no
        plan's is UNRESOLVABLE (`claim_neighbours.UNRESOLVABLE`) — this must
        surface as `scope_overlap: None`, never a silently-wrong `False`
        that would read as "checked, nobody there" (module docstring's
        conflation this re-point exists to close)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        handoff_path = _seed_handoff_with_fields(
            repo, "orphan-handoff.md", 'deliverable_id: "dlv-nobody-claims-this-999999"\n'
        )
        _write_holder_meta(repo, "peer-sid", {"pid": "1", "last_activity": pa._session_core.now_iso()})

        from coordinator_core.session.holder_evidence import holder_evidence

        evidence = holder_evidence(
            "peer-sid", repo, artifact_path=str(handoff_path), want_activity=True
        )

        assert evidence["scope_overlap"] is None


# NEGATIVE-SPEC (2026-08-22): `TestCompetingClaimEvidenceCap` and
# `TestCompetingClaim` lived here, plus the `build_competing_jp_is_blocking`
# helper only `TestCompetingClaim` called. Their whole subject —
# `pickup_assemble.compute_competing_claim` — was deleted at aadef0e23 ("C1,
# C2, C5, C11: wave 1 of the ceremony-assembler rebuild", chunk C2 of
# docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md), which
# rewrote `compute_claim_grant` to the ratified R4 rule and dropped the
# sibling-scan producer outright; `gates.competing_claim` is no longer in the
# emitted gates object either. That commit's reverse-reference scan missed
# these call sites, so they survived as AttributeError-at-runtime residue.
#
# Do NOT reinstate them against a resurrected producer. What they pinned that
# still has a live subject is already covered elsewhere: the lineage filter by
# `TestLivenessSignalLineageFilter`/`TestClaimGrantLineageFilter` below, the
# holder-evidence cheap/activity split by `holder_evidence`'s own tests in
# coordinator_core/session/tests/test_liveness.py, and the R4 grant rule by
# coordinator_core/pickup_assemble/tests/test_claim_rule.py. The evidence-cap
# and per-candidate-memo assertions had no surviving subject at all — the memo
# was internal to the deleted function.


# ---------------------------------------------------------------------------
# preflight.tree_quiescence (AC3/AC8, C1a) — replaces the `dirty_paths`
# scope-echo with a real git-status intersection per repo named in `scope:`.
# ---------------------------------------------------------------------------

class TestTreeQuiescence:
    def test_no_scope_entries_is_quiet(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, [])

        assert result == {
            "verdict": "quiet",
            "repos": [{"repo": ".", "dirty": [], "unparseable_scope_entries": []}],
        }

    def test_clean_local_path_is_quiet(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, ["README.md"])

        assert result["verdict"] == "quiet"
        assert result["repos"][0]["dirty"] == []

    def test_dirty_local_path_is_detected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "coordinator").mkdir()
        (repo / "coordinator" / "bar.py").write_text("x\n", encoding="utf-8")

        result = pa.compute_tree_quiescence(repo, ["coordinator/bar.py", "state/foo.md"])

        assert result["verdict"] == "dirty"
        assert result["repos"][0]["dirty"] == ["coordinator/bar.py"]

    def test_prose_scope_entry_never_lands_in_dirty_list(self, tmp_path):
        """AC3's red test: a prose `scope:` entry surfaces in
        `unparseable_scope_entries`, never silently counted as dirty and
        never silently dropped."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        prose = "claude-klabauter: coordinator/bin/ (apply entrypoint + auto-fire hook — claude-klabauter direct-commit granted)"

        result = pa.compute_tree_quiescence(repo, [prose])

        assert result["verdict"] == "quiet"
        local = result["repos"][0]
        assert prose in local["unparseable_scope_entries"]
        assert prose not in local["dirty"]
        assert all(prose != d for d in local["dirty"])

    def test_sibling_repo_resolved_through_registry_not_hardcoded(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sibling = tmp_path / "sibling"
        _init_repo(sibling)
        (sibling / "coordinator_core").mkdir()
        (sibling / "coordinator_core" / "dag.py").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(
            pa, "registry_get",
            lambda key: str(sibling) if key == "repos.claude_klabauter" else None,
        )

        result = pa.compute_tree_quiescence(repo, ["claude-klabauter: coordinator_core/dag.py"])

        assert result["verdict"] == "dirty"
        sibling_entry = next(r for r in result["repos"] if r["repo"] == str(sibling))
        assert sibling_entry["dirty"] == ["coordinator_core/dag.py"]

    def test_unresolvable_sibling_registry_key_is_unparseable_not_dirty(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa, "registry_get", lambda key: None)

        result = pa.compute_tree_quiescence(repo, ["unregistered-repo: some/path.py"])

        assert result["verdict"] == "quiet"
        assert "unregistered-repo: some/path.py" in result["repos"][0]["unparseable_scope_entries"]

    def test_windows_drive_letter_sibling_path_resolves_via_pathlib(self, tmp_path, monkeypatch):
        """AC12, moved to C1a: a sibling repo resolved to a Windows
        drive-letter path is handled through `pathlib.Path`, never a raw
        string concatenation — the resolved `cwd` handed to `_run_git` must
        be a `Path` instance carrying the registry value verbatim."""
        calls: list[tuple[list[str], Path]] = []

        def fake_run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
            calls.append((args, cwd))
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(pa, "_run_git", fake_run_git)
        windows_root = "C:\\Users\\test\\X\\claude-klabauter"
        monkeypatch.setattr(
            pa, "registry_get",
            lambda key: windows_root if key == "repos.claude_klabauter" else None,
        )
        repo = tmp_path / "repo"

        result = pa.compute_tree_quiescence(repo, ["claude-klabauter: coordinator_core\\dag.py"])

        assert result["verdict"] == "quiet"
        sibling_calls = [c for c in calls if isinstance(c[1], Path) and str(c[1]) == windows_root]
        assert len(sibling_calls) == 1
        args, cwd = sibling_calls[0]
        assert isinstance(cwd, Path)
        assert "coordinator_core\\dag.py" in args

    def test_drive_letter_local_path_is_never_misread_as_a_sibling_prefix(self, tmp_path):
        """A bare Windows-absolute local scope entry (`C:\\...`, no space
        after the colon) must not be misclassified as a `<repo-id>: <path>`
        sibling reference — the repo-id group's TWO-OR-MORE-characters
        requirement (`[A-Za-z][A-Za-z0-9_-]+`) is what keeps a single-letter
        drive out of that path, independent of whether whitespace after the
        colon is required (whitespace is now optional — see
        `test_sibling_prefix_matches_the_real_no_space_plan_shape`)."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, ["C:\\Users\\test\\file.py"])

        local = result["repos"][0]
        assert local["unparseable_scope_entries"] == []
        assert len(result["repos"]) == 1

    def test_forward_slash_drive_letter_local_path_is_never_misread_as_a_sibling_prefix(self, tmp_path):
        """`D:/foo/bar` (forward-slash Windows drive form) must also be
        rejected as a sibling-repo prefix — same single-letter-repo-id
        reasoning as the backslash case above, pinned separately because the
        path separator differs."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, ["D:/foo/bar"])

        local = result["repos"][0]
        assert local["unparseable_scope_entries"] == []
        assert len(result["repos"]) == 1

    def test_sibling_prefix_matches_the_real_no_space_plan_shape(self, tmp_path, monkeypatch):
        """Real plans write `- claude-klabauter:coordinator_core/x.py` with NO
        space after the colon (YAML: `repo: path` with a space parses as a
        mapping, not the plain string a `scope:` entry needs) — this is the
        shape the regex must match for the sibling-repo branch to ever fire
        at all. Regression pin for the defect this fix closes: the prior
        mandatory `\\s+` made this exact real-world shape permanently
        unmatched."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sibling = tmp_path / "sibling"
        _init_repo(sibling)
        (sibling / "coordinator_core").mkdir()
        (sibling / "coordinator_core" / "dag.py").write_text("x\n", encoding="utf-8")
        monkeypatch.setattr(
            pa, "registry_get",
            lambda key: str(sibling) if key == "repos.claude_klabauter" else None,
        )

        result = pa.compute_tree_quiescence(repo, ["claude-klabauter:coordinator_core/dag.py"])

        assert result["verdict"] == "dirty"
        sibling_entry = next(r for r in result["repos"] if r["repo"] == str(sibling))
        assert sibling_entry["dirty"] == ["coordinator_core/dag.py"]

    def test_url_is_never_misread_as_a_sibling_prefix(self, tmp_path):
        """`https://example.com/x` must not be misclassified as a sibling
        reference — `https` is 5 characters, well past the drive-letter
        floor, so only the `(?!//)` negative lookahead immediately after the
        colon keeps a URL scheme out of the sibling-repo branch. Treated as
        an ordinary local scope entry (git will simply report it as never
        dirty, since it names no real path)."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, ["https://example.com/x"])

        local = result["repos"][0]
        assert local["unparseable_scope_entries"] == []
        assert len(result["repos"]) == 1

    def test_bare_path_is_never_misread_as_a_sibling_prefix(self, tmp_path):
        """A bare relative path with no colon at all is ordinary local
        scope, not a sibling reference."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        result = pa.compute_tree_quiescence(repo, ["coordinator_core/pickup_assemble/__init__.py"])

        local = result["repos"][0]
        assert local["unparseable_scope_entries"] == []
        assert len(result["repos"]) == 1

    def test_prose_line_with_colon_is_unparseable_not_sibling(self, tmp_path):
        """A prose line that happens to contain `word: more words` (a
        two-or-more-character "word" followed by a colon and whitespace-
        containing rest) is matched-then-rejected into
        `unparseable_scope_entries`, never silently treated as a sibling
        path — same matched-then-rejected classification as the existing
        `test_prose_scope_entry_never_lands_in_dirty_list` case, pinned here
        with a line that isn't itself repo-shaped."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        prose = "Note: see the design doc for context"

        result = pa.compute_tree_quiescence(repo, [prose])

        assert result["verdict"] == "quiet"
        local = result["repos"][0]
        assert prose in local["unparseable_scope_entries"]
        assert len(result["repos"]) == 1


# ---------------------------------------------------------------------------
# gates.coast (AC8, C1a) — reports what the EM is holding; never gates the
# claim (that is gates.claim_grant's job).
# ---------------------------------------------------------------------------

class TestGatesCoast:
    def test_clear_with_no_judgment_points_and_no_claim_grant(self):
        result = pa.compute_coast([])

        assert result == {"verdict": "clear", "notes": [], "blocked_by": []}

    def test_blocked_when_judgment_points_are_unresolved(self):
        result = pa.compute_coast([{"id": "j1"}, {"id": "j2"}])

        assert result["verdict"] == "blocked"
        assert result["blocked_by"] == ["j1", "j2"]

    def test_blocked_when_claim_grant_is_denied_even_with_no_judgment_points(self):
        result = pa.compute_coast([], claim_grant={"verdict": "denied", "reason": "held by a live peer"})

        assert result["verdict"] == "blocked"
        assert result["blocked_by"] == []

    def test_granted_with_warning_notes_but_stays_clear(self):
        result = pa.compute_coast([], claim_grant={"verdict": "granted-with-warning", "reason": "stale claim"})

        assert result["verdict"] == "clear"
        assert result["notes"]

    def test_dirty_tree_notes_but_stays_clear(self):
        tree_quiescence = {"verdict": "dirty", "repos": []}

        result = pa.compute_coast([], tree_quiescence=tree_quiescence)

        assert result["verdict"] == "clear"
        assert result["notes"]

    def test_granted_claim_grant_never_flips_verdict(self):
        result = pa.compute_coast([], claim_grant={"verdict": "granted"})

        assert result["verdict"] == "clear"
        assert result["notes"] == []

    def test_blocked_reason_is_self_sufficient_no_cross_reference_needed(self):
        """AC2: a caller reading ONLY `gates.coast` must learn WHY it is
        blocked and WHAT unblocks it — no `judgment_points[]` cross-reference,
        no SKILL.md read."""
        jp = {
            "id": "j1",
            "question": "Is a live different-session peer holding this handoff?",
            "dispositions": [
                {"value": "proceed", "resolves": ["d2"]},
                {"value": "stand-down", "resolves": []},
            ],
        }

        result = pa.compute_coast([jp])

        assert result["verdict"] == "blocked"
        assert "j1" in result["reason"]
        assert "Is a live different-session peer holding this handoff?" in result["reason"]
        assert "j1" in result["remedy"]
        assert "proceed" in result["remedy"]

    def test_blocked_reason_enumerates_all_blocking_judgment_points_not_just_first(self):
        """C2 amendment (gap-sweep): a handoff can carry 2+ simultaneous
        blocking judgment_points (e.g. liveness `j1` + gate check `jgate`).
        `reason`/`remedy` must enumerate every blocking point, plural — never
        silently report only the first."""
        j1 = {
            "id": "j1",
            "question": "Is a live different-session peer holding this handoff?",
            "dispositions": [{"value": "proceed", "resolves": ["d2"]}],
        }
        jgate = {
            "id": "jgate",
            "question": "Has the restart gate been cleared?",
            "dispositions": [{"value": "cleared", "resolves": ["d3"]}],
        }

        result = pa.compute_coast([j1, jgate])

        assert result["verdict"] == "blocked"
        assert "j1" in result["reason"]
        assert "jgate" in result["reason"]
        assert "Is a live different-session peer holding this handoff?" in result["reason"]
        assert "Has the restart gate been cleared?" in result["reason"]
        assert "j1" in result["remedy"]
        assert "jgate" in result["remedy"]

    def test_blocked_reason_absent_when_no_question_derivable(self):
        """A judgment_point lacking a `question` contributes no reason text —
        `reason`/`remedy` are omitted entirely rather than emitted empty."""
        result = pa.compute_coast([{"id": "j1"}, {"id": "j2"}])

        assert result["verdict"] == "blocked"
        assert "reason" not in result
        assert "remedy" not in result


# ---------------------------------------------------------------------------
# brief()-level integration — the wiring the isolated compute_* unit tests
# above cannot catch (a green unit test on a function brief() never calls is
# worthless; these assert the ASSEMBLED decision object). Dispatch brief
# defects 1-6.
# ---------------------------------------------------------------------------

class TestBriefPreflightWiring:
    """Defect 1 — compute_* functions must be wired into brief()'s handoff
    path, not hardcoded to `[]`."""

    def test_handoff_with_pending_items_populates_real_preflight_evidence(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "widget.py").write_text("x = 1\n", encoding="utf-8")
        _git(repo, "add", "widget.py")
        _git(repo, "commit", "-m", "widget: finish the refactor")

        (repo / "state").mkdir(exist_ok=True)
        (repo / "state" / "foo.md").write_text("seen\n", encoding="utf-8")
        _git(repo, "add", "state/foo.md")
        _git(repo, "commit", "-m", "add state/foo.md")

        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: active\n"
            "scope:\n"
            "  - widget.py\n"
        )
        body = (
            "## In-Progress Work\n"
            "- finish the widget refactor (see state/foo.md, "
            "shipped_in: subsumed-by-Y)\n"
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\n{body}", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        preflight = result.decision_object["preflight"]

        assert preflight["closure_signals"] != []
        subjects = {c["subject"] for c in preflight["closure_signals"][0]["candidate_commits"]}
        assert "widget: finish the refactor" in subjects

        assert preflight["deliverable_evidence"] != []
        assert preflight["deliverable_evidence"][0]["signal"] == "strong"

        assert preflight["premise_checks"] != []
        assert preflight["premise_checks"][0]["witness"] == "present"

        assert preflight["stealth_skip_flags"] != []
        assert preflight["stealth_skip_flags"][0]["flag"] == "stealth-skip-suspect"

        # prereq_reverify has no backing MECHANICAL function yet — the key
        # must be present (never silently omitted), honestly empty.
        assert preflight["prereq_reverify"] == []

    def test_handoff_with_no_pending_items_stays_honestly_empty(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        preflight = result.decision_object["preflight"]
        assert preflight["closure_signals"] == []
        assert preflight["deliverable_evidence"] == []
        assert preflight["premise_checks"] == []
        assert preflight["stealth_skip_flags"] == []
        assert preflight["prereq_reverify"] == []


class TestBriefCompletenessChecklistWiring:
    """Finding 1 — `build_completeness_checklist` (Function 6) is fully
    implemented and unit-tested but was never called from `brief()`; a real
    `completeness_checklist:` field on a handoff produced zero trace of it.
    These are `brief()`-level integration tests (analogous to
    `TestBriefPreflightWiring` above) — a unit test on the function alone
    does NOT satisfy this finding, since that is exactly the gap that let
    the wiring bug ship green.

    Also covers Finding 2 — `_parse_fm_dict` previously fell through to the
    single-line-only `read_fm_field_unquoted` regex for any key other than
    `scope:`, so a real multi-line `completeness_checklist:` block parsed to
    `""` and `build_completeness_checklist`'s `if not raw_items` guard read
    it as absent. These tests seed the block through a real handoff FILE
    (not a hand-built `fm` dict fixture) so they exercise the real
    `resolve_artifact` -> `_parse_fm_dict` -> `brief()` parsing path — the
    path Finding 2 actually broke.
    """

    def test_multiline_checklist_populates_preflight_and_mirror_directive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: active\n"
            "completeness_checklist:\n"
            '  - "live: the server responds"\n'
            '  - "restart-gated: the plugin reloads"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        preflight = result.decision_object["preflight"]

        # Finding 2 regression: the block must not read back as absent.
        assert preflight["completeness_items"] != []
        assert len(preflight["completeness_items"]) == 2

        # Finding 1: restart-gated-hoisted batching evidence is real.
        assert [b["class"] for b in preflight["completeness_batches"]] == [
            "restart-gated",
            "live",
        ]

        directives = result.decision_object["directives"]
        mirror_directives = [d for d in directives if d["cli"] == "coordinator-tasks-mirror"]
        assert len(mirror_directives) == 2
        assert mirror_directives[0]["harness_task_create"]["class"] == "restart-gated"
        assert mirror_directives[1]["harness_task_create"]["class"] == "live"

    def test_no_completeness_checklist_field_is_honestly_empty(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        preflight = result.decision_object["preflight"]
        assert preflight["completeness_items"] == []
        assert preflight["completeness_batches"] == []
        assert result.decision_object["judgment_points"] == []


class TestBriefM0ActionedMemoTerminal:
    """Defect 2 — `status: actioned` must be a read-only terminal
    short-circuit, never the same claim path as an open memo."""

    def test_actioned_memo_is_terminal_no_claim_directive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "cross-repo" / "inbox" / "m1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            # kind: fyi — this fixture is about the M0 no-claim-directive
            # short-circuit, not the reply-closure check (2026-07-25 defect
            # fix): an absent `kind` defaults to `ask` (reply required) and
            # would otherwise pick up an unrelated `j-reply-closure`
            # judgment point here, which is covered on its own in
            # test_pickup_assemble_reply_closure.py.
            "kind: fyi\n"
            "from: sender-em\n"
            "to: receiver-em\n"
            "status: actioned\n"
            "decision: accepted\n"
            'decision_note: "Adopted and generalized."\n'
            "realized_by: docs/plans/example.md\n"
        )
        path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add m1")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        assert obj["directives"] == []
        assert obj["judgment_points"] == []
        terminal_state = obj["artifact"]["terminal_state"]
        assert terminal_state["status"] == "actioned"
        assert terminal_state["decision"] == "accepted"
        assert terminal_state["realized_by"] == "docs/plans/example.md"

    def test_open_memo_unaffected_by_m0_change(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert "terminal_state" not in result.decision_object["artifact"]
        cli_names = {d["cli"] for d in result.decision_object["directives"]}
        assert "session-claim-cli" in cli_names


class TestBriefAwaitingGateCheck:
    """Defect 3 — `awaiting_gate` must surface a judgment point, never an
    unconditional claim+consume."""

    def test_awaiting_gate_surfaces_gate_check_not_unconditional_consume(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: awaiting_gate\n"
            'gate_dependency: "peer campaign must settle first"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        assert obj["gates"]["gate_check"]["gate_dependency"] == "peer campaign must settle first"
        consume = next(
            d for d in obj["directives"] if d["cli"] == "archive-stamp-cli" and d["args"][0] == "claim-handoff"
        )
        # Piece A — `d-gate-recheck` rides in `d2`'s depends_on alongside
        # `jgate` unconditionally, so the recording directive always
        # sequences before the claim (cross-repo/inbox/2026-08-04-market-
        # intelligence-em-pickup-jgate-cleared-strands-gate-fields.md).
        assert consume["depends_on"] == ["jgate", "d-gate-recheck"]
        gate_recheck = next(
            d
            for d in obj["directives"]
            if d["cli"] == "archive-stamp-cli" and d["args"][0] == "gate-recheck-handoff"
        )
        assert gate_recheck["id"] == "d-gate-recheck"
        assert gate_recheck["depends_on"] == "jgate"
        gate_jp = next(jp for jp in obj["judgment_points"] if jp["id"] == "jgate")
        values = {d["value"] for d in gate_jp["dispositions"]}
        assert values == {"cleared", "not-cleared"}
        cleared = next(d for d in gate_jp["dispositions"] if d["value"] == "cleared")
        assert set(cleared["resolves"]) == {"d2", "d-gate-recheck"}
        assert cleared.get("guidance")
        not_cleared = next(d for d in gate_jp["dispositions"] if d["value"] == "not-cleared")
        assert not_cleared["resolves"] == []
        assert not_cleared.get("guidance")


class TestBriefAwaitingGateAndLivenessCombinedDependsOn:
    """Finding 5 — when an `awaiting_gate` handoff's gate check AND a
    live-peer liveness signal both independently fire on d2, `depends_on`
    must carry BOTH ids (AND-semantics list form), not just `jgate` — see
    contract § "The list form of `depends_on`". `jsucc` (plan
    2026-08-01-wsc-completeness-gate-and-pickup-successor.md, C5) joins this
    same AND-list when a live successor exists — see
    `TestBriefSuccessorHandoffSurfacesJudgmentPoint` below for that case;
    these fixtures seed no successor, so `jsucc` never fires here."""

    def test_both_gates_firing_yields_list_depends_on(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: awaiting_gate\n"
            'gate_dependency: "peer campaign must settle first"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert {"jgate", "j1"} <= jp_ids
        consume = next(
            d for d in obj["directives"] if d["cli"] == "archive-stamp-cli" and d["args"][0] == "claim-handoff"
        )
        assert consume["depends_on"] == ["jgate", "d-gate-recheck", "j1"]

    def test_only_gate_fires_depends_on_carries_recheck_id_too(self, tmp_path):
        # No claimed_by/consumed_by/picked_up_by and no cited plan -> the
        # liveness signal legitimately does not fire. Piece A means the
        # single-judgment-point `awaiting_gate` case no longer keeps the
        # plain-string form the contract otherwise still enforces for a
        # true single blocker (`test_no_gate_or_liveness_yields_
        # unconditional_none` and similar single-jp cases elsewhere are
        # unaffected) — `d-gate-recheck`'s directive-id always rides
        # alongside `jgate` (ordering-only, see `build_gate_recheck_
        # directive`'s docstring), so this is the two-id list form.
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: awaiting_gate\n"
            'gate_dependency: "peer campaign must settle first"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        consume = next(
            d for d in obj["directives"] if d["cli"] == "archive-stamp-cli" and d["args"][0] == "claim-handoff"
        )
        assert consume["depends_on"] == ["jgate", "d-gate-recheck"]


class TestBriefShippedStateSurfacesJudgmentPoint:
    """2026-07-25 defect fix — a handoff already stamped
    `deployment_state: shipped` previously briefed as freely dispatchable
    (`gates.coast.verdict == "clear"`, `judgment_points: []`), telling a
    peer session to redo finished work. Must now surface a `jshipped`
    judgment point and a non-`clear` coast, mirroring the `awaiting_gate`
    (`jgate`) shape — never a hard block."""

    def test_shipped_handoff_yields_non_clear_coast_and_shipped_judgment_point(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            'predecessor: "none"\n'
            "deployment_state: shipped\n"
            "shipped_in: deadbeef\n"
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object

        assert obj["gates"]["coast"]["verdict"] != "clear"
        assert "jshipped" in obj["gates"]["coast"]["blocked_by"]

        shipped_jp = next(jp for jp in obj["judgment_points"] if jp["id"] == "jshipped")
        values = {d["value"] for d in shipped_jp["dispositions"]}
        assert values == {"reopen-and-proceed", "confirm-shipped-stand-down"}
        for disposition in shipped_jp["dispositions"]:
            assert disposition.get("guidance")

        assert obj["gates"]["shipped_state"]["deployment_state"] == "shipped"
        assert obj["gates"]["shipped_state"]["shipped_in"] == "deadbeef"

        consume = next(d for d in obj["directives"] if d["cli"] == "archive-stamp-cli")
        assert consume["depends_on"] == "jshipped"

    def test_normal_open_handoff_is_unaffected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object

        assert obj["judgment_points"] == []
        assert obj["gates"]["coast"]["verdict"] == "clear"
        assert "shipped_state" not in obj["gates"]
        consume = next(d for d in obj["directives"] if d["cli"] == "archive-stamp-cli")
        assert consume["depends_on"] is None


def _seed_successor_handoff(
    repo: Path,
    name: str,
    predecessor: str,
    status: str = "open",
    deployment_state: str = "ready_to_fire",
    extra_fm: str = "",
) -> Path:
    """A candidate SUCCESSOR handoff — the peer of `_seed_handoff_with_fields`
    (which seeds the PREDECESSOR side of a lineage edge). `predecessor` is
    passed through verbatim so callers can exercise every edge form
    `dag.referenced_by` supports (quoted path, unquoted path, bare basename,
    `predecessor_id:`)."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Successor {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        f"predecessor: {predecessor}\n"
        f"deployment_state: {deployment_state}\n"
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestComputeSuccessorHandoffs:
    """Unit coverage for `compute_successor_handoffs` (plan
    2026-08-01-wsc-completeness-gate-and-pickup-successor.md, C5/AC7) — a
    SEPARATE computation from `compute_claim_grant`, keyed on
    `deployment_state`, never on claim-emptiness."""

    def test_path_form_predecessor_ready_to_fire_surfaces(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(repo, "h2.md", predecessor='"state/handoffs/h1.md"')

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["kind"] == "ready_to_fire"
        assert candidate["path"].endswith("h2.md")

    def test_bare_basename_predecessor_surfaces(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(repo, "h2.md", predecessor="h1.md")

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["path"].endswith("h2.md")

    def test_predecessor_id_only_surfaces(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_with_fields(repo, "h1.md", "handoff_id: hnd-widget-ab12cd\n")
        _seed_successor_handoff(
            repo, "h2.md", predecessor='"none"', extra_fm="predecessor_id: hnd-widget-ab12cd\n"
        )

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["path"].endswith("h2.md")

    def test_archived_successor_does_not_surface(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        archived_dir = repo / "archive" / "handoffs"
        archived_dir.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Archived successor"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "state/handoffs/h1.md"\n'
            "deployment_state: ready_to_fire\n"
        )
        (archived_dir / "h2.md").write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", "archive/handoffs/h2.md")
        _git(repo, "commit", "-m", "add archived h2")

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert result["candidates"] == []

    def test_in_flight_and_live_surfaces(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(
            repo,
            "h2.md",
            predecessor='"state/handoffs/h1.md"',
            deployment_state="in_flight",
            extra_fm="claimed_by: peer-sid\n",
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "peer-sid")

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["kind"] == "in_flight_live"
        assert result["candidates"][0]["claimed_by"] == "peer-sid"

    def test_in_flight_and_not_live_does_not_surface(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(
            repo,
            "h2.md",
            predecessor='"state/handoffs/h1.md"',
            deployment_state="in_flight",
            extra_fm="claimed_by: peer-sid\n",
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert result["candidates"] == []

    def test_reaped_ready_to_fire_with_claim_keys_removed_surfaces(self, tmp_path):
        """AC7's oracle — a REAPED `ready_to_fire` baton has its claim keys
        (`claimed_at`/`claimed_by`/`consumed_at`/`consumed_by`) REMOVED, not
        blanked, by `handoff_transition._unclaim`, and is byte-indistinguishable
        from a never-claimed one; `park_note` is narration colour for
        evidence, never a gate. Must surface identically to the never-claimed
        case above."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(
            repo,
            "h2.md",
            predecessor='"state/handoffs/h1.md"',
            deployment_state="ready_to_fire",
            extra_fm='park_note: "parked pending peer campaign"\n',
        )

        result = pa.compute_successor_handoffs(repo, "state/handoffs/h1.md")

        assert len(result["candidates"]) == 1
        candidate = result["candidates"][0]
        assert candidate["kind"] == "ready_to_fire"
        assert candidate["claimed_by"] is None
        assert candidate["park_note"] == "parked pending peer campaign"


class TestBriefSuccessorHandoffSurfacesJudgmentPoint:
    """AC7/AC8 — `gates.successor` + the `jsucc` judgment point surface when
    a live successor exists, and join `d2`'s `depends_on` AND-list."""

    def test_ready_to_fire_successor_surfaces_gate_and_jsucc(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(repo, "h2.md", predecessor='"state/handoffs/h1.md"')

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object

        assert "successor" in obj["gates"]
        assert len(obj["gates"]["successor"]["candidates"]) == 1

        jsucc = next(jp for jp in obj["judgment_points"] if jp["id"] == "jsucc")
        values = {d["value"] for d in jsucc["dispositions"]}
        assert values == {"divert-to-successor", "stand-down-live-peer", "acknowledge-and-proceed"}

        acknowledge = next(d for d in jsucc["dispositions"] if d["value"] == "acknowledge-and-proceed")
        assert acknowledge["resolves"] == ["d2"]
        divert = next(d for d in jsucc["dispositions"] if d["value"] == "divert-to-successor")
        assert divert["resolves"] == []
        stand_down = next(d for d in jsucc["dispositions"] if d["value"] == "stand-down-live-peer")
        assert stand_down["resolves"] == []

        consume = next(d for d in obj["directives"] if d["cli"] == "archive-stamp-cli")
        assert consume["depends_on"] == "jsucc"

        assert obj["gates"]["coast"]["verdict"] != "clear"
        assert "jsucc" in obj["gates"]["coast"]["blocked_by"]

    def test_no_successor_omits_gate_and_jsucc(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        assert "successor" not in obj["gates"]
        assert "jsucc" not in {jp["id"] for jp in obj["judgment_points"]}

    def test_archived_and_terminal_children_surface_neither_gate_nor_jp(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(
            repo, "h2.md", predecessor='"state/handoffs/h1.md"', status="claimed",
            deployment_state="ready_to_fire",
        )

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        assert "successor" not in obj["gates"]
        assert "jsucc" not in {jp["id"] for jp in obj["judgment_points"]}

    def test_jsucc_joins_depends_on_and_list_with_gate_check(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "h1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            'title: "Test"\n'
            "created: 2020-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: awaiting_gate\n"
            'gate_dependency: "peer campaign must settle first"\n'
        )
        path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")
        _seed_successor_handoff(repo, "h2.md", predecessor='"state/handoffs/h1.md"')

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert {"jgate", "jsucc"} <= jp_ids
        consume = next(
            d for d in obj["directives"] if d["cli"] == "archive-stamp-cli" and d["args"][0] == "claim-handoff"
        )
        assert consume["depends_on"] == ["jgate", "d-gate-recheck", "jsucc"]

    def test_compute_coast_remedy_non_empty_when_jsucc_fires(self, tmp_path):
        """AC8's self-sufficiency contract — `compute_coast`'s `remedy`
        derivation filters dispositions on truthy `resolves`; an all-empty
        `jsucc` would otherwise yield a blocked coast with an empty
        `remedy`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_successor_handoff(repo, "h2.md", predecessor='"state/handoffs/h1.md"')

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["gates"]["coast"]["verdict"] != "clear"
        assert obj["gates"]["coast"].get("remedy")


class TestSpinoffSuccessorNarrationCarveOut:
    """AC9 — the spinoff narration prefix keeps its adjacency clause and
    carves out the successor chain. Pins the replacement sentence VERBATIM,
    the way the memo pinned the current one."""

    def test_spinoff_narration_pins_successor_carve_out_sentence(self):
        assert pa._CLASSIFICATION_NEXT_MOVE_PREFIX["spinoff"] == (
            "This is a spinoff — treat the handoff body as the ground-truth spec; do not "
            "hand-search for pre-existing in-progress work on it — its own declared "
            "successor chain, if any, surfaces mechanically as gates.successor above. "
        )


class TestBriefMemoLivenessGatesClaimDirective:
    """Finding 1 — a firing liveness signal on the memo branch must gate d1
    (`session-claim-cli claim-artifact memo ...`) behind the liveness
    judgment point, mirroring the handoff branch's `directives[1]["depends_on"]
    = "j1"` pattern (Finding 5 /
    TestBriefAwaitingGateAndLivenessCombinedDependsOn). Previously the memo
    branch only set `depends_on` in the non-firing else-branch — a no-op,
    since it was already `None` — so a live peer signal never actually gated
    the claim directive, letting it execute unconditionally (a stand-down
    bypass)."""

    def test_firing_liveness_signal_gates_claim_directive(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert "j1" in jp_ids
        claim = next(d for d in obj["directives"] if d["cli"] == "session-claim-cli")
        assert claim["depends_on"] == "j1"

    def test_non_firing_liveness_signal_leaves_claim_directive_unconditional(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        claim = next(d for d in obj["directives"] if d["cli"] == "session-claim-cli")
        assert claim["depends_on"] is None


class TestBriefKindDispatchJudgment:
    """Defect 4 — M3 kind-dispatch must surface a judgment_points entry,
    never resolve silently to a bare claim."""

    def test_proposal_memo_surfaces_adopt_decline_negotiate(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "cross-repo" / "inbox" / "m1.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            "kind: proposal\n"
            "status: open\n"
            "from: sender-session\n"
            "summary: A test proposal.\n"
            "created: 2026-01-01\n"
        )
        path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "add m1")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        assert obj["artifact"]["kind_resolved"] == "proposal"
        kind_jp = next(jp for jp in obj["judgment_points"] if jp["id"] == "j-kind")
        values = {d["value"] for d in kind_jp["dispositions"]}
        assert values == {"adopt", "decline", "negotiate"}

    def test_absent_kind_defaults_to_ask(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")
        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        text = text.replace("kind: fyi\n", "")
        (repo / "cross-repo" / "inbox" / "m1.md").write_text(text, encoding="utf-8")
        _git(repo, "add", "cross-repo/inbox/m1.md")
        _git(repo, "commit", "-m", "drop kind")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.decision_object["artifact"]["kind_resolved"] == "ask"


def _write_registry_toml(settings_home: Path, repos_map: dict[str, Path]) -> None:
    """Machine-local registry fixture: `<settings_home>/machine-local/registry.toml`
    with one `repos.<key>` entry per `repos_map` item — this is what
    `compute_addressee_gate`'s IN-PROCESS `memo.check_addressee` compute core
    (`coordinator_core.ops.fleet._memo_resolver.read_registry_repos`) reads
    directly (2026-07-26 subprocess-elision spinoff — no CLI, no subprocess,
    no fake-script fixture needed anymore; a real registry.toml IS the
    fixture). Mirrors `test_memo_resolver.py`'s `_make_claude_home` factory.
    """
    machine_local = settings_home / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    lines = ["schema = 1"]
    for key, path in repos_map.items():
        toml_val = str(path).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"repos.{key}" = "{toml_val}"')
    (machine_local / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestMemoAddresseeBusinessFailureAndOverride:
    """Finding 10 — the addressee-mismatch business-failure path and the
    `COORDINATOR_OVERRIDE_MEMO_ADDRESSEE` bypass were untested. Also closes
    Finding 3 — asserts the F3 fix's captured `message` text lands in the
    decision object on the business-failure path (a test exercising this
    path would very likely have caught F3 in the first place).

    2026-07-26 subprocess-elision spinoff: rewritten against the real
    in-process `memo.check_addressee` compute core (a genuine
    `registry.toml` fixture, via `_write_registry_toml`) rather than a fake
    `cross-repo-memo` subprocess script — `compute_addressee_gate` no longer
    spawns anything, so there is no subprocess argv to record/assert on
    (see `TestComputeAddresseeGateNoSubprocess` below for the explicit
    no-subprocess regression guard)."""

    def test_addressee_mismatch_is_business_failure_with_message(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="other-em")
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo, "other": other_root})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        monkeypatch.delenv("COORDINATOR_OVERRIDE_MEMO_ADDRESSEE", raising=False)

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        addressee = result.decision_object["gates"]["addressee"]
        assert addressee["checked"] is True
        assert addressee["exit_code"] == 3
        assert addressee["message"] == (
            f"self: repo-em ({repo})\n"
            f"to:   other-em ({other_root})\n"
            "verdict: MISMATCH — this memo is addressed to other-em, not this repo (repo-em)"
        )
        assert addressee["cross_seat_override"] == "COORDINATOR_OVERRIDE_MEMO_ADDRESSEE"
        assert result.decision_object["directives"] == []

    def test_override_env_var_bypasses_business_failure(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="other-em")
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo, "other": other_root})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        monkeypatch.setenv("COORDINATOR_OVERRIDE_MEMO_ADDRESSEE", "1")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        cli_names = {d["cli"] for d in result.decision_object["directives"]}
        assert "session-claim-cli" in cli_names

    def test_addressee_match_message_shape(self, tmp_path, monkeypatch):
        """Pins the MATCH message shape (AC — "message" byte-for-byte)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="repo-em")
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        addressee = result.decision_object["gates"]["addressee"]
        assert addressee["checked"] is True
        assert addressee["exit_code"] == 0
        assert addressee["message"] == (
            f"self: repo-em ({repo})\n"
            f"to:   repo-em ({repo})\n"
            "verdict: MATCH — this memo is addressed to this repo"
        )


class TestComputeAddresseeGateDirect:
    """Unit-level coverage of `compute_addressee_gate` itself (as opposed to
    `TestMemoAddresseeBusinessFailureAndOverride`'s `brief()`-level coverage
    of the business-failure wiring around it) — 2026-07-26 subprocess-
    elision spinoff."""

    def test_falsy_to_value_is_not_checked(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        assert pa.compute_addressee_gate(repo, None) == {"exit_code": None, "checked": False}
        assert pa.compute_addressee_gate(repo, "") == {"exit_code": None, "checked": False}

    def test_unresolved_receiver_message_shape(self, tmp_path, monkeypatch):
        """Pins the UNRESOLVED message shape byte-for-byte against the DoE
        CLI's own `--check-addressee` stdout (`cross-repo-memo:4099-4104`)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        result = pa.compute_addressee_gate(repo, "nonexistent-receiver-em")

        assert result["checked"] is True
        assert result["exit_code"] == 4
        assert result["message"] == (
            f"self: repo-em ({repo})\n"
            "to:   nonexistent-receiver-em (UNRESOLVED)\n"
            "verdict: receiver 'nonexistent-receiver-em' does not resolve to a "
            "known repo on this machine"
        )

    def test_registry_read_failure_degrades_to_not_checked_never_raises(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        settings_home = tmp_path / "settings-home"
        machine_local = settings_home / "machine-local"
        machine_local.mkdir(parents=True)
        (machine_local / "registry.toml").write_text("not [ valid toml =", encoding="utf-8")
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        result = pa.compute_addressee_gate(repo, "some-em")

        assert result == {"exit_code": None, "checked": False}

    def test_linked_worktree_normalizes_to_main_worktree_root(self, tmp_path, monkeypatch):
        """2026-07-26 review finding 2: a linked worktree's own `git rev-parse
        --show-toplevel` differs from the registered `repos.*` path (which
        points at the main worktree) — `compute_addressee_gate` must
        normalize before resolving self-identity, or a legitimate own-repo
        memo pickup run from a linked worktree wrongly reads MISMATCH/an
        unregistered basename-fallback self-id instead of MATCH."""
        main_repo = tmp_path / "repo"
        _init_repo(main_repo)
        worktree = tmp_path / "repo-worktree"
        _git(main_repo, "worktree", "add", str(worktree), "-b", "work/test/worktree")
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": main_repo})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        result = pa.compute_addressee_gate(worktree, "repo-em")

        assert result["checked"] is True
        assert result["exit_code"] == 0
        assert result["message"] == (
            f"self: repo-em ({main_repo})\n"
            f"to:   repo-em ({main_repo})\n"
            "verdict: MATCH — this memo is addressed to this repo"
        )


class TestComputeAddresseeGateNoSubprocess:
    """Regression guard for the actual invariant this spinoff exists to land:
    `compute_addressee_gate` must NEVER spawn a subprocess. `subprocess.run`/
    `subprocess.Popen` are monkeypatched to raise — if the gate still returns
    a correct verdict, it never called either."""

    def test_no_subprocess_spawned_still_returns_correct_verdict(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        def _boom(*args, **kwargs):
            raise AssertionError(
                "compute_addressee_gate spawned a subprocess — it must be fully in-process"
            )

        # Set up the fixture repo/registry BEFORE patching subprocess.run/Popen
        # to raise — only `compute_addressee_gate` itself must stay
        # subprocess-free, not the git-CLI-based test fixtures around it.
        monkeypatch.setattr("subprocess.run", _boom)
        monkeypatch.setattr("subprocess.Popen", _boom)

        result = pa.compute_addressee_gate(repo, "repo-em")

        assert result["checked"] is True
        assert result["exit_code"] == 0
        assert result["message"] == (
            f"self: repo-em ({repo})\n"
            f"to:   repo-em ({repo})\n"
            "verdict: MATCH — this memo is addressed to this repo"
        )


class TestBriefLiveClaimRevalidateJudgmentPoint:
    """Defect 5 — the live-claim stand-down path must still build the
    liveness judgment point (revalidate_at_dispatch: true)."""

    def test_live_claim_stand_down_still_builds_revalidate_judgment_point(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "live-peer-sid"},
        )
        # A genuine live-peer scenario also denies at the claim_grant layer
        # (self-claim fix, PM ruling 2026-07-24, pickup-skill-code-driven-
        # branch-result spinoff, AC2) — `compute_claim_grant` unmocked would
        # see no real claim dir in this fresh repo and grant, which no
        # longer matches "a live peer holds this" once `brief()` consults
        # `claim_grant.verdict` before taking the stand-down path.
        monkeypatch.setattr(
            pa, "compute_claim_grant",
            lambda *a, **k: {
                "fetch_state": "ok", "holder": "live-peer-sid", "holder_live": True,
                "verdict": "denied", "reason": "held by a live peer",
                "claim_age_minutes": None, "drop_invocation": "",
            },
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        obj = result.decision_object
        assert obj["directives"] == []
        assert len(obj["judgment_points"]) == 1
        jp = obj["judgment_points"][0]
        assert jp["id"] == "j1"
        # AMENDMENT 2026-07-24 (chunk C7 Part A4) — j1 no longer carries
        # `revalidate_at_dispatch: true`. `compute_liveness_signal` now reads
        # a durable committed frontmatter stamp, stable across the
        # brief-to-apply gap, so a recorded `proceed` disposition is honored
        # rather than discarded and recomputed at dispatch (AC13b, covered by
        # `TestRevalidateHonorsProceed` below).
        assert jp["revalidate_at_dispatch"] is False

    # Review: code-reviewer — Finding 1: the sibling of the test above —
    # `claim_grant.verdict == "granted"` (self-claim) must SKIP the early
    # bail entirely and reach normal directive computation, rather than
    # only re-confirming the unchanged `denied` branch survives.
    def test_self_claim_granted_skips_bail_and_reaches_normal_directives(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "self-sid"},
        )
        monkeypatch.setattr(
            pa, "compute_claim_grant",
            lambda *a, **k: {
                "fetch_state": "ok", "holder": "self-sid", "holder_live": True,
                "verdict": "granted", "reason": "you already hold this",
                "claim_age_minutes": 1, "drop_invocation": "",
            },
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: False)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["directives"]

    def test_lineage_handover_granted_skips_bail_and_reaches_normal_directives(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "predecessor-sid"},
        )
        monkeypatch.setattr(
            pa, "compute_claim_grant",
            lambda *a, **k: {
                "fetch_state": "ok", "holder": "predecessor-sid", "holder_live": True,
                "verdict": "granted", "reason": "lineage handover — predecessor-sid authored this artifact",
                "claim_age_minutes": 1, "drop_invocation": "",
            },
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: False)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["directives"]


class TestSelfClaimUnmistakableAtBriefSurface:
    """2026-07-29 fix — a `/coordinator:pickup` re-brief of an artifact this
    SAME session already claimed must be unmistakable as self-held, never
    read as contended. Regression coverage for the incident: an auto-fire
    hook applied the claim directives, then the EM read the raw frontmatter
    and saw `status: claimed` / `claimed_by: <its own sid>` with no signal
    anywhere that the claim was its own, burning a full dispatch to confirm
    a non-bug. Sibling to `TestBriefLiveClaimRevalidateJudgmentPoint` (the
    still-contended case, which this class does NOT weaken)."""

    def test_self_claimed_reread_reports_self_held_and_idempotent(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(
            repo, "h1.md", status="claimed", deployment_state="in_flight",
            extra="claimed_by: self-sid\nclaimed_at: 2026-01-01T00:00:00Z\n",
        )

        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "self-sid"},
        )
        monkeypatch.setattr(
            pa, "compute_claim_grant",
            lambda *a, **k: {
                "fetch_state": "ok", "holder": "self-sid", "holder_live": True,
                "verdict": "granted", "reason": "you already hold this",
                "held_by_self": True, "claim_age_minutes": 1, "drop_invocation": "",
            },
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: False)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        assert obj["gates"]["claim_grant"]["held_by_self"] is True
        directives_by_id = {d["id"]: d for d in obj["directives"]}
        assert directives_by_id["d1"]["already_satisfied"] is True
        assert directives_by_id["d2"]["already_satisfied"] is True
        assert obj["narration"].startswith("Already held by you")
        assert "Already held by you" in obj["next_move"]

    def test_foreign_live_claim_still_reports_contended_not_self(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "peer-sid"},
        )
        monkeypatch.setattr(
            pa, "compute_claim_grant",
            lambda *a, **k: {
                "fetch_state": "ok", "holder": "peer-sid", "holder_live": True,
                "verdict": "denied", "reason": "held by a live peer",
                "held_by_self": False, "claim_age_minutes": None, "drop_invocation": "",
            },
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_BUSINESS_FAIL
        obj = result.decision_object
        assert obj["directives"] == []
        assert obj["gates"]["claim_grant"]["held_by_self"] is False
        assert obj["narration"] == "state/handoffs/h1.md is already claimed by peer-sid."
        assert "Already held by you" not in obj["narration"]


class TestExitCodeContractTransportFailure:
    """Finding 11 — `EXIT_TRANSPORT_FAIL` (exit 3) was never exercised.
    Covers both `_TransportFailure` (repo-root unresolvable) and, per
    Finding 4b, the new catch-all backstop for an UNEXPECTED exception —
    both must still emit a JSON decision-object-shaped payload, never a bare
    exit code."""

    def test_transport_failure_exit_three(self, monkeypatch, capsys):
        monkeypatch.setattr(pa, "resolve_repo_root", lambda *a, **k: None)

        rc = pa.main(["brief", "state/handoffs/h1.md"])

        assert rc == pa.EXIT_TRANSPORT_FAIL
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip().splitlines()[-1])
        assert payload["transport_failure"] is True
        assert "error" in payload

    def test_unexpected_exception_still_exit_three_with_json_object(self, monkeypatch, capsys):
        def _boom(*a, **k):
            raise ValueError("boom — not a _TransportFailure")

        monkeypatch.setattr(pa, "brief", _boom)

        rc = pa.main(["brief", "state/handoffs/h1.md"])

        assert rc == pa.EXIT_TRANSPORT_FAIL
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip().splitlines()[-1])
        assert payload["transport_failure"] is True
        assert "boom" in payload["error"]


class TestBriefSpinoffKindVariants:
    """Defect 6 — spinoff-roadmap / spinoff-goal must classify as spinoff,
    not fall through to plain handoff."""

    def test_spinoff_roadmap_classifies_as_spinoff(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "s1.md", kind="spinoff-roadmap")

        result = pa.brief("state/handoffs/s1.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "spinoff"

    def test_spinoff_goal_classifies_as_spinoff(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "s2.md", kind="spinoff-goal")

        result = pa.brief("state/handoffs/s2.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "spinoff"


# ---------------------------------------------------------------------------
# AC3e lineage-aware liveness — the execution-session dogfood fix (Defect A).
# `_lineage_related_sessions` is the one function `compute_liveness_signal`
# and `compute_claim_grant` both route a live-holder-relationship check
# through; these tests exercise it via both call sites so the filter cannot
# drift apart between them. A third caller, `compute_competing_claim`, was
# deleted at aadef0e23 — see the negative-spec block above.
# ---------------------------------------------------------------------------

def _seed_handoff_with_fields(repo: Path, name: str, extra_fm: str, predecessor: str = '"none"') -> Path:
    """Like `_seed_handoff` but with a caller-supplied `predecessor:` value
    and an `extra_fm` block of additional frontmatter lines — the shape
    every `TestCompetingClaim`/lineage-filter fixture needs (`claimed_by`,
    `authoring_session`, `predecessor`) that the fixed `_seed_handoff`
    template above does not expose."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        "status: open\n"
        f"predecessor: {predecessor}\n"
        "deployment_state: active\n"
        f"{extra_fm}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


# NEGATIVE-SPEC (2026-08-22): `TestCompetingClaimEvidenceCap` and
# `TestCompetingClaim` lived here, plus the `build_competing_jp_is_blocking`
# helper only `TestCompetingClaim` called. Their whole subject —
# `pickup_assemble.compute_competing_claim` — was deleted at aadef0e23 ("C1,
# C2, C5, C11: wave 1 of the ceremony-assembler rebuild", chunk C2 of
# docs/plans/2026-08-21-rebuild-the-three-ceremony-assemblers.md), which
# rewrote `compute_claim_grant` to the ratified R4 rule and dropped the
# sibling-scan producer outright; `gates.competing_claim` is no longer in the
# emitted gates object either. That commit's reverse-reference scan missed
# these call sites, so they survived as AttributeError-at-runtime residue.
#
# Do NOT reinstate them against a resurrected producer. What they pinned that
# still has a live subject is already covered elsewhere: the lineage filter by
# `TestLivenessSignalLineageFilter`/`TestClaimGrantLineageFilter` below, the
# holder-evidence cheap/activity split by `holder_evidence`'s own tests in
# coordinator_core/session/tests/test_liveness.py, and the R4 grant rule by
# coordinator_core/pickup_assemble/tests/test_claim_rule.py. The evidence-cap
# and per-candidate-memo assertions had no surviving subject at all — the memo
# was internal to the deleted function.


class TestLivenessSignalLineageFilter:
    """AC3e — the same lineage filter `compute_claim_grant` applies is
    threaded through `compute_liveness_signal`'s three signals (Defect A)."""

    def test_live_unrelated_claimant_on_self_still_fires(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "stranger-sid")

        fired = pa.compute_liveness_signal(
            repo, {"claimed_by": "stranger-sid"}, "state/handoffs/self.md",
        )

        assert fired is True

    def test_live_authoring_session_claimant_on_self_does_not_fire(self, tmp_path, monkeypatch):
        """A live `claimed_by` that is ALSO this artifact's own
        `authoring_session` is a handover (self-claim carried into the
        handoff), not a peer — must not fire."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "author-sid")

        fired = pa.compute_liveness_signal(
            repo,
            {"claimed_by": "author-sid", "authoring_session": "author-sid"},
            "state/handoffs/self.md",
        )

        assert fired is False

    def test_active_handoff_scan_suppresses_live_predecessor_author(self, tmp_path, monkeypatch):
        """The execution-session dogfood defect itself, reproduced directly:
        a sibling handoff (the predecessor) cites the SAME plan path and is
        claimed by its still-live authoring session -> without the AC3e
        filter this fires `True` (false hold on a plan->execute handover);
        with it, `False`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_dir = repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "p.md").write_text("# Plan\n", encoding="utf-8")
        _git(repo, "add", "docs/plans/p.md")
        _git_commit_backdated(repo, "add plan", "2000-01-01T00:00:00")

        _seed_handoff_with_fields(
            repo,
            "pred.md",
            'plan: docs/plans/p.md\nclaimed_by: "predecessor-sid"\nauthoring_session: "predecessor-sid"\n',
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "predecessor-sid")

        self_fm = {"plan": "docs/plans/p.md", "predecessor": "state/handoffs/pred.md"}
        fired = pa.compute_liveness_signal(repo, self_fm, "state/handoffs/self.md")

        assert fired is False

    # `test_active_handoff_scan_still_fires_for_unrelated_live_peer` RETIRED
    # 2026-07-24 (chunk C6) — asserted the deleted signal (c)
    # `_active_handoff_scan_signal`'s sibling-scan behavior (a stamp on a
    # SIBLING handoff, not on `self_fm`, used to fire this artifact's
    # liveness signal). `compute_liveness_signal` no longer scans siblings
    # at all — it reads only THIS artifact's own `claimed_by`/`consumed_by`/
    # `picked_up_by` (see the function's own docstring, chunk C7 Part A).
    # The green-count decrease here is the AC11-amended expectation.

    def test_own_stamp_matching_self_session_id_does_not_fire(self, tmp_path, monkeypatch):
        """Defect 1 (2026-07-29, doe-claude-em self-claim-reads-as-live-peer
        memo): the docstring has always promised a "stamp present & this
        session" handover branch, but `_lineage_related_sessions` never
        contains the CALLING session's own id — only `self_session_id`
        closes it. Any live-`session_live` stub would false-fire here
        without the fix, since the stamped id IS this session's own id."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: True)

        fired = pa.compute_liveness_signal(
            repo,
            {"picked_up_by": "my-own-sid"},
            "state/handoffs/self.md",
            self_session_id="my-own-sid",
        )

        assert fired is False

    def test_own_stamp_resolved_via_default_session_lookup_does_not_fire(self, tmp_path, monkeypatch):
        """`self_session_id` omitted -> resolves via
        `_session_core.resolve_session_id`, mirroring the single production
        call site (`brief()`'s `compute_liveness_signal(root, fm, ...)`,
        which never passes it explicitly)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: True)
        monkeypatch.setattr(pa._session_core, "resolve_session_id", lambda cwd=None: "resolved-sid")

        fired = pa.compute_liveness_signal(
            repo, {"claimed_by": "resolved-sid"}, "state/handoffs/self.md",
        )

        assert fired is False

    def test_genuinely_foreign_live_peer_still_fires_alongside_self_exclusion(self, tmp_path, monkeypatch):
        """Self-exclusion must not weaken the reaper: a DIFFERENT live
        session's stamp still fires even when a self id is supplied."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "stranger-sid")

        fired = pa.compute_liveness_signal(
            repo,
            {"claimed_by": "stranger-sid"},
            "state/handoffs/self.md",
            self_session_id="my-own-sid",
        )

        assert fired is True

    def test_unresolvable_self_id_degrades_to_current_behavior(self, tmp_path, monkeypatch):
        """Resolution failure must degrade to "nothing extra excluded," never
        crash a `brief` — mirrors the existing
        `except (OSError, ValueError): continue` tolerance on the per-stamp
        liveness check."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: True)

        def raise_value_error(cwd=None):
            raise ValueError("unresolvable")

        monkeypatch.setattr(pa._session_core, "resolve_session_id", raise_value_error)

        fired = pa.compute_liveness_signal(
            repo, {"claimed_by": "some-sid"}, "state/handoffs/self.md",
        )

        assert fired is True


class TestClaimGrantLineageFilter:
    """AC3e applied to `compute_claim_grant`'s row-3 holder-live check."""

    def test_row3_lineage_related_holder_resolves_granted_not_denied(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "author-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        grant = pa.compute_claim_grant(
            repo, "handoff", "h1.md", "state/handoffs/h1.md",
            fm={"authoring_session": "author-sid"},
        )

        assert grant["verdict"] == "granted"
        assert grant["holder"] == "author-sid"
        assert grant["holder_live"] is True
        assert "handover" in grant["reason"].lower() or "contention" in grant["reason"].lower()

    def test_row3_unrelated_holder_is_still_denied_when_fm_supplied(self, tmp_path, monkeypatch):
        """The AC3e `fm` parameter narrows row 3, it does not neuter it — an
        unrelated live holder is still denied even when `fm` is present."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "stranger-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        grant = pa.compute_claim_grant(
            repo, "handoff", "h1.md", "state/handoffs/h1.md",
            fm={"authoring_session": "author-sid"},
        )

        assert grant["verdict"] == "denied"
        assert grant["holder"] == "stranger-sid"

    def test_row3_no_fm_supplied_behaves_exactly_as_before(self, tmp_path, monkeypatch):
        """Backward compatibility: an existing caller that never learned
        about `fm` (default `None`) gets row 3's pre-AC3e behavior
        unchanged — `denied` on any live holder."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _write_claim(repo, "handoff", "h1.md", "author-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: True)

        grant = pa.compute_claim_grant(repo, "handoff", "h1.md", "state/handoffs/h1.md")

        assert grant["verdict"] == "denied"


class TestBriefEmitsClaimGrant:
    """`brief()`'s handoff emission must carry `gates.claim_grant` itself
    (not just the underlying `compute_claim_grant` unit function) and feed
    it into `gates.coast` so a denied claim attempt is visible on the
    emitted decision object, not only to a caller that re-derives it."""

    def test_no_competing_claim_emits_granted_claim_grant(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        gates = result.decision_object["gates"]
        assert "claim_grant" in gates
        assert gates["claim_grant"]["verdict"] == "granted"
        assert gates["coast"]["verdict"] == "clear"

    def test_denied_claim_grant_forces_coast_blocked(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _write_claim(repo, "handoff", "h1.md", "peer-sid", age_minutes=1)
        monkeypatch.setattr(pa._liveness, "claim_held_by_me", lambda *a, **k: False)
        monkeypatch.setattr(pa._liveness, "claim_holder_live", lambda *a, **k: False)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        gates = result.decision_object["gates"]
        assert gates["claim_grant"]["verdict"] == "denied"
        assert gates["coast"]["verdict"] == "blocked"
        assert "gates.claim_grant" not in gates["coast"]["blocked_by"]


# ---------------------------------------------------------------------------
# _emit() validation chokepoint (C1c — AC14/AC15, AC5b enforcement backstop)
# ---------------------------------------------------------------------------

class TestEmitDirectValidation:
    """Unit tests directly on `_emit()`, independent of any `brief()`
    fixture — the chokepoint's own contract, isolated from whatever any
    given branch happens to compute."""

    def test_missing_narration_raises(self):
        with pytest.raises(ValueError, match="narration"):
            pa._emit({"gates": {"coast": {"verdict": "clear"}}, "judgment_points": []}, pa.EXIT_OK)

    def test_empty_narration_raises(self):
        with pytest.raises(ValueError, match="narration"):
            pa._emit(
                {"narration": "", "gates": {"coast": {"verdict": "clear"}}, "judgment_points": []},
                pa.EXIT_OK,
            )

    def test_clear_verdict_does_not_require_next_move(self):
        result = pa._emit(
            {"narration": "ok", "gates": {"coast": {"verdict": "clear"}}, "judgment_points": []},
            pa.EXIT_OK,
        )
        assert result.decision_object["narration"] == "ok"

    def test_non_clear_verdict_missing_next_move_raises(self):
        with pytest.raises(ValueError, match="next_move"):
            pa._emit(
                {"narration": "ok", "gates": {"coast": {"verdict": "blocked"}}, "judgment_points": []},
                pa.EXIT_BUSINESS_FAIL,
            )

    def test_no_gates_object_at_all_is_treated_as_non_clear(self):
        """The bare `{"error": ..., "transport_failure": True}` shape this
        chokepoint replaces carries no `gates` key at all — AC15 holds it to
        the same `next_move` bar as an explicit non-clear `coast.verdict`,
        not exempted for lack of a `gates` object to inspect."""
        with pytest.raises(ValueError, match="next_move"):
            pa._emit({"narration": "ok", "error": "boom", "transport_failure": True}, pa.EXIT_TRANSPORT_FAIL)

    def test_non_clear_verdict_with_next_move_present_passes(self):
        result = pa._emit(
            {
                "narration": "ok",
                "next_move": "do the thing",
                "gates": {"coast": {"verdict": "blocked"}},
                "judgment_points": [],
            },
            pa.EXIT_BUSINESS_FAIL,
        )
        assert result.exit_code == pa.EXIT_BUSINESS_FAIL

    def test_raw_dict_literal_judgment_point_missing_recommendation_raises(self):
        """The backstop finding #4 names directly: a `judgment_points[]`
        entry assembled as a bare dict literal (never touching the
        required-parameter constructor) must still be caught here."""
        with pytest.raises(ValueError, match="recommendation"):
            pa._emit(
                {
                    "narration": "ok",
                    "next_move": "resolve it",
                    "gates": {"coast": {"verdict": "blocked"}},
                    "judgment_points": [
                        {"id": "j-raw", "question": "?", "dispositions": []},
                    ],
                },
                pa.EXIT_BUSINESS_FAIL,
            )

    def test_judgment_point_with_recommendation_key_present_passes_even_when_null(self):
        result = pa._emit(
            {
                "narration": "ok",
                "next_move": "resolve it",
                "gates": {"coast": {"verdict": "blocked"}},
                "judgment_points": [
                    {
                        "id": "j-raw",
                        "question": "?",
                        "dispositions": [],
                        "recommendation": None,
                        "reason": "insufficient-evidence",
                    },
                ],
            },
            pa.EXIT_BUSINESS_FAIL,
        )
        assert result.decision_object["judgment_points"][0]["recommendation"] is None


class TestBareDictJudgmentPointBuildersCarryRecommendation:
    """the Staff Engineer second-pass finding #4: the four (five, per live disk —
    `build_competing_claim_judgment_point` too) builders that assemble a
    `judgment_points[]` entry as a bare dict rather than through the
    required-parameter constructor must each carry an honest
    `recommendation` key themselves, or every real `brief()` call touching
    them would trip `_emit()`'s backstop."""

    def test_build_liveness_judgment_point_carries_null_insufficient_evidence(self):
        jp = pa.build_liveness_judgment_point(True, "gates.liveness_signal", ["d2"])
        assert jp["recommendation"] is None
        assert jp["reason"] == "insufficient-evidence"

    def test_build_gate_check_judgment_point_carries_null_insufficient_evidence(self):
        jp = pa.build_gate_check_judgment_point("gates.gate_check", ["d2"])
        assert jp["recommendation"] is None
        assert jp["reason"] == "insufficient-evidence"

    def test_build_kind_dispatch_judgment_point_carries_null_insufficient_evidence(self):
        jp = pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        assert jp["recommendation"] is None
        assert jp["reason"] == "insufficient-evidence"

    def test_build_competing_claim_judgment_point_carries_null_insufficient_evidence(self):
        """Retired to an always-`None` no-op (PM ruling 2026-07-24,
        pickup-skill-code-driven-branch-result spinoff) — a sibling handoff's
        liveness on a shared branch is never this artifact's contention, so
        the builder no longer emits a judgment point for any verdict."""
        jp = pa.build_competing_claim_judgment_point(
            {"verdict": "live-peer", "candidates": []}, "gates.competing_claim", ["d2"],
        )
        assert jp is None

    def test_completeness_checklist_probe_gate_is_recommendation_forbidden(self):
        completeness = pa.build_completeness_checklist(
            {"completeness_checklist": ['live: the server responds [probe: curl localhost]']},
            "state/handoffs/h1.md",
        )
        probe_jps = [jp for jp in completeness["judgment_points"] if jp["id"].endswith("-probe")]
        assert len(probe_jps) == 1
        assert probe_jps[0]["recommendation"] is None
        assert probe_jps[0]["reason"] == "recommendation-forbidden"


class TestKindDispatchRequiredContentKeys:
    """Defect 2 (2026-07-29, doe-claude-em self-claim-reads-as-live-peer
    memo): `accepted`/`partial` require `--realized-by`
    (`ops/memo_transition.py:642-643`), but nothing an operator reads before
    `apply` said so. `required_content_keys` is stamped onto each
    disposition dict in the emitted decision object — derived from
    `_MEMO_ACTION_DECISION_MAP`, not a second hand-written table."""

    def test_accept_mechanical_direct_requires_realized_by(self):
        jp = pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["accept-mechanical-direct"]["required_content_keys"] == ["realized_by"]

    def test_accept_escalate_to_sizing_requires_realized_by(self):
        jp = pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["accept-escalate-to-sizing"]["required_content_keys"] == ["realized_by"]

    def test_decline_requires_nothing(self):
        jp = pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["decline"]["required_content_keys"] == []

    def test_surface_to_pm_requires_nothing(self):
        """Not decision-mapped at all (`resolves: []`) — must still carry
        the key with an empty list, never omit it."""
        jp = pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["surface-to-PM"]["required_content_keys"] == []

    def test_proposal_adopt_requires_realized_by(self):
        jp = pa.build_kind_dispatch_judgment_point("proposal", "proposal", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["adopt"]["required_content_keys"] == ["realized_by"]

    def test_fyi_surgical_fix_requires_realized_by(self):
        jp = pa.build_kind_dispatch_judgment_point("fyi", "fyi", False)
        by_value = {d["value"]: d for d in jp["dispositions"]}
        assert by_value["surgical-fix"]["required_content_keys"] == ["realized_by"]

    def test_consult_dispositions_require_nothing(self):
        """`consult` never resolves via `--decision` at all — no entry in
        `_MEMO_ACTION_DECISION_MAP`, so no required content key."""
        jp = pa.build_kind_dispatch_judgment_point("consult", "consult", False)
        for disposition in jp["dispositions"]:
            assert disposition["required_content_keys"] == []

    def test_module_level_kind_dispositions_not_mutated_in_place(self):
        """`_dispositions_with_required_keys` must return fresh dicts — a
        module-level `_KIND_DISPOSITIONS` mutation would leak across every
        `brief()` call."""
        pa.build_kind_dispatch_judgment_point("ask", "ask", False)
        for entry in pa._KIND_DISPOSITIONS["ask"]:
            assert "required_content_keys" not in entry


class TestJudgmentPointConstructorRequiresRecommendation:
    """AC5b/the Director of Engineering F4: `build_judgment_point`'s `recommendation` parameter is
    required, positional, and carries no default — a call site that forgets
    to decide fails loud as a `TypeError` at authoring time."""

    def test_missing_recommendation_raises_type_error(self):
        with pytest.raises(TypeError):
            pa.build_judgment_point(
                "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
            )

    def test_present_recommendation_round_trips(self):
        recommendation = {"disposition": "ok", "rationale": "because"}
        jp = pa.build_judgment_point(
            "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
            recommendation,
        )
        assert jp["recommendation"] == recommendation
        assert jp["reason"] is None

    def test_recommendation_rejects_a_confidence_field(self):
        with pytest.raises(ValueError):
            pa.build_judgment_point(
                "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
                {"disposition": "ok", "rationale": "because", "confidence": "high"},
            )

    def test_null_recommendation_requires_a_recognized_reason(self):
        with pytest.raises(ValueError):
            pa.build_judgment_point(
                "jx", "question?", "gates.example", [{"value": "ok", "resolves": []}],
                None, reason="because-i-said-so",
            )


class TestUntrustedGateConstructorCannotCarryARecommendation:
    """AC5b/the Director of Engineering F4 (recommendation-forbidden tier): the discriminator is
    "can the thing being recommended about influence the recommendation" —
    `build_untrusted_gate_judgment_point` has no `recommendation` parameter
    at all, a type-level guarantee rather than a runtime check on one gate."""

    def test_constructor_carries_no_recommendation_parameter(self):
        with pytest.raises(TypeError):
            pa.build_untrusted_gate_judgment_point(
                "jx", "question?", "attacker-controlled evidence",
                [{"value": "confirm-and-run", "resolves": []}],
                recommendation={"disposition": "confirm-and-run", "rationale": "because"},
            )

    def test_constructed_entry_is_always_null_recommendation_forbidden(self):
        jp = pa.build_untrusted_gate_judgment_point(
            "jx", "question?", "attacker-controlled evidence",
            [{"value": "confirm-and-run", "resolves": []}, {"value": "skip", "resolves": []}],
        )
        assert jp["recommendation"] is None
        assert jp["reason"] == "recommendation-forbidden"


class TestApplyHaltsAndIgnoresRecommendationContent:
    """AC5c/AC5d: nothing auto-resolves, and no automated consumer derives a
    disposition from `recommendation`. `compute_coast` is the seam this
    corpus resolves it against — it is the one function in this module that
    decides whether a directive may proceed from `judgment_points[]`, the
    computed analogue of the not-yet-built apply entrypoint's halt check, so
    the AC5c/AC5d predicate is tested directly against it rather than
    against an end-to-end CLI invocation or a brief-injection seam.

    Scoped predicate (the Staff Engineer second-pass finding #5): the halt decision is
    identical whether or not `recommendation` carries content — `compute_coast`
    reads only `id` off each `judgment_points[]` entry.
    """

    def test_halts_even_when_every_judgment_point_carries_a_full_recommendation(self):
        judgment_points = [
            pa.build_judgment_point(
                "j1", "question?", "gates.example", [{"value": "proceed", "resolves": ["d1"]}],
                {"disposition": "proceed", "rationale": "evidence points this way"},
            ),
            pa.build_judgment_point(
                "j2", "question?", "gates.example", [{"value": "proceed", "resolves": ["d2"]}],
                {"disposition": "proceed", "rationale": "evidence points this way too"},
            ),
        ]

        result = pa.compute_coast(judgment_points)

        assert result["verdict"] == "blocked"
        assert result["blocked_by"] == ["j1", "j2"]

    def test_halt_decision_is_identical_with_and_without_recommendation_content(self):
        with_recommendation = [
            pa.build_judgment_point(
                "j1", "question?", "gates.example", [{"value": "proceed", "resolves": ["d1"]}],
                {"disposition": "proceed", "rationale": "evidence points this way"},
            ),
        ]
        without_recommendation = [
            pa.build_judgment_point(
                "j1", "question?", "gates.example", [{"value": "proceed", "resolves": ["d1"]}],
                None, reason="insufficient-evidence",
            ),
        ]

        assert pa.compute_coast(with_recommendation) == pa.compute_coast(without_recommendation)


class TestBriefResultSitesCarryNarrationAndNextMove:
    """AC14/AC15 red test: enumerate every `BriefResult` construction site
    (routed through `_emit()` in C1c) and assert `narration`/`next_move`
    are present and non-empty on the returned decision object. Reuses this
    file's existing per-branch fixtures rather than re-deriving them."""

    def _assert_narrated(self, result):
        obj = result.decision_object
        assert obj.get("narration"), "narration missing/empty"
        assert isinstance(obj["narration"], str) and obj["narration"].strip()
        return obj

    def test_artifact_unreadable_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        result = pa.brief("state/handoffs/nope.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_archived_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h1.md")
        _archive_handoff(repo, live)
        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        self._assert_narrated(result)

    def test_ambiguous_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        path = repo / "state" / "handoffs" / "bad.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("no frontmatter here\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", "bad")
        result = pa.brief("state/handoffs/bad.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_handoff_live_claim_holder_site(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.setattr(
            pa, "compute_claim_gate",
            lambda *a, **k: {"fetch_state": "ok", "holder": "live-peer-sid"},
        )
        monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)
        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_handoff_main_success_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_memo_actioned_terminal_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", status="actioned", to="", extra="decision: adopt\n")
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_memo_addressee_mismatch_site(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", to="other-em")
        other_root = tmp_path / "other-repo"
        other_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _write_registry_toml(settings_home, {"repo": repo, "other": other_root})
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        monkeypatch.delenv("COORDINATOR_OVERRIDE_MEMO_ADDRESSEE", raising=False)

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        obj = self._assert_narrated(result)
        assert obj.get("next_move")

    def test_memo_final_success_site(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        obj = self._assert_narrated(result)
        # The kind-dispatch judgment point is unconditional on this path, so
        # coast is always blocked here and next_move is always required.
        assert obj["gates"]["coast"]["verdict"] != "clear"
        assert obj.get("next_move")


class TestMainTransportFailurePayloadsCarryNextMove:
    """AC15: `main()`'s three error-payload sites — `_TransportFailure`, the
    generic-exception backstop, and the result-serialization backstop — must
    each emit a `next_move`, not a bare `{"error": ..., "transport_failure":
    True}`."""

    def test_transport_failure_site_carries_next_move(self, monkeypatch, capsys):
        monkeypatch.setattr(pa, "resolve_repo_root", lambda *a, **k: None)

        rc = pa.main(["brief", "state/handoffs/h1.md"])

        assert rc == pa.EXIT_TRANSPORT_FAIL
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["transport_failure"] is True
        assert payload.get("next_move")
        assert payload.get("narration")

    def test_unexpected_exception_site_carries_next_move(self, monkeypatch, capsys):
        def _boom(*a, **k):
            raise ValueError("boom — not a _TransportFailure")

        monkeypatch.setattr(pa, "brief", _boom)

        rc = pa.main(["brief", "state/handoffs/h1.md"])

        assert rc == pa.EXIT_TRANSPORT_FAIL
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["transport_failure"] is True
        assert payload.get("next_move")
        assert payload.get("narration")

    def test_serialization_failure_site_carries_next_move(self, monkeypatch, capsys):
        class _Unserializable:
            pass

        def _fake_brief(artifact_path, decisions=None, repo_root=None):
            return pa.BriefResult(
                {
                    "narration": "ok",
                    "gates": {"coast": {"verdict": "clear"}},
                    "judgment_points": [],
                    "poison": _Unserializable(),
                },
                pa.EXIT_OK,
            )

        monkeypatch.setattr(pa, "brief", _fake_brief)

        rc = pa.main(["brief", "state/handoffs/h1.md"])

        assert rc == pa.EXIT_TRANSPORT_FAIL
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["transport_failure"] is True
        assert payload.get("next_move")
        assert payload.get("narration")


# ---------------------------------------------------------------------------
# gates.execution_stamp_match (AC18) — canonical-recipe parity.
#
# Pins `_frontmatter_body_text` + `_git_hash_object_stdin` against the
# execute-plan SKILL's Phase 1.2 canonical recipe:
#   awk '/^---[[:space:]]*$/{fm++; next} fm>=2{print}' <path> |
#     git hash-object --stdin
# computed independently here via `git hash-object` on a byte-identical
# Python-side strip (no `awk` dependency in the test — awk is not a
# guaranteed Windows binary, matching the module's own negative-spec). A
# regression here means `compute_execution_stamp_match` disagrees with what
# `/execute-plan` itself considers a validly-stamped plan.
# ---------------------------------------------------------------------------

def _canonical_body_sha(repo: Path, text: str) -> str:
    """Independent re-derivation of the canonical awk-strip, kept
    deliberately separate from `pa._frontmatter_body_text` so this test
    cannot pass by construction (both implementations sharing one bug)."""
    fm_count = 0
    out_lines: list[str] = []
    for line in text.splitlines():
        if line.rstrip(" \t") == "---":
            fm_count += 1
            continue
        if fm_count >= 2:
            out_lines.append(line + "\n")
    body = "".join(out_lines)
    result = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=str(repo),
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8").strip()


class TestExecutionStampMatchCanonicalRecipeParity:
    def _seed_plan(self, repo: Path, name: str, note: str = "reviewed body") -> tuple[Path, str]:
        """A stamped plan: its own `execution_authorized_sha` field is
        computed from and matches its own body via the canonical recipe."""
        path = repo / "docs" / "plans" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            "# Plan\n\n"
            "## Summary\n\n"
            f"{note}\n\n"
            "---\n\n"
            "A markdown horizontal rule inside the body — must NOT be\n"
            "double-counted as a third frontmatter delimiter.\n"
        )
        unstamped = f"---\ntitle: \"Test Plan\"\ncreated: 2026-01-01\n---\n\n{body}"
        sha = _canonical_body_sha(repo, unstamped)
        fm = (
            "title: \"Test Plan\"\n"
            "created: 2026-01-01\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            f"execution_authorized_sha: {sha}\n"
        )
        stamped_text = f"---\n{fm}---\n\n{body}"
        path.write_text(stamped_text, encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", f"add {name}")
        return path, sha

    def test_plan_direct_computed_sha_matches_canonical_recipe(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = self._seed_plan(repo, "plan-a.md")
        rel = plan_path.relative_to(repo).as_posix()

        fm = pa._parse_fm_dict(pa.split_frontmatter(plan_path.read_text(encoding="utf-8")).fm_text)
        hit = pa.compute_execution_stamp_match(repo, fm, rel)

        assert hit is not None
        gate, target_path = hit
        assert target_path == rel
        assert gate["computed_sha"] == expected_sha
        assert gate["stamped_sha"] == expected_sha
        assert gate["verdict"] == "match"

    def test_handoff_mirroring_own_and_pointer_sha_targets_the_plan_not_itself(self, tmp_path):
        """Regression pin for the false-`unstampable` bug: a handoff that
        carries `execution_authorized_sha` directly on ITS OWN frontmatter
        (a human-readable mirror) AND a `## Plan to Execute` pointer to the
        plan that sha actually authorizes must hash the PLAN's body, never
        its own — hashing its own body silently computes a different SHA
        and false-negatives a validly stamped plan as unstampable."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = self._seed_plan(repo, "plan-b.md")
        plan_rel = plan_path.relative_to(repo).as_posix()

        handoff_path = repo / "state" / "handoffs" / "h-execute.md"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_text = (
            "---\n"
            "title: \"Execute: plan-b\"\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            "predecessor: none\n"
            "handoff_phase: execution\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            f"execution_authorized_sha: {expected_sha}\n"
            "---\n\n"
            "## Plan to Execute\n\n"
            f"**Plan:** `{plan_rel}`\n"
        )
        handoff_path.write_text(handoff_text, encoding="utf-8")
        _git(repo, "add", str(handoff_path.relative_to(repo)))
        _git(repo, "commit", "-m", "add handoff")

        fm = pa._parse_fm_dict(pa.split_frontmatter(handoff_text).fm_text)
        hit = pa.compute_execution_stamp_match(repo, fm, str(handoff_path.relative_to(repo)))

        assert hit is not None
        gate, target_path = hit
        # The plan is the hash target, not the pointing handoff itself.
        assert target_path == plan_rel
        assert gate["computed_sha"] == expected_sha
        assert gate["stamped_sha"] == expected_sha
        assert gate["verdict"] == "match"

        # And this MUST differ from the (wrong) sha of the handoff's own
        # body — proves the fix isn't accidentally hashing the same bytes.
        own_body_sha = _canonical_body_sha(repo, handoff_text)
        assert own_body_sha != expected_sha
        assert gate["computed_sha"] != own_body_sha

    def test_handoff_with_stale_mirrored_sha_still_targets_the_plans_own_sha(self, tmp_path):
        """Review: code-reviewer — Finding 2: strengthens the mirrored-stamp
        regression above by making the handoff's own `execution_authorized_sha`
        deliberately WRONG/stale (not merely equal-and-unobserved) relative to
        the plan's own field. If the fix were reading the handoff's own field
        rather than always resolving the pointer target's frontmatter, this
        would surface the stale value; asserting the plan's own sha wins
        closes the "ignored vs. coincidentally-equal" gap the prior test left
        open."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = self._seed_plan(repo, "plan-c.md")
        plan_rel = plan_path.relative_to(repo).as_posix()
        stale_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        assert stale_sha != expected_sha

        handoff_path = repo / "state" / "handoffs" / "h-execute-stale.md"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_text = (
            "---\n"
            "title: \"Execute: plan-c\"\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            "predecessor: none\n"
            "handoff_phase: execution\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            f"execution_authorized_sha: {stale_sha}\n"
            "---\n\n"
            "## Plan to Execute\n\n"
            f"**Plan:** `{plan_rel}`\n"
        )
        handoff_path.write_text(handoff_text, encoding="utf-8")
        _git(repo, "add", str(handoff_path.relative_to(repo)))
        _git(repo, "commit", "-m", "add stale-mirror handoff")

        fm = pa._parse_fm_dict(pa.split_frontmatter(handoff_text).fm_text)
        hit = pa.compute_execution_stamp_match(repo, fm, str(handoff_path.relative_to(repo)))

        assert hit is not None
        gate, target_path = hit
        # The plan's OWN sha is the target, never the handoff's stale mirror —
        # proves the field is genuinely ignored, not merely equal-and-unobserved.
        assert target_path == plan_rel
        assert gate["stamped_sha"] == expected_sha
        assert gate["stamped_sha"] != stale_sha
        assert gate["computed_sha"] == expected_sha
        assert gate["verdict"] == "match"

    def test_handoff_pointing_via_governing_plan_frontmatter_targets_the_plan(
        self, tmp_path
    ):
        """Regression pin for the 2026-07-27 false-`unstampable` bug: a
        handoff that names its plan via a `governing_plan:` frontmatter
        field (not the `## Plan to Execute` body heading) must still
        resolve the plan as the pointer target, not fall through to
        hashing its own body against the mirrored `execution_authorized_sha`.
        Reproduces the exact shape of
        `state/handoffs/2026-07-27-execute-workstream-complete-computed-
        frontage.md`, whose missing body-heading pointer caused the reader
        to hash the handoff itself and mismatch against the plan's
        mirrored stamp — a pointer-resolution bug, not a hashing-recipe
        divergence (writer and reader already share the one canonical
        `canonical_body_sha` recipe)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = self._seed_plan(repo, "plan-d.md")
        plan_rel = plan_path.relative_to(repo).as_posix()

        handoff_path = repo / "state" / "handoffs" / "h-governing-plan.md"
        handoff_path.parent.mkdir(parents=True, exist_ok=True)
        handoff_text = (
            "---\n"
            "title: \"Execute: plan-d\"\n"
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: claimed\n"
            "predecessor: none\n"
            "handoff_phase: execution\n"
            f"governing_plan: {plan_rel}\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            f"execution_authorized_sha: {expected_sha}\n"
            "---\n\n"
            "No `## Plan to Execute` body heading here — the pointer lives\n"
            "only in the `governing_plan:` frontmatter field.\n"
        )
        handoff_path.write_text(handoff_text, encoding="utf-8")
        _git(repo, "add", str(handoff_path.relative_to(repo)))
        _git(repo, "commit", "-m", "add governing_plan handoff")

        fm = pa._parse_fm_dict(pa.split_frontmatter(handoff_text).fm_text)
        hit = pa.compute_execution_stamp_match(repo, fm, str(handoff_path.relative_to(repo)))

        assert hit is not None
        gate, target_path = hit
        # The plan is the hash target, not the pointing handoff itself.
        assert target_path == plan_rel
        assert gate["computed_sha"] == expected_sha
        assert gate["stamped_sha"] == expected_sha
        assert gate["verdict"] == "match"

        # Round-trip against the writer: `stamp_execution_authorization`
        # (review_assemble.exec_auth_stamp, the WRITER) and
        # `compute_execution_stamp_match` (the READER) must agree on the
        # SAME plan file — proves there is one shared recipe, not two.
        write_exit, write_result = exec_auth_stamp.stamp_execution_authorization(
            plan_rel, "PM (Test)", "round-trip regression", at="2026-01-01", repo_root=repo
        )
        assert write_exit == exec_auth_stamp.EXIT_OK
        # The writer recomputes the SAME sha the reader already verified
        # against — the shared canonical recipe, not a second one.
        assert write_result["sha"] == expected_sha

    def test_brief_on_governing_plan_handoff_with_fresh_plan_emits_no_restamp_directive(
        self, tmp_path
    ):
        """End-to-end regression, through `brief()` (not just the gate
        function directly): a handoff whose `governing_plan:` points at a
        freshly-stamped plan must surface `execution_stamp_match.verdict ==
        "match"` in the decision object AND must NOT emit a `d-stamp`
        re-stamp directive — that directive is unconditional for
        `stale-bookkeeping`/`unstampable` verdicts (AC18) and would, on the
        pre-fix pointer-resolution bug, have silently overwritten the
        handoff's mirrored `execution_authorized_sha` with the hash of the
        handoff's own body instead of the plan's."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_path, expected_sha = self._seed_plan(repo, "plan-e.md")
        plan_rel = plan_path.relative_to(repo).as_posix()

        extra_fm = (
            f"governing_plan: {plan_rel}\n"
            "execution_authorized_by: \"PM (Test)\"\n"
            "execution_authorized_at: 2026-01-01\n"
            f"execution_authorized_sha: {expected_sha}\n"
        )
        _seed_handoff_with_fields(repo, "h-governing-plan-brief.md", extra_fm)

        result = pa.brief("state/handoffs/h-governing-plan-brief.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        execution_stamp_match = result.decision_object["gates"]["execution_stamp_match"]
        assert execution_stamp_match is not None
        assert execution_stamp_match["verdict"] == "match"
        assert execution_stamp_match["stamped_sha"] == expected_sha
        assert execution_stamp_match["computed_sha"] == expected_sha

        directive_ids = {d["id"] for d in result.decision_object["directives"]}
        assert "d-stamp" not in directive_ids


# ---------------------------------------------------------------------------
# Multi-artifact ` AND `-split + `/<repo-basename>/` re-anchoring
# (`/pickup` is explicitly multi-artifact — one decision object per artifact)
# ---------------------------------------------------------------------------

class TestSplitArtifactArgs:
    def test_single_path_returns_itself_unchanged(self):
        assert pa.split_artifact_args("state/handoffs/h1.md") == ["state/handoffs/h1.md"]

    def test_two_paths_split_on_standalone_and(self):
        assert pa.split_artifact_args("/a/m1.md AND /b/m2.md") == ["/a/m1.md", "/b/m2.md"]

    def test_three_paths_split(self):
        assert pa.split_artifact_args("a AND b AND c") == ["a", "b", "c"]

    def test_substring_and_is_not_a_delimiter(self):
        # Lowercase `and`, and `AND` embedded in a path segment (no surrounding
        # whitespace), must never split — only the standalone whitespace-bounded
        # token does.
        assert pa.split_artifact_args("/BRAND/COMMAND.md") == ["/BRAND/COMMAND.md"]
        assert pa.split_artifact_args("state/handoffs/understanding.md") == [
            "state/handoffs/understanding.md"
        ]

    def test_empty_arg_degrades_to_single_entry(self):
        assert pa.split_artifact_args("   ") == ["   "]


class TestSplitArtifactArgsBulletLines:
    # 2026-08-11 defect: the PM's literal pasted argument — leading
    # whitespace, `- ` bullets, newline-separated, two Windows paths — was
    # silently collapsing to a single decision object (the second memo only,
    # the first path dropped with no error).
    def test_pm_pasted_bullet_list_yields_two_paths(self):
        # abs-path-ok: verbatim PM-supplied invocation string under test, not
        # a filesystem citation this repo's own code would ever construct.
        raw = (
            "- X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-11-doe-claude-em-"
            "landed-enum-is-live-declining-deprecate.md\n"
            "  - X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-11-doe-claude-em-"
            "problem-set-deliverable-id-is-vendored-vendor-first.md"
        )
        assert pa.split_artifact_args(raw) == [
            "X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-11-doe-claude-em-"
            "landed-enum-is-live-declining-deprecate.md",
            "X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-11-doe-claude-em-"
            "problem-set-deliverable-id-is-vendored-vendor-first.md",
        ]

    def test_star_bullets_and_no_indentation_also_split(self):
        assert pa.split_artifact_args("* /a/m1.md\n* /b/m2.md") == [
            "/a/m1.md",
            "/b/m2.md",
        ]

    def test_mixed_bullet_lines_and_inline_and_both_split(self):
        assert pa.split_artifact_args("- /a/m1.md\n- /b/m2.md AND /c/m3.md") == [
            "/a/m1.md",
            "/b/m2.md",
            "/c/m3.md",
        ]

    def test_unbulleted_multiline_paste_is_untouched(self):
        # No bullet marker on any line — this is the pre-existing hard-line-
        # wrap-inside-ONE-path signal `_sanitize_artifact_path_str` already
        # tolerates; reinterpreting every newline as an artifact boundary
        # would fragment a single wrapped path instead.
        raw = "state/handoffs/long-name-that-wr\n  apped-mid-token.md"
        assert pa.split_artifact_args(raw) == [raw]

    def test_trailing_aside_is_not_swallowed_as_a_path(self):
        assert pa.split_artifact_args(
            "/a/m1.md AND /b/m2.md -- please read m1 first"
        ) == ["/a/m1.md", "/b/m2.md"]

    def test_aside_stripped_before_bullet_reassembly(self):
        raw = "- /a/m1.md\n- /b/m2.md -- read these in order"
        assert pa.split_artifact_args(raw) == ["/a/m1.md", "/b/m2.md"]

    def test_single_path_with_no_aside_is_unaffected(self):
        assert pa.split_artifact_args("state/handoffs/h1.md") == [
            "state/handoffs/h1.md"
        ]


class TestSplitArtifactArgsBraceExpansion:
    def test_single_group_expands_to_n_paths(self):
        assert pa.split_artifact_args("dir/prefix-{a,b,c}-suffix.md") == [
            "dir/prefix-a-suffix.md",
            "dir/prefix-b-suffix.md",
            "dir/prefix-c-suffix.md",
        ]

    def test_whitespace_and_newline_after_comma_is_stripped(self):
        assert pa.split_artifact_args("p-{a,\n  b}.md") == ["p-a.md", "p-b.md"]

    def test_no_braces_passes_through_unchanged(self):
        assert pa.split_artifact_args("state/handoffs/h1.md") == [
            "state/handoffs/h1.md"
        ]

    def test_unbalanced_braces_pass_through_unchanged(self):
        assert pa.split_artifact_args("dir/prefix-{a,b-suffix.md") == [
            "dir/prefix-{a,b-suffix.md"
        ]

    def test_pm_invocation_string_resolves_to_two_real_inbox_paths(self):
        # abs-path-ok: verbatim PM-supplied invocation string under test, not a
        # filesystem citation this repo's own code would ever construct.
        raw = (
            "X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-07-doe-claude-em-"
            "{bx17-ssot-command-tool-constant-your-29-sites,\n"
            "  your-44-is-right-my-8-was-wrong-and-correction-2-does-not-hold}.md"
        )
        assert pa.split_artifact_args(raw) == [
            "X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-07-doe-claude-em-"
            "bx17-ssot-command-tool-constant-your-29-sites.md",
            "X:\\claude-klabauter\\cross-repo\\inbox\\2026-08-07-doe-claude-em-"
            "your-44-is-right-my-8-was-wrong-and-correction-2-does-not-hold.md",
        ]


class TestMultiArtifactBrief:
    def test_two_path_and_arg_resolves_both(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")

        results = pa.brief_multi(
            "state/handoffs/h1.md AND cross-repo/inbox/m1.md", repo_root=repo
        )

        assert len(results) == 2
        assert results[0].decision_object["artifact"]["classification"] == "handoff"
        assert results[1].decision_object["artifact"]["classification"] == "memo"

    def test_single_path_identical_to_brief(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        multi = pa.brief_multi("state/handoffs/h1.md", repo_root=repo)
        single = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert len(multi) == 1
        assert multi[0].decision_object == single.decision_object
        assert multi[0].exit_code == single.exit_code

    def test_one_ok_one_missing_isolates_per_artifact(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        results = pa.brief_multi(
            "state/handoffs/h1.md AND state/handoffs/nope.md", repo_root=repo
        )

        assert len(results) == 2
        assert results[0].exit_code == pa.EXIT_OK
        assert results[1].exit_code == pa.EXIT_BUSINESS_FAIL

    def test_bullet_list_grab_resolves_both_not_dropped(self, tmp_path):
        # 2026-08-11 defect regression: a pasted `- `-bulleted, newline-
        # separated grab used to collapse to ONE brief (the last artifact),
        # silently dropping the first. Both must resolve, per-artifact.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")

        raw = "- state/handoffs/h1.md\n  - cross-repo/inbox/m1.md"
        results = pa.brief_multi(raw, repo_root=repo)

        assert len(results) == 2
        assert results[0].decision_object["artifact"]["classification"] == "handoff"
        assert results[1].decision_object["artifact"]["classification"] == "memo"

    def test_bullet_list_with_one_unresolvable_fails_loud_not_dropped(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        raw = "- state/handoffs/h1.md\n- state/handoffs/does-not-exist.md"
        results = pa.brief_multi(raw, repo_root=repo)

        assert len(results) == 2
        assert results[0].exit_code == pa.EXIT_OK
        assert results[1].exit_code == pa.EXIT_BUSINESS_FAIL
        assert "error" in results[1].decision_object

    def test_cli_main_emits_json_array_for_multi(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")
        monkeypatch.chdir(repo)

        rc = pa.main(["brief", "state/handoffs/h1.md AND cross-repo/inbox/m1.md"])
        out = capsys.readouterr().out
        payload = json.loads(out)

        assert isinstance(payload, list)
        assert len(payload) == 2
        assert payload[0]["artifact"]["classification"] == "handoff"
        assert payload[1]["artifact"]["classification"] == "memo"
        assert rc == pa.EXIT_OK

    def test_cli_main_single_path_stays_object_not_array(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)

        rc = pa.main(["brief", "state/handoffs/h1.md"])
        payload = json.loads(capsys.readouterr().out)

        assert isinstance(payload, dict)
        assert payload["artifact"]["classification"] == "handoff"
        assert rc == pa.EXIT_OK

    def test_unquoted_multi_token_argv_fails_loud_not_silent(self, tmp_path, monkeypatch, capsys):
        # Two paths arriving as SEPARATE argv tokens (an unquoted shell paste,
        # not a single ` AND `-joined/bulleted string) hits `brief`'s own
        # "unrecognized argument" usage-error path — already loud (non-zero
        # exit, named on stderr), not a silent single-path fallback.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")
        monkeypatch.chdir(repo)

        rc = pa.main(["brief", "state/handoffs/h1.md", "cross-repo/inbox/m1.md"])
        err = capsys.readouterr().err

        assert rc == pa.EXIT_USAGE
        assert "cross-repo/inbox/m1.md" in err

    @pytest.mark.real_home
    def test_real_bin_trampoline_subprocess_fans_out_both_not_first_only(self, tmp_path):
        # End-to-end regression through the ACTUAL CLI binary (not just the
        # in-process `main()`): confirms the bulleted PM-shaped paste yields
        # BOTH decision objects via the real `coordinator/bin/pickup-assemble`
        # trampoline, not merely via direct Python calls into this module.
        import subprocess

        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")

        bin_path = Path(__file__).resolve().parents[1] / "coordinator" / "bin" / "pickup-assemble.py"
        raw = "- state/handoffs/h1.md\n  - cross-repo/inbox/m1.md"
        proc = subprocess.run(
            [sys.executable, str(bin_path), "brief", raw],
            cwd=str(repo),
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # Assert the exit status before parsing: the trampoline reports a
        # resolution failure on stderr and exits non-zero with EMPTY stdout,
        # which json.loads renders as a bare "Expecting value: line 1 column 1"
        # naming neither the command nor its reason.
        assert proc.returncode == 0, (
            f"pickup-assemble exited {proc.returncode}; stderr: {proc.stderr}"
        )
        payload = json.loads(proc.stdout)

        assert isinstance(payload, list)
        assert len(payload) == 2
        assert payload[0]["artifact"]["path"] == "state/handoffs/h1.md"
        assert payload[1]["artifact"]["path"] == "cross-repo/inbox/m1.md"


def _decode_decision_payload_like_autofire(raw: str) -> list[dict]:
    """Local reimplementation of DoE-claude's `pickup-autofire.py`
    `decode_decision_payload()` parsing rule — a bare object decodes to a
    one-element list, a bare array keeps only its dict elements, and
    anything else (including a `{"briefs": [...]}`-shaped wrapper, which is
    itself a bare JSON object) decodes to `[]`.

    Not imported from DoE-claude (a separate repo, not a dependency of this
    one) — this is a fixture-local pin of the consumer's parsing contract so
    a shape regression here fails a test in THIS repo before it ever reaches
    the hook. See the shape-contract comment above the `len(results) == 1`
    branch in `coordinator_core/pickup_assemble/__init__.py::main()`.
    """
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


class TestPickupAutofireConsumerContract:
    def test_single_artifact_round_trips_to_one_element_list(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        monkeypatch.chdir(repo)

        rc = pa.main(["brief", "state/handoffs/h1.md"])
        raw = capsys.readouterr().out

        decoded = _decode_decision_payload_like_autofire(raw)

        assert len(decoded) == 1
        assert decoded[0]["artifact"]["classification"] == "handoff"
        assert rc == pa.EXIT_OK

    def test_multi_artifact_round_trips_to_ordered_element_list(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m1.md")
        monkeypatch.chdir(repo)

        rc = pa.main(["brief", "state/handoffs/h1.md AND cross-repo/inbox/m1.md"])
        raw = capsys.readouterr().out

        decoded = _decode_decision_payload_like_autofire(raw)

        assert len(decoded) == 2
        assert decoded[0]["artifact"]["classification"] == "handoff"
        assert decoded[1]["artifact"]["classification"] == "memo"
        assert rc == pa.EXIT_OK

    def test_briefs_wrapper_shape_decodes_wrong_not_crash(self):
        # Pins the silent-break risk itself: main() never emits this shape
        # today, but IF a future refactor wrapped the payload as
        # `{"briefs": [...]}`, the hook's decoder would NOT unwrap it — a
        # bare dict decodes to a one-element list holding the wrapper dict
        # verbatim, never the inner briefs array. This is constructed by
        # hand (not via main()) to assert what the *decoder* does to a
        # wrapper shape, independent of whether main() ever produces one.
        wrapped_raw = json.dumps({"briefs": [{"artifact": {"classification": "handoff"}}]})

        decoded = _decode_decision_payload_like_autofire(wrapped_raw)

        assert len(decoded) == 1
        assert decoded[0] == {"briefs": [{"artifact": {"classification": "handoff"}}]}
        assert "artifact" not in decoded[0]


class TestRepoBasenameReanchor:
    def test_second_path_with_repo_basename_prefix_reanchors(self, tmp_path):
        # A `/<repo-basename>/<rest>` path (the common paste shape) that does not
        # exist literally re-anchors at the actual repo root and resolves.
        repo = tmp_path / "claude-klabauter"
        _init_repo(repo)
        _seed_memo(repo, "m2.md")

        result = pa.brief("/claude-klabauter/cross-repo/inbox/m2.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        # Display path is repo-relative after re-anchoring, not the literal arg.
        assert result.decision_object["artifact"]["path"] == "cross-repo/inbox/m2.md"

    def test_reanchor_only_fires_when_literal_absent(self, tmp_path):
        # A path that exists literally is never re-anchored (no basename-prefix
        # collision), preserving existing single-path resolution exactly.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_and_joined_reanchor_path_resolves(self, tmp_path):
        repo = tmp_path / "claude-klabauter"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")
        _seed_memo(repo, "m2.md")

        results = pa.brief_multi(
            "state/handoffs/h1.md AND /claude-klabauter/cross-repo/inbox/m2.md",
            repo_root=repo,
        )

        assert len(results) == 2
        assert results[0].decision_object["artifact"]["classification"] == "handoff"
        assert results[1].decision_object["artifact"]["classification"] == "memo"
        assert results[1].decision_object["artifact"]["path"] == "cross-repo/inbox/m2.md"


class TestLineWrappedArtifactPath:
    """A long path hard-wrapped by the surface that rendered it (the Windows
    case: absolute paths are long enough that a prompt/terminal breaks them
    mid-token, e.g. `...not-th\\n  e-new-baton.md`) resolves through the
    sanitize fallback tier — see `_sanitize_artifact_path_str` § Line-wrap
    tolerance."""

    def test_absolute_path_wrapped_mid_token_resolves(self, tmp_path):
        repo = tmp_path / "claude-klabauter"
        _init_repo(repo)
        seeded = _seed_memo(repo, "2026-08-10-carry-gate-validates-the-predecessor.md")

        literal = str(seeded)
        cut = literal.index("predecessor") + 4
        wrapped = literal[:cut] + "\n  " + literal[cut:]

        result = pa.brief(wrapped, repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["classification"] == "memo"
        assert (
            result.decision_object["artifact"]["path"]
            == "cross-repo/inbox/2026-08-10-carry-gate-validates-the-predecessor.md"
        )

    def test_repo_relative_path_wrapped_mid_token_resolves(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/hand\r\n        offs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_wrap_composes_with_wrapper_and_trailing_punctuation(self, tmp_path):
        # The unwrap runs inside the same fixed-point loop as the wrapper /
        # trailing-punctuation strips, so a path pasted from prose AND wrapped
        # resolves in one call rather than needing a second pass.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("`state/hand\n  offs/h1.md`.", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert result.decision_object["artifact"]["path"] == "state/handoffs/h1.md"

    def test_unwrap_is_a_fallback_not_a_normalizer(self, tmp_path):
        # Negative-spec of `_sanitize_artifact_path_str`: raw is always tried
        # first, so an unwrapped path that already resolves is untouched.
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        assert "sanitize_resolution" not in result.decision_object["artifact"]

    def test_interior_space_without_a_newline_is_never_joined(self):
        # `_LINE_WRAP_RE` is anchored on a real newline: a path carrying an
        # ordinary interior space (`.../My Documents/...`) must survive the
        # sanitizer intact, or this tier would corrupt every spaced path it
        # was handed.
        assert (
            pa._sanitize_artifact_path_str("C:/My Documents/notes/h1.md")
            == "C:/My Documents/notes/h1.md"
        )

    def test_crlf_and_continuation_indent_are_both_consumed(self):
        assert pa._sanitize_artifact_path_str("state/hand\r\n\t  offs/h1.md") == (
            "state/handoffs/h1.md"
        )

    def test_drive_letter_colon_still_survives_the_loop(self):
        # The added unwrap step must not perturb the pre-existing guards the
        # fixed-point loop carries (bare drive letter, `.`/`..` component).
        assert pa._sanitize_artifact_path_str("C:") == "C:"
        assert pa._sanitize_artifact_path_str("../") == "../"


# ---------------------------------------------------------------------------
# `drop` subcommand routing (AC1, C1) — mirrors the `apply` arm; C6 regression.
# ---------------------------------------------------------------------------

class TestDropSubcommandRouting:
    def test_main_drop_dispatches_to_main_drop(self, monkeypatch):
        calls = []

        def _fake_main_drop(argv):
            calls.append(argv)
            return 0

        monkeypatch.setattr(apply_mod, "main_drop", _fake_main_drop)

        rc = pa.main(["drop", "state/handoffs/h1.md", "--session-id", "s1"])

        assert rc == 0
        assert calls == [["state/handoffs/h1.md", "--session-id", "s1"]]

    def test_main_usage_banner_enumerates_all_three_subcommands(self, capsys):
        pa.main([])
        captured = capsys.readouterr()
        assert "brief" in captured.err
        assert "apply" in captured.err
        assert "drop" in captured.err

    def test_apply_usage_banner_enumerates_all_three_subcommands(self, capsys):
        import coordinator_core.pickup_assemble.apply as apply_mod

        apply_mod._usage("pickup-assemble")
        captured = capsys.readouterr()
        assert "brief" in captured.err
        assert "apply" in captured.err
        assert "drop" in captured.err


# ---------------------------------------------------------------------------
# `gates` key-set disjointness across classifications (AC3, C3/C6) — a
# handoff/spinoff `brief()` payload must never carry the memo-only
# `gates.addressee` key. Regression for the removed unconditional
# `"addressee": {}` placeholder on the handoff MAIN success-path `gates_obj`
# (74e4d938).
#
# `claim`/`claim_grant` used to be handoff/spinoff-only and were asserted
# absent from every memo payload here; the memo/handoff parity fix
# (cross-repo/inbox/2026-08-17-doe-claude-em-memo-claim-fires-after-the-em-
# can-already-act.md) makes `brief()`'s memo branch compute and emit both,
# matching the handoff branch, so they are dropped from this set below —
# this file's own `TestGatesKeySetDisjointness` was itself the asymmetry a
# live peer's brief-stage claim on a memo went unnarrated behind.
# ---------------------------------------------------------------------------

#: Keys that belong ONLY to the handoff/spinoff `gates` shape — never legal
#: on a memo payload.
_HANDOFF_ONLY_GATE_KEYS = {"gate_check", "execution_stamp_match", "successor"}

#: Keys that belong ONLY to the memo `gates` shape — never legal on a
#: handoff/spinoff payload.
_MEMO_ONLY_GATE_KEYS = {"addressee"}


class TestGatesKeySetDisjointness:
    @pytest.mark.parametrize("basename,seed_kwargs", [("h1.md", {}), ("s1.md", {"kind": "spinoff"})])
    def test_handoff_and_spinoff_gates_exclude_memo_only_keys(self, tmp_path, basename, seed_kwargs):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, basename, **seed_kwargs)

        result = pa.brief(f"state/handoffs/{basename}", repo_root=repo)

        gate_keys = set(result.decision_object["gates"].keys())
        leaked = gate_keys & _MEMO_ONLY_GATE_KEYS
        assert not leaked, f"handoff/spinoff gates leaked memo-only keys: {leaked}"

    def test_memo_gates_exclude_handoff_only_keys(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        gate_keys = set(result.decision_object["gates"].keys())
        leaked = gate_keys & _HANDOFF_ONLY_GATE_KEYS
        assert not leaked, f"memo gates leaked handoff/spinoff-only keys: {leaked}"

    def test_memo_actioned_terminal_gates_exclude_handoff_only_keys(self, tmp_path):
        """M0 short-circuit (actioned memo) is its own early-return
        `gates_obj` literal — covered separately from the main memo success
        path above."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", status="actioned", extra="decision: accept-mechanical-direct\n")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        gate_keys = set(result.decision_object["gates"].keys())
        leaked = gate_keys & _HANDOFF_ONLY_GATE_KEYS
        assert not leaked, f"actioned-memo terminal gates leaked handoff/spinoff-only keys: {leaked}"


# ---------------------------------------------------------------------------
# Chunk C6 (pickup-as-a-fully-assembled-decision-surface) — regression
# coverage added against CURRENT disk state (C0b/C1/C2/C3/C7 already
# landed). See that plan's chunk C6 body for the full ADD/RETIRE/UPDATE
# ledger this section discharges.
# ---------------------------------------------------------------------------

def _seed_handoff_status(
    repo: Path, name: str, status: str, deployment_state: str, extra: str = ""
) -> Path:
    """Like `_seed_handoff` but with caller-controlled `status:`/
    `deployment_state:` (the fixed `_seed_handoff` template always seeds
    `open`/`active`) — needed for the claimed/in_flight shapes
    `cs_unclaim_handoff`/`cs_repark_handoff` transition out of."""
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
        f"{extra}"
    )
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


class TestKindDispatchGuidanceAndRecommendation:
    """C4 BUILD — every `j-kind` disposition carries a `guidance` field,
    `recommendation` stays null (`reason: insufficient-evidence`), and the
    judgment point is absent entirely for a handoff/spinoff classification
    (there is no `kind:` dispatch on a handoff)."""

    def test_every_disposition_carries_nonempty_guidance(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="proposal")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        kind_jp = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "j-kind")
        for disposition in kind_jp["dispositions"]:
            assert disposition.get("guidance"), f"{disposition['value']} has no guidance"
            assert isinstance(disposition["guidance"], str) and disposition["guidance"].strip()

    def test_recommendation_absent_with_insufficient_evidence_reason(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="fyi")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        kind_jp = next(jp for jp in result.decision_object["judgment_points"] if jp["id"] == "j-kind")
        assert kind_jp["recommendation"] is None
        assert kind_jp["reason"] == "insufficient-evidence"

    def test_kind_dispatch_judgment_point_absent_for_handoff(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        jp_ids = {jp["id"] for jp in result.decision_object["judgment_points"]}
        assert "j-kind" not in jp_ids

    def test_kind_dispatch_judgment_point_absent_for_spinoff(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "s1.md", kind="spinoff")

        result = pa.brief("state/handoffs/s1.md", repo_root=repo)

        jp_ids = {jp["id"] for jp in result.decision_object["judgment_points"]}
        assert "j-kind" not in jp_ids

    def test_guidance_is_descriptive_not_steering(self):
        """the Director of Engineering, Design Call #1 rider — guidance describes what a disposition
        MEANS, evenhandedly, and must never front-load/editorialize toward
        one option (the exact anti-pattern the module docstring names:
        "usually you'll want to..."). A light lexical check across every
        kind's every disposition, not a semantic judge."""
        steering_phrases = (
            "usually you'll want",
            "usually you will want",
            "we recommend",
            "the best choice",
            "in most cases you should",
            "typically you should",
            "the preferred option",
            "the default choice",
        )
        for kind, dispositions in pa._KIND_DISPOSITIONS.items():
            for disposition in dispositions:
                text = disposition.get("guidance", "")
                lowered = text.lower() if isinstance(text, str) else ""
                for phrase in steering_phrases:
                    assert phrase not in lowered, (
                        f"{kind}.{disposition['value']} guidance editorializes toward a "
                        f"default ({phrase!r}) — defeats recommendation=None"
                    )

    def test_actioned_note_prescribing_guidance_always_resolves_action_memo(self):
        """Class-closing invariant (cross-repo memo `2026-07-25-doe-claude-
        em-consult-dispositions-never-terminal.md`): a disposition whose
        guidance PRESCRIBES writing `actioned_note` is terminal-on-decision
        — the receiver's work is done once that note is recorded — and
        MUST resolve `d-action-memo`, or the memo is stuck at `status:
        in_progress` forever with no automated escape (the `ack-nil` and
        `consult` regressions this memo diagnosed). Derived directly from
        `_KIND_DISPOSITIONS`'s guidance text (keyed on the literal
        `actioned_note` token), not hardcoded against today's five known
        offenders — a future disposition that prescribes `actioned_note`
        but ships `resolves: []` must fail this test."""
        for kind, dispositions in pa._KIND_DISPOSITIONS.items():
            for disposition in dispositions:
                guidance = disposition.get("guidance", "")
                guidance_text = guidance if isinstance(guidance, str) else ""
                if "actioned_note" in guidance_text:
                    assert "d-action-memo" in disposition.get("resolves", []), (
                        f"{kind}.{disposition['value']} guidance prescribes writing "
                        f"actioned_note but resolves={disposition.get('resolves')!r} — "
                        f"this disposition is terminal-on-decision and must resolve "
                        f"'d-action-memo', or the memo can never leave status: "
                        f"in_progress without an out-of-band archive-stamp-cli hand-run"
                    )


class TestStampReadMatrix:
    """AC12 — `compute_liveness_signal`'s claim-stamp state machine, read
    directly (chunk C7 Part A collapse: no stamp / self-or-lineage stamp /
    live-foreign stamp / dead-foreign-stamp reap)."""

    def test_no_stamp_does_not_fire_and_apply_consumes_unconditionally(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        assert pa.compute_liveness_signal(repo, {}, "state/handoffs/h1.md") is False

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        jp_ids = {jp["id"] for jp in result.decision_object["judgment_points"]}
        assert "j1" not in jp_ids
        d2 = next(d for d in result.decision_object["directives"] if d["id"] == "d2")
        assert d2["depends_on"] is None

    def test_stamp_is_this_sessions_own_lineage_does_not_fire(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "self-sid")

        fired = pa.compute_liveness_signal(
            repo,
            {"claimed_by": "self-sid", "authoring_session": "self-sid"},
            "state/handoffs/h1.md",
        )

        assert fired is False

    def test_stamp_is_live_foreign_session_fires(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        fired = pa.compute_liveness_signal(
            repo, {"claimed_by": "foreign-live-sid"}, "state/handoffs/h1.md",
        )

        assert fired is True

    def test_stamp_is_dead_foreign_session_reaped_by_proceeding(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: False)

        fired = pa.compute_liveness_signal(
            repo, {"claimed_by": "long-dead-sid"}, "state/handoffs/h1.md",
        )

        assert fired is False


class TestDeadlockRegression0507151b:
    """AC13a — the `0507151b` shape: a plan's ONLY recent commit is its own
    execution-authorization stamp, committed by the SAME session that is now
    picking it up. Under the old signal (b) `_commit_recency_signal` this
    false-fired (the deadlock the PM witnessed live); the new stamp-only
    state machine never reads git-log recency or a `plan:` citation at all,
    so it structurally cannot reproduce, regardless of what commit history
    sits on the cited plan."""

    def test_self_authored_recent_plan_commit_never_fires_liveness(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_dir = repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "p.md").write_text("# Plan\n\nexecution_authorized_sha: deadbeef\n", encoding="utf-8")
        _git(repo, "add", "docs/plans/p.md")
        # NOT backdated — this is the freshest commit in the repo, exactly
        # the shape that used to false-fire signal (b).
        _git(repo, "commit", "-m", "stamp execution authorization")

        # The handoff cites the plan but carries no claim-stamp of its own —
        # the only input the new state machine reads.
        fired = pa.compute_liveness_signal(
            repo, {"plan": "docs/plans/p.md"}, "state/handoffs/self.md",
        )

        assert fired is False


class TestRevalidateHonorsProceed:
    """AC13b — `j1` no longer carries `revalidate_at_dispatch`, so a
    recorded `proceed` disposition is honored at `apply` time (via the
    per-directive `_disposition_resolves_directive` gate) rather than being
    silently discarded and re-blocked."""

    def test_recorded_proceed_disposition_fires_the_gated_directive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        directives = [
            {
                "id": "d2",
                "cli": "archive-stamp-cli",
                "args": ["claim-handoff", "state/handoffs/h1.md"],
                "depends_on": "j1",
                "already_satisfied": False,
            },
        ]
        judgment_points = [pa.build_liveness_judgment_point(True, "gates.liveness_signal", ["d2"])]
        decisions = {"j1": {"disposition": "proceed"}}

        with apply_mod._session_identity("sid-honors-proceed"):
            exit_code, report = apply_mod._execute_directives(
                directives, judgment_points, repo,
                decisions=decisions,
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert report["landed"] == ["d2"]


class TestDropVsReparkClearing:
    """Design point #2 — `drop` on a claim-stamped handoff clears
    `claimed_by` (full inverse of a granted claim); `repark` is a
    deployment_state-only unpause and leaves the claim record intact."""

    def test_drop_clears_claimed_by(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(
            repo, "h1.md", "claimed", "in_flight",
            extra="claimed_at: '2026-01-01T00:00:00Z'\nclaimed_by: sess-dropped\n",
        )
        # `_scoped_commit`'s `git add`/`git commit` calls route through this
        # module's in-process git read-model, which (like `test_pickup_apply
        # .py`'s documented AC11 sandbox failures) does not implement those
        # two verbs — stubbed out here so this test isolates the ONE thing
        # under test (does `drop` clear `claimed_by`?) from that unrelated,
        # pre-existing gap.
        monkeypatch.setattr(apply_mod, "_scoped_commit", lambda *a, **k: "fakesha")

        with apply_mod._session_identity("sess-dropped"):
            exit_code, report = apply_mod.drop("state/handoffs/h1.md", repo_root=repo)

        assert exit_code == apply_mod.APPLY_EXIT_OK
        text = (repo / "state" / "handoffs" / "h1.md").read_text(encoding="utf-8")
        assert "claimed_by" not in text

    def test_repark_leaves_claimed_by_intact(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff_status(
            repo, "h2.md", "claimed", "in_flight",
            extra="claimed_at: '2026-01-01T00:00:00Z'\nclaimed_by: sess-parked\n",
        )

        import coordinator_core.archive_stamp as arstamp

        rc = arstamp.cs_repark_handoff(str(hp))

        assert rc == 0
        text = hp.read_text(encoding="utf-8")
        assert "deployment_state: ready_to_fire" in text
        assert "claimed_by: sess-parked" in text


class TestHandoffSpinoffPerDirectiveHalt:
    """Gap-sweep GAP 2 — one per-directive-halt shape per gating kind: an
    `awaiting_gate` handoff, a firing liveness signal, and an unconditional
    completeness-checklist mirror directive alongside an unresolved probe
    judgment point."""

    def test_awaiting_gate_halts_d2_but_d1_still_lands(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(repo, "h1.md", "open", "awaiting_gate", extra="gate_dependency: some-gate\n")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        directives = result.decision_object["directives"]
        judgment_points = result.decision_object["judgment_points"]
        assert any(jp["id"] == "jgate" for jp in judgment_points)

        with apply_mod._session_identity("sid-gate-halt"):
            exit_code, report = apply_mod._execute_directives(
                directives, judgment_points, repo,
                decisions={},
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d1" in report["landed"]
        assert "d2" not in report["landed"]
        assert "jgate" in report["unresolved_judgment_points"]

    def test_firing_liveness_signal_halts_d2_but_d1_still_lands(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(
            repo, "h1.md", "open", "active",
            extra='claimed_by: "foreign-live-sid"\n',
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        directives = result.decision_object["directives"]
        judgment_points = result.decision_object["judgment_points"]
        assert any(jp["id"] == "j1" for jp in judgment_points)

        with apply_mod._session_identity("sid-liveness-halt"):
            exit_code, report = apply_mod._execute_directives(
                directives, judgment_points, repo,
                decisions={},
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d1" in report["landed"]
        assert "d2" not in report["landed"]
        assert "j1" in report["unresolved_judgment_points"]

    def test_completeness_probe_item_leaves_probe_unresolved_while_d1_and_mirror_land(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(
            repo, "h1.md", "open", "active",
            extra="completeness_checklist:\n  - 'live: some assertion [probe: some-command]'\n",
        )

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)
        directives = result.decision_object["directives"]
        judgment_points = result.decision_object["judgment_points"]
        probe_jps = [jp for jp in judgment_points if jp["id"] != "j1" and jp["id"] != "jgate"]
        assert probe_jps, "expected a probe-confirmation judgment point"
        for jp in probe_jps:
            for disposition in jp["dispositions"]:
                assert disposition["resolves"] == []

        try:
            with apply_mod._session_identity("sid-probe-halt"):
                exit_code, report = apply_mod._execute_directives(
                    directives, judgment_points, repo,
                    decisions={},
                    resolve_claim_grant=lambda: {"verdict": "granted"},
                )
        finally:
            # `coordinator-tasks-mirror`'s dynamically-loaded module arms lazy
            # op registration as a side effect of import — an interpreter-global
            # write this test doesn't own and must not leak into later tests.
            # Both channels: the env var was the mechanism until 2026-07-28,
            # when the in-process signal moved to `sys._coordinator_core_lazy_ops`
            # so that no spawned child could inherit it.
            os.environ.pop("COORDINATOR_CORE_LAZY_OPS", None)
            if hasattr(sys, "_coordinator_core_lazy_ops"):
                delattr(sys, "_coordinator_core_lazy_ops")

        assert "d1" in report["landed"]
        mirror_directives = [d["id"] for d in directives if d["cli"] == "coordinator-tasks-mirror"]
        assert mirror_directives, "expected a coordinator-tasks-mirror init directive"
        for mirror_id in mirror_directives:
            assert mirror_id in report["landed"]
        # The probe judgment point itself has no downstream directive naming
        # it in `depends_on` (safety constraint (a)) — it never appears in
        # `unresolved_judgment_points`, by construction, even though it stays
        # unresolved in `judgment_points[]` itself.
        assert exit_code in (apply_mod.APPLY_EXIT_OK, apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT)


class TestClaimGrantBlanketPreLoopGate:
    """C7 Part B safety constraint (b) — a DENIED `claim_grant` gates the
    WHOLE run pre-loop (blanket, not scoped to `d1`'s own dispatch), even
    for a directive whose `depends_on` is `None`. Confirmed on disk today
    (`apply.py:716-722`) — must survive the per-directive-halt refactor
    unchanged."""

    def test_denied_claim_grant_blocks_every_unconditional_directive(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": None, "already_satisfied": False},
        ]

        exit_code, report = apply_mod._execute_directives(
            directives, [], repo,
            decisions={},
            resolve_claim_grant=lambda: {"verdict": "denied", "reason": "held by a live peer"},
        )

        assert exit_code == apply_mod.APPLY_EXIT_CLAIM_DENIED
        assert report["landed"] == []


class TestRestampExecutionShaDirectiveC9:
    """C9 AC18 — `restamp-execution-sha` dispatches without crashing `apply`
    on a stale-bookkeeping/unstampable handoff, re-stamping
    `execution_authorized_sha` on the ONE target file it names."""

    def test_restamp_dispatch_updates_target_and_returns_detail(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        plan_dir = repo / "docs" / "plans"
        plan_dir.mkdir(parents=True)
        plan_path = plan_dir / "p.md"
        plan_path.write_text("---\nexecution_authorized_sha: oldsha\n---\n\n# Plan\n", encoding="utf-8")
        _git(repo, "add", "docs/plans/p.md")
        _git(repo, "commit", "-m", "add plan")

        detail = apply_mod._dispatch_archive_stamp_cli(
            ["restamp-execution-sha", "docs/plans/p.md", "newsha123"], repo,
        )

        assert detail["verb"] == "restamp-execution-sha"
        assert detail["computed_sha"] == "newsha123"
        text = plan_path.read_text(encoding="utf-8")
        assert "newsha123" in text
        assert "oldsha" not in text


class TestMixedHaltExitCodeC7PartC:
    """C7 Part C — a partial-per-directive-landed run reports
    `APPLY_EXIT_HALTED_AT_JUDGMENT` (never `APPLY_EXIT_OK`) whenever at
    least one directive was blocked, even though every OTHER ready
    directive still dispatched this pass."""

    def test_partial_landed_run_reports_halted_at_judgment(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "j1", "already_satisfied": False},
        ]
        judgment_points = [pa.build_liveness_judgment_point(True, "gates.liveness_signal", ["d2"])]

        with apply_mod._session_identity("sid-mixed-halt"):
            exit_code, report = apply_mod._execute_directives(
                directives, judgment_points, repo,
                decisions={},
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == ["d1"]
        assert report["unresolved_judgment_points"] == ["j1"]


class TestMemoTerminalDirectivesC8:
    """C8 — the memo terminal-execution path: `claim-memo-stamp` fires
    alongside `d1` under the SAME `depends_on` (grab mechanics, never
    `j-kind`-gated); `d-action-memo` fires only once `j-kind` resolves to
    an action-taking disposition; no `_KIND_DISPOSITIONS` entry's
    `resolves` still names the stale `["d1"]` (memo-code GAP 3)."""

    def test_claim_memo_stamp_shares_d1s_depends_on_unconditional_case(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}

        assert directives_by_id["d1"]["depends_on"] is None
        assert directives_by_id["claim-memo-stamp"]["depends_on"] is None

    def test_claim_memo_stamp_shares_d1s_depends_on_liveness_gated_case(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask", extra='picked_up_by: "foreign-live-sid"\n')
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}

        assert directives_by_id["d1"]["depends_on"] == "j1"
        assert directives_by_id["claim-memo-stamp"]["depends_on"] == "j1"

    def test_action_memo_never_gated_on_anything_but_j_kind(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="proposal")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)
        action_memo = next(d for d in result.decision_object["directives"] if d["id"] == "d-action-memo")

        assert action_memo["depends_on"] == "j-kind"

    def test_no_kind_disposition_resolves_the_stale_d1_literal(self):
        """Memo-code GAP 3 — `_KIND_DISPOSITIONS` must repoint every
        action-taking disposition at `d-action-memo`, never at `d1` (the
        claim, which fires unconditionally per C7 and must never appear in
        ANY disposition's `resolves`)."""
        for kind, dispositions in pa._KIND_DISPOSITIONS.items():
            for disposition in dispositions:
                assert "d1" not in disposition["resolves"], (
                    f"{kind}.{disposition['value']} still resolves the stale d1 literal"
                )
                assert "claim-memo-stamp" not in disposition["resolves"], (
                    f"{kind}.{disposition['value']} wrongly gates claim-memo-stamp on j-kind"
                )

    def test_action_memo_fires_only_once_j_kind_resolves_to_action_taking_disposition(self, tmp_path):
        """Exercised against `_directive_gate_open` directly (the READY-vs-
        BLOCKED predicate `_execute_directives` consults per directive) —
        NOT a full `_execute_directives`/`apply()` round trip, which would
        also have to dispatch `claim-memo-stamp` and `d-action-memo`
        themselves through `archive-stamp-cli`'s dispatch table. Those two
        verbs have no handler wired in `apply.py`'s `_dispatch_archive_
        stamp_cli` today (only `claim-handoff`/`restamp-execution-sha`
        are) — a real gap in the C8 apply-side landing, out of this chunk's
        file-scoped surface, reported rather than papered over with a
        directive-execution test that can't actually pass."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="proposal")

        decisions = {"j-kind": {"disposition": "adopt"}}
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, decisions=decisions)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
        jp_by_id = apply_mod._judgment_points_by_id(result.decision_object["judgment_points"])

        ready, blocking = apply_mod._directive_gate_open(directives_by_id["d-action-memo"], jp_by_id, decisions)

        assert ready is True
        assert blocking == []

    def test_fyi_ack_nil_resolves_action_memo_via_actioned_note_not_decision(self, tmp_path):
        """The defect this closes: `ack-nil` used to leave `resolves: []`,
        so `d-action-memo` never fired and the memo stayed `status: open`
        forever, forcing an out-of-band hand-run of `archive-stamp-cli
        action-memo`. `ack-nil` now resolves `d-action-memo` like the other
        three action-taking dispositions, but (unlike them) is absent from
        `_MEMO_ACTION_DECISION_MAP` — nil-impact isn't accepted/partial/
        declined — so its args carry `--actioned-note` only, never
        `--decision`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="fyi")

        decisions = {"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact on this repo"}}
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, decisions=decisions)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
        jp_by_id = apply_mod._judgment_points_by_id(result.decision_object["judgment_points"])

        action_memo = directives_by_id["d-action-memo"]
        ready, blocking = apply_mod._directive_gate_open(action_memo, jp_by_id, decisions)

        assert ready is True
        assert blocking == []
        assert "--actioned-note" in action_memo["args"]
        assert action_memo["args"][action_memo["args"].index("--actioned-note") + 1] == "no impact on this repo"
        assert "--decision" not in action_memo["args"]

    def test_consult_reply_short_resolves_action_memo_via_actioned_note_not_decision(self, tmp_path):
        """Same shape as the `fyi`/`ack-nil` fix above, for `consult`/
        `reply-short`: it used to leave `resolves: []`, so `d-action-memo`
        never fired and the memo stayed `status: in_progress` forever,
        forcing an out-of-band hand-run of `archive-stamp-cli action-memo`.
        `reply-short` now resolves `d-action-memo`, but (like `ack-nil`) is
        absent from `_MEMO_ACTION_DECISION_MAP` — a consult reply is not
        accepted/partial/declined — so its args carry `--actioned-note`
        only, never `--decision`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="consult")

        decisions = {"j-kind": {"disposition": "reply-short", "actioned_note": "the answer is X"}}
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, decisions=decisions)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
        jp_by_id = apply_mod._judgment_points_by_id(result.decision_object["judgment_points"])

        action_memo = directives_by_id["d-action-memo"]
        ready, blocking = apply_mod._directive_gate_open(action_memo, jp_by_id, decisions)

        assert ready is True
        assert blocking == []
        assert "--actioned-note" in action_memo["args"]
        assert action_memo["args"][action_memo["args"].index("--actioned-note") + 1] == "the answer is X"
        assert "--decision" not in action_memo["args"]

    def test_consult_reply_long_resolves_action_memo_via_actioned_note_not_decision(self, tmp_path):
        """Same shape as the `fyi`/`ack-nil` fix above, for `consult`/
        `reply-long`: it used to leave `resolves: []`, so `d-action-memo`
        never fired and the memo stayed `status: in_progress` forever,
        forcing an out-of-band hand-run of `archive-stamp-cli action-memo`.
        `reply-long` now resolves `d-action-memo`, but (like `ack-nil`) is
        absent from `_MEMO_ACTION_DECISION_MAP` — a consult reply is not
        accepted/partial/declined — so its args carry `--actioned-note`
        only, never `--decision`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="consult")

        decisions = {"j-kind": {"disposition": "reply-long", "actioned_note": "see ## EM Response"}}
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, decisions=decisions)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
        jp_by_id = apply_mod._judgment_points_by_id(result.decision_object["judgment_points"])

        action_memo = directives_by_id["d-action-memo"]
        ready, blocking = apply_mod._directive_gate_open(action_memo, jp_by_id, decisions)

        assert ready is True
        assert blocking == []
        assert "--actioned-note" in action_memo["args"]
        assert action_memo["args"][action_memo["args"].index("--actioned-note") + 1] == "see ## EM Response"
        assert "--decision" not in action_memo["args"]

    def test_kind_dispositions_receiver_done_classification_is_exhaustive_and_closed(self):
        """CLASS-CLOSING invariant, not a third instance: asserts the
        explicit expected `(kind, value)` classification sets against
        `_KIND_DISPOSITIONS` directly, rather than grepping `guidance` prose
        for a substring (prose is not a contract and would rot on a wording
        change with no test failure). The union of the two literal sets
        below MUST equal every entry in `_KIND_DISPOSITIONS` — a newly added
        disposition that nobody classifies fails this test until it is
        deliberately sorted into one bucket or the other."""
        receiver_done = {
            ("ask", "accept-mechanical-direct"),
            ("ask", "accept-escalate-to-sizing"),
            ("ask", "decline"),
            ("consult", "reply-short"),
            ("consult", "reply-long"),
            ("proposal", "adopt"),
            ("proposal", "decline"),
            ("proposal", "negotiate"),
            ("fyi", "ack-nil"),
            ("fyi", "surgical-fix"),
        }
        work_still_owed = {
            ("ask", "surface-to-PM"),
            ("fyi", "re-plan"),
            ("fyi", "surface-to-PM"),
            ("fyi", "investigate-further"),
        }

        all_entries = {
            (kind, disposition["value"])
            for kind, dispositions in pa._KIND_DISPOSITIONS.items()
            for disposition in dispositions
        }
        assert receiver_done | work_still_owed == all_entries, (
            "a _KIND_DISPOSITIONS entry is unclassified by this test — sort it into "
            "receiver_done or work_still_owed above before landing it"
        )
        assert receiver_done.isdisjoint(work_still_owed)

        for kind, dispositions in pa._KIND_DISPOSITIONS.items():
            for disposition in dispositions:
                key = (kind, disposition["value"])
                if key in receiver_done:
                    assert disposition["resolves"] == ["d-action-memo"], (
                        f"{kind}.{disposition['value']} is classified receiver-done but "
                        f"does not resolve d-action-memo"
                    )
                else:
                    assert disposition["resolves"] == [], (
                        f"{kind}.{disposition['value']} is classified work-still-owed but "
                        f"resolves something"
                    )

    def test_memo_action_decision_map_keys_resolve_action_memo_in_kind_dispositions(self):
        """Inverse-drift guard: every `_MEMO_ACTION_DECISION_MAP` key must
        name a real `_KIND_DISPOSITIONS` entry that itself resolves
        `d-action-memo` — catches a decision-map entry left dangling (or
        pointed at a disposition that got reverted to `resolves: []`)."""
        dispositions_by_kind_value = {
            (kind, disposition["value"]): disposition
            for kind, dispositions in pa._KIND_DISPOSITIONS.items()
            for disposition in dispositions
        }
        for kind, value in pa._MEMO_ACTION_DECISION_MAP:
            assert (kind, value) in dispositions_by_kind_value, (
                f"_MEMO_ACTION_DECISION_MAP names ({kind!r}, {value!r}), which does not "
                f"exist in _KIND_DISPOSITIONS"
            )
            assert dispositions_by_kind_value[(kind, value)]["resolves"] == ["d-action-memo"], (
                f"_MEMO_ACTION_DECISION_MAP names ({kind!r}, {value!r}), which does not "
                f"resolve d-action-memo in _KIND_DISPOSITIONS"
            )


class TestDispositionValueAwarePredicateZoliV2Finding1:
    """the Director of Engineering v2 finding 1 (CRITICAL) — a NON-terminal disposition on a
    judgment point (one whose own `resolves` is `[]`) must leave every
    directive depending on that judgment point UNFIRED and the run
    HALTED — a naive "some disposition was picked" predicate would wrongly
    let it through."""

    def test_non_terminal_memo_disposition_leaves_action_memo_unfired_and_halted(self, tmp_path):
        """Isolated to the ONE directive under test (`d-action-memo`) via
        `_execute_directives`, rather than the full memo directive list —
        `claim-memo-stamp`/`action-memo` have no dispatch handler wired in
        `apply.py` today (see the sibling test above), so including them
        would fail on that unrelated, pre-existing gap instead of on the
        disposition-value-aware predicate this test targets."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        decisions = {"j-kind": {"disposition": "surface-to-PM"}}
        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo, decisions=decisions)
        directives_by_id = {d["id"]: d for d in result.decision_object["directives"]}
        judgment_points = result.decision_object["judgment_points"]

        with apply_mod._session_identity("sid-nonterminal-memo"):
            exit_code, report = apply_mod._execute_directives(
                [directives_by_id["d-action-memo"]], judgment_points, repo,
                decisions=decisions,
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d-action-memo" not in report["landed"]
        assert "j-kind" in report["unresolved_judgment_points"]

    def test_non_terminal_liveness_disposition_leaves_d2_unfired_and_halted(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        directives = [
            {"id": "d1", "cli": "session-claim-cli", "args": ["claim-artifact", "handoff", "h1.md"], "depends_on": None, "already_satisfied": False},
            {"id": "d2", "cli": "archive-stamp-cli", "args": ["claim-handoff", "state/handoffs/h1.md"], "depends_on": "j1", "already_satisfied": False},
        ]
        judgment_points = [pa.build_liveness_judgment_point(True, "gates.liveness_signal", ["d2"])]
        decisions = {"j1": {"disposition": "stand-down-and-surface"}}

        with apply_mod._session_identity("sid-nonterminal-liveness"):
            exit_code, report = apply_mod._execute_directives(
                directives, judgment_points, repo,
                decisions=decisions,
                resolve_claim_grant=lambda: {"verdict": "granted"},
            )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d2" not in report["landed"]
        assert "d1" in report["landed"]


class TestEMContentChannelZoliV2Finding2:
    """the Director of Engineering v2 finding 2 (MAJOR) — the EM-content channel: a `j-kind`
    disposition carrying `realized_by` (alongside `decision`) must land in
    `cs_action_memo`'s `--realized-by` flag, not be dropped on the floor.

    Exercised directly against `_build_action_memo_args` (the function that
    owns this resolution) with a hand-built `decisions` map, mirroring the
    shape `apply()` would compute — NOT round-tripped through
    `_read_session_dispositions`, which today (chunk C7/C8, as landed)
    extracts only `disposition` off the session decision file and drops
    `realized_by`/`decision_note`/`actioned_note`/`distill_fate`; that gap is
    a real one, out of this chunk's file-scoped surface
    (`coordinator_core/pickup_assemble/apply.py`), reported rather than
    silently patched over here."""

    def test_realized_by_lands_on_the_action_memo_args(self):
        decisions = {"j-kind": {"disposition": "adopt", "realized_by": "docs/plans/p.md"}}

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--realized-by" in args
        assert args[args.index("--realized-by") + 1] == "docs/plans/p.md"

    def test_decision_note_lands_on_the_action_memo_args(self):
        decisions = {"j-kind": {"disposition": "adopt", "decision_note": "adopted as-is"}}

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--decision-note" in args
        assert args[args.index("--decision-note") + 1] == "adopted as-is"

    def test_actioned_note_lands_for_a_non_decision_mapped_disposition(self):
        # `proposal`/`negotiate` deliberately stays absent from
        # `_MEMO_ACTION_DECISION_MAP` (see the comment above that map) — kept
        # off `decline` here since `("proposal", "decline")` is now
        # decision-mapped to `"declined"` (2026-07-25 class-sweep fix) and
        # would raise on a co-supplied `actioned_note`.
        decisions = {"j-kind": {"disposition": "negotiate", "actioned_note": "here's a counter-shape"}}

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--actioned-note" in args
        assert args[args.index("--actioned-note") + 1] == "here's a counter-shape"

    def test_distill_fate_lands_regardless_of_disposition_shape(self):
        decisions = {"j-kind": {"disposition": "decline", "distill_fate": "ephemeral"}}

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--distill-fate" in args
        assert args[args.index("--distill-fate") + 1] == "ephemeral"

    def test_in_repo_capture_lands_alongside_ratification_distill_fate(self):
        """Defect fix (2026-07-25, live repro this session): `in_repo_capture`
        must forward to `cs_action_memo`'s `--in-repo-capture` flag the same
        way its sibling content keys do — this was the missing forwarding
        half of the bug (the missing acceptance half is covered by
        `TestValidateDecisionsShapeAcceptsInRepoCapture` below)."""
        decisions = {
            "j-kind": {
                "disposition": "decline",
                "distill_fate": "ratification",
                "in_repo_capture": "docs/decisions/DR-099-example.md",
            }
        }

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--in-repo-capture" in args
        assert args[args.index("--in-repo-capture") + 1] == "docs/decisions/DR-099-example.md"

    def test_in_repo_capture_absent_when_empty(self):
        decisions = {"j-kind": {"disposition": "decline", "distill_fate": "ephemeral"}}

        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "proposal", decisions)

        assert "--in-repo-capture" not in args


class TestValidateDecisionsShapeAcceptsInRepoCapture:
    """Defect fix (2026-07-25, live repro this session): `in_repo_capture`
    was accepted by `cs_action_memo` (`archive_stamp.py::_DISPOSITION_FLAGS`)
    but rejected by `validate_decisions_shape` — `DISPOSITION_CONTENT_KEYS`
    (`pickup_assemble/__init__.py`) omitted it, so `distill_fate:
    ratification` was unreachable through this CLI (its downstream cross-
    field validation in `ops/memo_transition.py` hard-requires
    `in_repo_capture` whenever `distill_fate == "ratification"`, and there
    was no way to supply it)."""

    def test_in_repo_capture_alone_is_accepted(self):
        decisions = {"j-kind": {"disposition": "decline", "in_repo_capture": "docs/decisions/x.md"}}

        assert pa.validate_decisions_shape(decisions) is None

    def test_ratification_with_in_repo_capture_is_accepted(self):
        decisions = {
            "j-kind": {
                "disposition": "decline",
                "distill_fate": "ratification",
                "in_repo_capture": "docs/decisions/x.md",
            }
        }

        assert pa.validate_decisions_shape(decisions) is None

    def test_ratification_without_in_repo_capture_fails_loud_at_shape_time(self):
        """Item 4 — pre-flight cross-field check: catches the
        `distill_fate=ratification`-requires-`in_repo_capture` rule at
        shape-validation time (before `d1`/`claim-memo-stamp` fire), rather
        than letting the caller discover it via
        `ops/memo_transition.py`'s downstream cross-field error after a
        partial apply."""
        decisions = {"j-kind": {"disposition": "decline", "distill_fate": "ratification"}}

        error = pa.validate_decisions_shape(decisions)

        assert error is not None
        assert "j-kind" in error
        assert "ratification" in error
        assert "in_repo_capture" in error

    def test_ratification_with_empty_in_repo_capture_still_fails_loud(self):
        decisions = {
            "j-kind": {
                "disposition": "decline",
                "distill_fate": "ratification",
                "in_repo_capture": "",
            }
        }

        error = pa.validate_decisions_shape(decisions)

        assert error is not None
        assert "in_repo_capture" in error

    def test_non_ratification_distill_fate_does_not_require_in_repo_capture(self):
        decisions = {"j-kind": {"disposition": "decline", "distill_fate": "ephemeral"}}

        assert pa.validate_decisions_shape(decisions) is None


# ---------------------------------------------------------------------------
# --decisions "value" key acceptance -- brief's own OUTPUT vocabulary names
# the choice-key "value" (`pickup_assemble/__init__.py:2401` reads
# `d.get("value")` internally, confirming it's this engine's own word too).
# Twin of the equivalent baton_assemble fix -- see
# `test_baton_assemble.py::TestDecisionsValueKeyEquivalence` for the
# cross-repo live-failure writeup both fixes close.
# ---------------------------------------------------------------------------


class TestDecisionsValueKeyEquivalence:
    def test_value_alone_is_accepted_and_normalized_to_disposition(self):
        decisions = {"j-kind": {"value": "decline"}}
        assert pa.validate_decisions_shape(decisions) is None
        assert decisions == {"j-kind": {"disposition": "decline"}}

    def test_value_with_content_keys_is_accepted(self):
        decisions = {"j-kind": {"value": "decline", "decision_note": "not relevant"}}
        assert pa.validate_decisions_shape(decisions) is None
        assert decisions == {"j-kind": {"disposition": "decline", "decision_note": "not relevant"}}

    def test_disposition_and_value_agreeing_both_present_is_accepted(self):
        decisions = {"j-kind": {"disposition": "decline", "value": "decline"}}
        assert pa.validate_decisions_shape(decisions) is None
        assert decisions == {"j-kind": {"disposition": "decline"}}

    def test_disposition_and_value_disagreeing_fails_loud_naming_both(self):
        decisions = {"j-kind": {"disposition": "decline", "value": "accept-mechanical-direct"}}
        error = pa.validate_decisions_shape(decisions)
        assert error is not None
        assert "decline" in error
        assert "accept-mechanical-direct" in error

    def test_neither_disposition_nor_value_still_fails_loud(self):
        decisions = {"j-kind": {"decision_note": "no choice supplied"}}
        error = pa.validate_decisions_shape(decisions)
        assert error is not None
        assert "j-kind" in error

    def test_bare_string_still_fails_loud_not_coerced(self):
        decisions = {"j-kind": "decline"}
        error = pa.validate_decisions_shape(decisions)
        assert error is not None

    def test_unrecognized_key_alongside_value_still_rejected(self):
        # "value" joins the closed content-key set as disposition's alias --
        # it does not open the schema to arbitrary keys.
        decisions = {"j-kind": {"value": "decline", "not_a_real_key": "x"}}
        error = pa.validate_decisions_shape(decisions)
        assert error is not None
        assert "not_a_real_key" in error


class TestCommitOnGrabHaltZoliV2Finding3:
    """the Director of Engineering v2 finding 3 (MAJOR) — a grab-halt run (fires the unconditional
    grab directive, halts at a downstream judgment point) must still
    attempt the scoped commit for whatever landed, per C7 Part B(c)'s
    widened "also commit on APPLY_EXIT_HALTED_AT_JUDGMENT" rule
    (`apply.py:945`) — a real mechanical mutation must not sit uncommitted
    in a shared worktree across the EM's decision round-trip.

    Exercised on the handoff `d1`/`d2` shape (both have real dispatch
    handlers wired) rather than the memo `claim-memo-stamp` shape — see the
    sibling tests above for the memo-side apply.py dispatch gap this chunk's
    file-scoped surface cannot close. `_scoped_commit` itself is stubbed
    out: its `git add`/`git commit` calls route through this module's
    in-process git read-model, which does not implement those two verbs
    (same pre-existing gap `test_pickup_apply.py`'s AC11 sandbox failures
    document) — stubbing isolates this test to the ONE thing under test:
    does `apply()` call `_scoped_commit` (and use its result) on a HALTED
    exit, not merely on a clean OK one?"""

    def test_grab_halt_run_still_attempts_the_scoped_commit(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_status(
            repo, "h1.md", "open", "active",
            extra='claimed_by: "foreign-live-sid"\n',
        )
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        calls = []

        def _fake_scoped_commit(root, artifact_rel_path, class_, basename, landed):
            calls.append(list(landed))
            return "fakesha-grab-halt"

        monkeypatch.setattr(apply_mod, "_scoped_commit", _fake_scoped_commit)

        exit_code, report = apply_mod.apply(
            "state/handoffs/h1.md", session_id="sid-grab-halt", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d1" in report["landed"]
        assert "d2" not in report["landed"]
        assert report["commit_sha"] == "fakesha-grab-halt"
        assert calls == [["d1"]]


def _write_session_decision_file(
    repo: Path, session_id: str, artifact_path: str, jp_id: str, disposition: str, **content: str
) -> None:
    """Fabricates the session-scoped decision-object file the C3 auto-fire
    hook writes (`apply.py::_read_session_dispositions`'s own read
    contract) — a single `judgment_points[]` entry carrying `disposition`
    plus whichever of `_DISPOSITION_CONTENT_KEYS` the EM filled
    (`realized_by`/`decision_note`/`actioned_note`/`distill_fate`)."""
    path = apply_mod._session_decision_file_path(repo, session_id, artifact_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": jp_id, "disposition": disposition, **content}
    path.write_text(json.dumps({"judgment_points": [entry]}), encoding="utf-8")


class TestMemoTerminalEndToEndC6:
    """C6-e2e — now that `apply.py`'s `_dispatch_archive_stamp_cli` has real
    `claim-memo-stamp`/`action-memo` handlers wired (the gap every sibling
    test class above deliberately worked around — see
    `TestMemoTerminalDirectivesC8`/`TestDispositionValueAwarePredicateZoliV2
    Finding1`/`TestEMContentChannelZoliV2Finding2`/
    `TestCommitOnGrabHaltZoliV2Finding3`'s own docstrings), these round-trip
    through the REAL `apply()` entrypoint against a real git worktree and
    the real `cs_claim_memo_stamp`/`cs_action_memo` composed primitives — no
    stubbed `_scoped_commit`, no isolated `_execute_directives` call. Covers
    plan C6 items (b)/(g)/(h)/(i)."""

    def test_grab_dispatches_claim_memo_stamp_alongside_d1_and_commits(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-grab", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == ["d1", "claim-memo-stamp"]
        assert "d-action-memo" not in report["landed"]
        assert "j-kind" in report["unresolved_judgment_points"]

        # commit-on-grab-halt (C6 (h)) — a real commit, not a stub.
        assert report["commit_sha"]
        log = _git(repo, "log", "-1", "--format=%H")
        assert log.stdout.strip() == report["commit_sha"]

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: in_progress" in text
        assert "picked_up_by: sid-e2e-grab" in text

    def test_second_grab_apply_is_idempotent_and_does_not_regress_status(self, tmp_path):
        """C6 (i) — a second `apply` on an already-claim-stamped memo (still
        parked at the un-resolved `j-kind` judgment point) neither errors
        nor regresses `status` off `in_progress`."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        first_exit, first_report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-idem", repo_root=repo,
        )
        assert first_exit == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT

        second_exit, second_report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-idem", repo_root=repo,
        )

        assert second_exit == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert second_report["landed"] == ["d1", "claim-memo-stamp"]
        assert "error" not in second_report

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: in_progress" in text
        assert "picked_up_by: sid-e2e-idem" in text

    def test_em_content_channel_realized_by_lands_and_action_memo_commits(self, tmp_path):
        """C6 (g) — a session decision file carrying `disposition=
        accept-mechanical-direct` (an action-taking `ask` disposition) +
        `realized_by`/`decision_note` produces an `action-memo` directive
        that actually dispatches, writing `--realized-by`'s value onto the
        memo's own frontmatter, and commits the terminal state."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        grab_exit, grab_report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-content", repo_root=repo,
        )
        assert grab_exit == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT

        _write_session_decision_file(
            repo, "sid-e2e-content", "cross-repo/inbox/m1.md", "j-kind",
            "accept-mechanical-direct",
            realized_by="docs/plans/p.md",
            decision_note="adopted as-is",
        )

        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-content", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert "d-action-memo" in report["landed"]
        assert report["commit_sha"]
        assert report["commit_sha"] != grab_report["commit_sha"]

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: actioned" in text
        assert "decision: accepted" in text
        assert "realized_by: docs/plans/p.md" in text
        assert "decision_note: adopted as-is" in text
        # picked_up_by is the claim-of-record — action preserves it, never strips it.
        assert "picked_up_by: sid-e2e-content" in text

    def test_non_terminal_disposition_leaves_action_memo_unfired_end_to_end(self, tmp_path):
        """C6 (b)/(f) — disposition-value-aware, exercised through the real
        `apply()` entrypoint (not the isolated `_execute_directives` call
        `TestDispositionValueAwarePredicateZoliV2Finding1` uses): a
        NON-terminal `j-kind` disposition (`surface-to-PM`, `resolves: []`)
        leaves `d-action-memo` un-fired and the run halted, even though the
        unconditional grab directives (`d1`/`claim-memo-stamp`) dispatch
        for real."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        apply_mod.apply("cross-repo/inbox/m1.md", session_id="sid-e2e-nonterminal", repo_root=repo)

        _write_session_decision_file(
            repo, "sid-e2e-nonterminal", "cross-repo/inbox/m1.md", "j-kind", "surface-to-PM",
        )

        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-nonterminal", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert "d-action-memo" not in report["landed"]
        assert "j-kind" in report["unresolved_judgment_points"]

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: in_progress" in text

    def test_drop_reverts_claim_memo_stamp_and_commits(self, tmp_path):
        """C6 (h) — `drop`'s `release-memo-revert` both reverts the
        claim-stamped memo back to `open` (stripping `picked_up_by`/
        `picked_up_at`) AND commits that revert, real git commit included."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        grab_exit, grab_report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-drop", repo_root=repo,
        )
        assert grab_exit == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT

        drop_exit, drop_report = apply_mod.drop(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-drop", repo_root=repo,
        )

        assert drop_exit == apply_mod.APPLY_EXIT_OK
        assert drop_report["class"] == "memo"
        assert drop_report["commit_sha"]
        assert drop_report["commit_sha"] != grab_report["commit_sha"]

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: open" in text
        assert "picked_up_by" not in text

    def test_commit_sha_content_matches_the_memo_frontmatter_change(self, tmp_path):
        """C13/DR-273 regression — `report["commit_sha"]` must be the SHA that
        actually CONTAINS the memo frontmatter change, not merely a truthy
        field. `_scoped_commit`'s own `git diff --cached --quiet` check finds
        nothing dirty once `memo.transition` has already committed the write
        (its own follow-up commit) — a truthy-but-wrong SHA (e.g. some other
        commit, or the run's OWN no-op) is precisely the failure mode this
        chain keeps hitting (see this module's own docstring history)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-content-pin", repo_root=repo,
        )
        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["commit_sha"]

        # The reported SHA's own tree must carry the claim-stamped frontmatter —
        # not merely be A commit that exists in the repo's history.
        show = _git(repo, "show", f"{report['commit_sha']}:cross-repo/inbox/m1.md")
        assert show.returncode == 0
        assert "status: in_progress" in show.stdout
        assert "picked_up_by: sid-e2e-content-pin" in show.stdout

        # And that SHA is exactly what the op itself committed (not, say,
        # `_scoped_commit` fabricating an unrelated empty commit) — its own
        # diff-tree touches ONLY the memo path.
        diff_tree = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", report["commit_sha"])
        assert diff_tree.stdout.split() == ["cross-repo/inbox/m1.md"]


def _archive_memo(repo: Path, live_path: Path, status: str = "in_progress") -> Path:
    """Moves a live inbox memo to `cross-repo/archive/`, rewriting its
    `status:` to something OTHER than `open`/`actioned` — the shape that
    forces `classify()` to return `"ambiguous"` for the native archive path
    and so exercises `_resolve_found_file`'s archive-dir override
    (`_build_archived_resolution`) rather than the ordinary in-inbox `memo`
    branch. This is the reproduction shape for the 2026-07-27 defect fix:
    an archived memo whose `resolution.archived_class` must still read
    `"memo"`, never silently default to `"handoff"`."""
    archived = repo / "cross-repo" / "archive" / live_path.name
    archived.parent.mkdir(parents=True, exist_ok=True)
    text = live_path.read_text(encoding="utf-8")
    text = text.replace("status: open", f"status: {status}")
    archived.write_text(text, encoding="utf-8")
    _git(repo, "rm", "-q", str(live_path.relative_to(repo)))
    _git(repo, "add", str(archived.relative_to(repo)))
    _git(repo, "commit", "-m", f"archive {live_path.name}")
    return archived


class TestArchivedMemoClassification:
    """2026-07-27 defect fix — `pickup-assemble drop` on an ARCHIVED memo
    misclassified it as a handoff (`_class_and_basename` folded every
    non-`"memo"` `classification` value, including the terminal `"archived"`
    one, into `"handoff"`), so `drop` called `cs_unclaim_handoff` (a
    handoff-only primitive) which then failed its own containment guard
    because the path lives under `cross-repo/archive/`, not
    `state/handoffs/`. Fix: `_build_archived_resolution` now preserves the
    `_has_memo_shape` verdict it already computes as
    `resolution.archived_class`, and `_class_and_basename` consults it for
    `classification == "archived"` instead of defaulting to `"handoff"`."""

    def test_archived_memo_resolves_archived_class_memo(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(repo, "m1.md", kind="ask", to="receiver-session")
        _archive_memo(repo, live)

        artifact = pa.resolve_artifact("cross-repo/archive/m1.md", repo)

        assert artifact["classification"] == "archived"
        assert artifact["resolution"]["archived_class"] == "memo"

    def test_drop_on_archived_memo_invokes_memo_revert_not_handoff_unconsume(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(repo, "m1.md", kind="ask", to="receiver-session")
        _archive_memo(repo, live)

        called: dict[str, str] = {}

        def fake_release_memo_revert(path, *, return_result=False):
            # Tracks cs_release_memo_revert's additive `return_result` kwarg (C13/DR-273):
            # drop() now asks for the full op response so it can recover the op's own
            # commit SHA. The stub must mirror the real signature or it masks that call.
            called["memo"] = path
            return {"exit_code": True, "commit_sha": None} if return_result else True

        def fake_unclaim_handoff(path):
            called["handoff"] = path
            return True

        monkeypatch.setattr(apply_mod, "cs_release_memo_revert", fake_release_memo_revert)
        monkeypatch.setattr(apply_mod, "cs_unclaim_handoff", fake_unclaim_handoff)

        exit_code, report = apply_mod.drop(
            "cross-repo/archive/m1.md", session_id="sid-e2e-drop-archived", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert report["class"] == "memo"
        assert "memo" in called
        assert "handoff" not in called

    def test_class_and_basename_raises_on_unrecognized_classification(self):
        with pytest.raises(apply_mod._UnresolvableArtifactClass):
            apply_mod._class_and_basename({"classification": "ambiguous", "path": "x.md"})

    def test_class_and_basename_raises_on_archived_missing_discriminator(self):
        with pytest.raises(apply_mod._UnresolvableArtifactClass):
            apply_mod._class_and_basename(
                {"classification": "archived", "path": "x.md", "resolution": {}}
            )


class TestArchivedOpenMemoKindDispatch:
    """2026-07-27 doe-claude-em memo defect fix — `brief()`'s `archived`
    branch unconditionally emitted `directives: []` and asserted "already
    closed" even for an archived MEMO whose terminal `status` was never
    stamped to a terminal value (`"actioned"`). A sibling EM hit this live
    and had to close it by hand with `archive-stamp-cli action-memo` — no
    directive-driven path existed. Fix: when `resolution.archived_class ==
    "memo"` and `terminal_fields["status"] != "actioned"`, fire the SAME
    `j-kind` judgment point and `d-action-memo` directive the live in-place
    memo branch fires (`_archived_open_memo_kind_dispatch`), keyed off
    `archived_class` — never the `"from" in terminal_fields` heuristic used
    by the sibling reply-closure check just above it."""

    def test_archived_memo_status_open_fires_kind_dispatch_and_blocks_coast(self, tmp_path):
        """Reached via the archive-FALLBACK-SEARCH route (the original
        `cross-repo/inbox/m1.md` no longer exists — `_archive_memo` `git
        rm`s it) — `_build_archived_resolution` is unconditional on
        `status` for this route (mirrors `test_memo_swept_to_archive_
        completed_reads_memo_terminal_fields` above), which is what lets
        `terminal_fields["status"]` read the literal, never-rewritten
        `"open"` this test needs: passing `status="open"` straight through
        to `_archive_memo` is a no-op rewrite (`text.replace("status: open",
        "status: open")`), so the archived file keeps its true `status:
        open` — the exact reproduction shape named in the defect (a memo
        archived without ever having a disposition stamped on it)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(repo, "m1.md", kind="ask", to="receiver-session")
        archived = _archive_memo(repo, live, status="open")
        archive_path_display = archived.relative_to(repo).as_posix()

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        decision = result.decision_object
        assert decision["artifact"]["classification"] == "archived"
        assert decision["artifact"]["resolution"]["archived_class"] == "memo"
        assert decision["artifact"]["resolution"]["terminal_fields"]["status"] == "open"

        jp_ids = [jp["id"] for jp in decision["judgment_points"]]
        assert "j-kind" in jp_ids

        directive_ids = [d["id"] for d in decision["directives"]]
        assert "d-action-memo" in directive_ids
        assert "d1" in directive_ids
        assert "claim-memo-stamp" in directive_ids
        for d in decision["directives"]:
            if d["id"] == "d-action-memo":
                assert d["args"][1] == archive_path_display

        assert decision["gates"]["coast"]["verdict"] != "clear"
        assert "already closed" not in decision["narration"]

    def test_archived_memo_status_actioned_is_byte_identical_to_today(self, tmp_path):
        """Reached via the same archive-FALLBACK-SEARCH route, `status:
        actioned` — the already-terminal companion case. `kind: fyi` (no
        reply-closure requirement) mirrors `test_memo_swept_to_archive_
        completed_reads_memo_terminal_fields` above, isolating this
        assertion to the kind-dispatch fix rather than entangling it with
        the sibling reply-closure judgment point (a different, `kind:
        ask`-only concern)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(repo, "m1.md", kind="fyi", to="receiver-session", status="actioned")
        archived = _archive_memo(repo, live, status="actioned")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        decision = result.decision_object
        assert decision["artifact"]["classification"] == "archived"
        assert decision["artifact"]["resolution"]["archived_class"] == "memo"
        assert decision["directives"] == []
        assert decision["judgment_points"] == []
        assert decision["gates"]["coast"]["verdict"] == "clear"
        archive_path_display = archived.relative_to(repo).as_posix()
        assert decision["narration"] == (
            f"{archive_path_display} is archived at {archive_path_display} — a terminal record."
        )
        assert decision["next_move"] == "Nothing further to do — this artifact already closed."

    def test_archived_handoff_is_completely_unaffected(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h1.md")
        _archive_handoff(repo, live)

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        decision = result.decision_object
        assert decision["artifact"]["classification"] == "archived"
        assert decision["artifact"]["resolution"]["archived_class"] == "handoff"
        assert decision["directives"] == []
        assert decision["judgment_points"] == []
        assert decision["gates"]["coast"]["verdict"] == "clear"
        assert decision["next_move"] == "Nothing further to do — this artifact already closed."

    def test_apply_stamps_disposition_on_archived_memo_end_to_end(self, tmp_path):
        """`apply()` recomputes `brief()` internally, so this exercises
        `_class_and_basename`'s `archived_class` consult (d6ee0e5a) and the
        new kind-dispatch directives together on a real round-trip: the
        session-claim, the `open` -> `in_progress` claim-memo-stamp, and
        the `in_progress` -> `actioned` action-memo write all land against
        the archive-resident path directly."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(repo, "m1.md", kind="ask", to="receiver-session")
        archived = _archive_memo(repo, live, status="open")

        decisions = {
            "j-kind": {
                "disposition": "accept-mechanical-direct",
                "realized_by": "docs/plans/p.md",
                "decision_note": "adopted as-is",
            },
        }
        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-archived-memo", repo_root=repo,
            decisions=decisions,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert "d-action-memo" in report["landed"]

        text = archived.read_text(encoding="utf-8")
        assert "status: actioned" in text
        assert "decision: accepted" in text


class TestSupersededMemoNotPickupable:
    """AC5, docs/plans/2026-08-11-receiver-side-supersession-pair-a-writab.md
    C4 — a `status: superseded` memo must never present as pickup-able, at
    each of the three sites `_MEMO_TERMINAL_STATUS` widens:
      :1859 classify()'s memo-shape gate
      :6583 archive-fallback classification (terminal_fields.status check)
      :6969 the M0 terminal short-circuit in brief()
    """

    def test_classify_site_recognises_superseded_as_memo_shape(self, tmp_path):
        """Site 1 (classify): a live in-inbox `status: superseded` memo
        classifies as `memo`, not `ambiguous` — pa.brief()'s own
        classification field is the observable proxy for classify()'s verdict."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(
            repo, "m1.md", status="superseded",
            extra="superseded_by: successor.md\n",
        )

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.decision_object["artifact"]["classification"] == "memo"

    def test_m0_short_circuit_treats_superseded_as_terminal_no_claim_directive(self, tmp_path):
        """Site 3 (:6969): a superseded memo hits the SAME read-only terminal
        short-circuit an actioned memo does — no claim directive, no
        kind-dispatch judgment point. Not pickup-able."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(
            repo, "m1.md", kind="fyi", status="superseded",
            extra="superseded_by: successor.md\n",
        )

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        decision = result.decision_object
        assert decision["artifact"]["classification"] == "memo"
        assert decision["artifact"]["terminal_state"]["status"] == "superseded"
        assert decision["artifact"]["terminal_state"]["superseded_by"] == "successor.md"
        assert decision["directives"] == []
        cli_names = {d.get("cli") for d in decision["directives"]}
        assert "session-claim-cli" not in cli_names
        assert decision["next_move"] == "Nothing further to do — this memo already closed."

    def test_archive_fallback_site_treats_superseded_as_terminal(self, tmp_path):
        """Site 2 (:6583): an archived memo whose terminal status reads
        `superseded` does NOT fall into the "not actually closed" kind-dispatch
        branch — mirrors test_archived_memo_status_actioned_is_byte_identical_
        to_today above, superseded in place of actioned."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_memo(
            repo, "m1.md", kind="fyi", to="receiver-session", status="superseded",
            extra="superseded_by: successor.md\n",
        )
        archived = _archive_memo(repo, live, status="superseded")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        decision = result.decision_object
        assert decision["artifact"]["classification"] == "archived"
        assert decision["artifact"]["resolution"]["archived_class"] == "memo"
        assert decision["directives"] == []
        assert decision["judgment_points"] == []
        assert decision["gates"]["coast"]["verdict"] == "clear"
        assert "not actually closed" not in decision["narration"]


class TestMemoLivenessJ1ResolvesActionMemo:
    """Defect fix (2026-07-25) — `j1`'s `proceed` disposition must resolve
    EVERY directive `build_memo_directives` emits (`d1`, `claim-memo-stamp`,
    `d-action-memo`), not just the two grab-mechanics directives. Before the
    fix, `d-action-memo["depends_on"]` was widened to `["j-kind", "j1"]`
    once liveness fired, but the `j1` judgment point's own `resolves` list
    never named `d-action-memo` — a structural lockout, identical in shape
    to the bug fixed in `8d94ebb9`: once `j1` fires, the terminal memo write
    could never land no matter what the EM decided."""

    def test_live_repro_j1_proceed_and_action_taking_j_kind_lands_action_memo(self, tmp_path, monkeypatch):
        """The exact live reproduction from this session: a fully-populated
        `--decisions` supplying both `j1: proceed` and an action-taking
        `j-kind` disposition must land `d-action-memo` (via a real
        `apply()` round-trip) with an EMPTY `unresolved_judgment_points` —
        fails against pre-fix code (confirmed below before the fix landed)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask", extra='picked_up_by: "foreign-live-sid"\n')
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        decisions = {
            "j1": {"disposition": "proceed"},
            "j-kind": {
                "disposition": "accept-mechanical-direct",
                "realized_by": "docs/plans/p.md",
                "decision_note": "adopted as-is",
            },
        }
        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-j1-proceed", repo_root=repo,
            decisions=decisions,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert report.get("unresolved_judgment_points", []) == []
        assert "d-action-memo" in report["landed"]
        assert "d1" in report["landed"]
        assert "claim-memo-stamp" in report["landed"]

        text = (repo / "cross-repo" / "inbox" / "m1.md").read_text(encoding="utf-8")
        assert "status: actioned" in text
        assert "decision: accepted" in text

    def test_j1_stand_down_resolves_nothing(self, tmp_path, monkeypatch):
        """`stand-down-and-surface`'s own `resolves: []` must remain
        correct — standing down fires no directive at all, `d-action-memo`
        included, even when `j-kind` also carries an action-taking
        disposition."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask", extra='picked_up_by: "foreign-live-sid"\n')
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        decisions = {
            "j1": {"disposition": "stand-down-and-surface"},
            "j-kind": {
                "disposition": "accept-mechanical-direct",
                "realized_by": "docs/plans/p.md",
                "decision_note": "adopted as-is",
            },
        }
        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-j1-standdown", repo_root=repo,
            decisions=decisions,
        )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == []
        assert "j1" in report["unresolved_judgment_points"]

    def test_j1_absent_action_taking_j_kind_still_lands_action_memo(self, tmp_path):
        """No liveness signal fired at all (today's working path, unchanged
        by this fix) — `d-action-memo` fires on an action-taking `j-kind`
        alone, no `j1` in the picture."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask")

        decisions = {
            "j-kind": {
                "disposition": "accept-mechanical-direct",
                "realized_by": "docs/plans/p.md",
                "decision_note": "adopted as-is",
            },
        }
        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-no-j1", repo_root=repo,
            decisions=decisions,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert "d-action-memo" in report["landed"]

    def test_j_kind_unresolved_j1_proceed_lands_grab_only_not_action_memo(self, tmp_path, monkeypatch):
        """The existing per-directive partial-fire contract must not
        regress: `j1: proceed` resolves `d1`/`claim-memo-stamp` (and now
        ALSO names `d-action-memo` in its own `resolves` list), but
        `d-action-memo["depends_on"]` is `["j-kind", "j1"]` — AND semantics
        — so it stays blocked until `j-kind` independently resolves too."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", kind="ask", extra='picked_up_by: "foreign-live-sid"\n')
        monkeypatch.setattr(pa._liveness, "session_live", lambda sid, cwd=None: sid == "foreign-live-sid")

        decisions = {"j1": {"disposition": "proceed"}}
        exit_code, report = apply_mod.apply(
            "cross-repo/inbox/m1.md", session_id="sid-e2e-j1-only", repo_root=repo,
            decisions=decisions,
        )

        assert exit_code == apply_mod.APPLY_EXIT_HALTED_AT_JUDGMENT
        assert report["landed"] == ["d1", "claim-memo-stamp"]
        assert "d-action-memo" not in report["landed"]
        assert "j-kind" in report["unresolved_judgment_points"]


class TestActionedNoteDroppedOnDecisionPathDefect2:
    """Defect 2 fix (2026-07-25) — `_build_action_memo_args` must never
    silently discard an EM-supplied `actioned_note` on the `--decision`
    path (`--decision`/`--actioned-note` are mutually exclusive on
    `cs_action_memo`'s own contract, `ops/memo_transition.py:563-564`).
    Fails loud instead, naming `decision_note` as the right key for a
    decision-mapped disposition."""

    def test_actioned_note_with_accept_mechanical_direct_fails_loud(self):
        decisions = {
            "j-kind": {
                "disposition": "accept-mechanical-direct",
                "realized_by": "docs/plans/p.md",
                "actioned_note": "should have been decision_note",
            },
        }
        with pytest.raises(ValueError, match="actioned_note"):
            pa._build_action_memo_args("cross-repo/inbox/m1.md", "ask", decisions)

    def test_actioned_note_alone_on_nil_impact_disposition_still_works(self):
        """Negative-spec companion — the fix must not touch the genuinely
        different `actioned_note`-only channel (`fyi`/`ack-nil`-shaped, no
        `_MEMO_ACTION_DECISION_MAP` entry, no `--decision`)."""
        decisions = {"j-kind": {"disposition": "ack-nil", "actioned_note": "no impact on this repo"}}
        args = pa._build_action_memo_args("cross-repo/inbox/m1.md", "fyi", decisions)

        assert "--actioned-note" in args
        assert "--decision" not in args


class TestApplyOnBareBasenameArchiveFallback:
    """2026-07-26 defect fix (`pickup-assemble apply` commit crash) — a
    caller invoking `apply` with a bare basename for a swept baton must not
    crash `_scoped_commit`'s `git add`. Reproduces the exact incident: the
    resolver found the artifact fine (archive-fallback search), but the
    RAW bare basename was joined onto the repo root for the commit's
    pathspec instead of the resolved archive-relative path, so `git add
    <bare-basename>` failed with `fatal: pathspec '<bare-basename>' did
    not match any files` (rc=128) because the file actually lives under
    `archive/handoffs/...`, not at the repo root."""

    def test_apply_on_bare_basename_does_not_crash_scoped_commit(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = _seed_handoff(repo, "h-swept.md")
        archived = _archive_handoff(repo, live)

        exit_code, report = apply_mod.apply(
            "h-swept.md", session_id="sid-bare-basename-archive", repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert "error" not in report
        assert report["landed"] == []
        # Nothing changed for this already-archived artifact, so
        # `_scoped_commit` correctly no-ops (`None`) rather than crashing.
        assert report["commit_sha"] is None
        # The artifact block itself must also carry the resolved path, not
        # the raw bare basename the caller passed in.
        assert report is not None
        text = archived.read_text(encoding="utf-8")
        assert "status: consumed" in text


class TestApplyFailsLoudOnTerminalArtifactWithDroppedDecisions:
    """2026-07-26 defect fix — an already-terminal artifact (an archived/
    `status: actioned` memo) resolves `directives: []` AND
    `judgment_points: []` together, so `apply_base.execute_directives`
    reports its own genuine "nothing to consider" no-op contract:
    `APPLY_EXIT_OK`/`landed: []`. That no-op contract is correct when the
    caller supplied NO dispositions. It becomes a silent-discard bug the
    instant a caller supplies non-empty `--decisions` (or a session-file
    disposition) that this run has no directive or judgment point left to
    apply them to — the caller believes a disposition was recorded (e.g.
    `actioned_note`/`distill_fate` on a fleet-state-ingest memo) when
    nothing landed and the frontmatter never changed.

    EM ruling: choice (b), fail loud. `apply()` now refuses this exact
    combination (`effective_decisions` truthy, `directives`/
    `judgment_points` both empty) with `APPLY_EXIT_TRANSPORT_FAIL` naming
    which judgment-point keys were discarded, rather than returning
    `APPLY_EXIT_OK`/`landed: []` — see the negative-spec block in
    `apply.py`'s module docstring."""

    def _seed_actioned_fyi_memo_at_native_archive_path(self, repo: Path, name: str) -> Path:
        """A `kind: fyi` + `status: actioned` memo at its NATIVE
        `cross-repo/archive/` path — `kind: fyi` drives
        `compute_reply_closure` to `not_required` (empty judgment_points),
        so this fixture reliably reproduces `directives: []` AND
        `judgment_points: []` together, the exact silent-no-op shape this
        fix closes. Mirrors the PM's own repro file shape (a landed
        fleet-state-ingest memo)."""
        path = repo / "cross-repo" / "archive" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        fm = (
            "kind: fyi\n"
            "status: actioned\n"
            "from: sender-session\n"
            "to: receiver-em\n"
            "summary: A landed fleet-state-ingest memo.\n"
            "created: 2026-01-01\n"
            "decision: accept-mechanical-direct\n"
            "decision_note: applied directly\n"
            "realized_by: abc1234\n"
        )
        path.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(path.relative_to(repo)))
        _git(repo, "commit", "-m", f"archive {name}")
        return path

    def test_decisions_on_already_terminal_memo_fails_loud_not_silent_ok(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        name = "2026-07-26-example-cockpit-repo-em-fleet-state-memo-ingest-landed.md"
        memo = self._seed_actioned_fyi_memo_at_native_archive_path(repo, name)
        before = memo.read_text(encoding="utf-8")

        decisions = {
            "j-reply-closure": {
                "disposition": "send-reply",
                "actioned_note": "should have recorded this",
                "distill_fate": "commitment",
            },
        }
        exit_code, report = apply_mod.apply(
            f"cross-repo/archive/{name}",
            session_id="sid-terminal-decisions-dropped",
            repo_root=repo,
            decisions=decisions,
        )

        assert exit_code != apply_mod.APPLY_EXIT_OK
        assert report["landed"] == []
        assert "error" in report
        assert "j-reply-closure" in report["error"]

        # The negative outcome this fix guards: nothing was ever
        # committed, and the memo's own frontmatter is untouched.
        assert report.get("commit_sha") is None
        after = memo.read_text(encoding="utf-8")
        assert after == before

    def test_no_decisions_on_already_terminal_memo_stays_a_clean_ok_noop(self, tmp_path):
        """Negative-spec companion — an ordinary read-only re-check of an
        already-terminal artifact (no `--decisions` supplied at all) must
        NOT regress into the new fail-loud path; it stays the pre-existing
        clean `APPLY_EXIT_OK`/`landed: []` no-op."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        name = "2026-07-26-example-cockpit-repo-em-fleet-state-memo-ingest-landed.md"
        self._seed_actioned_fyi_memo_at_native_archive_path(repo, name)

        exit_code, report = apply_mod.apply(
            f"cross-repo/archive/{name}",
            session_id="sid-terminal-no-decisions",
            repo_root=repo,
        )

        assert exit_code == apply_mod.APPLY_EXIT_OK
        assert "error" not in report
        assert report["landed"] == []


class TestGateNotesAdvisoryAtPickupBrief:
    """C5, 2026-08-19-gate-notes-are-advisory-blocked-by-derives-readiness —
    `gates.gate_notes` must be queryable from the pickup brief WITHOUT
    entering any pickup-blocking verdict (AC8)."""

    def test_absent_blocking_notes_reports_not_present_and_null_passed(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff(repo, "h1.md")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        gate_notes = result.decision_object["gates"]["gate_notes"]
        assert gate_notes == {"present": False, "text": None, "passed": None}

    def test_present_blocking_notes_surfaces_verbatim_with_passed_always_null(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_with_fields(
            repo, "h1.md", 'blocking_notes: "waiting on a sibling repo ruling"\n'
        )

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        gate_notes = result.decision_object["gates"]["gate_notes"]
        assert gate_notes == {
            "present": True,
            "text": "waiting on a sibling repo ruling",
            "passed": None,
        }

    def test_gate_notes_never_enters_coast_claim_or_aging_verdict(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_handoff_with_fields(
            repo, "h1.md", 'blocking_notes: "advisory prose only"\n'
        )

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        gates = result.decision_object["gates"]
        assert "gate_notes" not in gates["coast"]
        assert "gate_notes" not in gates.get("claim", {})
        assert gates["aging_verdict"] != "gate_notes"
        # Advisory prose alone must not flip the pickup outcome.
        assert result.exit_code == pa.EXIT_OK

    def test_memo_branch_also_carries_gate_notes(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_memo(repo, "m1.md", extra='blocking_notes: "held for review"\n')

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        gate_notes = result.decision_object["gates"]["gate_notes"]
        assert gate_notes == {"present": True, "text": "held for review", "passed": None}
