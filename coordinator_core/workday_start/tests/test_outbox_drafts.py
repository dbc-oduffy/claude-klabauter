"""Rule branches of the stale outbox drafts reader."""
from __future__ import annotations

import os
from pathlib import Path

from coordinator_core.session.machinery_paths import LEGACY_MEMO_OUTBOX_RELDIR, MEMO_OUTBOX_RELDIR
from coordinator_core.workday_start.outbox_drafts import OFFER, stale_outbox_drafts

NOW = 1_800_000_000.0
HOUR = 3600.0


def _draft(root: Path, rel: str, name: str, status: str, age_hours: float, to: str = "doe", title: str = "T") -> None:
    d = root.joinpath(*rel.split("/"))
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(f"---\nstatus: {status}\nto: {to}\ntitle: {title}\n---\n\nbody\n", encoding="utf-8")
    t = NOW - age_hours * HOUR
    os.utime(p, (t, t))


def test_absent_roots_are_silent(tmp_path):
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_empty_root_is_silent(tmp_path):
    tmp_path.joinpath(*MEMO_OUTBOX_RELDIR.split("/")).mkdir(parents=True)
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_fresh_draft_is_silent(tmp_path):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "a.md", "draft", 23)
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_exactly_window_is_not_stale(tmp_path):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "a.md", "draft", 24)
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_stale_machinery_root_draft(tmp_path):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "topic-a.md", "draft", 30, to="rag", title="Hello")
    code, text = stale_outbox_drafts(tmp_path, now=NOW)
    assert code == 0
    assert text == f"  - topic-a (30h, to: rag) Hello\n{OFFER}"


def test_stale_legacy_root_draft_alone(tmp_path):
    _draft(tmp_path, LEGACY_MEMO_OUTBOX_RELDIR, "old.md", "draft", 65)
    _, text = stale_outbox_drafts(tmp_path, now=NOW)
    assert text.split("\n")[0].startswith("  - old (65h")
    assert text.endswith(OFFER)


def test_both_roots_sorted_by_topic(tmp_path):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "b.md", "draft", 48)
    _draft(tmp_path, LEGACY_MEMO_OUTBOX_RELDIR, "a.md", "draft", 48)
    _, text = stale_outbox_drafts(tmp_path, now=NOW)
    assert [ln.split()[1] for ln in text.split("\n")[:2]] == ["a", "b"]


def test_non_draft_status_and_non_md_skipped(tmp_path):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "sent.md", "sent", 99)
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "x.txt", "draft", 99)
    (tmp_path / LEGACY_MEMO_OUTBOX_RELDIR).mkdir(parents=True)
    (tmp_path / LEGACY_MEMO_OUTBOX_RELDIR / "README.md").write_text("# readme\n", encoding="utf-8")
    os.utime(tmp_path / LEGACY_MEMO_OUTBOX_RELDIR / "README.md", (NOW - 99 * HOUR,) * 2)
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_env_overrides_window(tmp_path, monkeypatch):
    _draft(tmp_path, MEMO_OUTBOX_RELDIR, "a.md", "draft", 3)
    monkeypatch.setenv("COORDINATOR_OUTBOX_STALE_HOURS", "2")
    assert stale_outbox_drafts(tmp_path, now=NOW)[1].endswith(OFFER)
    monkeypatch.setenv("COORDINATOR_OUTBOX_STALE_HOURS", "junk")
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")


def test_sent_cross_repo_outbox_never_read(tmp_path):
    _draft(tmp_path, "state/cross-repo/outbox", "a.md", "draft", 99)
    assert stale_outbox_drafts(tmp_path, now=NOW) == (0, "")
