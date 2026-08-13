"""
coordinator_core.test_pickup_assemble_reply_closure — co-located pytest for
`compute_reply_closure`/`_render_reply_closure` (the 2026-07-25 reply-closure
defect fix in `coordinator_core.pickup_assemble`).

Regression backstop for the defect documented at that module's "Reply-closure
check (2026-07-25 defect)" section: `cross-repo/archive/2026-07-25-coordinator-claude-
em-test-red-record-contract-consult.md` was `status: actioned` with the reply
written into claude-klabauter's own archived copy — invisible to the sender — and
`brief()` reported `coast=clear`/"Nothing further to do" on both terminal-memo
emit paths. Every test class here targets ONE of those two emit paths (or the
shared predicate/renderer directly) so a future one-site-only fix trips this
suite (§ "Both emit paths" below).

Run: cd /Users/example-operator/X/claude-klabauter && python3 -m pytest coordinator_core/test_pickup_assemble_reply_closure.py -q
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa

_GIT_ENV = None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
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


def _register_sender(settings_home: Path, sender_key: str, sender_root: Path) -> None:
    """Write a minimal machine-local registry mapping `repos.<sender_key>` ->
    `sender_root`, mirroring `_memo_resolver`'s own test fixture idiom
    (`coordinator_core/ops/fleet/tests/test_memo_resolver.py::_make_claude_home`)."""
    machine_local = settings_home / "machine-local"
    machine_local.mkdir(parents=True, exist_ok=True)
    (machine_local / "registry.toml").write_text("schema = 1\n", encoding="utf-8")
    toml_val = str(sender_root).replace("\\", "\\\\").replace('"', '\\"')
    (machine_local / "registry.local.toml").write_text(
        f'"repos.{sender_key}" = "{toml_val}"\n', encoding="utf-8"
    )


def _seed_inbox_memo(
    repo: Path,
    name: str,
    *,
    kind: str | None,
    from_id: str = "sender-em",
    created: str = "2026-07-20",
    status: str = "actioned",
    to: str = "receiver-em",
) -> Path:
    path = repo / "cross-repo" / "inbox" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_lines = []
    if kind is not None:
        fm_lines.append(f"kind: {kind}\n")
    fm_lines += [
        f"from: {from_id}\n",
        f"to: {to}\n",
        f"status: {status}\n",
        f"created: {created}\n",
        "summary: A test memo.\n",
        'actioned_note: "Replied — see sender tree."\n',
        "decision: accepted\n",
        "realized_by: abc1234\n",
    ]
    path.write_text(f"---\n{''.join(fm_lines)}---\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _archive_memo(repo: Path, live_path: Path, archive_rel: str) -> Path:
    """Move a live inbox memo to `archive_rel` (a `cross-repo/archive/...`
    path), reproducing the archived-fallback shape `resolve_artifact` finds."""
    archived = repo / archive_rel
    archived.parent.mkdir(parents=True, exist_ok=True)
    archived.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    _git(repo, "rm", "-q", str(live_path.relative_to(repo)))
    _git(repo, "add", str(archived.relative_to(repo)))
    _git(repo, "commit", "-m", f"archive {live_path.name}")
    return archived


def _seed_sender_reply(
    sender_root: Path,
    rel_dir: str,
    name: str,
    *,
    from_id: str = "repo-em",
    created: str = "2026-07-21",
    in_reply_to: str | None = None,
    body: str = "Reply to your m.md request. Details follow.",
) -> Path:
    """Write a candidate reply memo directly into a (non-git, disk-only)
    sender-tree fixture — `compute_reply_closure` reads from disk, it never
    requires the sender tree to itself be a git repo.

    Cites the (default) inbound memo's basename in `body` by default, so a
    caller that wants an UNLINKED (same-sender/same-day but not citing)
    fixture must pass `body=` explicitly — the linkage requirement is the
    thing under test in most of this file, so "linked" is the safer default.
    """
    path = sender_root / rel_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f"from: {from_id}\n"
        "to: sender-em\n"
        f"created: {created}\n"
        "status: open\n"
        "summary: Our reply.\n"
    )
    if in_reply_to is not None:
        fm += f"in_reply_to: {in_reply_to}\n"
    path.write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# compute_reply_closure — direct predicate tests
# ---------------------------------------------------------------------------

class TestComputeReplyClosurePredicate:
    def test_fyi_kind_is_not_required(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        closure = pa.compute_reply_closure(
            {"kind": "fyi", "from": "sender-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] == "not_required"

    def test_absent_kind_treated_as_ask_reply_required(self, tmp_path, monkeypatch):
        """No `kind:` line -> treated as `ask` (reply required) per the
        cross-repo-memo CLI's own documented reader default."""
        repo = tmp_path / "repo"
        repo.mkdir()
        settings_home = tmp_path / "settings-home"
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        # sender-em unregistered on this machine -> unknown, not silently open;
        # either way `kind` absent must NOT short-circuit to not_required.
        closure = pa.compute_reply_closure(
            {"from": "sender-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] in ("open", "unknown")

    def test_consult_no_reply_in_sender_tree_is_open(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert closure["reason"]

    def test_reply_present_in_sender_inbox_is_evidenced(self, tmp_path, monkeypatch):
        # Inbound basename must clear `_MIN_LINK_STEM_LENGTH` (10 chars) for
        # the default cited-in-body fixture to count as LINKED — a bare
        # "m.md" is exactly the degenerate-stem shape the floor exists to
        # reject (see `_MIN_LINK_STEM_LENGTH`'s comment), so it would no
        # longer be "genuinely evidenced" under the tightened contract.
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        inbound_basename = "2026-07-20-sender-em-topic-consult.md"
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-21",
            body=f"Reply to your {inbound_basename} request. Details follow.",
        )
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, inbound_basename, repo
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]
        # Candidates are relative to the SENDER's tree; the rendered citation
        # has to say so, or the reader searches its own cross-repo/ in vain.
        assert closure["sender_root"] == str(sender_root)
        _jps, narration, _next_move = pa._render_reply_closure(closure, inbound_basename, "Base.", "Next.")
        assert f"{sender_root}/cross-repo/inbox/answers.md" in narration

    def test_reply_present_in_sharded_archive_subdir_is_evidenced(self, tmp_path, monkeypatch):
        """`cross-repo/archive/YYYY-MM/...` sharding — must be walked
        recursively, not string-matched against a flat directory listing."""
        # Same `_MIN_LINK_STEM_LENGTH` rationale as the sibling test above —
        # the inbound basename must clear the floor for the cited-in-body
        # linkage to count.
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        inbound_basename = "2026-07-20-sender-em-topic-consult.md"
        _seed_sender_reply(
            sender_root,
            "cross-repo/archive/2026-07",
            "answers.md",
            created="2026-07-21",
            body=f"Reply to your {inbound_basename} request. Details follow.",
        )
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, inbound_basename, repo
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/archive/2026-07/answers.md"]

    def test_reply_dated_before_inbound_created_is_not_a_candidate(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        # Reply predates the inbound memo -> can't be the reply to THIS memo.
        _seed_sender_reply(sender_root, "cross-repo/inbox", "old.md", created="2026-07-01")
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "ask", "from": "sender-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []

    def test_unresolvable_sender_repo_is_unknown_distinct_from_open(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        settings_home = tmp_path / "settings-home"
        settings_home.mkdir()
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "nobody-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] == "unknown"
        assert closure["reason"]

    def test_sender_repo_with_no_cross_repo_tree_is_unknown(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()  # no cross-repo/ tree at all
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "ask", "from": "sender-em", "created": "2026-07-20"}, "m.md", repo
        )
        assert closure["verdict"] == "unknown"

    def test_missing_from_or_created_is_unknown(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        closure = pa.compute_reply_closure({"kind": "ask"}, "m.md", repo)
        assert closure["verdict"] == "unknown"


# ---------------------------------------------------------------------------
# brief() wiring — both terminal-memo emit paths (regression guard against
# the one-site-only fix: the original defect was reproduced on the
# archived-fallback path even after the actioned-in-place path was patched
# in an earlier draft).
# ---------------------------------------------------------------------------

class TestBriefActionedInPlaceEmitPath:
    def test_open_reply_surfaces_judgment_point_and_actionable_next_move(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        _seed_inbox_memo(repo, "m1.md", kind="consult")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert "j-reply-closure" in jp_ids
        assert obj["gates"]["coast"]["verdict"] == "blocked"
        assert "cross-repo-memo" in obj["next_move"]
        assert "Nothing further to do" not in obj["next_move"]

    def test_open_next_move_names_in_reply_to_flag_and_basename(self, tmp_path, monkeypatch):
        """2026-07-27 defect: the `open` next_move used to say "send the
        reply" with no mention of `--in-reply-to` — a real reply sent
        without it comes back `open` again on the next pickup because the
        linkage scan (`_candidate_is_linked`) has no other way to confirm
        it. The next_move must name the exact flag AND the inbound memo's
        own basename."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        _seed_inbox_memo(repo, "m1.md", kind="consult")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        assert "--in-reply-to m1.md" in obj["next_move"]

    def test_evidenced_reply_stays_terminal_zero_judgment_points(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        _seed_sender_reply(
            sender_root, "cross-repo/inbox", "answers.md", created="2026-07-21", in_reply_to="m1.md"
        )
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        _seed_inbox_memo(repo, "m1.md", kind="consult", created="2026-07-20")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["judgment_points"] == []
        assert obj["gates"]["coast"]["verdict"] == "clear"
        assert "Nothing further to do" in obj["next_move"]

    def test_fyi_kind_stays_terminal_unchanged(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _seed_inbox_memo(repo, "m1.md", kind="fyi")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["judgment_points"] == []
        assert obj["next_move"] == "Nothing further to do — this memo already closed."


class TestBriefArchivedFallbackEmitPath:
    """The exact emit path the originating 2026-07-25 defect hit: a memo
    swept to `cross-repo/archive/` reads its `kind`/`from`/`created` from
    `resolution.terminal_fields` (`_build_archived_resolution` hands the
    branch an empty `frontmatter: {}`), not from a live `fm` dict."""

    def test_open_reply_surfaces_judgment_point_on_archived_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        live = _seed_inbox_memo(repo, "m1.md", kind="consult")
        _archive_memo(repo, live, "cross-repo/archive/2026-07/m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        assert result.exit_code == pa.EXIT_OK
        obj = result.decision_object
        assert obj["artifact"]["classification"] == "archived"
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert "j-reply-closure" in jp_ids
        assert obj["gates"]["coast"]["verdict"] == "blocked"
        assert "Nothing further to do" not in obj["next_move"]

    def test_evidenced_reply_stays_terminal_on_archived_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        # Same sender, later date, AND cited in-reply_to the inbound memo's
        # basename — genuinely LINKED, so `evidenced` is correct under the
        # tightened contract (bare same-day co-occurrence, as this test used
        # to seed, is `open` post-tightening — see the module comment on
        # `_inbound_link_stems`).
        _seed_sender_reply(
            sender_root, "cross-repo/inbox", "answers.md", created="2026-07-21", in_reply_to="m1.md"
        )
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        live = _seed_inbox_memo(repo, "m1.md", kind="consult", created="2026-07-20")
        _archive_memo(repo, live, "cross-repo/archive/2026-07/m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["artifact"]["classification"] == "archived"
        assert obj["judgment_points"] == []
        assert obj["gates"]["coast"]["verdict"] == "clear"

    def test_open_question_reflects_actual_status_not_hardcoded_actioned(self, tmp_path, monkeypatch):
        """2026-07-27 defect: the `open` judgment-point question and
        narration hardcoded the literal `status: actioned` regardless of
        what was actually on disk. A memo archived while still
        `status: open` (swept but never stamped with a disposition) got a
        question asserting `status: actioned`, which is false and
        misdirects the reader into believing the memo's own disposition
        was already handled. The rendered text must name the real status."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True)
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        live = _seed_inbox_memo(repo, "m1.md", kind="consult", status="open")
        _archive_memo(repo, live, "cross-repo/archive/2026-07/m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        jp_ids = {jp["id"]: jp for jp in obj["judgment_points"]}
        assert "status: open" in jp_ids["j-reply-closure"]["question"]
        assert "status: actioned" not in jp_ids["j-reply-closure"]["question"]
        assert "status: open" in obj["narration"]
        assert "status: actioned" not in obj["narration"]

    def test_archived_handoff_is_unaffected_no_reply_closure_noise(self, tmp_path):
        """A handoff swept to `archive/handoffs/` has no `from:`/`to:` shape
        at all — `terminal_fields` never carries `from`, so this branch must
        not spuriously fire an `unknown` reply-closure judgment point on
        every archived handoff."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        live = repo / "state" / "handoffs" / "h1.md"
        live.parent.mkdir(parents=True)
        fm = (
            'title: "Test Handoff"\n'
            "created: 2026-01-01\n"
            "branch: work/test/2026-01-01\n"
            "status: open\n"
            'predecessor: "none"\n'
            "deployment_state: active\n"
        )
        live.write_text(f"---\n{fm}---\n\nBody.\n", encoding="utf-8")
        _git(repo, "add", str(live.relative_to(repo)))
        _git(repo, "commit", "-m", "add h1")
        archived = repo / "archive" / "handoffs" / "2026-01" / "h1.md"
        archived.parent.mkdir(parents=True)
        text = live.read_text(encoding="utf-8").replace("status: open", "status: consumed").replace(
            "deployment_state: active", "deployment_state: shipped\nshipped_in: deadbeef"
        )
        archived.write_text(text, encoding="utf-8")
        _git(repo, "rm", "-q", str(live.relative_to(repo)))
        _git(repo, "add", str(archived.relative_to(repo)))
        _git(repo, "commit", "-m", "archive h1")

        result = pa.brief("state/handoffs/h1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["artifact"]["classification"] == "archived"
        assert obj["judgment_points"] == []
        assert obj["next_move"] == "Nothing further to do — this artifact already closed."


# ---------------------------------------------------------------------------
# Linkage tightening — the 2026-07-25 second-pass defect this file's
# `test_evidenced_reply_stays_terminal_on_archived_path` used to reproduce:
# date-only ("same sender, same/later day") matching is NOT evidence.
# ---------------------------------------------------------------------------

class TestLinkageRegressionGuard:
    """The escaped-to-prod shape: a busy fleet day where several same-day
    outbound memos to the sender exist, none of them the actual reply.
    On the live tree this produced 28 spurious `evidenced` candidates and
    suppressed a genuinely-unanswered consult — this is that shape,
    minimized."""

    def test_several_same_day_replies_none_citing_is_open_not_evidenced(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        for i in range(3):
            _seed_sender_reply(
                sender_root,
                "cross-repo/inbox",
                f"unrelated-{i}.md",
                created="2026-07-20",
                body="An unrelated memo about something else entirely.",
            )
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"},
            "2026-07-20-sender-em-q.md",
            repo,
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert len(closure["unconfirmed_candidates"]) == 3


class TestLinkageMatching:
    """Each of the three linkage paths `_candidate_is_linked` recognizes,
    exercised directly against `compute_reply_closure`."""

    def _setup(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        return repo, sender_root

    def test_linked_via_in_reply_to_frontmatter(self, tmp_path, monkeypatch):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-21",
            in_reply_to="2026-07-20-sender-em-q.md",
            body="No textual citation here at all.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"},
            "2026-07-20-sender-em-q.md",
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]

    def test_linked_via_full_basename_cited_in_body(self, tmp_path, monkeypatch):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-21",
            body="Replied fully in 2026-07-20-sender-em-q.md's thread.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"},
            "2026-07-20-sender-em-q.md",
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]

    def test_linked_via_tail_stem_elided_citation(self, tmp_path, monkeypatch):
        """The elision shape the real reply that closed the originating
        defect actually used — citing the memo with the date AND sender-id
        segment elided by a literal `…`, leaving only the tail stem. A
        whole-basename match misses this; the tail stem exists precisely
        for it."""
        # Deliberately NOT `self._setup` — `coordinator-claude-em` resolves through
        # `receiver_em_to_repo_key`'s convention path (strip `-em`, dashes to
        # underscores) to `repos.example_doctrine_repo`, a different registry key than
        # the shared helper's `repos.sender`.
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "example_doctrine_repo", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        inbound_basename = "2026-07-25-coordinator-claude-em-test-red-record-contract-consult.md"
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers-q1-q2-q3.md",
            created="2026-07-25",
            body="Reply to your `2026-07-25-…-test-red-record-contract-consult.md` — see Q1/Q2/Q3 below.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "coordinator-claude-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers-q1-q2-q3.md"]

    def test_citation_match_is_case_insensitive(self, tmp_path, monkeypatch):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-21",
            body="RE: 2026-07-20-SENDER-EM-Q.MD — answered above.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"},
            "2026-07-20-sender-em-q.md",
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]

    def test_one_linked_among_several_unlinked_is_evidenced_with_only_the_linked_candidate(
        self, tmp_path, monkeypatch
    ):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        inbound_basename = "2026-07-20-sender-em-q.md"
        for i in range(3):
            _seed_sender_reply(
                sender_root,
                "cross-repo/inbox",
                f"unrelated-{i}.md",
                created="2026-07-20",
                body="An unrelated memo about something else entirely.",
            )
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "the-real-answer.md",
            created="2026-07-21",
            body=f"Reply to your {inbound_basename} — full answer below.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, inbound_basename, repo
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/the-real-answer.md"]

    def test_unlinked_only_above_cap_truncates_narration_but_states_true_total(self, tmp_path, monkeypatch):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        for i in range(7):
            _seed_sender_reply(
                sender_root,
                "cross-repo/inbox",
                f"unrelated-{i:02d}.md",
                created="2026-07-20",
                body="An unrelated memo about something else entirely.",
            )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"},
            "2026-07-20-sender-em-q.md",
            repo,
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert len(closure["unconfirmed_candidates"]) == 7
        assert "7 memo(s)" in closure["reason"]
        assert "(+2 more)" in closure["reason"]

    def test_open_no_candidates_vs_open_unlinked_candidates_reason_wording_differs(self, tmp_path, monkeypatch):
        repo, sender_root = self._setup(tmp_path, monkeypatch)
        (sender_root / "cross-repo" / "inbox").mkdir(parents=True, exist_ok=True)
        inbound_basename = "2026-07-20-sender-em-q.md"

        no_reply_closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, inbound_basename, repo
        )
        assert no_reply_closure["verdict"] == "open"
        assert "no reply from" in no_reply_closure["reason"]
        assert "none citing" not in no_reply_closure["reason"]

        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "unrelated.md",
            created="2026-07-20",
            body="An unrelated memo about something else entirely.",
        )
        unlinked_closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-20"}, inbound_basename, repo
        )
        assert unlinked_closure["verdict"] == "open"
        assert "none citing" in unlinked_closure["reason"]
        assert no_reply_closure["reason"] != unlinked_closure["reason"]


class TestMinLinkStemLengthFloor:
    """`_MIN_LINK_STEM_LENGTH` (10 chars) — a stem shorter than the floor is
    skipped as a substring needle entirely, never treated as a match.
    Regression coverage for the empirical finding that motivated the floor:
    a short `--topic` slug degrades `tail_stem` to something like a single
    character, which then substring-matches almost any prose."""

    def test_degenerate_single_char_tail_stem_does_not_spuriously_link(self, tmp_path, monkeypatch):
        """The exact empirical shape that motivated the floor: an agent
        writing this feature's own tests used `m.md` as a fixture basename,
        yielding `tail_stem == "m"` — which then substring-matched
        deliberately-unrelated candidate prose. Must stay `open`, not
        `evidenced`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "example_doctrine_repo", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        inbound_basename = "2026-07-25-coordinator-claude-em-m.md"
        for i in range(2):
            _seed_sender_reply(
                sender_root,
                "cross-repo/inbox",
                f"unrelated-{i}.md",
                created="2026-07-26",
                body="Some completely unrelated prose that happens to contain the letter m many times.",
            )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "coordinator-claude-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert len(closure["unconfirmed_candidates"]) == 2

    def test_stem_one_char_below_floor_is_not_used_as_a_needle(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        slug = "belowfloo"  # 9 chars — one short of _MIN_LINK_STEM_LENGTH (10)
        assert len(slug) == 9
        inbound_basename = f"2026-07-25-sender-em-{slug}.md"
        # Cites the bare (elided) tail stem only — not the full basename —
        # so this isolates the tail_stem needle specifically.
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-26",
            body=f"Reply to your …-{slug}.md — see above.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert len(closure["unconfirmed_candidates"]) == 1

    def test_stem_exactly_at_floor_length_is_still_used(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        slug = "atfloorlen"  # 10 chars — exactly _MIN_LINK_STEM_LENGTH
        assert len(slug) == 10
        inbound_basename = f"2026-07-25-sender-em-{slug}.md"
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-26",
            body=f"Reply to your …-{slug}.md — see above.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]

    def test_all_three_needles_below_floor_degrades_to_open_with_unconfirmed_candidates(
        self, tmp_path, monkeypatch
    ):
        """When `basename`, `basename_no_ext`, AND `tail_stem` are all below
        the floor (a memo path with no date prefix and a one-token name),
        `_candidate_is_linked` must return `False` for every candidate —
        the correct degrade is `open` with the date-filtered candidates
        carried as `unconfirmed_candidates`, not a spurious `evidenced`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        inbound_basename = "ab.md"  # basename=5, basename_no_ext=2, tail_stem=2 — all < 10
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-26",
            body="Prose that mentions ab in passing, but cites nothing specific.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "open"
        assert closure["candidates"] == []
        assert closure["unconfirmed_candidates"] == ["cross-repo/inbox/answers.md"]
        assert closure["reason"]

    def test_in_reply_to_still_links_when_all_stems_below_floor(self, tmp_path, monkeypatch):
        """The `in_reply_to` frontmatter path is an exact field match, not a
        substring scan — it must keep working regardless of stem length,
        proving it is independent of `_MIN_LINK_STEM_LENGTH`."""
        repo = tmp_path / "repo"
        repo.mkdir()
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        inbound_basename = "ab.md"
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "answers.md",
            created="2026-07-26",
            in_reply_to="ab.md",
            body="No textual citation here at all.",
        )

        closure = pa.compute_reply_closure(
            {"kind": "consult", "from": "sender-em", "created": "2026-07-25"},
            inbound_basename,
            repo,
        )
        assert closure["verdict"] == "evidenced"
        assert closure["candidates"] == ["cross-repo/inbox/answers.md"]


class TestBothEmitPathsSurfaceUnlinkedOnlyOpen:
    """The two-site regression guard (this suite's own header promise)
    re-run specifically for the unlinked-only `open` sub-case introduced by
    the tightening — same-sender/same-day candidates exist, none linked,
    and both emit sites must still surface the judgment point rather than
    treating the mere presence of candidates as closure."""

    def test_actioned_in_place_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "unrelated.md",
            created="2026-07-20",
            body="An unrelated memo about something else entirely.",
        )
        _seed_inbox_memo(repo, "m1.md", kind="consult", created="2026-07-20")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert "j-reply-closure" in jp_ids
        assert obj["gates"]["coast"]["verdict"] == "blocked"
        assert "Nothing further to do" not in obj["next_move"]

    def test_archived_fallback_path(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        sender_root = tmp_path / "sender-repo"
        sender_root.mkdir()
        settings_home = tmp_path / "settings-home"
        _register_sender(settings_home, "sender", sender_root)
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings_home))
        _seed_sender_reply(
            sender_root,
            "cross-repo/inbox",
            "unrelated.md",
            created="2026-07-20",
            body="An unrelated memo about something else entirely.",
        )
        live = _seed_inbox_memo(repo, "m1.md", kind="consult", created="2026-07-20")
        _archive_memo(repo, live, "cross-repo/archive/2026-07/m1.md")

        result = pa.brief("cross-repo/inbox/m1.md", repo_root=repo)

        obj = result.decision_object
        assert obj["artifact"]["classification"] == "archived"
        jp_ids = {jp["id"] for jp in obj["judgment_points"]}
        assert "j-reply-closure" in jp_ids
        assert obj["gates"]["coast"]["verdict"] == "blocked"
        assert "Nothing further to do" not in obj["next_move"]
