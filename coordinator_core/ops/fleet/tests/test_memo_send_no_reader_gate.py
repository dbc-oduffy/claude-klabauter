"""memo.send warns once where the memo would have no reader.

WHY. Example-market-data-repo's CLAUDE.md says "use `cross-repo-memo`, never
hand-write into a sibling's tree". On a host with no peer EM that instruction
mandates an artifact with no reader while forbidding the only workable
alternative. The memo does not FAIL there — it succeeds, producing something
that looks exactly like a dispatched request and is not one.

The fix is a warning, not a refusal, and the distinction is the whole design:

  - It never permanently blocks. A memo is still the right artifact for
    plan-weight work with no reader today, because it is a durable record for
    whoever picks the repo up. The second attempt sends.
  - It never gates a `dry_run` preview. A preview delivers nothing, so
    warning there would spend the operator's one warning on a call that was
    never going to deliver anyway.
  - It fails OPEN in both directions: an unimportable capability layer, or an
    unwritable ack marker, both send. A gate that cannot promise the retry
    behaves differently must not refuse the first attempt, or the memo is
    stranded permanently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet import memo_send


@pytest.fixture()
def no_reader(monkeypatch):
    """Both overrides must go, not just the one this surface reads:
    `peer_ems_reachable` is DERIVED from `fleet_present`, so leaving the
    latter pinned by this package's conftest would infer a reader back into
    existence. Function-scoped, so it runs after that autouse pin."""
    monkeypatch.delenv("COORDINATOR_CAP_PEER_EMS_REACHABLE", raising=False)
    monkeypatch.delenv("COORDINATOR_CAP_FLEET_PRESENT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "remote")


@pytest.fixture()
def fleet_machine(monkeypatch, tmp_path):
    home = tmp_path / "settings-home"
    home.mkdir()
    (home / "settings.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr("coordinator_core._settings_home.settings_home", lambda: home)
    for var in (
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_CONTAINER_ID",
        "COORDINATOR_CAP_PEER_EMS_REACHABLE",
        "COORDINATOR_CAP_FLEET_PRESENT",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDECODE", "1")


def test_a_drained_inbox_never_warns(fleet_machine, tmp_path):
    """Where a reader demonstrably exists, nothing about this surface changes.

    REWRITTEN when the probe became real. The old version asserted that an
    INSTALLED COORDINATOR meant a reader — true only while
    `peer_ems_reachable` was an alias of `fleet_present`. Reachability is now
    a property of the addressee's inbox, not of this host's install state, so
    the fixture has to supply the evidence rather than the venue."""
    import datetime as dt

    receiver = tmp_path / "receiver"
    inbox = receiver / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
    (inbox / "2026-09-05-peer-em-topic.md").write_text(
        f"---\nstatus: actioned\npicked_up_at: '{recent}'\n---\n", encoding="utf-8"
    )

    assert (
        memo_send._no_reader_gate(tmp_path, "topic", dry_run=False, receiver_root=receiver)
        is None
    )


def test_an_undrained_receiver_warns_even_on_a_fleet_machine(fleet_machine, tmp_path):
    """The inverse, and the reason the split is not ceremony: a fully
    installed workstation still gets the warning when THIS addressee's inbox
    shows nobody working it. The old alias could not express that."""
    receiver = tmp_path / "receiver"
    inbox = receiver / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "2026-09-05-peer-em-topic.md").write_text(
        "---\nstatus: open\n---\n", encoding="utf-8"
    )

    warning = memo_send._no_reader_gate(
        tmp_path, "topic", dry_run=False, receiver_root=receiver
    )
    assert warning is not None
    assert "record, not a dispatch" in warning


def test_first_send_warns_and_second_sends(no_reader, tmp_path):
    """THE contract, in one test: warn once, then get out of the way."""
    first = memo_send._no_reader_gate(tmp_path, "topic", dry_run=False)
    assert first is not None

    second = memo_send._no_reader_gate(tmp_path, "topic", dry_run=False)
    assert second is None, "the second attempt must send — this is not a block"


def test_the_warning_is_per_topic_not_per_session(no_reader, tmp_path):
    """A second, different memo is a second decision and earns its own
    warning. Clearing the gate once for everything would make the mechanism a
    one-time nag rather than a per-decision prompt."""
    assert memo_send._no_reader_gate(tmp_path, "topic-a", dry_run=False) is not None
    assert memo_send._no_reader_gate(tmp_path, "topic-a", dry_run=False) is None
    assert memo_send._no_reader_gate(tmp_path, "topic-b", dry_run=False) is not None


def test_dry_run_is_never_gated(no_reader, tmp_path):
    """A preview delivers nothing; warning there would burn the one warning
    on a call that could not have produced the problem."""
    assert memo_send._no_reader_gate(tmp_path, "topic", dry_run=True) is None
    # ...and must not have consumed the topic's warning either.
    assert memo_send._no_reader_gate(tmp_path, "topic", dry_run=False) is not None


def test_the_warning_names_the_test_and_the_way_through(no_reader, tmp_path):
    """Register check. The text must give the EM the decision rule and the
    exit, not scold them — an override key dressed as a punishment is the
    shape guard-messaging doctrine bans."""
    warning = memo_send._no_reader_gate(tmp_path, "topic", dry_run=False)
    assert "record, not a dispatch" in warning
    assert "clear win" in warning
    assert "plan-weight" in warning
    assert "re-run" in warning
    assert "Nothing was written" in warning


def test_override_restores_normal_sending(no_reader, tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_CAP_PEER_EMS_REACHABLE", "1")
    assert memo_send._no_reader_gate(tmp_path, "topic", dry_run=False) is None


def test_unwritable_ack_marker_sends_rather_than_stranding_the_memo(no_reader, tmp_path, monkeypatch):
    """If the retry cannot be made to behave differently, refusing the first
    attempt would strand the memo forever. Send."""
    def boom(*args, **kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert memo_send._no_reader_gate(tmp_path, "topic", dry_run=False) is None


def test_broken_capability_layer_sends_exactly_as_before(no_reader, tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if name == "coordinator_core.environment":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exploding_import)
    assert memo_send._no_reader_gate(tmp_path, "topic", dry_run=False) is None


def test_the_ack_marker_lands_untracked_under_coordinator_local(no_reader, tmp_path):
    """It is bookkeeping about a warning, not a fleet artifact — it must not
    land anywhere git history or a receiver's tree would carry it."""
    memo_send._no_reader_gate(tmp_path, "topic", dry_run=False, receiver_root=tmp_path)
    ack_dir = tmp_path / ".coordinator-local" / "memo-send-ack"
    markers = list(ack_dir.iterdir())
    assert len(markers) == 1, "exactly one marker per warned topic"
    # Keyed by digest, not by the topic string: transliterating collapsed `a/b`,
    # `a-b` and `a b` onto one marker, letting one topic eat another's warning.
    assert markers[0].name == memo_send._send_ack_path(tmp_path, "topic").name
    assert markers[0].name != "topic"


def test_the_warning_separates_liveness_from_drainage(no_reader, tmp_path):
    """Measured 2026-09-11 on example-cockpit-repo: an EM read "no peer EM is reachable",
    knew it had been messaging that session all day, and overrode a refusal that was
    right — it had measured inbox drainage, not session liveness. The text has to make
    the claim it actually makes, because the override is one keystroke."""
    warning = memo_send._no_reader_warning("topic", "12 memos sampled, none stamped")

    assert "draining that inbox" in warning
    assert "Not a claim that the session is dead" in warning
    assert "Measured: 12 memos sampled, none stamped" in warning
    assert "no peer EM is reachable" not in warning
