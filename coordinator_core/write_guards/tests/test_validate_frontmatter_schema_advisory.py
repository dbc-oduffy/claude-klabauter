"""Behavioral tests for
coordinator_core.write_guards.validate_frontmatter_schema_advisory — the
warn-mode (default) leg of the fan-in split of coordinator-claude's
validate-frontmatter-schema.py PreToolUse hook.

Requires the sibling coordinator-claude checkout (for coordinator/schemas/ and the
registry manifest) — skipped entirely when absent, per the
coordinator_core.testing.doe_root convention (parity-oracle tests that need
real schema content, not a synthetic stand-in, follow this same pattern
elsewhere in this tree; see orient_assemble/tests/test_envelope_schema_
conformance.py).

`coordinator_doe_root` is monkeypatched to the resolved sibling root for
every test (rather than relying on REPO_EXAMPLE_DOCTRINE_REPO / machine-local at test
time) so the suite is deterministic regardless of this machine's registry
state.

Covers: non-write-tool/non-dict-tool_input passthrough, coordinator-claude-root-unresolvable
fail-open, the four warn-by-default payloads (schema-validation warning,
mislocated-memo offer, routing-mismatch offer, scaffold offer) firing in
default (non-strict) mode and going silent (None) under
COORDINATOR_SCHEMA_STRICT=1 (that shape belongs to the deny sibling), the
own-inbox misplacement guard staying silent here (unconditional-deny,
exclusively the deny sibling's territory) even though a schema-valid memo
would otherwise warn, a fully schema-conformant handoff/memo write passing
through silently, and the module contract (CLASS/MATCHERS/PRIORITY).

Spec backlink: docs/plans/2026-07-29-hook-fan-in-write-path.md § C11
Source: coordinator-claude coordinator/hooks/scripts/validate-frontmatter-schema.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

import yaml

from coordinator_core.frontmatter.schema_validate import compute_grouping_digest
from coordinator_core.testing.doe_root import doe_root_and_present
from coordinator_core.write_guards import validate_frontmatter_schema_advisory as guard

_doe_root, _doe_present = doe_root_and_present()


@pytest.fixture(autouse=True)
def _pin_doe_root(monkeypatch):
    if not _doe_present:
        pytest.skip("sibling coordinator-claude checkout not found")
    monkeypatch.setattr(guard, "coordinator_doe_root", lambda: _doe_root)


def _payload(tool_name, file_path, cwd, **tool_input_extra):
    tool_input = {"file_path": file_path}
    tool_input.update(tool_input_extra)
    return {"tool_name": tool_name, "tool_input": tool_input, "cwd": cwd}


def _advisory_text(result: dict) -> str:
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso
    return hso["additionalContext"]


class TestModuleContract:
    def test_class_matchers_priority(self):
        assert guard.CLASS == "advisory"
        assert guard.MATCHERS == ["Write", "Edit", "MultiEdit"]
        assert guard.PRIORITY == 100


class TestGateOnToolAndPayloadShape:
    def test_non_guarded_tool_passes_through(self, tmp_path):
        assert guard.check(_payload("Read", str(tmp_path / "x.md"), str(tmp_path))) is None

    def test_tool_input_not_dict_passes_through(self):
        assert guard.check({"tool_name": "Write", "tool_input": "nope"}) is None

    def test_missing_file_path_passes_through(self, tmp_path):
        assert guard.check({"tool_name": "Write", "tool_input": {}, "cwd": str(tmp_path)}) is None

    def test_doe_root_unresolvable_still_warns_via_vendored_schemas(self, tmp_path, monkeypatch):
        # Pre-repoint this fully fail-opened (registry unresolvable -> zero
        # enforcement). Post-repoint (AC2), schema-shape advisories no
        # longer depend on the coordinator-claude sibling at all -- the corpus is vendored
        # in-repo, so a title-only handoff still warns even with the
        # sibling unreachable. Manifest-derived behaviour (scaffold offers,
        # which need the offerable-doc-types map) still degrades silently,
        # which is why this hits the schema-warning path rather than the
        # scaffold-offer path for a brand-new file.
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: None)
        result = guard.check(
            _payload("Write", str(tmp_path / "state" / "handoffs" / "x.md"), str(tmp_path),
                     content="---\ntitle: t\n---\nbody")
        )
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "handoff:" in text


class TestSchemaCorpusResolutionWithDoeSiblingAbsent:
    """AC2 twin (docs/plans/2026-08-06-repoint-write-enforcement-at-vendored-
    corpus.md): the advisory guard still validates against the schema
    corpus with the coordinator-claude sibling completely unreachable, because the corpus
    is vendored in-repo now instead of read from coordinator-claude's live working
    tree. Only manifest-DERIVED behaviour (memo routing, scaffold offers)
    is expected to degrade.
    """

    def _absent_doe_root(self, monkeypatch):
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: None)

    def test_schema_shape_warning_still_fires_with_sibling_absent(self, tmp_path, monkeypatch):
        self._absent_doe_root(monkeypatch)
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "handoff:" in text

    def test_fully_conformant_handoff_still_passes_with_sibling_absent(self, tmp_path, monkeypatch):
        self._absent_doe_root(monkeypatch)
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fm = (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nold"
        )
        fp.write_text(fm, encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is None

    def test_registry_still_resolves_vendored_schemas_dir_with_sibling_absent(self, monkeypatch):
        self._absent_doe_root(monkeypatch)
        registry = guard._load_doe_registry()
        assert registry is not None
        # Manifest-derived fields degrade to empty defaults, never a crash.
        assert registry["manifest"] == {}
        assert registry["central_em_ids"] == set()
        assert registry["central_canonical_id"] is None


class TestSchemaCorpusSourcePinned:
    """AC3 twin: pins the resolution SOURCE, not just behaviour — asserts
    the guard's resolved schemas directory is the in-repo vendored path and
    is never derived from `coordinator_doe_root()`.
    """

    def test_resolved_schemas_dir_is_the_vendored_in_repo_path(self):
        assert guard._VENDORED_SCHEMAS_DIR == (
            Path(guard.__file__).resolve().parents[1] / "frontmatter" / "schemas"
        )
        assert guard._VENDORED_SCHEMAS_DIR.is_dir()

    def test_resolved_schemas_dir_does_not_move_when_doe_root_changes(self, tmp_path, monkeypatch):
        fake_doe_root = str(tmp_path / "not-a-real-coordinator-claude-checkout")
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: fake_doe_root)
        # _VENDORED_SCHEMAS_DIR is a module-level constant -- re-pointing
        # coordinator_doe_root() cannot move it, unlike the old
        # doe_root-derived schemas_dir it replaces.
        assert guard._VENDORED_SCHEMAS_DIR == (
            Path(guard.__file__).resolve().parents[1] / "frontmatter" / "schemas"
        )
        assert "coordinator" not in guard._VENDORED_SCHEMAS_DIR.parts

    def test_resolved_schemas_dir_is_never_derived_from_coordinator_doe_root(self):
        assert str(guard._VENDORED_SCHEMAS_DIR) != str(Path(_doe_root) / "coordinator" / "schemas")
        assert guard._VENDORED_SCHEMAS_DIR.resolve().is_relative_to(
            Path(guard.__file__).resolve().parents[1]
        )


class TestTornWriteRetry:
    """coordinator-claude's schema corpus and registry manifest are read from the sibling
    checkout's LIVE working tree on every check() — a concurrent session can
    leave a torn/partial JSON file mid-write. `_retry_on_transient_read_failure`
    narrows that race without changing the guard's fail-open contract once
    every attempt is exhausted.
    """

    def _present_path(self, tmp_path):
        """A path that exists, so the retry loop is in play (presence is
        the discriminator the helper checks)."""
        p = tmp_path / "present.json"
        p.write_text("{}", encoding="utf-8")
        return p

    def test_recovers_from_a_transient_failure_within_budget(self, tmp_path):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < guard._TORN_WRITE_RETRY_ATTEMPTS:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return "ok"

        assert guard._retry_on_transient_read_failure(flaky, exists_path=self._present_path(tmp_path)) == "ok"
        assert calls["n"] == guard._TORN_WRITE_RETRY_ATTEMPTS

    def test_reraises_the_last_exception_once_attempts_are_exhausted(self, tmp_path):
        calls = {"n": 0}

        def always_broken():
            calls["n"] += 1
            raise ValueError(f"torn write #{calls['n']}")

        with pytest.raises(ValueError, match="torn write #3"):
            guard._retry_on_transient_read_failure(always_broken, exists_path=self._present_path(tmp_path))
        assert calls["n"] == guard._TORN_WRITE_RETRY_ATTEMPTS

    def test_absent_path_calls_once_and_never_sleeps_or_retries(self, tmp_path, monkeypatch):
        """A missing path (partial install, no schemas tree, permissions
        problem) is a STEADY STATE, not a race — assert the attempt count
        and that time.sleep is never invoked, never wall-time."""
        calls = {"n": 0}
        sleep_calls = {"n": 0}
        monkeypatch.setattr(guard.time, "sleep", lambda secs: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))

        def always_broken():
            calls["n"] += 1
            raise FileNotFoundError("no such file")

        missing = tmp_path / "does-not-exist.json"
        assert not missing.exists()

        with pytest.raises(FileNotFoundError):
            guard._retry_on_transient_read_failure(always_broken, exists_path=missing)
        assert calls["n"] == 1
        assert sleep_calls["n"] == 0

    def test_transient_schema_load_failure_still_warns_like_today(self, tmp_path, monkeypatch):
        """A schema corpus read that fails once (torn write) then succeeds
        must produce the SAME warning it would have with no torn-write at
        all — the retry must never surface a weaker verdict than a clean
        read (and never a STRONGER one either — this module never denies).
        """
        real_load_schemas = guard.load_schemas
        calls = {"n": 0}

        def flaky_load_schemas(schemas_dir):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("torn write on first read")
            return real_load_schemas(schemas_dir)

        monkeypatch.setattr(guard, "load_schemas", flaky_load_schemas)

        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert calls["n"] >= 1

    def test_permanently_malformed_schema_corpus_still_fails_open(self, tmp_path, monkeypatch):
        """Once every retry attempt is exhausted, behavior must be identical
        to today: `check()` returns None (fail-open on infra), never an
        advisory manufactured from partial state.
        """

        def always_broken(schemas_dir):
            raise ValueError("schemas dir is permanently malformed")

        monkeypatch.setattr(guard, "load_schemas", always_broken)

        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is None

    def test_missing_manifest_degrades_manifest_only_with_no_sleep(self, tmp_path, monkeypatch):
        # Pre-repoint this fully fail-opened (missing manifest -> zero
        # enforcement, registry unresolvable). Post-repoint (AC2), the
        # schema corpus is vendored in-repo and no longer coupled to the
        # manifest read, so a missing manifest degrades ONLY the
        # manifest-derived behaviour (scaffold offers, memo routing) --
        # schema-shape validation still fires. The "no sleep" assertion
        # (an absent manifest path is never retried) still holds.
        sleep_calls = {"n": 0}
        monkeypatch.setattr(guard.time, "sleep", lambda secs: sleep_calls.__setitem__("n", sleep_calls["n"] + 1))
        fake_root = str(tmp_path / "nonexistent-coordinator-claude-root")
        monkeypatch.setattr(guard, "coordinator_doe_root", lambda: fake_root)
        result = guard.check(
            _payload("Write", "/tmp/state/handoffs/x.md", "/tmp", content="---\ntitle: t\n---\nbody")
        )
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "handoff:" in text
        assert sleep_calls["n"] == 0


class TestSchemaValidationWarn:
    def _handoff_path(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "existing.md"

    def test_missing_required_fields_warns(self, tmp_path):
        fp = self._handoff_path(tmp_path)
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        # Review: staff-eng (B8 leg (d)+(f)) -- the override-key pointer is
        # now audience-gated via `operator_override_note`
        # (`session.identity.resolves_em_audience`), which requires a
        # real envelope (a `session_id`, no `agent_id`) to positively
        # resolve EM before rendering the pointer at all -- see that
        # function's own docstring. This EM-shaped payload exercises the
        # positive branch; audience-gating itself (subagent -> "") is
        # covered by `operator_override_note`'s own tests.
        payload = _payload(
            "Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body"
        )
        payload["session_id"] = "test-session-em"
        result = guard.check(payload)
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "handoff:" in text
        # AC9 remediation (chunk C9, agent-facing-messages-not-apology plan):
        # the override key itself is no longer named in agent-facing text --
        # only that an override route exists, routed to the doc.
        assert "COORDINATOR_SCHEMA_STRICT" not in text
        assert "docs/reference/guard-override-keys.md" in text

    def test_missing_required_fields_subagent_audience_no_pointer(self, tmp_path):
        # Review: staff-eng (B8 leg (d)+(f)) -- this module used to hand-roll
        # "see docs/reference/guard-override-keys.md" for EVERY audience,
        # a dispatched subagent included. A subagent-shaped payload (an
        # `agent_id`) must now degrade to no pointer at all.
        fp = self._handoff_path(tmp_path)
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        payload = _payload(
            "Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body"
        )
        payload["session_id"] = "test-session-subagent"
        payload["agent_id"] = "a" * 16
        result = guard.check(payload)
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "docs/reference/guard-override-keys.md" not in text
        assert "COORDINATOR_SCHEMA_STRICT" not in text

    def test_strict_mode_yields_none_not_mine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = self._handoff_path(tmp_path)
        fp.write_text("---\ntitle: t\n---\nold body", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old body", new_string="new body")
        )
        assert result is None

    def test_fully_conformant_handoff_passes_through_silent(self, tmp_path):
        fp = self._handoff_path(tmp_path)
        fm = (
            "---\ntitle: t\ncreated: 2026-07-29\nbranch: main\nstatus: open\n"
            "predecessor: none\ncategory: infra\nsummary: a one-line summary\n---\nold"
        )
        fp.write_text(fm, encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is None


class TestMemoOffers:
    def test_mislocated_memo_path_offers_cli(self, tmp_path):
        d = tmp_path / "state" / "memos"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "hand-rolled.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="To: someone\nFrom: me\n\nhi")
        )
        assert result is not None
        assert "[cross-repo-memo offer]" in _advisory_text(result)

    def test_mislocated_memo_strict_mode_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "memos"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "hand-rolled.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="To: someone\nFrom: me\n\nhi")
        )
        assert result is None

    def test_canonical_cross_repo_write_is_not_mislocated(self, tmp_path):
        d = tmp_path / "cross-repo" / "archive"
        d.mkdir(parents=True, exist_ok=True)
        assert guard.is_memo_path_mislocated("cross-repo/archive/2026-01-01-x.md") is False


class TestOwnInboxIsDenySiblingTerritory:
    """The own-inbox guard is an UNCONDITIONAL deny in the source hook —
    never gated on COORDINATOR_SCHEMA_STRICT. This module must never emit an
    advisory for it (that would violate the mutual-exclusivity contract with
    the deny sibling), even when the memo itself is otherwise fully
    schema-conformant (i.e. would silently pass schema validation on its
    own merits).
    """

    def test_own_inbox_misplacement_yields_none(self):
        # Uses the real coordinator-claude checkout as both cwd (repo root) and the
        # REPO_EXAMPLE_DOCTRINE_REPO-resolved central path, so the guard's
        # this-repo-is-central branch engages. No file is ever opened for a
        # Write payload's own content (see _memo_guards_decision), so this
        # never touches disk.
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-own-inbox-test.md"
        memo_content = (
            "---\nfrom: coordinator-claude-em\nto: example-game-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        result = guard.check(_payload("Write", fp, _doe_root, content=memo_content))
        assert result is None

    def test_correct_inbound_memo_passes_through_silent(self):
        fp = f"{_doe_root}/cross-repo/inbox/2099-01-01-fake-correct-inbound-test.md"
        memo_content = (
            "---\nfrom: example-game-repo-em\nto: coordinator-claude-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        result = guard.check(_payload("Write", fp, _doe_root, content=memo_content))
        assert result is None


class TestRoutingMismatchOffer:
    def test_routing_mismatch_offers_redirect(self):
        fp = f"{_doe_root}/cross-repo/2099-01-01-fake-routing-mismatch-test.md"
        memo_content = (
            "---\nfrom: example-game-repo-em\nto: example-retrieval-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        result = guard.check(_payload("Write", fp, _doe_root, content=memo_content))
        assert result is not None
        text = _advisory_text(result)
        assert "[cross-repo-memo routing offer]" in text
        assert "example-retrieval-repo-em" in text

    def test_routing_mismatch_strict_mode_yields_none(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        fp = f"{_doe_root}/cross-repo/2099-01-01-fake-routing-mismatch-strict-test.md"
        memo_content = (
            "---\nfrom: example-game-repo-em\nto: example-retrieval-repo-em\ntopic: test\ntitle: t\n"
            "created: 2026-07-29\nstatus: open\ndelivery_mode: receiver-repo\n---\nbody"
        )
        result = guard.check(_payload("Write", fp, _doe_root, content=memo_content))
        assert result is None


class TestScaffoldOffer:
    def test_new_schema_matching_file_offers_scaffolder(self, tmp_path):
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "brand-new.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="---\ntitle: t\n---\nbody")
        )
        assert result is not None
        text = _advisory_text(result)
        assert "[scaffold offer]" in text
        assert "coordinator-doc-new --type handoff" in text

    def test_scaffold_offer_strict_mode_yields_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "brand-new.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="---\ntitle: t\n---\nbody")
        )
        assert result is None

    def test_existing_file_does_not_trigger_scaffold_offer(self, tmp_path):
        # New-file-only discriminator (C0a): an Edit against an
        # already-existing schema-matching file must never re-trigger the
        # scaffold offer -- it falls through to schema validation instead.
        d = tmp_path / "state" / "handoffs"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / "existing.md"
        fp.write_text("---\ntitle: t\n---\nold", encoding="utf-8")
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string="old", new_string="new")
        )
        assert result is not None
        assert "[scaffold offer]" not in _advisory_text(result)
        assert "[frontmatter-schema warning]" in _advisory_text(result)


class TestNonMemoPathUntouched:
    def test_unrelated_path_with_no_schema_match_passes_through(self, tmp_path):
        fp = tmp_path / "README.md"
        result = guard.check(
            _payload("Write", str(fp), str(tmp_path), content="# hello\n\nnothing schema-shaped here")
        )
        assert result is None


class TestPlanTasksSpineWarn:
    """Write-time row validation of a plan's `## Tasks` task-spine fenced
    block against coordinator/schemas/plan-tasks.schema.json — closed enums
    (change_kind/disposition/queue_scope) were previously enforced only by
    claude-klabauter's mutation verb, which sees only rows a mutation touches; a
    hand-authored row bypassed it entirely (the incident this covers).
    """

    _FRONTMATTER = (
        "---\ntitle: Test plan\ncreated: 2026-07-29\nauthor: test\nstatus: draft\n---\n\n"
        "# Plan\n\n## Tasks\n"
    )

    def _plan_path(self, tmp_path):
        d = tmp_path / "docs" / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return d / "2026-07-29-test-plan.md"

    def _write_and_check(self, tmp_path, tasks_block):
        fp = self._plan_path(tmp_path)
        old_content = self._FRONTMATTER
        fp.write_text(old_content, encoding="utf-8")
        new_content = self._FRONTMATTER + tasks_block
        return guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=old_content, new_string=new_content)
        )

    def test_valid_spine_passes_silent(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        assert self._write_and_check(tmp_path, tasks_block) is None

    def test_bad_change_kind_warns_naming_row_and_field(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-port\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        result = self._write_and_check(tmp_path, tasks_block)
        assert result is not None
        text = _advisory_text(result)
        assert "[frontmatter-schema warning]" in text
        assert "tasks[C1].change_kind" in text
        assert 'invalid enum value "script-port"' in text
        assert "Allowed values:" in text
        assert "script-edit" in text

    def test_bad_disposition_warns(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/foo\n"
            "  disposition: done\n"
            "```\n"
        )
        result = self._write_and_check(tmp_path, tasks_block)
        assert result is not None
        text = _advisory_text(result)
        assert "tasks[C1].disposition" in text
        assert 'invalid enum value "done"' in text

    def test_bad_queue_scope_warns(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/foo\n"
            "  queue_scope: global\n"
            "```\n"
        )
        result = self._write_and_check(tmp_path, tasks_block)
        assert result is not None
        text = _advisory_text(result)
        assert "tasks[C1].queue_scope" in text
        assert 'invalid enum value "global"' in text

    def test_zero_fences_is_silent_noop(self, tmp_path):
        # No spine yet -- legitimate mid-authoring state, not a finding.
        assert self._write_and_check(tmp_path, "\nNo spine yet.\n") is None

    def test_two_fences_is_silent_not_this_guards_job(self, tmp_path):
        # MALFORMED (>1 fence) is plan-coverage-checker's fail-loud, not
        # duplicated here.
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n  title: a\n  change_kind: script-edit\n  surface: x\n"
            "```\n"
            "```yaml plan-tasks\n"
            "- id: C2\n  title: b\n  change_kind: script-edit\n  surface: y\n"
            "```\n"
        )
        assert self._write_and_check(tmp_path, tasks_block) is None

    def test_row_missing_id_names_zero_based_index(self, tmp_path):
        tasks_block = (
            "```yaml plan-tasks\n"
            "- title: Do a thing\n"
            "  change_kind: bogus\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        result = self._write_and_check(tmp_path, tasks_block)
        assert result is not None
        text = _advisory_text(result)
        assert "tasks[index 0]" in text

    def test_strict_mode_yields_none_not_mine(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COORDINATOR_SCHEMA_STRICT", "1")
        tasks_block = (
            "```yaml plan-tasks\n"
            "- id: C1\n"
            "  title: Do a thing\n"
            "  change_kind: script-port\n"
            "  surface: coordinator/bin/foo\n"
            "```\n"
        )
        assert self._write_and_check(tmp_path, tasks_block) is None

    def test_governed_closed_row_without_pm_approved_passes_schema_layer(self, tmp_path):
        """Gap 1 regression (2026-07-29 grouping-approval contract,
        write-guard-bypass fix), advisory-guard mirror of the deny sibling's
        `test_governed_closed_row_without_pm_approved_passes_schema_layer`
        (test_validate_frontmatter_schema_deny.py). On a GOVERNED plan a
        closed row carries NO `pm_approved` key by design -- authorization
        lives in the plan's `grouping_approvals` block, not the row. Before
        the fix, this advisory guard's `_plan_tasks_spine_errors` validated
        every row against the raw, UNFILTERED vendored schema (whose
        `deferred=>pm_approved` and CLOSED-dispositions=>pm_approved
        allOf/if-then branches ARE evaluated) regardless of governed status,
        so a fully-approved governed plan's closed row would have warned at
        the schema layer even though the plan's grouping approval had
        already cleared it. This is the test that would have caught that on
        the advisory side specifically -- it drives `guard.check` directly,
        exercising the same governed-aware filtering path the deny sibling's
        test exercises, but asserting silence (None) rather than a deny,
        matching this guard's warn-not-block contract.
        """
        rows_yaml = (
            "- id: C1\n"
            "  title: live\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/foo\n"
            "- id: C2\n"
            "  title: declined\n"
            "  change_kind: script-edit\n"
            "  surface: coordinator/bin/bar\n"
            "  disposition: wont_do\n"
            "  disposition_detail: PM said no, out of scope\n"
            # Required by plan-tasks 1.6.0's allOf conditional on the two scope-cut
            # dispositions. Present so the ONLY variable this test isolates stays the
            # pm_approved-required branch. NOTE: this fixture tracks coordinator-claude's LIVE tree,
            # not claude-klabauter's vendored copy -- these guards resolve schemas_dir from
            # coordinator_doe_root(), so a coordinator-claude-side bump reaches them with no re-vendor.
            "  case_against: Superseded by the C4 rewrite; carrying it forward would\n"
            "    duplicate that surface.\n"
        )
        rows = yaml.safe_load(rows_yaml)
        digest = compute_grouping_digest(rows, "ruled_out")
        frontmatter = (
            "---\ntitle: Test plan\ncreated: 2026-07-29\nauthor: test\nstatus: draft\n"
            "grouping_approvals:\n"
            "  ruled_out:\n"
            "    status: approved\n"
            "    approver: pm\n"
            "    approved_at: 2026-07-29\n"
            "    pm_utterance: 'yes, drop C2'\n"
            f"    digest: '{digest}'\n"
            "---\n\n"
            "# Plan\n\n## Tasks\n"
        )
        fp = self._plan_path(tmp_path)
        fp.write_text(frontmatter, encoding="utf-8")
        tasks_block = f"```yaml plan-tasks\n{rows_yaml}```\n"
        new_content = frontmatter + tasks_block
        result = guard.check(
            _payload("Edit", str(fp), str(tmp_path), old_string=frontmatter, new_string=new_content)
        )
        assert result is None, (
            "a governed, approved plan's closed row without pm_approved must not "
            f"warn at the schema layer: {result}"
        )
