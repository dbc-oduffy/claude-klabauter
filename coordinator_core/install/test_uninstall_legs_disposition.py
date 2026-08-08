"""
coordinator_core.install.test_uninstall_legs_disposition — tests for the
honest uninstall disposition report (Rule 1 / Rule 2 total-function
coverage).

Spec backlink: docs/plans/2026-08-06-writer-declared-write-surface-manifest.md,
    chunk C8
"""

from __future__ import annotations

import pytest

import coordinator_core.install.uninstall_legs as uninstall_legs
from coordinator_core.install.uninstall_legs import (
    DISPOSITION_CANNOT_SAFELY,
    DISPOSITION_DELIBERATE,
    DISPOSITION_REVERSED,
    UNINSTALL_DISPOSITIONS,
    UninstallLegError,
    build_disposition_report,
    classify_entry_disposition,
    orchestrate_uninstall,
    render_disposition_report,
    render_uninstall_dry_run_report,
)
from coordinator_core.install.receipt import InstallReceipt, ReceiptEntry, persist_receipt
from coordinator_core.install.write_surface import (
    ABSENT_ON_LEGACY_INSTALLS,
    WriteSurfaceEntry,
)
from coordinator_core.install.uninstall_legs import uninstall_reverse_git_config_group
import coordinator_core.ops.configure_git as configure_git
from coordinator_core.ops.configure_git import GitSetting


def _entry(**kwargs) -> WriteSurfaceEntry:
    defaults = {"kind": "file-path", "path": "/tmp/example"}
    defaults.update(kwargs)
    return WriteSurfaceEntry(**defaults)


class TestClassifyEntryDisposition:
    def test_attempted_ok_true_is_reversed(self):
        record = classify_entry_disposition(
            _entry(kind="git-config-key", key="gc.autoDetach"),
            attempted_ok=True,
            reason="git-config key unset",
        )
        assert record.disposition == DISPOSITION_REVERSED

    def test_attempted_ok_false_is_cannot_reverse_safely(self):
        record = classify_entry_disposition(
            _entry(kind="file-path", path="/tmp/hand-modified"),
            attempted_ok=False,
            reason="hand-modified since install, fail-loud-on-ambiguity",
            manual_command="rm -i /tmp/hand-modified",
        )
        assert record.disposition == DISPOSITION_CANNOT_SAFELY
        assert record.manual_command == "rm -i /tmp/hand-modified"

    def test_attempted_ok_none_is_deliberately_not_reversed(self):
        record = classify_entry_disposition(
            _entry(kind="file-path", path="/some/example-doctrine-repo/checkout"),
            attempted_ok=None,
            reason="full-remove mode never deletes <example-doctrine-repo>/coordinator source (PM-gated)",
        )
        assert record.disposition == DISPOSITION_DELIBERATE

    def test_absent_on_legacy_installs_forces_cannot_reverse_safely_rc_block(self):
        entry = _entry(
            kind="rc-block",
            path="~/.ssh/config",
            begin_marker="# Added by coordinator setup-github-auth-1password",
            end_marker=ABSENT_ON_LEGACY_INSTALLS,
        )
        record = classify_entry_disposition(entry, attempted_ok=True, reason="ignored")
        assert record.disposition == DISPOSITION_CANNOT_SAFELY
        assert record.manual_command is not None
        assert "begin_marker" not in record.manual_command
        assert entry.begin_marker in record.manual_command

    def test_absent_on_legacy_installs_overrides_even_a_successful_attempt(self):
        entry = _entry(
            kind="hook-gate-region",
            path="~/.claude/settings.json",
            begin_marker="# gate: shell-init",
            end_marker=ABSENT_ON_LEGACY_INSTALLS,
        )
        record = classify_entry_disposition(entry, attempted_ok=True, reason="claimed reversed")
        assert record.disposition == DISPOSITION_CANNOT_SAFELY

    def test_none_end_marker_on_hook_gate_region_is_not_uncertainty(self):
        """Rule 2: `end_marker=None` on a `hook-gate-region` is the
        structural-terminator case, distinct from `ABSENT_ON_LEGACY_INSTALLS`
        — it routes from the caller-supplied outcome like any other kind,
        not forced to cannot-reverse-safely."""
        entry = _entry(
            kind="hook-gate-region",
            path="~/.claude/settings.json",
            begin_marker="# gate: shell-init",
            end_marker=None,
        )
        record = classify_entry_disposition(entry, attempted_ok=True, reason="gate stripped")
        assert record.disposition == DISPOSITION_REVERSED

    def test_none_end_marker_on_rc_block_is_not_forced_either(self):
        # Review: code-reviewer (Finding 4, P3) — the prior version passed
        # attempted_ok=False and asserted DISPOSITION_CANNOT_SAFELY, which
        # is exactly what the ordinary (unforced) path already produces for
        # attempted_ok=False -- it could not distinguish "not forced by
        # Rule 2" from "would have landed there anyway." Using
        # attempted_ok=True (matching the sibling hook-gate-region test)
        # actually proves non-forcing: DISPOSITION_REVERSED would be
        # impossible here if Rule 2 accidentally forced rc-block+None too.
        entry = _entry(kind="rc-block", path="~/.bashrc", begin_marker="# begin", end_marker=None)
        record = classify_entry_disposition(entry, attempted_ok=True, reason="stripped")
        assert record.disposition == DISPOSITION_REVERSED

    def test_non_bool_non_none_attempted_ok_raises(self):
        entry = _entry(kind="file-path", path="/tmp/x")
        with pytest.raises(TypeError):
            classify_entry_disposition(entry, attempted_ok="yes", reason="bad caller")

    @pytest.mark.parametrize("outcome", [True, False, None])
    @pytest.mark.parametrize(
        "kind",
        [
            "git-config-key",
            "machine-local-key",
            "os-env-var",
            "file-path",
            "rc-block",
            "hook-gate-region",
            "structured-file-key",
            "line-membership",
        ],
    )
    def test_every_kind_and_outcome_lands_in_exactly_one_disposition(self, kind, outcome):
        entry = _entry(kind=kind, key="k" if kind != "file-path" else None, path=None if kind != "file-path" else "/p")
        record = classify_entry_disposition(entry, attempted_ok=outcome, reason="r")
        assert record.disposition in UNINSTALL_DISPOSITIONS


class TestBuildDispositionReportTotalCoverage:
    def test_mixed_report_covers_every_record_exactly_once(self):
        records = [
            classify_entry_disposition(
                _entry(kind="git-config-key", key="gc.autoDetach"),
                attempted_ok=True,
                reason="reversed",
            ),
            classify_entry_disposition(
                _entry(kind="file-path", path="/example-doctrine-repo"),
                attempted_ok=None,
                reason="deliberate policy",
            ),
            classify_entry_disposition(
                _entry(
                    kind="hook-gate-region",
                    path="~/.claude/settings.json",
                    begin_marker="# gate",
                    end_marker=ABSENT_ON_LEGACY_INSTALLS,
                ),
                attempted_ok=True,
                reason="ignored",
            ),
        ]
        report = build_disposition_report(records)
        assert len(report.by_disposition(DISPOSITION_REVERSED)) == 1
        assert len(report.by_disposition(DISPOSITION_DELIBERATE)) == 1
        assert len(report.by_disposition(DISPOSITION_CANNOT_SAFELY)) == 1
        report.assert_total_coverage()

    def test_manually_constructed_bad_disposition_fails_total_coverage(self):
        """A DispositionRecord's own __post_init__ rejects a bad disposition
        value outright, so total-coverage failure is mechanically
        unreachable via this constructor — assert that guard fires instead,
        which is the same "no entry silently dropped" property enforced one
        layer earlier."""
        from coordinator_core.install.uninstall_legs import DispositionRecord

        with pytest.raises(ValueError):
            DispositionRecord(entry=_entry(), disposition="reverted-partially", reason="bad")

    def test_empty_report_is_trivially_total(self):
        report = build_disposition_report([])
        report.assert_total_coverage()


class TestRenderDispositionReport:
    def test_dry_run_labels_as_preview(self):
        report = build_disposition_report(
            [
                classify_entry_disposition(
                    _entry(kind="file-path", path="/tmp/x"),
                    attempted_ok=True,
                    reason="removed",
                )
            ]
        )
        text = render_disposition_report(report, dry_run=True)
        assert "DRY RUN" in text
        assert "reversed" in text

    def test_entry_label_falls_back_to_begin_marker(self):
        """Finding 5, P3: an entry with no key/path but a begin_marker must
        not degrade into an unidentifiable `<kind entry>` line."""
        entry = _entry(
            kind="hook-gate-region",
            path=None,
            begin_marker="# gate: shell-init",
            end_marker=None,
        )
        report = build_disposition_report(
            [classify_entry_disposition(entry, attempted_ok=True, reason="gate stripped")]
        )
        text = render_disposition_report(report)
        assert "# gate: shell-init" in text
        assert "<hook-gate-region entry>" not in text

    def test_cannot_reverse_safely_prints_manual_command(self):
        entry = _entry(
            kind="rc-block",
            path="~/.ssh/config",
            begin_marker="# Added by coordinator setup-github-auth-1password",
            end_marker=ABSENT_ON_LEGACY_INSTALLS,
        )
        report = build_disposition_report(
            [classify_entry_disposition(entry, attempted_ok=True, reason="ignored")]
        )
        text = render_disposition_report(report)
        assert "manual command:" in text
        assert entry.begin_marker in text


class TestRenderUninstallDryRunReport:
    def test_none_receipt_is_honest_unknown(self):
        text = render_uninstall_dry_run_report(None)
        assert "no install receipt found" in text

    def test_empty_receipt_is_honest_unknown(self):
        text = render_uninstall_dry_run_report(InstallReceipt(entries=()))
        assert "no install receipt found" in text

    def test_populated_receipt_renders_dispositions_without_attempting(self):
        # Review: code-reviewer (Finding 1, P1) — a plain git-config-key
        # entry is exactly what a real run WOULD reverse, so it must land
        # in the "reversed" bucket (with "would reverse" reason text), not
        # "deliberately-not-reversed" — the prior assertion proved the bug
        # this fix corrects.
        receipt = InstallReceipt(
            entries=(
                ReceiptEntry(writer_id="w", kind="git-config-key", key="gc.autoDetach"),
            )
        )
        text = render_uninstall_dry_run_report(receipt)
        assert "DRY RUN" in text
        assert "reversed (1):" in text
        assert "would reverse" in text
        assert "gc.autoDetach" in text
        assert "deliberately-not-reversed (0):" in text

    def test_would_reverse_and_genuinely_deliberate_render_distinguishably(self):
        """render_disposition_report(dry_run=True) must render a
        would-reverse entry (attempted_ok=True, the dry-run-preview
        proposal) and a genuinely-deliberate entry (attempted_ok=None, a
        named policy decision) into visibly different buckets/reasons —
        the exact distinction Finding 1 says the prior code collapsed."""
        would_reverse = classify_entry_disposition(
            _entry(kind="git-config-key", key="gc.autoDetach"),
            attempted_ok=True,
            reason="dry-run preview — a real run would reverse this entry",
        )
        genuinely_deliberate = classify_entry_disposition(
            _entry(kind="file-path", path="/some/example-doctrine-repo/checkout"),
            attempted_ok=None,
            reason="full-remove mode never deletes <example-doctrine-repo>/coordinator source (PM-gated)",
        )
        report = build_disposition_report([would_reverse, genuinely_deliberate])
        text = render_disposition_report(report, dry_run=True)

        assert "reversed (1):" in text
        assert "deliberately-not-reversed (1):" in text
        assert "gc.autoDetach" in text
        assert "/some/example-doctrine-repo/checkout" in text
        # the two entries land in different buckets, not the same one
        assert would_reverse.disposition != genuinely_deliberate.disposition

    def test_marker_rule_2_override_still_fires_in_preview(self):
        receipt = InstallReceipt(
            entries=(
                ReceiptEntry(
                    writer_id="w",
                    kind="rc-block",
                    path="~/.ssh/config",
                    begin_marker="# Added by coordinator setup-github-auth-1password",
                    end_marker=ABSENT_ON_LEGACY_INSTALLS,
                ),
            )
        )
        text = render_uninstall_dry_run_report(receipt)
        assert "manual command:" in text


class TestOrchestrateUninstallDryRunWiring:
    def test_dry_run_with_no_receipt_prints_honest_message_and_mutates_nothing(
        self, monkeypatch, capsys, tmp_path
    ):
        monkeypatch.setattr(uninstall_legs, "_load_install_receipt", lambda: None)
        for leg_name in (
            "uninstall_strip_settings_hooks",
            "uninstall_remove_shim",
            "uninstall_remove_substrate",
            "uninstall_set_plugin_endstate",
        ):

            def _boom(*a, **kw):
                raise AssertionError("a mutating leg was called during --dry-run")

            monkeypatch.setattr(uninstall_legs, leg_name, _boom)

        rc = orchestrate_uninstall(["--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no install receipt found" in out

    def test_dry_run_with_populated_receipt_prints_report_and_mutates_nothing(
        self, monkeypatch, capsys
    ):
        receipt = InstallReceipt(
            entries=(ReceiptEntry(writer_id="w", kind="git-config-key", key="gc.autoDetach"),)
        )
        monkeypatch.setattr(uninstall_legs, "_load_install_receipt", lambda: receipt)
        for leg_name in (
            "uninstall_strip_settings_hooks",
            "uninstall_remove_shim",
            "uninstall_remove_substrate",
            "uninstall_set_plugin_endstate",
        ):

            def _boom(*a, **kw):
                raise AssertionError("a mutating leg was called during --dry-run")

            monkeypatch.setattr(uninstall_legs, leg_name, _boom)

        rc = orchestrate_uninstall(["--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "disposition report" in out
        assert "gc.autoDetach" in out


class TestLoadInstallReceiptReadSide:
    """C3 (docs/research/2026-08-06-install-receipt-persistence-design.md)
    — `_load_install_receipt` wired to `receipt.load_receipt` for real."""

    def _isolate_settings_home(self, monkeypatch, tmp_path):
        for var in ("CLAUDE_HOME", "HOME", "USERPROFILE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
        monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)
        monkeypatch.delenv("COORDINATOR_DISABLE_MACHINE_MUTATION", raising=False)

    def test_absent_receipt_preserves_honest_unknown(self, monkeypatch, tmp_path):
        # A machine that has never installed: no receipt file on disk at all.
        self._isolate_settings_home(monkeypatch, tmp_path)
        assert uninstall_legs._load_install_receipt() is None
        text = render_uninstall_dry_run_report(uninstall_legs._load_install_receipt())
        assert "no install receipt found" in text

    def test_populated_receipt_loads_for_real(self, monkeypatch, tmp_path):
        self._isolate_settings_home(monkeypatch, tmp_path)
        receipt = InstallReceipt(
            entries=(ReceiptEntry(writer_id="w", kind="git-config-key", key="gc.autoDetach"),),
            reported_writer_ids=frozenset({"w"}),
        )
        persist_receipt(receipt)

        loaded = uninstall_legs._load_install_receipt()
        assert loaded is not None
        assert loaded.reported("w") is True
        assert any(e.key == "gc.autoDetach" for e in loaded.entries)

    def test_malformed_receipt_still_degrades_to_honest_unknown(self, monkeypatch, tmp_path):
        self._isolate_settings_home(monkeypatch, tmp_path)
        settings_home = tmp_path / ".coordinator-claude-settings"
        settings_home.mkdir(parents=True, exist_ok=True)
        (settings_home / "install-receipt.json").write_text("not json{", encoding="utf-8")

        assert uninstall_legs._load_install_receipt() is None
        text = render_uninstall_dry_run_report(uninstall_legs._load_install_receipt())
        assert "no install receipt found" in text


class TestUnreportedWriterRendersAsCoverageUnknown:
    """C3 negative spec: an unreported writer must render distinguishably
    from a writer that reported and wrote nothing — it belongs alongside
    'would reverse' and 'deliberately-not-reversed' as a third
    distinguishable outcome, never collapsing into either."""

    def test_unreported_writer_is_not_rendered_as_empty_or_nothing_to_remove(self):
        receipt = InstallReceipt(
            entries=(),
            reported_writer_ids=frozenset(),
            unreported_writer_ids=frozenset({"configure_git"}),
        )
        text = render_uninstall_dry_run_report(receipt)

        assert "no install receipt found" not in text
        assert "configure_git" in text
        assert "did not report" in text
        assert "coverage" in text.lower()

    def test_unreported_writer_lands_in_cannot_reverse_safely_bucket(self):
        receipt = InstallReceipt(
            entries=(),
            unreported_writer_ids=frozenset({"configure_git"}),
        )
        text = render_uninstall_dry_run_report(receipt)
        assert "cannot-reverse-safely (1):" in text

    def test_three_way_distinguishability_would_reverse_deliberate_unknown(self):
        """The three real outcomes -- would reverse, deliberately not
        reversed, coverage unknown for this writer -- must each render
        distinguishably in one report."""
        would_reverse = classify_entry_disposition(
            _entry(kind="git-config-key", key="gc.autoDetach"),
            attempted_ok=True,
            reason="dry-run preview — a real run would reverse this entry",
        )
        genuinely_deliberate = classify_entry_disposition(
            _entry(kind="file-path", path="/some/example-doctrine-repo/checkout"),
            attempted_ok=None,
            reason="full-remove mode never deletes <example-doctrine-repo>/coordinator source (PM-gated)",
        )
        coverage_unknown = classify_entry_disposition(
            _entry(kind="file-path", path="<writer:configure_git>"),
            attempted_ok=False,
            reason="writer 'configure_git' did not report this run — coverage unknown",
        )
        report = build_disposition_report([would_reverse, genuinely_deliberate, coverage_unknown])
        text = render_disposition_report(report, dry_run=True)

        assert "reversed (1):" in text
        assert "deliberately-not-reversed (1):" in text
        assert "cannot-reverse-safely (1):" in text
        assert "gc.autoDetach" in text
        assert "/some/example-doctrine-repo/checkout" in text
        assert "configure_git" in text
        # all three land in genuinely different buckets
        dispositions = {
            would_reverse.disposition,
            genuinely_deliberate.disposition,
            coverage_unknown.disposition,
        }
        assert len(dispositions) == 3

    def test_unreported_writer_placeholder_carries_synthetic_marker(self, monkeypatch):
        """Review: code-reviewer (Finding 3, P3) -- a consumer of
        `report.records` that inspects `.entry` directly (bypassing reason
        text) needs a structural signal this record is a fabricated
        stand-in, not a genuine declared surface. A hand-built real entry
        (same kind/path shape) must NOT set the marker -- only
        `render_uninstall_dry_run_report`'s own placeholder does."""
        assert _entry(kind="file-path", path="<writer:configure_git>").synthetic is False

        seen_entries = []
        real_classify = uninstall_legs.classify_entry_disposition

        def _spy(entry, **kwargs):
            seen_entries.append(entry)
            return real_classify(entry, **kwargs)

        monkeypatch.setattr(uninstall_legs, "classify_entry_disposition", _spy)

        receipt = InstallReceipt(
            entries=(),
            unreported_writer_ids=frozenset({"configure_git"}),
        )
        render_uninstall_dry_run_report(receipt)

        placeholder_entries = [e for e in seen_entries if e.path == "<writer:configure_git>"]
        assert len(placeholder_entries) == 1
        assert placeholder_entries[0].synthetic is True

    def test_unreported_writer_alongside_reported_entries_both_appear(self):
        receipt = InstallReceipt(
            entries=(ReceiptEntry(writer_id="reported_writer", kind="git-config-key", key="gc.autoDetach"),),
            reported_writer_ids=frozenset({"reported_writer"}),
            unreported_writer_ids=frozenset({"unreported_writer"}),
        )
        text = render_uninstall_dry_run_report(receipt)

        assert "reversed (1):" in text
        assert "would reverse" in text
        assert "gc.autoDetach" in text
        assert "cannot-reverse-safely (1):" in text
        assert "unreported_writer" in text
        assert "did not report" in text


# ---------------------------------------------------------------------------
# C6 — `uninstall_reverse_git_config_group`: net-new git-config reversal
# leg, honouring unset-as-a-unit. Spec backlink:
# docs/plans/2026-08-07-git-help-browser-settings-shape.md § C6
#
# Every test stubs the git subprocess seam (`config_get`/`config_unset`
# injected callables) -- none touches this machine's real git config, and
# no test runs `git config --global`/`--unset` against the real
# environment.
# ---------------------------------------------------------------------------

# Derived from the production declaration, never restated. A local copy would
# let a reorder of configure_git._SETTINGS silently change the leg's unset
# order while every test below stayed green -- and that order is the whole
# safety property (see test_declaration_order_is_the_safe_unset_order).
HELP_BROWSER_SETTINGS = tuple(
    s for s in configure_git._SETTINGS if s.unset_group == "help-browser"
)


def test_declaration_order_is_the_safe_unset_order():
    """The leg walks `_SETTINGS` in declaration order, so that order IS the
    degradation ladder: unsetting `web.browser` before `browser.noop.cmd`
    is what keeps `web.browser=noop` from ever outliving the command it
    names. Pinned here because reordering `_SETTINGS` is an innocuous-looking
    edit that would silently reintroduce the stranded state."""
    assert [s.key for s in HELP_BROWSER_SETTINGS] == [
        "help.format",
        "web.browser",
        "browser.noop.cmd",
    ]


class _FakeGitStore:
    """An in-memory stand-in for a git config store, keyed on (scope, key)."""

    def __init__(self, initial):
        self._store = dict(initial)
        self.get_calls = []
        self.unset_calls = []
        self.fail_on_key = None

    def get(self, scope, key):
        scope = tuple(scope)
        self.get_calls.append((scope, key))
        return self._store.get((scope, key))

    def unset(self, scope, key):
        scope = tuple(scope)
        self.unset_calls.append((scope, key))
        if self.fail_on_key == key:
            return 1  # generic non-recoverable failure
        if (scope, key) not in self._store:
            return 5  # already absent in this scope -- success
        del self._store[(scope, key)]
        return 0


def _fully_configured_store() -> _FakeGitStore:
    return _FakeGitStore(
        {
            (("--global",), "help.format"): "web",
            (("--global",), "web.browser"): "noop",
            (("--global",), "browser.noop.cmd"): "echo not-opening-browser-for:",
        }
    )


class TestUninstallReverseGitConfigGroup:
    def test_all_three_reversed_in_declared_order(self):
        store = _fully_configured_store()
        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=HELP_BROWSER_SETTINGS,
            config_get=store.get,
            config_unset=store.unset,
        )
        assert [r.entry.key for r in records] == [
            "help.format",
            "web.browser",
            "browser.noop.cmd",
        ]
        assert all(r.disposition == DISPOSITION_REVERSED for r in records)
        assert store.unset_calls == [
            (("--global",), "help.format"),
            (("--global",), "web.browser"),
            (("--global",), "browser.noop.cmd"),
        ]

    def test_scope_flag_omitted_run_never_classifies_as_reversed(self):
        """Constraint 1 (SCOPE) — the leg must always thread the record's
        own declared scope through to both `config_get`/`config_unset`,
        never omit it: `git config --unset` with no scope flag defaults to
        LOCAL, which is not the scope the help-browser triple was written
        to, so a missing scope flag combined with the exit-5-is-success
        rule could otherwise misreport a still-stranded global key as
        `reversed`. Assert every call this leg makes carries the declared
        `"--global"` scope -- the structural guarantee that makes the
        false-positive combination unreachable."""
        calls = []

        def spy_unset(scope, key):
            calls.append(tuple(scope))
            return 5  # already absent -- success, IF the scope was declared

        def matching_get(scope, key):
            setting = next(s for s in HELP_BROWSER_SETTINGS if s.key == key)
            return setting.value

        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=HELP_BROWSER_SETTINGS,
            config_get=matching_get,
            config_unset=spy_unset,
        )

        assert calls, "config_unset was never called"
        assert all(scope == ("--global",) for scope in calls), (
            "a reversal run must never omit the declared scope flag -- an "
            f"omitted-scope call would default to LOCAL, not the declared "
            f"scope; observed calls: {calls}"
        )
        assert all(r.disposition == DISPOSITION_REVERSED for r in records)

    def test_no_prefix_strands_web_browser_without_noop_cmd(self):
        """example-doctrine-repo ruling (Ask 2, cross-repo/inbox/2026-08-07-example-doctrine-repo-em-
        configure-git-per-key-scope-ruled-a.md): no prefix of the reversal
        sequence may leave `web.browser=noop` set without
        `browser.noop.cmd` also set -- that combination makes git print
        "unknown browser" and fall through to the operator's real default
        browser, worse than either end state."""
        for prefix_len in range(0, len(HELP_BROWSER_SETTINGS) + 1):
            store = _fully_configured_store()
            for setting in HELP_BROWSER_SETTINGS[:prefix_len]:
                store.unset(("--global",), setting.key)

            web_browser_set = store.get(("--global",), "web.browser") == "noop"
            noop_cmd_set = store.get(("--global",), "browser.noop.cmd") is not None

            assert not (web_browser_set and not noop_cmd_set), (
                f"prefix_len={prefix_len} left web.browser=noop stranded "
                "without browser.noop.cmd -- git would print 'unknown "
                "browser' and open the default browser"
            )

    def test_exit_5_in_declared_scope_is_success(self):
        store = _FakeGitStore(
            {
                # web.browser already absent -- simulates exit 5.
                (("--global",), "browser.noop.cmd"): "echo not-opening-browser-for:",
            }
        )
        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=(HELP_BROWSER_SETTINGS[1],),  # web.browser only
            config_get=lambda scope, key: "noop",  # value-match gate passes
            config_unset=store.unset,
        )
        assert len(records) == 1
        assert records[0].disposition == DISPOSITION_REVERSED
        assert records[0].disposition != DISPOSITION_CANNOT_SAFELY

    def test_mid_group_failure_aborts_remainder_as_cannot_reverse_safely(self):
        store = _fully_configured_store()
        store.fail_on_key = "web.browser"
        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=HELP_BROWSER_SETTINGS,
            config_get=store.get,
            config_unset=store.unset,
        )
        assert records[0].disposition == DISPOSITION_REVERSED  # help.format
        assert records[1].disposition == DISPOSITION_CANNOT_SAFELY  # web.browser (failed)
        assert records[2].disposition == DISPOSITION_CANNOT_SAFELY  # browser.noop.cmd (aborted)
        assert records[2].manual_command is not None
        # the aborted member was never even attempted.
        assert (("--global",), "browser.noop.cmd") not in store.unset_calls

    def test_value_match_skip_does_not_abort_group(self):
        store = _fully_configured_store()
        # Operator hand-modified web.browser -- value-match SKIP, not a failure.
        store._store[(("--global",), "web.browser")] = "firefox"
        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=HELP_BROWSER_SETTINGS,
            config_get=store.get,
            config_unset=store.unset,
        )
        assert records[0].disposition == DISPOSITION_REVERSED  # help.format
        assert records[1].disposition == DISPOSITION_DELIBERATE  # web.browser SKIP
        assert records[2].disposition == DISPOSITION_REVERSED  # browser.noop.cmd -- group continues
        assert (("--global",), "browser.noop.cmd") in store.unset_calls

    def test_assert_total_coverage_holds_with_group_bearing_entries(self):
        store = _fully_configured_store()
        store.fail_on_key = "web.browser"
        records = uninstall_reverse_git_config_group(
            "help-browser",
            settings=HELP_BROWSER_SETTINGS,
            config_get=store.get,
            config_unset=store.unset,
        )
        report = build_disposition_report(records)  # raises on non-total coverage
        assert len(report.records) == 3
        report.assert_total_coverage()
